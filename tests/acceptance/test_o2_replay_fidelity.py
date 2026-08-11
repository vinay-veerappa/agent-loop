"""
O2: a replay must hold the prompt constant, or a verdict flip measures nothing.

`run_replay` could not reconstruct the regions, so it BUILT ITS OWN review prompt
-- the implement prompt truncated to 2000 chars plus the raw implementer output
truncated to 8000. The recorded verdict came from the real prompt: BEFORE/AFTER
blocks, gate summary, settled decisions, acceptance tests, graph context, learning
feedback. So a "flip" compared two different prompts and said nothing about the
change under test, while run_replay_corpus returns non-zero on any flip -- a CI
gate that fails on noise.

Second defect on the same path: `art = ticket_dir` handed the recorded ticket
directory to review_panel, which writes r{N}_review_{model}.txt into it. **A
replay overwrote the corpus it was replaying**, so the second replay of a ticket
read artifacts the first replay had already changed.

The fix these tests pin:
  * the loop records the rendered review prompt and the rendered arbiter prompt;
  * replay re-sends the recorded prompt BYTE-FOR-BYTE;
  * a corpus with no recorded prompt is REFUSED, not silently approximated --
    an unfaithful comparison is worse than no comparison, because it looks like
    a measurement; and
  * replay artifacts are written under ticket_dir/replay/, never into the corpus.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import replay as replay_mod
from agent_loop.loop import PanelResult, Vote
from agent_loop.profiles import Profile

PROFILE = Profile(
    name="test-o2-replay",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="test", reviewer_priorities="test",
)

RECORDED_PROMPT = (
    "# Review ticket T9 (round 1)\n\n"
    "## BEFORE\n```python\ndef f():\n    return 1\n```\n\n"
    "## AFTER\n```python\ndef f():\n    return 2\n```\n\n"
    "## Gate summary\nstatic: ok; compile: ok; test: ok\n\n"
    "## Settled decisions\n- do not rename public members\n"
)


def _corpus(tmp_path: Path, with_prompt: bool = True, verdict: str = "APPROVE") -> Path:
    """A minimal recorded ticket directory."""
    d = tmp_path / "T9"
    d.mkdir(parents=True)
    (d / "result.json").write_text(
        json.dumps({"ticket": "T9", "final_verdict": verdict}), encoding="utf-8"
    )
    (d / "r1_impl_raw.txt").write_text(
        "<<<BLOCK R1>>>\ndef f():\n    return 2\n<<<END BLOCK>>>\n", encoding="utf-8"
    )
    (d / "00_implement_prompt.md").write_text("the implement prompt", encoding="utf-8")
    if with_prompt:
        (d / "r1_review_prompt.md").write_text(RECORDED_PROMPT, encoding="utf-8")
    return d


def _approving_panel():
    # Vote.counted is a derived property (status in _RANK), not a field.
    return PanelResult(
        votes=[Vote(model="m1", status="APPROVE")],
        verdict="APPROVE",
        valid=True,
    )


def test_replay_sends_the_recorded_prompt_byte_for_byte(tmp_path):
    """The whole point: same prompt in, so a flip is attributable to the change."""
    d = _corpus(tmp_path)
    seen = {}

    def fake_panel(reviewers, prompt, system, art, rnd, deadline_secs=1800):
        seen["prompt"] = prompt
        seen["art"] = art
        return _approving_panel()

    with patch.object(replay_mod, "review_panel", fake_panel):
        result = replay_mod.run_replay(tmp_path, d, PROFILE, ["m1"], "")

    assert "error" not in result, result
    assert seen["prompt"] == RECORDED_PROMPT, (
        "replay must re-send the recorded prompt verbatim, not rebuild one"
    )


def test_replay_refuses_a_corpus_with_no_recorded_prompt(tmp_path):
    """No recorded prompt means no faithful comparison is possible.

    Refuse and say so, rather than approximating: an unfaithful flip looks like
    a measurement and is worse than no measurement. It must also not spend a
    model call to produce that non-answer.
    """
    d = _corpus(tmp_path, with_prompt=False)
    called = {"n": 0}

    def fake_panel(*a, **kw):
        called["n"] += 1
        return _approving_panel()

    with patch.object(replay_mod, "review_panel", fake_panel):
        result = replay_mod.run_replay(tmp_path, d, PROFILE, ["m1"], "")

    assert "error" in result, "must refuse a corpus that predates prompt recording"
    assert "prompt" in result["error"].lower()
    assert called["n"] == 0, "must not call the panel when the comparison is meaningless"


def test_replay_does_not_write_into_the_corpus(tmp_path):
    """A replay must not modify the corpus it is measuring."""
    d = _corpus(tmp_path)
    before = sorted(p.name for p in d.iterdir())

    def fake_panel(reviewers, prompt, system, art, rnd, deadline_secs=1800):
        # Behave like the real panel: write an artifact where told to.
        art.mkdir(parents=True, exist_ok=True)
        (art / f"r{rnd}_review_m1.txt").write_text("verdict", encoding="utf-8")
        return _approving_panel()

    with patch.object(replay_mod, "review_panel", fake_panel):
        replay_mod.run_replay(tmp_path, d, PROFILE, ["m1"], "")

    after = sorted(p.name for p in d.iterdir())
    new = set(after) - set(before)
    assert new <= {"replay"}, f"replay wrote into the corpus: {sorted(new)}"
    assert (d / "replay").is_dir(), "replay artifacts should land in ticket_dir/replay/"
    assert (d / "replay" / "r1_review_m1.txt").exists()


def test_replay_reports_the_recorded_verdict_it_compares_against(tmp_path):
    d = _corpus(tmp_path, verdict="APPROVE")

    def fake_panel(reviewers, prompt, system, art, rnd, deadline_secs=1800):
        return _approving_panel()

    with patch.object(replay_mod, "review_panel", fake_panel):
        result = replay_mod.run_replay(tmp_path, d, PROFILE, ["m1"], "")

    assert result["recorded_verdict"] == "APPROVE"
    assert result["replayed_verdict"] == "APPROVE"
    assert result["flipped"] is False


def test_adjudication_records_the_prompt_it_sent():
    """The arbiter prompt must be recordable, or arbiter replay cannot be faithful."""
    from agent_loop.arbiter import Adjudication

    adj = Adjudication(True, prompt="the rendered arbiter prompt")
    assert adj.prompt == "the rendered arbiter prompt"


def test_arbiter_prompt_override_is_sent_verbatim():
    """Replay re-sends the recorded arbiter prompt instead of rebuilding it."""
    from agent_loop import arbiter as arb

    sent = {}

    def fake_chat(model, messages, **kw):
        sent["user"] = messages[-1]["content"]
        from agent_loop.providers import Completion
        return Completion(
            text="<<<RULING>>>\nSHIP\n<<<END RULING>>>", model=model,
            input_tokens=1, output_tokens=1,
        )

    finding = type("F", (), {"text": "a finding", "blocking": True, "author": "m1"})()
    with patch.object(arb, "chat", fake_chat):
        arb.adjudicate(
            "arb-model", {"id": "T9", "title": "t", "defect": "d", "spec": "s"},
            [finding], "gates ok", "",
            prompt_override="EXACTLY THIS PROMPT",
        )

    assert sent["user"] == "EXACTLY THIS PROMPT"
