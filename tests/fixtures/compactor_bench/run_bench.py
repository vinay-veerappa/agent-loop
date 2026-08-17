"""Labelled compactor benchmark: does the summary keep what was REJECTED?

Phase 4b replaces prior rounds with a summary that the implementer then reasons
from. The failure that matters is not prose quality -- it is dropping a rejected
approach, because the next round will then propose it again and burn a round
rediscovering the rejection.

So the metric is recall of planted rejections, not readability.

Ground truth: eight distinctly-tagged rejections are planted across a history
sized like a real Phase 4b input. A summary that mentions a tag has carried that
rejection forward; one that does not has lost it.

    python tests/fixtures/compactor_bench/run_bench.py [model ...]
"""
import sys, json, dataclasses
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agent_loop import config, models, compaction
from agent_loop.profiles import Profile

# Eight rejections, each with a tag the summary must carry forward.
REJECTIONS = [
    ("RJ-ALPHA",  "caching the ledger in memory", "loses appends if the process dies"),
    ("RJ-BRAVO",  "widening the protected-path glob", "would let the patch edit its own tests"),
    ("RJ-CHARLIE","parsing the gate name out of the detail prose", "is what the defect IS"),
    ("RJ-DELTA",  "counting every round as a separate ticket", "double-counts one ticket"),
    ("RJ-ECHO",   "defaulting the gate field to empty string", "collapses clean and legacy entries"),
    ("RJ-FOXTROT","calling git apply --3way", "writes conflict markers into the live file"),
    ("RJ-GOLF",   "skipping the frozen baseline", "turns pre-existing failures into regressions"),
    ("RJ-HOTEL",  "raising max_tokens to fix empty content", "the model was emitting a tool call"),
]
FILLER = ("The implementer re-emitted all blocks in full. Region source follows, unchanged "
          "from the previous round, included so the model can edit it verbatim. " * 90)


def build_history():
    """A history at the scale Phase 4b actually runs at (>160000 chars)."""
    h = [{"role": "system", "content": "SYSTEM PROMPT (pinned)"},
         {"role": "user", "content": "IMPLEMENT PROMPT (pinned): fix the gate-failure distribution."}]
    for i, (tag, approach, reason) in enumerate(REJECTIONS):
        h.append({"role": "assistant", "content": f"Round {i+1} candidate. {FILLER}"})
        h.append({"role": "user", "content":
                  f"REVIEW round {i+1}. BLOCKER [{tag}]: {approach} -- REJECTED because it {reason}. "
                  f"Do not propose it again. {FILLER}"})
    h.append({"role": "user", "content": "Latest feedback: the newest candidate still fails the test gate."})
    return h


# Keywords that must ALL appear for a rejection to count as carried forward.
# The first version of this scored literal tag presence, which measured
# tag-COPYING rather than faithfulness: gemma4:31b scored 0/8 while its summary
# said "Widening the protected-path glob: Rejected because it would allow the
# patch to edit its own tests". A metric that punishes paraphrase is measuring
# the wrong thing, and it produced a confidently wrong ranking.
KEYWORDS = {
    "RJ-ALPHA":   ("cach", "ledger"),
    "RJ-BRAVO":   ("protected-path", "glob"),
    "RJ-CHARLIE": ("gate name", "prose"),
    "RJ-DELTA":   ("separate ticket", "double-count"),
    "RJ-ECHO":    ("gate field", "empty string"),
    "RJ-FOXTROT": ("3way",),
    "RJ-GOLF":    ("frozen baseline",),
    "RJ-HOTEL":   ("max_tokens",),
}


def score(summary_text):
    """Rejections carried forward, by CONTENT. Tag or paraphrase both count."""
    low = summary_text.lower()
    kept = []
    for tag, _, _ in REJECTIONS:
        if tag in summary_text or all(k.lower() in low for k in KEYWORDS[tag]):
            kept.append(tag)
    return kept


def main():
    candidates = sys.argv[1:] or [
        "glm-5.2:cloud", "deepseek-v4-flash:0731-cloud", "gemma4:31b-cloud",
        "qwen3.5:cloud", "gemma4:latest",
    ]
    history = build_history()
    chars = sum(len(m["content"]) for m in history)
    print(f"history: {len(history)} messages, {chars} chars "
          f"(Phase 4b fires above {40000*4})\n")

    prof = Profile(name="compactor-bench", language="python", file_suffixes=(".py",),
                   line_comment="#", block_comment=(), block_kind="indent",
                   implementer_rules="t", reviewer_priorities="t")
    base = config.DEFAULTS
    rows = []
    for model in candidates:
        roles = dict(base.roles)
        roles["compactor"] = dataclasses.replace(roles["compactor"], model=model)
        for rep in (1, 2):
            try:
                config.set_active(dataclasses.replace(base, roles=roles))
                models.reload_default_registry(config.get())
                out = compaction._llm_summary(history, 200_000, prof)
            except Exception as exc:
                print(f"  rep{rep} {model:<28} EXC {type(exc).__name__}: {exc}"); continue
            finally:
                config.reset(); models.reload_default_registry(config.DEFAULTS)
            if out is None:
                print(f"  rep{rep} {model:<28} returned None (fell back to mechanical)")
                rows.append((model, rep, 0, "None")); continue
            blob = "\n".join(m["content"] for m in out)
            kept = score(blob)
            (Path(__file__).parent / f"summary_{model.replace(':','_')}_rep{rep}.txt").write_text(blob, encoding="utf-8")
            missing = [t for t, _, _ in REJECTIONS if t not in kept]
            print(f"  rep{rep} {model:<28} kept {len(kept)}/8  missing={missing}")
            rows.append((model, rep, len(kept), ",".join(missing)))

    print("\n=== SUMMARY (rejections carried forward, out of 8) ===")
    for model, rep, kept, missing in rows:
        print(f"  {kept}/8  rep{rep}  {model:<28} missing={missing}")
    json.dump([{"model": m, "rep": r, "kept": k, "missing": x} for m, r, k, x in rows],
              open(Path(__file__).parent / "results_latest.json", "w"), indent=2)


if __name__ == "__main__":
    main()
