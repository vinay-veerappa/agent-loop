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


# --- anchor NOT FOUND: the feedback must name real lines (O39) --------------
#
# Both live feature runs died here. The plan model supplies exact-match anchors
# into files it has never been shown, so it anchors from memory: five rounds
# hunting `LoadCopierConfig` in a file whose method is `LoadFromDisk`, then two
# more inventing a parameter name. Re-prompting cannot fix a guess about text
# the model cannot see, so the failure now carries the candidates.

FILE = [
    "namespace Copier\n",
    "{\n",
    "    public class Engine\n",
    "    {\n",
    "        public void LoadFromDisk(string filePath)\n",
    "        {\n",
    "            return;\n",
    "        }\n",
    "        public string TranslateSymbol(string rawSymbol, CopierRelationship rel = null)\n",
    "        {\n",
    "            string translated = TranslateSymbol(leaderInstrument.FullName, rel);\n",
    "        }\n",
    "    }\n",
    "}\n",
]


_CAND_HEADER = "or on a unique substring of one: "


def _candidates(msg):
    """The offered lines, as (label, text) -- the header itself contains ': '.

    Asserts the message is the NOT-FOUND variant first. Twice while writing
    these tests I picked an anchor that was a literal substring of the fixture,
    got `anchor not unique` instead, and read an IndexError here as a code bug.
    """
    assert "anchor not found" in msg, (
        f"expected a not-found failure with candidates, got: {msg[:160]}"
    )
    body = msg.split(_CAND_HEADER, 1)[1]
    out = []
    for cand in body.split("; "):
        label, _, text = cand.partition(": ")
        out.append((label.strip(), text))
    return out


def test_missing_anchor_names_the_real_declaration():
    msg = _error_for(FILE, anchor="private void LoadCopierConfig")
    assert "anchor not found" in msg
    assert "LoadFromDisk(string filePath)" in msg, "the real method was not offered"
    assert "L5:" in msg


def test_the_exact_real_line_is_offered_first():
    # The live round-10 failure: the model completed a truncated preview as
    # `FullName, relationship)` where the file says `FullName, rel)`.
    msg = _error_for(
        FILE, anchor="string translated = TranslateSymbol(leaderInstrument.FullName, relationship)"
    )
    label, text = _candidates(msg)[0]
    assert label == "L11"
    assert text == "string translated = TranslateSymbol(leaderInstrument.FullName, rel);"


def test_every_candidate_is_a_real_line_from_the_file():
    """The invariant that matters: feedback may not invent text."""
    msg = _error_for(FILE, anchor="public string TranslateSymbol(string rawSymbol, X relationship)")
    source = [ln.strip() for ln in FILE]
    for label, text in _candidates(msg):
        text = text.split(" ...[TRUNCATED")[0]
        assert any(text in s for s in source), f"offered a line that is not in the file: {text!r}"


def test_truncated_candidates_are_marked_as_not_copyable():
    # Long AND similar: SequenceMatcher penalises a big length difference, so a
    # long line that merely shares a short prefix falls under the floor and is
    # never offered at all -- it has to be a near-copy to reach the preview.
    args = ", ".join(f"int arg{i}" for i in range(20))
    long_line = f"        public void Configure({args})\n"
    msg = _error_for([long_line, "        public void Other(int y)\n"],
                     anchor=f"public void Configurex({args})")
    assert "TRUNCATED" in msg
    assert "not a copyable anchor" in msg
    # And it is still a real prefix of the real line.
    text = _candidates(msg)[0][1].split(" ...[TRUNCATED")[0]
    assert text in long_line.strip()


def test_no_plausible_candidate_leaves_the_bare_message():
    # Nothing in this file resembles the anchor; offering noise would be worse
    # than offering nothing.
    msg = _error_for(["x = 1\n", "y = 2\n"], anchor="zzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert msg == "anchor not found: 'zzzzzzzzzzzzzzzzzzzzzzzzzzzz'"


def test_regex_anchors_also_get_candidates():
    msg = _error_for(FILE, anchor="re:private void LoadCopierConfig")
    assert "LoadFromDisk" in msg, "an re: anchor that matches nothing needs the same help"


def test_a_near_miss_spelling_is_offered_alongside_the_real_line():
    """Ranking is pure similarity, and both plausible lines must be visible.

    Deliberately NOT asserting which of these comes first. A near-identical
    misspelling scoring above a longer real signature is reasonable -- the
    caller sees both and picks. An earlier version of this test asserted an
    ordering that only an untested weight produced, which is how a heuristic
    becomes a requirement by accident.
    """
    lines = [
        "        public void Foa(int a)\n",
        "        public void Foo(int a, int b, int c, int d)\n",
    ]
    msg = _error_for(lines, anchor="public void Foo(int a)")
    labels = [label for label, _ in _candidates(msg)]
    assert labels == ["L1", "L2"] or labels == ["L2", "L1"]


def test_lone_punctuation_is_never_offered():
    lines = [
        "{\n",
        "}\n",
        "};\n",
        "        public void Configure(int x)\n",
    ]
    msg = _error_for(lines, anchor="public void Configuration(int x)")
    for label, text in _candidates(msg):
        assert text not in ("{", "}", "};"), f"offered lone punctuation: {text!r}"


def test_the_similarity_floor_is_pinned_at_its_boundary():
    """The floor is now the ONLY noise filter, so its value must be pinned.

    Two mutations survived before this test existed -- weakening the floor to
    `> 0.0` and removing a redundant length guard -- because every other test
    happened to use lines scoring either ~1.0 or exactly 0.0. A threshold no
    test approaches is a threshold that can be changed to anything.
    """
    import difflib

    # None of these lines may CONTAIN the anchor, or the failure is "not unique".
    anchor = "alphax"
    weak = "zzzzzalphzzzzzzzzzzzzzzzzzzzzzzzz"
    strong = "alphaz"

    # Pin the premise: one line straddles the floor from below, one from above.
    r_weak = difflib.SequenceMatcher(None, anchor, weak).ratio()
    r_strong = difflib.SequenceMatcher(None, anchor, strong).ratio()
    assert 0.0 < r_weak < 0.3 < r_strong, f"premise broken: {r_weak} {r_strong}"

    msg = _error_for([f"    {weak}\n", f"    {strong}\n"], anchor=anchor)
    offered = [text for _, text in _candidates(msg)]
    assert strong in offered
    assert weak not in offered, "a line below the floor was offered as a candidate"


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
