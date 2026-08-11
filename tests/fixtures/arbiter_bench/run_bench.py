"""Labelled arbiter benchmark.

Ground truth: glm-5.2 produced six findings on the O3 patch. Five were verified
correct by hand -- four of them are now fixed in the tree, and the fifth (a
missing/None `stage`) was fixed independently while writing failed_gate_names.
The shipped arbiter (deepseek-v4-pro, think=False, 24000) upheld NONE of them
and ruled SHIP.

Metric: of the five known-correct findings, how many does a given arbiter
configuration UPHOLD? Higher is better. Ruling SHIP on this patch is wrong.
"""
import sys, json, itertools
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unittest.mock import patch as mpatch

from agent_loop import arbiter as arb
from agent_loop.loop import parse_review
from agent_loop.profiles import get as get_profile
import profiles.self  # noqa

ART = Path(__file__).resolve().parent  # frozen corpus; logs/agent_loop/DEV is overwritten every run
prof = get_profile("agent-loop-self")

vote = parse_review(ART.joinpath("findings_glm.txt").read_text(encoding="utf-8"), "glm-5.2:cloud")
findings = list(vote.finding_list)
patch_diff = ART.joinpath("o3.patch").read_text(encoding="utf-8")

# 1-based indices of the findings verified correct by hand. #3 (all-rounds vs
# blocking-gate semantics) is excluded: it is a legitimate design question, not
# a defect, so upholding or rejecting it is defensible.
CORRECT = {1, 2, 4, 5, 6}

ticket = {
    "id": "O3", "title": "gate-failure distribution is inferred from prose",
    "defect": ART.joinpath("defect.txt").read_text(encoding="utf-8"),
}
GATES = "compile: ok. test: ok - no regressions; 247 passed, 0 failed; all 3 acceptance test(s) green."

print(f"findings parsed: {len(findings)} (expect 6); ground-truth correct: {sorted(CORRECT)}\n")

ARMS = [
    ("deepseek-v4-pro:cloud", False, 24000),   # shipped default
    ("deepseek-v4-pro:cloud", True,  64000),   # same model, allowed to reason
    ("glm-5.2:cloud",         False, 24000),
    ("glm-5.2:cloud",         True,  64000),
    ("kimi-k3:cloud",         True,  64000),
    ("mistral-large-3:675b-cloud", False, 24000),
]

real_chat = arb.chat
rows = []
for rep in (1, 2):
  for model, think, budget in ARMS:
      def chat_with_think(m, msgs, **kw):
          kw["think"] = think
          kw["max_tokens"] = budget
          return real_chat(m, msgs, **kw)
      try:
        with mpatch.object(arb, "chat", side_effect=chat_with_think):
            adj = arb.adjudicate(model, ticket, findings, GATES, patch_diff,
                                 rules=prof.arbiter_rules, max_tokens=budget)
      except Exception as exc:
        rows.append((model, think, budget, "EXC", str(exc)[:60], 0)); continue
      if not adj.ok:
        rows.append((model, think, budget, "UNREACHABLE", (adj.error or "")[:60], 0)); continue
      upheld = {r.index for r in adj.by(arb.UPHELD)}
      hits = len(upheld & CORRECT)
      rows.append((model, think, budget, adj.recommendation, f"upheld={sorted(upheld)}", hits))
      print(f"  rep{rep} {model:<30} think={str(think):<5} -> {adj.recommendation:<8} upheld={sorted(upheld)} correct-caught={hits}/5")

print("\n=== SUMMARY (correct findings upheld, out of 5) ===")
for model, think, budget, rec, note, hits in rows:
    print(f"  {hits}/5  {rec:<12} {model:<30} think={think} budget={budget}  {note}")
json.dump([{"model": m, "think": t, "budget": b, "rec": r, "note": n, "hits": h}
           for m, t, b, r, n, h in rows], open(ART / "results_latest.json", "w"), indent=2)
