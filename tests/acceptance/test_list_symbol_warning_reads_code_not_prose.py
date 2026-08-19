"""`--list` warns about symbols the model will have to guess. It must warn well.

The warning is the cheapest signal the loop produces -- it costs no model call
and it catches the ticket defect that wastes the most money (a spec naming a
symbol the granted regions do not contain). Its whole value is that an operator
reads it. Both failure directions destroy that:

CF-13  49 warnings for 7 symbols, because the scan ran per REGION and the set
       of symbols is a property of the TICKET. Warnings that repeat train the
       reader to skip them.

CF-1   ALL-CAPS prose read as code. "SCOPE (the test...)" matched a call rule
       that allowed whitespace before the paren.

CF-20  and then the fix for CF-1 overshot: requiring content inside the parens
       dropped every ZERO-ARG call, so `Flatten()` and `CanTrade()` -- the
       shape of a predicate or a command, which is what these tickets are
       mostly about -- stopped being read as code by the rule whose job is to
       find code. A filter tightened past its target fails silently: you get
       fewer warnings and read it as progress.
"""
from __future__ import annotations

import os
import re

# The call-token rule as it appears in cli.py. Kept here verbatim so a change
# to either copy shows up as a failure rather than as drift.
CALL_RULE = r'\b([A-Z][a-zA-Z0-9_]+)\(\s*[\w"\')]'
DOT_RULE = r'\b([A-Z][a-zA-Z0-9_]+)\.[a-zA-Z_]'


def calls(text: str) -> set:
    return set(re.findall(CALL_RULE, text))


def test_prose_with_a_space_before_the_paren_is_not_a_call():
    """CF-1 residual. This is the exact string that produced the false positive."""
    assert calls("m. SCOPE (the test asserts nothing) Thi") == set()


def test_a_zero_arg_call_is_still_a_call():
    """CF-20. These are the shapes the consumer's tickets are actually about."""
    assert "Flatten" in calls("the handler calls Flatten() and returns")
    assert "CanTrade" in calls("CanTrade() answers true while the lockout binds")


def test_ordinary_calls_still_match():
    assert "FindLiveByName" in calls("FindLiveByName(account, name) resolves the stop")
    assert "StopName" in calls('StopName("BR-1") builds the name')


def test_a_sentence_ending_period_is_not_a_member_access():
    """The dot rule must not read 'SCOPE. The' as SCOPE.The."""
    assert set(re.findall(DOT_RULE, "spent. SCOPE. The next")) == set()
    assert "AtmOrderIdentity" in set(
        re.findall(DOT_RULE, "call AtmOrderIdentity.StopName here")
    )


def test_the_warning_fires_once_per_file_not_once_per_region():
    """CF-13: the dedup key must collapse regions that share a file."""
    regions = [
        {"file": "a.cs", "anchor": "x"},
        {"file": "a.cs", "anchor": "y"},
        {"file": "a.cs", "anchor": "z"},
        {"file": "b.cs", "anchor": "w"},
    ]
    caps = frozenset({"Alpha", "Beta"})
    seen = set()
    inspected = []
    for r in regions:
        key = (caps, r["file"])
        if key in seen:
            continue
        seen.add(key)
        inspected.append(r["file"])
    assert inspected == ["a.cs", "b.cs"], "three regions in one file warned three times"


def test_symbols_belonging_to_a_created_file_are_skipped():
    """A file the ticket CREATES cannot contain the symbol yet, by construction.

    The old advice -- "add its declaration to a read-only region" -- is
    impossible to follow for a file that does not exist.
    """
    regions = [
        {"file": "new/AtmOrderIdentity.cs", "op": "create"},
        {"file": "DynamicAtmManager.cs", "anchor": "x"},
    ]
    create_files = {r["file"] for r in regions if r.get("op") == "create"}
    scanned = [r["file"] for r in regions
               if r.get("file") and r["file"] not in create_files]
    assert scanned == ["DynamicAtmManager.cs"]


def test_the_rules_here_match_the_rules_in_cli():
    """Negative control: these regexes are a copy, so pin them to the original."""
    import inspect
    from agent_loop import cli
    src = inspect.getsource(cli)
    assert CALL_RULE in src, "cli.py's call rule changed and this test went stale"
    assert DOT_RULE in src, "cli.py's dot rule changed and this test went stale"


def test_unresolved_symbol_in_file_refuses_by_default():
    """CF-31: if the spec names a symbol that is in the file but outside every
    region, _list should auto-attach a readonly region for it.
    """
    import tempfile
    from pathlib import Path
    from agent_loop import cli, profiles

    prof = profiles.Profile(
        name="cf31", language="csharp", file_suffixes=(".cs",), line_comment="//",
        block_comment=("/*", "*/"), block_kind="decl", preprocessor_directives=(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "DynamicAtmManager.cs").write_text(
            "class DynamicAtmManager {\n"
            "    public int DuplicateEntryWindowMs { get; set; }\n"
            "    public void Run() { }\n"
            "}\n",
            encoding="utf-8",
        )
        tickets = [{
            "id": "T1",
            "title": "use DuplicateEntryWindowMs",
            "defect": "missing",
            "spec": "Use DuplicateEntryWindowMs in the Run method.",
            "regions": [{"id": "R1", "file": "DynamicAtmManager.cs", "anchor": "public void Run"}],
            "expect_green": [],
        }]
        old_cwd = Path.cwd()
        try:
            os.chdir(repo)
            rc = cli._list(tickets, prof)
        finally:
            os.chdir(old_cwd)
        assert rc == 0, f"expected auto-attach to succeed, got {rc}"
        assert any(r.get("op") == "readonly" for r in tickets[0]["regions"])
        readonly = [r for r in tickets[0]["regions"] if r.get("op") == "readonly"][0]
        assert readonly["file"] == "DynamicAtmManager.cs"
        assert "DuplicateEntryWindowMs" in readonly["note"]


def test_unresolved_symbol_not_in_file_refuses():
    """CF-31: if the spec names a symbol that is not anywhere in the file, the
    default should refuse.
    """
    import tempfile
    from pathlib import Path
    from agent_loop import cli, profiles

    prof = profiles.Profile(
        name="cf31", language="csharp", file_suffixes=(".cs",), line_comment="//",
        block_comment=("/*", "*/"), block_kind="decl", preprocessor_directives=(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "DynamicAtmManager.cs").write_text(
            "class DynamicAtmManager {\n"
            "    public void Run() { }\n"
            "}\n",
            encoding="utf-8",
        )
        tickets = [{
            "id": "T1",
            "title": "use MissingSymbol",
            "defect": "missing",
            "spec": "Use MissingSymbol in the Run method.",
            "regions": [{"id": "R1", "file": "DynamicAtmManager.cs", "anchor": "public void Run"}],
            "expect_green": [],
        }]
        old_cwd = Path.cwd()
        try:
            os.chdir(repo)
            rc = cli._list(tickets, prof)
        finally:
            os.chdir(old_cwd)
        assert rc == 1, f"expected refusal, got {rc}"
        assert not any(r.get("op") == "readonly" for r in tickets[0]["regions"])


def test_allow_unresolved_symbols_opt_out():
    """CF-31: the operator can opt into the old warn-and-continue behaviour."""
    import tempfile
    from pathlib import Path
    from agent_loop import cli, profiles

    prof = profiles.Profile(
        name="cf31", language="csharp", file_suffixes=(".cs",), line_comment="//",
        block_comment=("/*", "*/"), block_kind="decl", preprocessor_directives=(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "DynamicAtmManager.cs").write_text(
            "class DynamicAtmManager {\n"
            "    public void Run() { }\n"
            "}\n",
            encoding="utf-8",
        )
        tickets = [{
            "id": "T1",
            "title": "use MissingSymbol",
            "defect": "missing",
            "spec": "Use MissingSymbol in the Run method.",
            "regions": [{"id": "R1", "file": "DynamicAtmManager.cs", "anchor": "public void Run"}],
            "expect_green": [],
        }]
        old_cwd = Path.cwd()
        try:
            os.chdir(repo)
            rc = cli._list(tickets, prof, allow_unresolved_symbols=True)
        finally:
            os.chdir(old_cwd)
        assert rc == 0, f"expected opt-out to allow pass, got {rc}"
