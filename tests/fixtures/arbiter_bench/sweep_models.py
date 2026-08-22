"""Full model sweep: inverted arbiter approach across all available cloud models.

Runs each model 3 times against the O3 ground-truth corpus (5 correct, 1
defensible, 0 known-wrong out of 6 findings) using the INVERTED arbiter prompt
("reject only demonstrably wrong findings, keep everything else").

Also runs the CURRENT arbiter for direct comparison, so each model's
improvement from the prompt change is visible.

Metrics per (model, approach, rep):
  hits  — correct findings caught (out of 5)
  fp    — false positives (wrong findings kept)
  rec   — recommendation (SHIP/REVISE/ESCALATE)
  secs  — wall-clock seconds
  rejected — which finding numbers were rejected

Output: results_sweep.json + a formatted table.
"""
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
          "spec": "Record the failing gate name structurally in the ledger entry.",
          "context": ""}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."
CORRECT = {1, 2, 4, 5, 6}
DEFENSIBLE = {3}
ALL = {1, 2, 3, 4, 5, 6}

REPS = 3
MODELS = [
    "glm-5.2:cloud",
    "mistral-large-3:675b-cloud",
    "deepseek-v4-flash:cloud",
    "deepseek-v4-pro:cloud",
    "kimi-k2.7-code:cloud",
    "kimi-k3:cloud",
    "qwen3.5:cloud",
]

real_chat = arb.chat

# ---------------------------------------------------------------------------
# Current arbiter prompt (uphold-gate)
# ---------------------------------------------------------------------------
def run_current(model):
    try:
        adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                             rules=prof.arbiter_rules, max_tokens=48000)
        if not adj.ok:
            return {"error": (adj.error or "")[:80], "surviving": set(), "rec": "ERROR", "rejected": ALL}
        upheld = {r.index for r in adj.by(arb.UPHELD)}
        rejected = ALL - upheld
        return {"surviving": upheld, "rec": adj.recommendation, "rejected": rejected}
    except Exception as e:
        return {"error": str(e)[:80], "surviving": set(), "rec": "ERROR", "rejected": ALL}


# ---------------------------------------------------------------------------
# Inverted arbiter prompt (reject-only)
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
<<<RECOMMENDATION>>>
SHIP | REVISE
<<<END RECOMMENDATION>>>
"""


def run_inverted(model):
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
        ], max_tokens=8000, think=False)
        raw = out.text
    except Exception as e:
        return {"error": str(e)[:80], "surviving": set(), "rec": "ERROR", "rejected": ALL}

    rejected = set()
    for m in re.finditer(r"^-[\s\[\]*_]*(REJECT|KEEP)[\s\[\]*_]*#(\d+)", raw, re.MULTILINE):
        if m.group(1).upper() == "REJECT":
            rejected.add(int(m.group(2)))

    surviving = ALL - rejected
    rec = "SHIP" if not surviving else "REVISE"
    return {"surviving": surviving, "rec": rec, "rejected": rejected}


# ---------------------------------------------------------------------------
# Run the sweep
# ---------------------------------------------------------------------------
print("=" * 80)
print(f"FULL MODEL SWEEP: inverted vs current arbiter")
print(f"Ground truth: {len(CORRECT)} correct, {len(DEFENSIBLE)} defensible, {len(findings)} total")
print(f"Models: {len(MODELS)} | Reps: {REPS}")
print("=" * 80)

all_results = []

for model in MODELS:
    for approach, runner in [("CURRENT", run_current), ("INVERTED", run_inverted)]:
        for rep in range(1, REPS + 1):
            t0 = time.time()
            r = runner(model)
            secs = time.time() - t0
            surviving = r["surviving"]
            hits = len(surviving & CORRECT)
            fp = len(surviving - CORRECT - DEFENSIBLE)
            rec = r["rec"]
            rejected = sorted(r["rejected"])
            error = r.get("error", "")
            row = {
                "model": model, "approach": approach, "rep": rep,
                "surviving": sorted(surviving), "hits": hits, "fp": fp,
                "rec": rec, "rejected": rejected, "secs": round(secs),
                "error": error,
            }
            all_results.append(row)
            status = f"hits={hits}/5 fp={fp} rec={rec} rejected={rejected}"
            if error:
                status = f"ERROR: {error}"
            print(f"  {model:<35} {approach:<10} rep{rep}  {status}  ({secs:.0f}s)")

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
print("\n" + "=" * 80)
print("SUMMARY: average hits per model/approach across reps")
print("=" * 80)
print(f"{'Model':<35} {'Approach':<10} {'Avg Hits':>9} {'Avg FP':>7} {'Avg Secs':>9} {'Ship%':>6}")
print("-" * 80)

for model in MODELS:
    for approach in ["CURRENT", "INVERTED"]:
        rows = [r for r in all_results if r["model"] == model and r["approach"] == approach and not r["error"]]
        if not rows:
            print(f"{model:<35} {approach:<10} {'N/A':>9} {'N/A':>7} {'N/A':>9} {'N/A':>6}")
            continue
        avg_hits = statistics.mean(r["hits"] for r in rows)
        avg_fp = statistics.mean(r["fp"] for r in rows)
        avg_secs = statistics.mean(r["secs"] for r in rows)
        ship_pct = sum(1 for r in rows if r["rec"] == "SHIP") / len(rows) * 100
        print(f"{model:<35} {approach:<10} {avg_hits:>8.1f}/5 {avg_fp:>7.1f} {avg_secs:>8.0f}s {ship_pct:>5.0f}%")

# Improvement table
print("\n" + "=" * 80)
print("IMPROVEMENT: inverted vs current (avg hits)")
print("=" * 80)
print(f"{'Model':<35} {'Current':>8} {'Inverted':>9} {'Delta':>7}")
print("-" * 80)
for model in MODELS:
    cur = [r for r in all_results if r["model"] == model and r["approach"] == "CURRENT" and not r["error"]]
    inv = [r for r in all_results if r["model"] == model and r["approach"] == "INVERTED" and not r["error"]]
    if not cur or not inv:
        print(f"{model:<35} {'N/A':>8} {'N/A':>9} {'N/A':>7}")
        continue
    cur_avg = statistics.mean(r["hits"] for r in cur)
    inv_avg = statistics.mean(r["hits"] for r in inv)
    delta = inv_avg - cur_avg
    arrow = "+" if delta > 0 else ""
    print(f"{model:<35} {cur_avg:>7.1f}/5 {inv_avg:>8.1f}/5 {arrow}{delta:>5.1f}")

# Save
out = ART / "results_sweep.json"
json.dump(all_results, out.open("w"), indent=2)
print(f"\nSaved {len(all_results)} rows to {out}")