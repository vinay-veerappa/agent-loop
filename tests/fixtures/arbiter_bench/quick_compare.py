"""Quick comparison: current arbiter vs serial gauntlet vs grounded arbiter.

Uses the same O3 ground truth as run_bench.py: 5 correct findings out of 6.
"""
import sys, re, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_loop import arbiter as arb
from agent_loop.loop import parse_review
from agent_loop.profiles import get as get_profile
from agent_loop.providers import chat, ProviderError
import profiles.self  # noqa

ART = Path(__file__).resolve().parent
prof = get_profile("agent-loop-self")

vote = parse_review(ART.joinpath("findings_glm.txt").read_text(encoding="utf-8"), "glm-5.2:cloud")
findings = list(vote.finding_list)
patch_diff = ART.joinpath("o3.patch").read_text(encoding="utf-8")
defect = ART.joinpath("defect.txt").read_text(encoding="utf-8")
ticket = {"id": "O3", "title": "gate-failure distribution", "defect": defect}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."
CORRECT = {1, 2, 4, 5, 6}
DEFENSIBLE = {3}


def serial_gauntlet(models):
    shared = (
        f"## Ticket: {ticket['id']}: {ticket['title']}\n"
        f"## Defect\n{defect}\n\n"
        f"## Gate results\n{GATES}\n\n"
        f"## Patch under review\n```diff\n{patch_diff[:30000]}\n```\n\n"
        f"## Findings to adjudicate\n"
    )
    for i, f in enumerate(findings, 1):
        shared += f"\n### Finding #{i} [{f.severity}]\n{f.text}\n"

    surviving = {i + 1 for i in range(len(findings))}
    log = []

    for rnd, model in enumerate(models, 1):
        system = (
            "You are a skeptical peer reviewer in a serial consensus pipeline. "
            "You review findings from a prior reviewer. For EACH finding, state "
            "AGREE or DISAGREE with a one-sentence reason. A finding survives "
            "only if you AGREE. DISAGREE means the claimed mechanism does not "
            "hold, contradicts the gates, or is not caused by this patch.\n\n"
            "OUTPUT FORMAT:\n"
            "<<<RULINGS>>>\n"
            "- [AGREE|DISAGREE] #<n>: one sentence\n"
            "<<<END RULINGS>>>\n"
        )
        user_msg = (
            f"{shared}\n## Your job\n"
            f"Rule on each finding. AGREE = real and caused by this patch. "
            f"DISAGREE = not. Only AGREE findings survive.\n"
        )
        if not surviving:
            break
        t0 = time.time()
        try:
            out = chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ], max_tokens=8000, think=False)
            raw = out.text
        except Exception as e:
            log.append(f"  round {rnd} {model}: UNREACHABLE ({time.time()-t0:.0f}s)")
            continue

        agreed = set()
        for m in re.finditer(r"^-[\s\[\]*_]*(AGREE|DISAGREE)[\s\[\]*_]*#(\d+)", raw, re.MULTILINE):
            if m.group(1).upper() == "AGREE" and int(m.group(2)) in surviving:
                agreed.add(int(m.group(2)))
        surviving = agreed & surviving
        log.append(f"  round {rnd} {model}: agreed={sorted(agreed)} surviving={sorted(surviving)} ({time.time()-t0:.0f}s)")

    return surviving, log


def grounded_arbiter(model):
    repo_root = Path(__file__).resolve().parents[3]
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
            if len(content) > 20000:
                content = content[:20000] + "\n... [truncated] ...\n"
            file_context += f"\n### Full source: {fp}\n```\n{content}\n```\n"

    augmented = patch_diff + "\n\n## Full source files (for context verification)\n" + file_context
    t0 = time.time()
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, augmented,
                             rules=prof.arbiter_rules, max_tokens=48000)
        upheld = {r.index for r in adj.by(arb.UPHELD)} if adj.ok else set()
        return upheld, adj.recommendation, adj.ok, time.time() - t0
    except Exception as e:
        return set(), str(e)[:80], False, time.time() - t0


# --- Run ---
print("=" * 70)
print(f"Ground truth: {len(CORRECT)} correct findings out of {len(findings)}")
print("=" * 70)

# 1. Current arbiter
for model in ["mistral-large-3:675b-cloud", "glm-5.2:cloud"]:
    t0 = time.time()
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        upheld = {r.index for r in adj.by(arb.UPHELD)} if adj.ok else set()
        hits = len(upheld & CORRECT)
        fp = len(upheld - CORRECT - DEFENSIBLE)
        print(f"CURRENT  {model:<35} upheld={sorted(upheld)} hits={hits}/5 fp={fp} rec={adj.recommendation} ({time.time()-t0:.0f}s)")
    except Exception as e:
        print(f"CURRENT  {model:<35} ERROR: {str(e)[:60]} ({time.time()-t0:.0f}s)")

# 2. Serial gauntlet
for order_label, models in [
    ("glm->mistral", ["glm-5.2:cloud", "mistral-large-3:675b-cloud"]),
    ("mistral->glm", ["mistral-large-3:675b-cloud", "glm-5.2:cloud"]),
]:
    surviving, log = serial_gauntlet(models)
    hits = len(surviving & CORRECT)
    fp = len(surviving - CORRECT - DEFENSIBLE)
    print(f"SERIAL   {order_label:<35} upheld={sorted(surviving)} hits={hits}/5 fp={fp}")
    for line in log:
        print(f"         {line}")

# 3. Grounded arbiter
for model in ["mistral-large-3:675b-cloud", "glm-5.2:cloud"]:
    upheld, rec, ok, secs = grounded_arbiter(model)
    hits = len(upheld & CORRECT)
    fp = len(upheld - CORRECT - DEFENSIBLE)
    print(f"GROUNDED {model:<35} upheld={sorted(upheld)} hits={hits}/5 fp={fp} rec={rec} ({secs:.0f}s)")