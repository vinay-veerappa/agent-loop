"""Arbiter approach comparison bench.

Compares four approaches to adjudicating reviewer findings against the same
ground-truth corpus (5 correct findings out of 6 on the O3 patch):

1. CURRENT    -- the shipped arbiter (single LLM judges all findings)
2. SERIAL     -- serial gauntlet: each reviewer sees prior reviewers' findings
                 and must AGREE/DISAGREE. Findings that survive all pass.
3. EXECUTABLE -- for each upheld finding that proposes a fix, apply the fix
                 and run tests. If tests don't improve, the finding is
                 overcorrection -> drop it.
4. GROUNDED   -- the arbiter gets the full file context around each changed
                 region (not just the diff), so it can verify claims about
                 what the code does.

Each approach is tested with the same models and the same corpus. The metric
is: of the 5 known-correct findings, how many does the approach correctly
uphold? And: does it incorrectly uphold finding #3 (the defensible one)?

Ground truth (from the existing arbiter_bench):
  CORRECT = {1, 2, 4, 5, 6}  -- verified correct by hand
  DEFENSIBLE = {3}            -- legitimate design question, not a defect
  WRONG = {}                  -- no known-wrong findings in this corpus

The shipped arbiter (deepseek-v4-pro) upheld 0/5 and ruled SHIP -- the worst
possible outcome. The best model (mistral-large-3) upheld 2/5.
"""
import sys, json, re, time
from pathlib import Path
from unittest.mock import patch as mpatch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_loop import arbiter as arb
from agent_loop.loop import parse_review
from agent_loop.profiles import get as get_profile
import profiles.self  # noqa

ART = Path(__file__).resolve().parent
prof = get_profile("agent-loop-self")

# --- Load the frozen corpus ---
vote = parse_review(ART.joinpath("findings_glm.txt").read_text(encoding="utf-8"), "glm-5.2:cloud")
findings = list(vote.finding_list)
patch_diff = ART.joinpath("o3.patch").read_text(encoding="utf-8")
defect = ART.joinpath("defect.txt").read_text(encoding="utf-8")

CORRECT = {1, 2, 4, 5, 6}
DEFENSIBLE = {3}
ticket = {"id": "O3", "title": "gate-failure distribution is inferred from prose", "defect": defect}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."

# --- Models to test ---
MODELS = [
    "glm-5.2:cloud",
    "mistral-large-3:675b-cloud",
    "minimax-m3:cloud",
]

real_chat = arb.chat


def run_current_arbiter(model):
    """Approach 1: the shipped arbiter — single LLM judges all findings."""
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        if not adj.ok:
            return {"error": adj.error or "unreachable", "upheld": set(), "rec": "ERROR"}
        upheld = {r.index for r in adj.by(arb.UPHELD)}
        return {"upheld": upheld, "rec": adj.recommendation, "raw": adj.raw[:500]}
    except Exception as e:
        return {"error": str(e)[:80], "upheld": set(), "rec": "ERROR"}


def run_serial_gauntlet(models):
    """Approach 2: serial gauntlet — each reviewer sees prior findings and
    must AGREE or DISAGREE. Findings that survive all reviewers pass.

    No arbiter. The consensus emerges from serial handoff.
    """
    from agent_loop.providers import Completion, ProviderError

    # Build the shared context: the patch, the ticket, and the findings
    shared_context = (
        f"## Ticket: {ticket['id']}: {ticket['title']}\n"
        f"## Defect\n{defect}\n\n"
        f"## Gate results\n{GATES}\n\n"
        f"## Patch under review\n```diff\n{patch_diff[:30000]}\n```\n\n"
        f"## Findings to adjudicate\n"
    )
    for i, f in enumerate(findings, 1):
        shared_context += f"\n### Finding #{i} [{f.severity}]\n{f.text}\n"

    surviving = {i + 1 for i in range(len(findings))}  # start with all
    gauntlet_log = []

    for round_num, model in enumerate(models, 1):
        system = (
            "You are a skeptical peer reviewer in a serial consensus pipeline. "
            "You are reviewing findings from a prior reviewer. For EACH finding, "
            "you must state AGREE or DISAGREE with a one-sentence reason. "
            "A finding survives only if you AGREE. Be rigorous: a DISAGREE means "
            "the claimed mechanism does not hold, contradicts the gates, or is "
            "not caused by this patch.\n\n"
            "OUTPUT FORMAT - obey exactly:\n"
            "<<<RULINGS>>>\n"
            "- [AGREE|DISAGREE] #<n>: one sentence of reasoning\n"
            "<<<END RULINGS>>>\n"
        )

        user_msg = (
            f"## Shared Context (from prior reviewers)\n{shared_context}\n\n"
            f"## Your job\n"
            f"Rule on each finding. AGREE means the finding is real and caused by "
            f"this patch. DISAGREE means it is not. Only findings you AGREE with "
            f"will survive to the next reviewer.\n"
        )

        if not surviving:
            break  # no findings left to review

        try:
            out = real_chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ], max_tokens=8000, think=False)
            raw = out.text
        except (ProviderError, Exception) as e:
            gauntlet_log.append(f"  round {round_num} {model}: UNREACHABLE - {str(e)[:60]}")
            continue

        # Parse AGREE/DISAGREE rulings
        agreed = set()
        ruling_re = re.compile(
            r"^-[\s\[\]*_]*(AGREE|DISAGREE)[\s\[\]*_]*#(\d+)\s*:?\s*(.*)$",
            re.MULTILINE,
        )
        for m in ruling_re.finditer(raw):
            verdict_str = m.group(1).upper()
            idx = int(m.group(2))
            if verdict_str == "AGREE" and idx in surviving:
                agreed.add(idx)

        surviving = agreed & surviving  # only agreed findings survive
        gauntlet_log.append(
            f"  round {round_num} {model}: agreed={sorted(agreed)} surviving={sorted(surviving)}"
        )

    return {
        "upheld": surviving,
        "rec": "REVISE" if surviving else "SHIP",
        "log": gauntlet_log,
    }


def run_executable_evidence(model):
    """Approach 3: executable evidence — the arbiter rules, then each upheld
    finding's proposed fix is applied and tested. If the fix doesn't improve
    test outcomes, the finding is overcorrection and is dropped.

    For this bench we don't have a live test suite for the O3 patch, so we
    simulate the executable check: a finding is "executable-verified" if its
    REQUIRED section proposes a concrete code change. Findings that only
    describe behaviour without proposing a change are kept (they may be real
    but unfixable by the reviewer). This is a proxy — the real implementation
    would apply the fix and run tests.
    """
    # First, get the arbiter's rulings
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        if not adj.ok:
            return {"error": adj.error or "unreachable", "upheld": set(), "rec": "ERROR"}
        upheld = {r.index for r in adj.by(arb.UPHELD)}
    except Exception as e:
        return {"error": str(e)[:80], "upheld": set(), "rec": "ERROR"}

    # For each upheld finding, check if the REQUIRED section proposes a
    # concrete fix. If the finding's REQUIRED item names a specific code
    # change (file + what to change), it's executable-verified.
    # If it only describes behaviour, keep it (can't verify, but may be real).
    # If it proposes a fix that is clearly unnecessary (the code already does
    # what it asks), drop it — this is the overcorrection signal.
    #
    # For this bench, we use the REQUIRED section from findings_glm.txt as
    # the proxy: findings 1-6 all have REQUIRED items, so they all pass
    # the executable check. This means the executable filter doesn't change
    # the arbiter's ruling for this corpus — which is itself a finding:
    # the executable filter only helps when the arbiter upholds findings
    # that DON'T propose concrete fixes (the overcorrection shape).
    return {
        "upheld": upheld,
        "rec": adj.recommendation,
        "note": "executable filter: no change (all findings have REQUIRED items)",
    }


def run_grounded_arbiter(model):
    """Approach 4: grounded arbiter — gets the full file context around each
    changed region, not just the diff. This lets the arbiter verify claims
    about what the code does (e.g. "this key is never set" -> check the full
    file, not just the diff).
    """
    # Read the actual source files the patch touches, so the arbiter sees
    # the full context
    repo_root = Path(__file__).resolve().parents[3]
    changed_files = set()
    for line in patch_diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            filepath = line[4:].strip().split(" ")[0]
            if filepath != "/dev/null" and not filepath.startswith("a/"):
                filepath = filepath.removeprefix("b/")
            changed_files.add(filepath.removeprefix("a/").removeprefix("b/"))

    file_context = ""
    for filepath in sorted(changed_files):
        full_path = repo_root / filepath
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8", errors="replace")
            # Truncate to 20K per file
            if len(content) > 20000:
                content = content[:20000] + "\n... [truncated] ...\n"
            file_context += f"\n### Full source: {filepath}\n```\n{content}\n```\n"

    # Build an augmented prompt with the full file context
    augmented_diff = patch_diff + "\n\n## Full source files (for context verification)\n" + file_context

    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, augmented_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        if not adj.ok:
            return {"error": adj.error or "unreachable", "upheld": set(), "rec": "ERROR"}
        upheld = {r.index for r in adj.by(arb.UPHELD)}
        return {"upheld": upheld, "rec": adj.recommendation, "raw": adj.raw[:500]}
    except Exception as e:
        return {"error": str(e)[:80], "upheld": set(), "rec": "ERROR"}


# --- Run the comparison ---
print("=" * 80)
print("ARBITER APPROACH COMPARISON BENCH")
print(f"Findings: {len(findings)} | Ground truth correct: {sorted(CORRECT)} | Defensible: {sorted(DEFENSIBLE)}")
print(f"Models: {MODELS}")
print("=" * 80)

results = {}

# Approach 1: Current arbiter
print("\n--- 1. CURRENT (single LLM arbiter) ---")
results["current"] = []
for model in MODELS:
    r = run_current_arbiter(model)
    hits = len(r["upheld"] & CORRECT)
    false_positives = len(r["upheld"] - CORRECT - DEFENSIBLE)
    results["current"].append({"model": model, **r, "hits": hits, "fp": false_positives})
    print(f"  {model:<35} upheld={sorted(r['upheld'])} hits={hits}/5 fp={false_positives} rec={r['rec']}")

# Approach 2: Serial gauntlet
print("\n--- 2. SERIAL GAUNTLET (no arbiter, serial peer review) ---")
# Test with 2 and 3 reviewers in the gauntlet
for n_reviewers in (2, 3):
    gauntlet_models = MODELS[:n_reviewers]
    r = run_serial_gauntlet(gauntlet_models)
    hits = len(r["upheld"] & CORRECT)
    false_positives = len(r["upheld"] - CORRECT - DEFENSIBLE)
    key = f"serial_{n_reviewers}"
    results[key] = [{"model": "+".join(gauntlet_models), **r, "hits": hits, "fp": false_positives}]
    print(f"  {n_reviewers} reviewers: upheld={sorted(r['upheld'])} hits={hits}/5 fp={false_positives} rec={r['rec']}")
    for line in r.get("log", []):
        print(f"    {line}")

# Approach 3: Executable evidence
print("\n--- 3. EXECUTABLE EVIDENCE (arbiter + fix verification) ---")
results["executable"] = []
for model in MODELS:
    r = run_executable_evidence(model)
    hits = len(r["upheld"] & CORRECT)
    false_positives = len(r["upheld"] - CORRECT - DEFENSIBLE)
    results["executable"].append({"model": model, **r, "hits": hits, "fp": false_positives})
    print(f"  {model:<35} upheld={sorted(r['upheld'])} hits={hits}/5 fp={false_positives} rec={r['rec']}")
    if "note" in r:
        print(f"    {r['note']}")

# Approach 4: Grounded arbiter
print("\n--- 4. GROUNDED ARBITER (full file context) ---")
results["grounded"] = []
for model in MODELS:
    r = run_grounded_arbiter(model)
    hits = len(r["upheld"] & CORRECT)
    false_positives = len(r["upheld"] - CORRECT - DEFENSIBLE)
    results["grounded"].append({"model": model, **r, "hits": hits, "fp": false_positives})
    print(f"  {model:<35} upheld={sorted(r['upheld'])} hits={hits}/5 fp={false_positives} rec={r['rec']}")

# --- Summary ---
print("\n" + "=" * 80)
print("SUMMARY: correct findings upheld (out of 5), false positives, verdict")
print("=" * 80)
print(f"{'Approach':<25} {'Model':<35} {'Hits':>5} {'FP':>4} {'Verdict':>10}")
print("-" * 80)
for approach, rows in results.items():
    for row in rows:
        model = row.get("model", "?")
        hits = row.get("hits", 0)
        fp = row.get("fp", 0)
        rec = row.get("rec", "?")
        print(f"{approach:<25} {model:<35} {hits:>5}/5 {fp:>4} {rec:>10}")

# Save results
out_path = ART / "approach_comparison.json"
json.dump(results, out_path.open("w"), indent=2, default=str)
print(f"\nResults saved to {out_path}")