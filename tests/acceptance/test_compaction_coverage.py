"""
Phase 4b must summarise the history it CLAIMS to summarise.

`_llm_summary` cut each prior message to 2000 chars and the whole prompt to
20000 chars. Phase 4b only fires once the pruned history exceeds
round_input_token_budget (40000 tokens = 160000 chars), so at the only moment
this code runs, the compactor read about an eighth of its input and the result
was still labelled "[PRIOR ROUNDS SUMMARY (LLM compacted)]".

Compaction has never run in a real loop (BACKLOG O7), so nothing would have
noticed.
"""
from unittest.mock import patch

from agent_loop import compaction
from agent_loop.compaction import _select_for_summary, _llm_summary
from agent_loop.providers import Completion, ProviderError


def _prior(n, chars):
    """n prior messages, alternating roles, each `chars` long and identifiable."""
    return [
        {"role": "assistant" if i % 2 else "user", "content": f"MSG{i:03d} " + "x" * chars}
        for i in range(n)
    ]


def test_a_realistic_4b_history_is_read_whole():
    """40 messages x 4000 chars = 160000 chars, which is exactly the scale at
    which 4b fires. The old code passed 20000 chars of it."""
    prior = _prior(40, 4000)
    body, covered, total = _select_for_summary(prior)
    assert total == 40
    assert covered == 40, f"only {covered}/40 messages reached the compactor"
    assert len(body) > 150_000, len(body)
    for i in range(40):
        assert f"MSG{i:03d}" in body


def test_when_it_does_not_fit_the_oldest_are_dropped_whole():
    """Half a finding is worse than no finding: it reads as complete."""
    prior = _prior(200, 4000)  # 800k chars, far past any budget
    body, covered, total = _select_for_summary(prior)
    assert total == 200
    assert 0 < covered < 200
    # The NEWEST survive, the oldest are gone, and nothing is half-present.
    assert f"MSG{199:03d}" in body
    assert "MSG000" not in body
    assert body.count("... (this message truncated)") == 0


def test_partial_coverage_is_stated_not_implied(monkeypatch):
    """The label used to claim a complete summary regardless."""
    history = (
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "implement"}]
        + _prior(200, 4000)
        + [{"role": "user", "content": "latest feedback"}]
    )
    with patch.object(compaction, "_split_for_summary", wraps=compaction._split_for_summary):
        with patch("agent_loop.providers.chat", return_value=Completion(text="a summary", model="m")):
            out = _llm_summary(history, 200_000, _profile())
    assert out is not None
    blob = "\n".join(m["content"] for m in out)
    assert "covers" in blob and "/200" in blob, blob[:400]


def test_a_provider_failure_is_reported_not_swallowed(capsys):
    history = (
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "implement"}]
        + _prior(6, 100)
        + [{"role": "user", "content": "latest"}]
    )
    with patch("agent_loop.providers.chat", side_effect=ProviderError("model down")):
        assert _llm_summary(history, 100_000, _profile()) is None
    assert "model down" in capsys.readouterr().out


def _profile():
    from agent_loop.profiles import Profile
    return Profile(
        name="compaction-probe", language="python", file_suffixes=(".py",),
        line_comment="#", block_comment=(), block_kind="indent",
        implementer_rules="t", reviewer_priorities="t",
    )
