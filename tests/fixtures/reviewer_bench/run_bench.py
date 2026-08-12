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
import concurrent.futures
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_loop.config import MODEL_CATALOG, _BACKEND_PREFIXES   # noqa: E402
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


def is_local(name: str) -> bool:
    """Does this model run on THIS box rather than on a provider?

    MEASURED, and the reason local arms are off by default: qwen3-vl:8b at a
    73728-token context wants 17 GB, the RTX 4060 Laptop has 8, so ollama put
    69% of it on the CPU and the GPU sat at 4% utilisation. It ran for over
    nine minutes on one review and had not finished; the ten CLOUD arms before
    it took ten minutes BETWEEN them. They are also the models the catalogue
    already rules out for every role, so they cost the most and inform least.

    The catalogue records "local" only in prose, so this reads the naming
    convention instead: a cloud model carries a `:cloud`/`-cloud` suffix, and
    everything reached through another backend carries its prefix.
    """
    n = (name or "").strip().lower()
    if any(n.startswith(p) for p in _BACKEND_PREFIXES):
        return False
    if n.startswith(("gemini-", "claude-", "gpt-")):
        return False
    return not (n.endswith(":cloud") or n.endswith("-cloud"))


def _matches(text: str, entry: dict) -> bool:
    """Keyword hint. Every term must appear; deliberately crude, see the header."""
    low = text.lower()
    return all(t.lower() in low for t in entry["must_mention"])


def score(findings, key):
    """Match each key entry against a SINGLE finding, never against the join.

    The first version joined every finding into one blob and asked whether the
    key's terms appeared anywhere in it. That rewards VOLUME: deepseek-v4-pro
    filed 37 findings on this case, most of them self-refuting ("[BLOCKER] ...
    **No blocker here.**"), and scored 2 hits -- more than any concise model --
    because with 37 findings some sentence somewhere contains "null" and some
    other sentence contains "Relationships". The blob cannot tell "raised the
    defect" from "used both words at some point".

    Requiring one finding to carry all the terms is still only a HINT -- a model
    can name MaxPositionSize while saying something wrong about it, which is why
    the header insists the honest number comes from reading out/. But it no
    longer scores a model higher for talking longer.
    """
    hits = [e["id"] for e in key.get("correct", [])
            if any(_matches(f.text, e) for f in findings)]
    noise = [e["id"] for e in key.get("inert", [])
             if any(_matches(f.text, e) for f in findings)]
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
    ap.add_argument(
        "--jobs", type=int, default=10,
        help="arms in flight at once. The arms are independent and the panel "
             "already runs two reviewers concurrently against this endpoint.",
    )
    ap.add_argument(
        "--temperature", type=float, default=None,
        help="override the sampling temperature (config's default is 0.1). "
             "This is the LOTTERY axis: a review is a long generation whose "
             "content is a search over what to examine, so one divergent token "
             "early produces a wholly different review. glm-5.2 raised this "
             "case's strongest defect on one draw and not on a byte-identical "
             "re-draw. Set 0.0 to ask whether that variance is sampling.",
    )
    ap.add_argument(
        "--reps", type=int, default=1,
        help="repetitions per arm. MEASURED need: deepseek-v4-flash:cloud "
             "returned 147267 chars on one call and ~7000 on the next, same "
             "model, same prompt, same think=false. One rep cannot tell a model "
             "apart from a bad day.",
    )
    ap.add_argument(
        "--include-local", action="store_true",
        help="also run the models that execute on this box. OFF by default: "
             "see is_local() for the measurement that says why.",
    )
    args = ap.parse_args()

    think = None if args.think is None else args.think == "true"
    if args.models:
        arms = [(m, bool(think), args.budget) for m in args.models]
    else:
        arms = all_arms(args.budget, think)

    # An explicit --models list is obeyed as given; the filter only applies to
    # the "everything in the catalogue" default, so asking for a local model by
    # name still runs it.
    if not args.models and not args.include_local:
        skipped = sorted({m for m, _, _ in arms if is_local(m)})
        arms = [a for a in arms if not is_local(a[0])]
        if skipped:
            print(f"skipping {len(skipped)} local model(s): {', '.join(skipped)}")
            print("  (--include-local to run them; they spill to CPU on this box)\n")

    for reasoning in args.reasoning:
        if reasoning not in REASONING_ARMS:
            print(f"unknown --reasoning {reasoning!r}; have {sorted(REASONING_ARMS)}")
            return 1

    dirs = sorted(p for p in CASES.iterdir() if p.is_dir())
    if args.case:
        dirs = [d for d in dirs if d.name == args.case]
    cases = [c for c in (load_case(d) for d in dirs) if c]
    if not cases:
        print("no cases found under", CASES)
        return 1

    OUT.mkdir(exist_ok=True)
    print(f"cases: {[c['name'] for c in cases]}   arms: {len(arms)}   "
          f"budget: {args.budget}   jobs: {args.jobs}\n"
          "rows appear in COMPLETION order, not catalogue order.\n")
    hdr = (f"{'case':<16} {'model':<30} {'reason':<8} {'rep':>3} {'status':<12} "
           f"{'find':>4} {'hits':>5} {'noise':>5} {'out':>7} {'think':>8} {'secs':>6}")
    print(hdr)
    print("-" * len(hdr))

    def run_one(case, reasoning, model, thinking, budget, rep):
        think_value, system_suffix = REASONING_ARMS[reasoning]
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
                temperature=args.temperature,
            )
            text, err = out.text or "", ""
        except ProviderError as exc:
            text, err, out = "", str(exc)[:80], None
        except Exception as exc:  # a bench must not lose 9 arms to 1 bad one
            text, err, out = "", f"{type(exc).__name__}: {exc}"[:80], None
        secs = time.time() - t0

        stem = (f"{case['name']}__{model.replace(':', '_')}__{reasoning}"
                + (f"__t{args.temperature}" if args.temperature is not None else "")
                + (f"__r{rep}" if rep > 1 else ""))
        (OUT / f"{stem}.txt").write_text(text or f"ERROR: {err}", encoding="utf-8")

        if err:
            status, n, hits, noise, sev = "UNREACHABLE", 0, [], [], {}
        else:
            vote = parse_review(text, model)
            status = vote.status
            n = len(vote.finding_list)
            hits, noise = score(vote.finding_list, case["key"])
            # Severity is recorded because the key's headline is that
            # severity grading is where glm failed: its only BLOCKER was
            # wrong and its best finding was graded MAJOR. A model that
            # files no BLOCKER on this case is not thereby worse.
            sev = {}
            for f in vote.finding_list:
                k = (f.severity or "?").upper()
                sev[k] = sev.get(k, 0) + 1

        # `chat` returns these as ATTRIBUTES on its result, not as a usage
        # dict. The first draft read `out.usage["eval_count"]` and
        # `out.thinking`, neither of which exists, so both columns were
        # structurally zero -- a harness that would have reported a clean
        # table with the one axis this bench exists to measure blanked out.
        # Found by running it, not by reading it.
        return {
            "case": case["name"], "model": model, "reasoning": reasoning,
            "rep": rep, "think": effective, "temperature": args.temperature,
            "status": status, "findings": n,
            "hits": hits, "noise": noise, "severity": sev,
            "out_tokens": getattr(out, "output_tokens", 0) if out else 0,
            "think_chars": getattr(out, "thinking_chars", 0) if out else 0,
            "chars": len(text),
            "secs": round(secs, 1), "error": err,
        }

    # PERSIST AFTER EVERY ARM, not at the end. The first version wrote
    # results.json once, on the last line of main() -- so interrupting a
    # 40-minute sweep threw away every completed arm's token and reasoning
    # counts, which are exactly what cannot be recovered from the saved raw
    # text. Learned by interrupting one.
    #
    # MERGE rather than overwrite, because the sweep is run in stages and a
    # plain write meant the last stage silently deleted every earlier one.
    prior = OUT / "results.json"
    merged = {}
    if prior.is_file():
        for r in json.loads(prior.read_text(encoding="utf-8")):
            merged[(r["case"], r["model"], r["reasoning"], r.get("rep", 1),
                str(r.get("temperature")))] = r

    def persist(row):
        merged[(row["case"], row["model"], row["reasoning"], row["rep"],
                str(row["temperature"]))] = row
        prior.write_text(
            json.dumps([merged[k] for k in sorted(merged)], indent=2),
            encoding="utf-8")

    jobs = [(c, r, m, th, b, rep)
            for c in cases for r in args.reasoning for (m, th, b) in arms
            for rep in range(1, args.reps + 1)]

    # Concurrency, because the arms are independent and the panel already
    # proves this endpoint serves parallel requests -- review_panel has run two
    # reviewers at once since it was written. Serial was simply never revisited,
    # and it cost ten minutes for ten cloud arms that answer in about sixty
    # seconds each.
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(run_one, *j): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            row = fut.result()
            rows.append(row)
            persist(row)
            print(f"{row['case']:<16} {row['model']:<30} {row['reasoning']:<8} "
                  f"{row['rep']:>3} {row['status']:<12} {row['findings']:>4} "
                  f"{len(row['hits']):>5} {len(row['noise']):>5} "
                  f"{row['out_tokens']:>7} {row['think_chars']:>8} "
                  f"{row['secs']:>6}", flush=True)
            if row["error"]:
                print(f"{'':>16} {'':>30} -> {row['error']}", flush=True)
    print(f"\nraw responses + results.json in {OUT}")
    print("hits/noise are KEYWORD HINTS. Read the raw files before believing an order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
