"""Targeted sweep: minimax-m3 and gemini-3.7-flash-high with the inverted arbiter."""
import sys, re, time, json, statistics
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
ticket = {"id": "O3", "title": "gate-failure distribution", "defect": defect,
          "spec": "Record the failing gate name structurally in the ledger entry.", "context": ""}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."
CORRECT = {1, 2, 4, 5, 6}
DEFENSIBLE = {3}
ALL = {1, 2, 3, 4, 5, 6}
REPS = 3

# minimax needs specific budget sizes to avoid the reasoning-budget trap
MODELS = [
    ("minimax-m3:cloud", 8000),
    ("agy:gemini-3.7-flash-high", 8000),
]

real_chat = arb.chat

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
<<<RECOMMENDATION>>>
SHIP | REVISE
<<<END RECOMMENDATION>>>
"""


def run_inverted(model, budget):
    findings_text = "\n".join(
        f"- #{i+1} [{f.severity}] {f.text}" for i, f in enumerate(findings)
    )
    user_msg = (
        f"## Ticket: {ticket['id']}: {ticket['title']}\n"
        f"## Defect\n{defect}\n\n"
        f"## Gate results\n{GATES}\n\n"
        f"## Patch\n```diff\n{patch_diff[:40000]}\n```\n\n"
        f"## Findings to rule on\n{findings_text}\n\n"
        f"Rule on each finding. REJECT only if demonstrably wrong. KEEP everything else.\n"
    )
    try:
        out = real_chat(model, [
            {"role": "system", "content": INVERTED_SYSTEM},
            {"role": "user", "content": user_msg},
        ], max_tokens=budget, think=False)
        raw = out.text
    except Exception as e:
        return set(), "ERROR", str(e)[:80]

    rejected = set()
    for m in re.finditer(r"^-[\s\[\]*_]*(REJECT|KEEP)[\s\[\]*_]*#(\d+)", raw, re.MULTILINE):
        if m.group(1).upper() == "REJECT":
            rejected.add(int(m.group(2)))
    surviving = ALL - rejected
    rec = "SHIP" if not surviving else "REVISE"
    return surviving, rec, None


print("=" * 70)
print(f"TARGETED SWEEP: minimax-m3 + gemini-3.7-flash-high (inverted arbiter)")
print(f"Ground truth: {len(CORRECT)} correct, {len(DEFENSIBLE)} defensible")
print(f"Reps: {REPS}")
print("=" * 70)

results = []
for model, budget in MODELS:
    for rep in range(1, REPS + 1):
        t0 = time.time()
        surviving, rec, err = run_inverted(model, budget)
        secs = time.time() - t0
        hits = len(surviving & CORRECT)
        fp = len(surviving - CORRECT - DEFENSIBLE)
        status = f"hits={hits}/5 fp={fp} rec={rec} surviving={sorted(surviving)}"
        if err:
            status = f"ERROR: {err}"
        print(f"  {model:<35} rep{rep}  {status}  ({secs:.0f}s)")
        results.append({
            "model": model, "rep": rep, "surviving": sorted(surviving),
            "hits": hits, "fp": fp, "rec": rec, "secs": round(secs), "error": err or "",
        })

# Summary
print(f"\n{'='*70}")
print(f"{'Model':<35} {'Avg Hits':>9} {'Avg FP':>7} {'Avg Secs':>9} {'Stable':>7}")
print("-" * 70)
for model, _ in MODELS:
    rows = [r for r in results if r["model"] == model and not r["error"]]
    if not rows:
        print(f"{model:<35} {'N/A':>9} {'N/A':>7} {'N/A':>9} {'N/A':>7}")
        continue
    avg_hits = statistics.mean(r["hits"] for r in rows)
    avg_fp = statistics.mean(r["fp"] for r in rows)
    avg_secs = statistics.mean(r["secs"] for r in rows)
    hit_set = {r["hits"] for r in rows}
    stable = "yes" if len(hit_set) == 1 else f"no ({sorted(hit_set)})"
    print(f"{model:<35} {avg_hits:>8.1f}/5 {avg_fp:>7.1f} {avg_secs:>8.0f}s {stable:>7}")

json.dump(results, (ART / "results_sweep_extra.json").open("w"), indent=2)