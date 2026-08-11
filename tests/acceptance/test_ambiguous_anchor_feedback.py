"""Ambiguous-anchor feedback must carry enough information to act on.

The live failure this covers: a feature plan against a C# file with two
`CalculateFollowerQuantity` overloads was rejected with `anchor not unique
(2 hits)`, and the preview truncated both hits at 60 characters -- where the
two signatures are still byte-identical. The model was told the hits differed
while being shown the same string twice, so it lengthened the anchor with an
INVENTED parameter list, turning a recoverable ambiguity into `anchor not
found` and burning four of six rounds.

Same class as the lint-digest defect: feedback the model cannot act on is a
gate that only looks like one.
"""
from __future__ import annotations

import json

import pytest

from agent_loop import regions
from agent_loop.profiles import Profile

# The real pair, at the real indentation, from TradeCopierEngine.cs.
OVERLOADS = [
    "        public int CalculateFollowerQuantity(CopierRelationship rel, int leaderQty, string rawSymbol, int currentFollowerPosition, bool isExit, out bool isClamped)\n",
    "        {\n",
    "            return 0;\n",
    "        }\n",
    "        public int CalculateFollowerQuantity(CopierRelationship rel, int leaderQty, string rawSymbol, bool isExit = false)\n",
    "        {\n",
    "            return 0;\n",
    "        }\n",
]

ANCHOR = "public int CalculateFollowerQuantity(CopierRelationship rel,"


def _error_for(lines, anchor=ANCHOR):
    with pytest.raises(regions.RegionError) as exc:
        regions.find_region(lines, anchor, kind="decl")
    return str(exc.value)


def test_ambiguous_previews_are_distinguishable():
    msg = _error_for(OVERLOADS)
    assert "anchor not unique (2 hits)" in msg

    # The whole point: the two rendered hits must not be the same string.
    body = msg.split("-> ", 1)[1]
    rendered = [p.strip() for p in body.split(";")]
    hits = [p for p in rendered if p.startswith("L")]
    assert len(hits) == 2, f"expected two rendered hits, got {rendered}"

    # Compare the CODE TEXT, not the whole rendered hit. The "L1: "/"L5: "
    # prefixes make any two hits differ, so asserting on the full string is
    # satisfied by a preview whose code halves are still identical -- it stayed
    # green under a mutation that reverted the truncation to a fixed 60 chars.
    texts = [h.split(": ", 1)[1] for h in hits]
    assert texts[0] != texts[1], f"both hits show the same code text: {texts[0]!r}"


def test_preview_shows_the_text_that_actually_differs():
    msg = _error_for(OVERLOADS)
    # `out bool isClamped` vs `bool isExit = false` is the distinguishing tail;
    # at least the point of divergence must be visible in each hit.
    assert "currentFollowerPosition" in msg
    assert "isExit = false" in msg


def test_preview_carries_line_numbers():
    msg = _error_for(OVERLOADS)
    assert "L1:" in msg
    assert "L5:" in msg


def test_truly_identical_lines_say_so_instead_of_repeating_themselves():
    lines = [
        "        foo(bar);\n",
        "        baz();\n",
        "        foo(bar);\n",
    ]
    msg = _error_for(lines, anchor="foo(bar);")
    assert "IDENTICAL" in msg
    assert "re:" in msg, "must point at the only mechanism that CAN disambiguate"


def test_more_than_four_hits_reports_the_remainder():
    lines = [f"    call_{i}(x);  # same anchor here\n" for i in range(7)]
    msg = _error_for(lines, anchor="same anchor here")
    assert "anchor not unique (7 hits)" in msg
    assert "and 3 more" in msg


def test_unique_anchor_still_resolves():
    # The fix must not change the success path.
    start, end = regions.find_region(OVERLOADS, "out bool isClamped", kind="decl")
    assert (start, end) == (0, 3)


def test_short_distinct_lines_are_not_padded_pointlessly():
    lines = ["    alpha(1);\n", "    alpha(2);\n"]
    msg = _error_for(lines, anchor="alpha(")
    assert "alpha(1);" in msg and "alpha(2);" in msg
    assert "IDENTICAL" not in msg


def test_rejected_feature_plan_is_persisted(tmp_path, monkeypatch):
    """A plan that parses but never validates must survive the run."""
    from agent_loop import plan_mode

    # run_plan writes to <repo>/logs/agent_loop/PLAN; use the real path rather
    # than stubbing it, so the test also pins WHERE the file lands.
    art = tmp_path / "logs" / "agent_loop" / "PLAN"

    ticket = {
        "id": "F1",
        "title": "part one",
        "regions": [{"id": "R1", "file": "x.cs", "op": "insert", "anchor": "nope"}],
        "expect_green": ["tests/x.cs"],
    }
    raw = "<<<TICKET>>>\n" + json.dumps(ticket) + "\n<<<END TICKET>>>"

    class _Out:
        text = raw

        def usage_line(self):
            return "stub 0.0s"

    monkeypatch.setattr(plan_mode, "chat", lambda *a, **k: _Out())
    monkeypatch.setattr(plan_mode, "_validate_feature_plan", lambda *a, **k: "anchor not found: 'nope'")

    profile = Profile(
        name="stub", language="csharp", file_suffixes=(".cs",), line_comment="//",
        block_comment=(), block_kind="decl", preprocessor_directives=(),
        build_cmd="true", test_cmd="true", lock_name="", risk_calls=(),
        test_sources=("tests/*.cs",),
    )

    result = plan_mode.run_plan(
        tmp_path, "a feature", profile, "stub-model", [], arbiter_model="",
        max_rounds=2, feature=True,
    )

    assert result["verdict"] == "MAX_ROUNDS_EXHAUSTED"
    assert result["plan"] is None
    rejected = art / "plan_rejected.json"
    assert rejected.exists(), "the parsed-but-rejected plan was discarded"

    # Same wrapper shape as plan.json, so it is usable via --tickets once fixed.
    payload = json.loads(rejected.read_text(encoding="utf-8"))
    assert list(payload.keys()) == ["tickets"]
    assert payload["tickets"][0]["id"] == "F1"
    assert not (art / "plan.json").exists(), "a rejected plan must not land as plan.json"
