"""
CF-40: a retired model must be found BEFORE a run spends a round, not inside it.

Ollama retired the second reviewer (CF-37) and then the arbiter (CF-40), and
both times the loop learned it when that call failed mid-run -- the arbiter case
after the implementer had spent a round and every gate had passed. The
catalogue's RETIRED marker could not help: it records what someone already
knew. `cli.main()` now probes every model the run will call; HTTP 410 refuses
the run, any other failure only warns.

These drive the real `main()` and the real `probe.check` with `chat` stubbed at
the probe's import site, so nothing reaches a provider.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_loop import probe
from agent_loop.cli import main
from agent_loop.profiles import Profile, register
from agent_loop.providers import Completion, ProviderError

register(Profile(
    name="test-cf40-probe",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
))

GONE = ('{m} failed after 1 attempt: HTTPError 410: Gone -- '
        '{{"error":"{m} was retired at 2026-09-25"}}')
REFUSED = "{m} failed after 1 attempt: URLError: <urlopen error [WinError 10061] refused>"


@pytest.fixture
def probe_on(monkeypatch):
    monkeypatch.delenv("AGENT_LOOP_NO_PROBE", raising=False)


def _fake_chat(dead=(), flaky=(), seen=None):
    def fake(model, messages, **kw):
        if seen is not None:
            seen.append(model)
        if model in dead:
            raise ProviderError(GONE.format(m=model))
        if model in flaky:
            raise ProviderError(REFUSED.format(m=model))
        return Completion(text="OK", model=model)
    return fake


def _run(tmp_path, *argv):
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps({"tickets": [{
        "id": "T1", "title": "t", "defect": "d", "spec": "s",
        "regions": [{"id": "R1", "file": "src/x.py", "anchor": "def x"}],
    }]}), encoding="utf-8")
    ran = []

    def fake_run_ticket(repo, ticket, *a, **kw):
        ran.append(ticket["id"])
        return {"ticket": ticket["id"], "final_verdict": "APPROVE", "applied": False,
                "rounds": [], "cost_usd": 0.0}

    with patch("agent_loop.cli.run_ticket", side_effect=fake_run_ticket):
        rc = main(["--profile", "test-cf40-probe", "--tickets", str(path),
                   "--reviewers", "rev-a:cloud,other-b:cloud", "--arbiter", "arb-c:cloud",
                   "--implementer", "imp-d:cloud", *argv])
    return rc, ran


def test_a_retired_arbiter_refuses_the_run_before_any_round(probe_on, tmp_path, capsys):
    with patch.object(probe, "chat", _fake_chat(dead={"arb-c:cloud"})):
        rc, ran = _run(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2 and ran == [], "no ticket may run once a configured model is gone"
    assert "PROBE REFUSED: arb-c:cloud (arbiter) is RETIRED" in out


def test_a_retired_extra_reviewer_is_caught_too(probe_on, tmp_path, capsys):
    # CF-37's instance: it was the SECOND reviewer that died.
    with patch.object(probe, "chat", _fake_chat(dead={"other-b:cloud"})):
        rc, ran = _run(tmp_path)
    assert rc == 2 and ran == []
    assert "PROBE REFUSED: other-b:cloud (reviewer)" in capsys.readouterr().out


def test_an_unreachable_model_warns_and_the_run_proceeds(probe_on, tmp_path, capsys):
    # Negative control for the refusal: a transient failure is not retirement.
    with patch.object(probe, "chat", _fake_chat(flaky={"rev-a:cloud"})):
        rc, ran = _run(tmp_path)
    out = capsys.readouterr().out
    assert ran == ["T1"] and rc == 0
    assert "PROBE WARNING: rev-a:cloud (reviewer) did not answer" in out
    assert "PROBE REFUSED" not in out


def test_every_role_is_probed_once_per_distinct_model(probe_on, tmp_path):
    seen = []
    with patch.object(probe, "chat", _fake_chat(seen=seen)):
        rc, ran = _run(tmp_path)
    assert rc == 0 and ran == ["T1"]
    assert {"imp-d:cloud", "rev-a:cloud", "other-b:cloud", "arb-c:cloud"} <= set(seen)
    assert len(seen) == len(set(seen)), f"a model was probed twice: {seen}"


@pytest.mark.parametrize("argv", [("--no-probe",), ("--list",)])
def test_the_opt_outs_spend_no_call(probe_on, tmp_path, argv):
    seen = []
    with patch.object(probe, "chat", _fake_chat(dead={"arb-c:cloud"}, seen=seen)):
        _run(tmp_path, *argv)
    assert seen == [], f"{argv} must not probe; it called {seen}"


def test_the_env_opt_out_spends_no_call(tmp_path):
    # No probe_on fixture: the suite-wide conftest sets AGENT_LOOP_NO_PROBE=1.
    seen = []
    with patch.object(probe, "chat", _fake_chat(dead={"arb-c:cloud"}, seen=seen)):
        rc, ran = _run(tmp_path)
    assert seen == [] and ran == ["T1"]


def test_retirement_is_read_from_the_status_code_not_the_prose():
    assert probe.is_retired("x failed after 1 attempt: HTTPError 410: Gone -- {}")
    assert not probe.is_retired("x failed after 1 attempt: HTTPError 404: Not Found -- retired")
    assert not probe.is_retired("x failed after 3 attempts: HTTPError 503: unavailable")
