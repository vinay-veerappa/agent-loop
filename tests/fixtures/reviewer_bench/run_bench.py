"""Labelled REVIEWER benchmark: which models are worth their place on the panel?

The arbiter has been measured to death (18 configurations, O28) and the reviewer
never has -- yet the reviewer is the role that actually FINDS things, and on the
CM2 ticket the panel caught a naked-risk regression that every mechanical gate
had passed. The panel is also where the budget went: `minimax-m3` spent all
24000 of its tokens on 104128 characters of reasoning and returned empty content
(O57).

WHAT THIS MEASURES, per (case, model, think, budget) arm:

  reachable   did it return parseable content at all, or die like O57
  findings    how many it raised, by severity
  hits        how many of the case's KNOWN-CORRECT findings it raised
  noise       how many it raised that the key marks INERT
  out         output tokens spent
  think       characters of reasoning (the O57 axis)
  secs        wall clock

`hits` and `noise` are matched by keyword against the answer key and are a
HINT, not a score: a model can name `MaxPositionSize` while saying something
wrong about it. Every raw response is written to out/ and the honest number
comes from reading them. This is stated because O28's whole lesson is that the
bottleneck is labelling, not running, and an automated proxy that looks like a
score is how that lesson gets lost.

⚠️ ONE CASE RANKS NOTHING. O28 measured eighteen arbiter arms against a single
patch and the finding was "the bottleneck is the corpus, not the pool" -- the
ranking may be a property of that one case. Add cases before believing an order.

Usage:
    python run_bench.py                      # every reachable arm, every case
    python run_bench.py --models glm-5.2:cloud minimax-m3:cloud
    python run_bench.py --budget 48000 --think false
    python run_bench.py --case cm2_roundtrip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_loop.config import MODEL_CATALOG               # noqa: E402
from agent_loop.loop import parse_review                  # noqa: E402
from agent_loop.providers import ProviderError, chat      # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
OUT = HERE / "out"

# Every model in the catalogue that could plausibly sit on a review panel.
# Backends other than ollama need credentials this box does not have; they are
# listed so the run REPORTS them as unreachable rather than quietly omitting
# them -- "we evaluated everything" has to mean something.
def all_arms(budget: int, think: bool | None):
    arms = []
    for name, prof in MODEL_CATALOG.items():
        if think is None:
            # Ask a model to think only if it can. A think=True on a model that
            # cannot is rejected by config's own guard.
            arms.append((name, False, budget))
            if prof.thinking:
                arms.append((name, True, budget))
        else:
            if think and not prof.thinking:
                continue
            arms.append((name, think, budget))
    return arms


def load_case(d: Path):
    prompt = next(iter(sorted(d.glob("*review_prompt.md"))), None)
    system = d / "system_prompt.md"
    key = d / "answer_key.json"
    if not (prompt and system.is_file()):
        return None
    return {
        "name": d.name,
        "system": system.read_text(encoding="utf-8"),
        "prompt": prompt.read_text(encoding="utf-8"),
        "key": json.loads(key.read_text(encoding="utf-8")) if key.is_file() else
               {"correct": [], "inert": []},
    }


# Asked for by the user: "maybe each LLM might need a specific way of prompting
# to reduce the amount of reasoning output". This is the axis that answers it.
#
# `off` is not a switch. MEASURED on kimi-k2.7-code with think=False: 203119
# chars of reasoning on one round and 53096 on the next -- reduced from the
# 282935/435641 it produced with think=True, and still very far from none. So
# the question is not "on or off" but "which lever does THIS model answer to",
# and the honest way to find out is to send each one and read the counter.
#
# A provider that rejects a value is a RESULT, not an error: it means the lever
# does not exist there and the prompt is the only remaining route.
REASONING_ARMS = {
    "default": (None, ""),
    "off": (False, ""),
    "low": ("low", ""),
    "medium": ("medium", ""),
    "high": ("high", ""),
    "bounded": (None, (
        "\n\nBEFORE answering, think in AT MOST 3 short bullet points. Do not "
        "restate the diff, do not enumerate what you checked and found clean, "
        "and do not write a plan. Then give the answer in the required format."
    )),
}


def _matches(text: str, entry: dict) -> bool:
    """Keyword hint. Every term must appear; deliberately crude, see the header."""
    low = text.lower()
    return all(t.lower() in low for t in entry["must_mention"])


def score(findings, key):
    joined = "\n".join(f.text for f in findings)
    hits = [e["id"] for e in key.get("correct", []) if _matches(joined, e)]
    noise = [e["id"] for e in key.get("inert", []) if _matches(joined, e)]
    return hits, noise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--budget", type=int, default=48000)
    ap.add_argument("--think", default=None, choices=["true", "false"])
    ap.add_argument("--case", default=None)
    ap.add_argument(
        "--reasoning", nargs="*", default=["default"],
        help="how to try to bound reasoning: default | off | low | medium | high "
             "| bounded (a system-prompt instruction). Each is an ARM, so one run "
             "answers 'does this model respond to this lever at all'.",
    )
    args = ap.parse_args()

    think = None if args.think is None else args.think == "true"
    if args.models:
        arms = [(m, bool(think), args.budget) for m in args.models]
    else:
        arms = all_arms(args.budget, think)

    dirs = sorted(p for p in CASES.iterdir() if p.is_dir())
    if args.case:
        dirs = [d for d in dirs if d.name == args.case]
    cases = [c for c in (load_case(d) for d in dirs) if c]
    if not cases:
        print("no cases found under", CASES)
        return 1

    OUT.mkdir(exist_ok=True)
    print(f"cases: {[c['name'] for c in cases]}   arms: {len(arms)}   "
          f"budget: {args.budget}\n")
    hdr = (f"{'case':<16} {'model':<30} {'reason':<8} {'status':<12} "
           f"{'find':>4} {'hits':>5} {'noise':>5} {'out':>7} {'think':>8} {'secs':>6}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for case in cases:
      for reasoning in args.reasoning:
        if reasoning not in REASONING_ARMS:
            print(f"unknown --reasoning {reasoning!r}; have {sorted(REASONING_ARMS)}")
            return 1
        think_value, system_suffix = REASONING_ARMS[reasoning]
        for model, thinking, budget in arms:
            # The reasoning arm owns `think` when it sets one; otherwise the
            # model arm's own flag applies.
            effective = think_value if reasoning != "default" else thinking
            t0 = time.time()
            try:
                out = chat(
                    model,
                    [{"role": "system", "content": case["system"] + system_suffix},
                     {"role": "user", "content": case["prompt"]}],
                    max_tokens=budget, timeout=900, think=effective,
                )
                text, err = out.text or "", ""
            except ProviderError as exc:
                text, err, out = "", str(exc)[:80], None
            secs = time.time() - t0

            stem = (f"{case['name']}__{model.replace(':', '_')}__{reasoning}")
            (OUT / f"{stem}.txt").write_text(text or f"ERROR: {err}", encoding="utf-8")

            if err:
                status, n, hits, noise = "UNREACHABLE", 0, [], []
            else:
                vote = parse_review(text, model)
                status = vote.status
                n = len(vote.finding_list)
                hits, noise = score(vote.finding_list, case["key"])

            usage = getattr(out, "usage", {}) if out else {}
            row = {
                "case": case["name"], "model": model, "reasoning": reasoning,
                "think": effective,
                "status": status, "findings": n,
                "hits": hits, "noise": noise,
                "out_tokens": (usage or {}).get("eval_count", 0),
                "think_chars": len(getattr(out, "thinking", "") or "") if out else 0,
                "secs": round(secs, 1), "error": err,
            }
            rows.append(row)
            print(f"{row['case']:<16} {model:<30} {reasoning:<8} "
                  f"{status:<12} {n:>4} {len(hits):>5} {len(noise):>5} "
                  f"{row['out_tokens']:>7} {row['think_chars']:>8} {row['secs']:>6}")
            if err:
                print(f"{'':>16} {'':>30} -> {err}")

    (OUT / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nraw responses + results.json in {OUT}")
    print("hits/noise are KEYWORD HINTS. Read the raw files before believing an order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
