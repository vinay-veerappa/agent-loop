"""An arbiter-shipped FEATURE plan must keep all of its parts, and be reviewed whole.

Found on the first feature plan that ever reached the arbiter. The run printed

    round 3: plan: 4 part(s), regions check OK
    [arbiter] SHIP (upheld=0 rejected=18 out-of-scope=0)
    plan: logs/agent_loop/PLAN/plan.json (1 part(s))

-- four parts validated, one part written, and nothing in the output said three
had been discarded. The SHIP branch assigned the bare first ticket while the
panel-approve and fast-plan branches both assigned `tickets if feature else
ticket`, so every arbiter-shipped feature plan since `--feature` existed was
silently truncated to its first part.

The same seam showed the panel only ever seeing part 1, which hides exactly what
is worth reviewing in a decomposed plan: whether the parts compose.
"""
from __future__ import annotations

import json

import pytest

from agent_loop import plan_mode
from agent_loop.profiles import Profile

PROFILE = Profile(
    name="stub", language="csharp", file_suffixes=(".cs",), line_comment="//",
    block_comment=(), block_kind="decl", preprocessor_directives=(),
    build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
    test_sources=("tests/*.cs",),
)


def _parts(n):
    return [
        {
            "id": f"F{i}",
            "title": f"part {i}",
            "regions": [{"id": "R1", "file": f"f{i}.cs", "op": "create"}],
            "expect_green": [f"tests/t{i}.cs"],
        }
        for i in range(1, n + 1)
    ]


def _raw(parts):
    return "\n".join(
        "<<<TICKET>>>\n" + json.dumps(p) + "\n<<<END TICKET>>>" for p in parts
    )


class _Out:
    def __init__(self, text):
        self.text = text

    def usage_line(self):
        return "stub 0.0s"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A run where validation passes, the panel rejects, and the arbiter ships."""
    parts = _parts(4)
    captured = {}

    monkeypatch.setattr(plan_mode, "chat", lambda *a, **k: _Out(_raw(parts)))
    monkeypatch.setattr(plan_mode, "_validate_feature_plan", lambda *a, **k: "")

    class _Vote:
        counted = True
        status = "REJECT"
        blockers = 7
        model = "stub-reviewer:cloud"
        finding_list = []

    class _Panel:
        votes = [_Vote()]
        verdict = "REJECT"
        unanimous_approve = False
        valid = True
        findings = "some findings"

    def _panel(reviewers, prompt, *a, **k):
        captured["prompt"] = prompt
        return _Panel()

    class _Adj:
        ok = True
        recommendation = "SHIP"
        raw = "ruling"
        error = ""

        def summary(self):
            return "SHIP (upheld=0 rejected=18)"

    import agent_loop.loop as loop_mod
    monkeypatch.setattr(loop_mod, "review_panel", _panel)
    monkeypatch.setattr(plan_mode.arbiter_mod, "adjudicate", lambda *a, **k: _Adj())
    monkeypatch.setattr(plan_mode.arbiter_mod, "SHIP", "SHIP")
    monkeypatch.setattr(plan_mode.arbiter_mod, "ESCALATE", "ESCALATE")

    return parts, captured, tmp_path


def test_the_panel_is_shown_where_every_region_resolved(tmp_path, monkeypatch):
    """The real validator, because the stub cannot populate the notes.

    Both reviewers on the live run blocked with "the Resolved regions block in
    this review request is empty" -- true: the feature branch set `regs = []` and
    rendered an empty section, so a reviewer asked to judge regions was shown
    none.
    """
    src = tmp_path / "Engine.cs"
    src.write_text(
        "namespace X\n{\n    public class Engine\n    {\n"
        "        public void Existing()\n        {\n        }\n    }\n}\n",
        encoding="utf-8",
    )

    parts = [
        {
            "id": "F1",
            "title": "types",
            "regions": [
                {"id": "R1", "file": "Types.cs", "op": "create"},
                {"id": "R2", "file": "Engine.cs", "op": "insert",
                 "anchor": "public void Existing()"},
            ],
            "expect_green": ["tests/t1.cs"],
        },
        {
            "id": "F2",
            "title": "consumer",
            "regions": [
                {"id": "R1", "file": "Types.cs", "op": "insert", "anchor": "class Types"},
            ],
            "expect_green": ["tests/t2.cs"],
        },
    ]

    captured = {}

    monkeypatch.setattr(plan_mode, "chat", lambda *a, **k: _Out(_raw(parts)))

    class _Panel:
        votes = []
        verdict = "APPROVE"
        unanimous_approve = True
        valid = True
        findings = ""

    def _panel(reviewers, prompt, *a, **k):
        captured["prompt"] = prompt
        return _Panel()

    import agent_loop.loop as loop_mod
    monkeypatch.setattr(loop_mod, "review_panel", _panel)

    plan_mode.run_plan(
        tmp_path, "a feature", PROFILE, "m", ["r"], arbiter_model="",
        max_rounds=1, feature=True,
    )

    prompt = captured["prompt"]
    assert "Resolved regions" in prompt
    assert "F1/R1: Types.cs (new file)" in prompt
    assert "F1/R2: Engine.cs lines" in prompt, "a real anchor's line range is missing"
    # The NOTE itself, not the sentence that explains what "deferred" means --
    # a bare `"deferred" in prompt` was satisfied by that prose and stayed green
    # when the note was removed.
    assert "F2/R1: Types.cs (insert, anchor deferred" in prompt, (
        "a region inside a file an earlier part creates must be listed as deferred"
    )
    assert "- (none)" not in prompt


def test_arbiter_ship_keeps_every_part(wired):
    parts, _, tmp_path = wired
    result = plan_mode.run_plan(
        tmp_path, "a feature", PROFILE, "m", ["r"], arbiter_model="arb",
        max_rounds=1, feature=True,
    )
    assert result["verdict"] == "ARBITER_SHIP"
    assert isinstance(result["plan"], list), "a feature plan must stay a list of parts"
    assert [t["id"] for t in result["plan"]] == ["F1", "F2", "F3", "F4"]


def test_arbiter_ship_writes_every_part_to_plan_json(wired):
    parts, _, tmp_path = wired
    plan_mode.run_plan(
        tmp_path, "a feature", PROFILE, "m", ["r"], arbiter_model="arb",
        max_rounds=1, feature=True,
    )
    payload = json.loads(
        (tmp_path / "logs" / "agent_loop" / "PLAN" / "plan.json").read_text(encoding="utf-8")
    )
    assert len(payload["tickets"]) == 4


def test_a_defect_plan_still_ships_a_single_bare_ticket(wired):
    """The non-feature shape must not change: one ticket, not a list."""
    _, _, tmp_path = wired
    result = plan_mode.run_plan(
        tmp_path, "a defect", PROFILE, "m", ["r"], arbiter_model="arb",
        max_rounds=1, feature=False,
    )
    assert result["verdict"] == "ARBITER_SHIP"
    assert isinstance(result["plan"], dict)
    assert result["plan"]["id"] == "F1"


def test_the_panel_sees_every_part_of_a_feature_plan(wired):
    parts, captured, tmp_path = wired
    plan_mode.run_plan(
        tmp_path, "a feature", PROFILE, "m", ["r"], arbiter_model="arb",
        max_rounds=1, feature=True,
    )
    prompt = captured["prompt"]
    for pid in ("F1", "F2", "F3", "F4"):
        assert pid in prompt, f"the panel never saw {pid}"
    assert "4 ORDERED parts" in prompt
    assert "compose" in prompt, "nothing asked the panel about composition"


def test_a_rejection_from_an_earlier_round_does_not_survive_success(wired, monkeypatch):
    """result.json used to report ARBITER_SHIP and a stale anchor error together.

    This has to REACH the failure first. An earlier version ran a single round in
    which validation never failed, so `error` was never set and the assertion was
    satisfied by a run that could not have exhibited the bug -- green either way.
    """
    _, _, tmp_path = wired

    calls = {"n": 0}

    def _validate(*a, **k):
        calls["n"] += 1
        return "anchor not unique (3 hits): 'public string FollowerAccountName'" if calls["n"] == 1 else ""

    monkeypatch.setattr(plan_mode, "_validate_feature_plan", _validate)

    result = plan_mode.run_plan(
        tmp_path, "a feature", PROFILE, "m", ["r"], arbiter_model="arb",
        max_rounds=3, feature=True,
    )
    assert calls["n"] >= 2, "the first round must have been rejected for this to test anything"
    assert result["verdict"] == "ARBITER_SHIP"
    assert not result.get("error"), f"stale error kept on a successful run: {result.get('error')!r}"

    written = json.loads(
        (tmp_path / "logs" / "agent_loop" / "PLAN" / "result.json").read_text(encoding="utf-8")
    )
    assert not written.get("error"), "result.json reports a success verdict and an error together"
