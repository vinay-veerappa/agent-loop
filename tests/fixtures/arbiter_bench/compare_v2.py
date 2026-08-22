"""Rethought arbiter comparison.

PREVIOUS TEST FLAWS:
1. The serial gauntlet required AGREE to survive — a one-way ratchet that
   can only remove findings. Correct findings missed by one reviewer are
   gone forever.
2. The grounded arbiter got full file context but still had to RULE on
   correctness — the same hard task, just with more context.
3. The current arbiter only sees a truncated diff, not the BEFORE/AFTER
   region comparison the reviewers saw.

NEW APPROACH: "collect, don't filter"

The insight: the measured problem is false NEGATIVES (correct findings
rejected), not false POSITIVES (wrong findings upheld — zero across all
approaches). So the filter should be PERMISSIVE on uphold and STRICT on
reject. Instead of "uphold findings we're confident are correct," it should
be "reject only findings that are DEMONSTRABLY wrong."

Three approaches tested:

A. CURRENT     — the shipped arbiter (requires UPHELD to keep a finding)
B. INVERTED    — the arbiter's job is to REJECT demonstrably wrong findings,
                 not to UPHOLD correct ones. Everything not rejected goes back.
                 The burden of proof is on rejection, not on upholdment.
C. FULL_CONTEXT — same as B, but the arbiter gets the SAME context the
                 reviewers saw (BEFORE/AFTER regions + full file context),
                 not just a truncated diff.

The metric: of the 5 known-correct findings, how many survive to the
implementer? And: how many wrong findings leak through (false positives)?
"""
import sys, re, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_loop import arbiter as arb
from agent_loop.loop import parse_review, build_review_prompt
from agent_loop.profiles import get as get_profile
from agent_loop.providers import chat, ProviderError
from agent_loop import regions, profiles
import profiles.self  # noqa

ART = Path(__file__).resolve().parent
prof = get_profile("agent-loop-self")

vote = parse_review(ART.joinpath("findings_glm.txt").read_text(encoding="utf-8"), "glm-5.2:cloud")
findings = list(vote.finding_list)
patch_diff = ART.joinpath("o3.patch").read_text(encoding="utf-8")
defect = ART.joinpath("defect.txt").read_text(encoding="utf-8")
ticket = {"id": "O3", "title": "gate-failure distribution", "defect": defect,
          "spec": "Record the failing gate name structurally in the ledger entry.",
          "context": ""}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."
CORRECT = {1, 2, 4, 5, 6}
DEFENSIBLE = {3}

MODELS = ["mistral-large-3:675b-cloud", "glm-5.2:cloud"]
real_chat = arb.chat


# ---------------------------------------------------------------------------
# A. CURRENT: the shipped arbiter — must UPHELD to keep a finding
# ---------------------------------------------------------------------------
def run_current(model):
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        if not adj.ok:
            return set(), "ERROR", 0
        upheld = {r.index for r in adj.by(arb.UPHELD)}
        return upheld, adj.recommendation, time.time()
    except Exception as e:
        return set(), str(e)[:60], 0


# ---------------------------------------------------------------------------
# B. INVERTED: the arbiter's job is to REJECT demonstrably wrong findings.
# Everything NOT rejected goes to the implementer.
# ---------------------------------------------------------------------------
INVERTED_SYSTEM = """You are the arbiter for a patch that has ALREADY passed every mechanical gate.

Multiple reviewers raised findings against this patch. Your job is NOT to judge
whether each finding is "correct" — that requires understanding intent, which
is a semantic judgment you cannot reliably make. Your job is narrower and more
grounded: identify findings that are DEMONSTRABLY WRONG and should be dropped.

A finding is DEMONSTRABLY WRONG if any of these apply:
  1. CONTRADICTS A GATE: the finding claims the code doesn't compile or tests
     fail, but the gate results show they pass.
  2. CODE DOESN'T EXIST: the finding references code, variables, or functions
     that are not in the patch or the surrounding context.
  3. OUT OF SCOPE: the finding is about pre-existing code the patch didn't
     touch, or is named in the ticket's scope block as deliberately excluded.
  4. RESTATES A SETTLED DECISION: the finding contradicts a decision that was
     already settled on a prior ticket.
  5. MECHANISM DOESN'T HOLD: the specific failure the finding describes cannot
     actually occur given the code as written.

For each finding, rule:
  REJECT     - demonstrably wrong, drop it (cite which of the 5 criteria)
  KEEP       - cannot be demonstrably rejected; it goes to the implementer

Everything you do NOT reject goes back to the implementer. The burden of proof
is on rejection, not on keep. When in doubt, KEEP — the implementer and the
human reviewer can judge correctness; your job is to remove noise.

OUTPUT FORMAT:
<<<RULINGS>>>
- [REJECT|KEEP] #<n>: one sentence citing the criterion (1-5) or "no rejection criterion met"
<<<END RULINGS>>>
<<<RECOMMENDATION>>
SHIP | REVISE
<<<END RECOMMENDATION>>>
"""


def run_inverted(model, extra_context=""):
    """The inverted arbiter: reject only what's demonstrably wrong."""
    findings_text = "\n".join(
        f"- #{i+1} [{f.severity}] {f.text}" for i, f in enumerate(findings)
    )
    user_msg = (
        f"## Ticket: {ticket['id']}: {ticket['title']}\n"
        f"## Defect\n{defect}\n\n"
        f"## Gate results\n{GATES}\n\n"
        f"## Patch\n```diff\n{patch_diff[:40000]}\n```\n"
        f"{extra_context}\n"
        f"## Findings to rule on\n{findings_text}\n\n"
        f"Rule on each finding. REJECT only if demonstrably wrong. KEEP everything else.\n"
    )
    t0 = time.time()
    try:
        out = real_chat(model, [
            {"role": "system", "content": INVERTED_SYSTEM},
            {"role": "user", "content": user_msg},
        ], max_tokens=8000, think=False)
        raw = out.text
    except Exception as e:
        return set(), "ERROR", 0, str(e)[:60]

    # Parse KEEP/REJECT rulings
    rejected = set()
    for m in re.finditer(r"^-[\s\[\]*_]*(REJECT|KEEP)[\s\[\]*_]*#(\d+)", raw, re.MULTILINE):
        if m.group(1).upper() == "REJECT":
            rejected.add(int(m.group(2)))

    all_findings = {i + 1 for i in range(len(findings))}
    surviving = all_findings - rejected
    rec = "SHIP" if not surviving else "REVISE"
    return surviving, rec, time.time() - t0, raw[:300]


# ---------------------------------------------------------------------------
# C. FULL_CONTEXT: same as B, but the arbiter gets the BEFORE/AFTER region
# comparison (what the reviewers saw) plus full file context.
# ---------------------------------------------------------------------------
def run_full_context(model):
    """Inverted arbiter with full context: BEFORE/AFTER + full source files."""
    # Build the same BEFORE/AFTER comparison the reviewers see
    repo_root = Path(__file__).resolve().parents[3]

    # Read the full source files the patch touches
    changed_files = set()
    for line in patch_diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            fp = line[4:].strip().split(" ")[0]
            if fp != "/dev/null":
                fp = fp.removeprefix("a/").removeprefix("b/")
                changed_files.add(fp)

    file_context = ""
    for fp in sorted(changed_files):
        full_path = repo_root / fp
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > 15000:
                content = content[:15000] + "\n... [truncated] ...\n"
            file_context += f"\n### Full source: {fp}\n```\n{content}\n```\n"

    extra = (
        "\n## Full source files (for verification)\n"
        "Use these to verify claims about what the code does. A finding that "
        "references code not in these files, or claims a value is 'never set' "
        "when you can see it IS set in the full source, is demonstrably wrong.\n"
        + file_context
    )
    return run_inverted(model, extra_context=extra)


# ---------------------------------------------------------------------------
# Run the comparison
# ---------------------------------------------------------------------------
print("=" * 70)
print(f"Ground truth: {len(CORRECT)} correct, {len(DEFENSIBLE)} defensible, {len(findings)} total")
print(f"Models: {MODELS}")
print("=" * 70)

results = []

# A. Current arbiter
print("\n--- A. CURRENT (uphold-gate arbiter) ---")
for model in MODELS:
    t0 = time.time()
    upheld, rec, _ = run_current(model)
    hits = len(upheld & CORRECT)
    fp = len(upheld - CORRECT - DEFENSIBLE)
    missed = CORRECT - upheld
    secs = time.time() - t0
    results.append(("CURRENT", model, upheld, hits, fp, rec, secs))
    print(f"  {model:<35} upheld={sorted(upheld)} hits={hits}/5 fp={fp} missed={sorted(missed)} rec={rec} ({secs:.0f}s)")

# B. Inverted arbiter
print("\n--- B. INVERTED (reject-only arbiter) ---")
for model in MODELS:
    surviving, rec, secs, raw = run_inverted(model)
    hits = len(surviving & CORRECT)
    fp = len(surviving - CORRECT - DEFENSIBLE)
    missed = CORRECT - surviving
    results.append(("INVERTED", model, surviving, hits, fp, rec, secs))
    print(f"  {model:<35} surviving={sorted(surviving)} hits={hits}/5 fp={fp} missed={sorted(missed)} rec={rec} ({secs:.0f}s)")

# C. Full-context inverted arbiter
print("\n--- C. FULL_CONTEXT (inverted + full source files) ---")
for model in MODELS:
    surviving, rec, secs, raw = run_full_context(model)
    hits = len(surviving & CORRECT)
    fp = len(surviving - CORRECT - DEFENSIBLE)
    missed = CORRECT - surviving
    results.append(("FULL_CONTEXT", model, surviving, hits, fp, rec, secs))
    print(f"  {model:<35} surviving={sorted(surviving)} hits={hits}/5 fp={fp} missed={sorted(missed)} rec={rec} ({secs:.0f}s)")

# Summary
print("\n" + "=" * 70)
print(f"{'Approach':<15} {'Model':<35} {'Hits':>6} {'FP':>4} {'Missed':>8} {'Verdict':>10}")
print("-" * 70)
for approach, model, surviving, hits, fp, rec, secs in results:
    missed = CORRECT - surviving
    print(f"{approach:<15} {model:<35} {hits:>5}/5 {fp:>4} {len(missed):>8} {rec:>10}")

# Save
out = ART / "approach_comparison_v2.json"
json.dump([{
    "approach": a, "model": m, "surviving": sorted(s),
    "hits": h, "fp": fp, "missed": sorted(CORRECT - s),
    "rec": r, "secs": round(secs),
} for a, m, s, h, fp, r, secs in results], out.open("w"), indent=2)
print(f"\nSaved to {out}")