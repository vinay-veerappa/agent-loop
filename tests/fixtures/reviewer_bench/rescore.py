"""Re-score the SAVED responses in out/ without spending a single model call.

O28's lesson is that the bottleneck is labelling, not running. The answer key is
hand-written and will keep changing as cases are added and as claims get checked
against the code -- and every change used to mean re-running the sweep, which
both costs an hour and CHANGES THE DATA, because these models are not
deterministic (deepseek-v4-pro returned 37 findings on one call and an
budget-exhausting repetition loop on another, same prompt, same flags).

So: parsing and scoring are re-run from the raw text on disk; the columns that
can only come from the call itself -- out_tokens, think_chars, secs -- are
carried over from the existing results.json untouched.

    python rescore.py            # rewrite results.json in place
    python rescore.py --diff     # show what changed, write nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from agent_loop.loop import parse_review          # noqa: E402

from run_bench import CASES, OUT, load_case, score   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args()

    results = OUT / "results.json"
    if not results.is_file():
        print(f"no {results}; run run_bench.py first")
        return 1
    rows = json.loads(results.read_text(encoding="utf-8"))
    keys = {d.name: load_case(d)["key"]
            for d in CASES.iterdir() if d.is_dir() and load_case(d)}

    changed = 0
    for row in rows:
        rep = row.get("rep", 1)
        stem = (f"{row['case']}__{row['model'].replace(':', '_')}"
                f"__{row['reasoning']}" + (f"__r{rep}" if rep > 1 else ""))
        raw = OUT / f"{stem}.txt"
        if not raw.is_file():
            print(f"MISSING raw text for {stem}; left as recorded")
            continue
        text = raw.read_text(encoding="utf-8")
        if text.startswith("ERROR: "):
            continue

        vote = parse_review(text, row["model"])
        hits, noise = score(vote.finding_list, keys.get(row["case"], {}))
        before = (row["status"], row["findings"], row["hits"], row["noise"])
        after = (vote.status, len(vote.finding_list), hits, noise)
        if before != after:
            changed += 1
            print(f"{row['model']:<28} {row['reasoning']:<8} r{rep}  "
                  f"{before[0]}/{before[1]}f hits={before[2]} noise={before[3]}"
                  f"   ->   {after[0]}/{after[1]}f hits={after[2]} noise={after[3]}")
        row["status"], row["findings"] = vote.status, len(vote.finding_list)
        row["hits"], row["noise"] = hits, noise

    print(f"\n{changed} of {len(rows)} row(s) changed")
    if args.diff:
        print("--diff: nothing written")
        return 0
    results.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"rewrote {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
