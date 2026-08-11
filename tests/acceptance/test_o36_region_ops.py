"""
O36: a ticket must be able to create a file and to ADD to an existing one.

The region model could only ever say "replace these lines". `regions.extract`
refuses a file that does not exist, so a feature -- whose code is not written yet
-- was rejected on every round until max_rounds ran out. And even for a file that
does exist, there was no way to say "add a function here": HANDOVER §6 trap 4
records that limitation biting F1-F6 on DEFECT work, where F5 emitted a mid-file
`import` because its region began at a `def` and it had no legal alternative.

So `op` is per-region, and a single ticket can mix all three:

    replace  (default)  file exists, anchor resolves -> body replaces the span
    create              file must NOT exist          -> body becomes the file
    insert              file exists, anchor resolves -> body is added after it

`op` is deliberately NOT folded into the existing `kind` field. `kind` is the
LOCATOR strategy (decl/indent/line) consumed by `find_region`; the operation is a
different axis, and overloading one field with both would be the ambiguous helper
this repo has been told to avoid.
"""
from pathlib import Path

import pytest

from agent_loop import regions
from agent_loop.profiles import Profile, register


PROFILE = Profile(
    name="test-o36",
    language="python", file_suffixes=(".py",), line_comment="#",
    block_comment=(), block_kind="indent",
    implementer_rules="t", reviewer_priorities="t",
)
register(PROFILE)

EXISTING = (
    "import os\n"
    "\n"
    "\n"
    "def alpha():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def omega():\n"
    "    return 99\n"
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text(EXISTING, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# replace: unchanged
# --------------------------------------------------------------------------
def test_replace_is_the_default_and_behaves_as_before(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/thing.py", "anchor": "def alpha"},
    ], PROFILE)
    assert regs[0].op == "replace"
    regions.apply(regs, {"R1": "def alpha():\n    return 2"})
    assert "return 2" in (repo / "src" / "thing.py").read_text()
    assert "def omega" in (repo / "src" / "thing.py").read_text()


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
def test_create_resolves_against_a_file_that_does_not_exist(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/brand_new.py", "op": "create"},
    ], PROFILE)
    assert regs[0].op == "create"
    assert regs[0].text == "", "a file that does not exist has no existing text"


def test_create_writes_the_whole_file(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/brand_new.py", "op": "create"},
    ], PROFILE)
    touched = regions.apply(regs, {"R1": "def brand():\n    return 7\n"})
    assert "src/brand_new.py" in touched
    assert (repo / "src" / "brand_new.py").read_text().startswith("def brand():")


def test_create_needs_no_anchor(repo):
    """There is nothing to anchor to. Requiring one would make the op unusable."""
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/brand_new.py", "op": "create"},
    ], PROFILE)
    assert regs[0].anchor == ""


def test_create_refuses_a_file_that_already_exists(repo):
    """Otherwise `create` silently truncates real work. Say so instead."""
    with pytest.raises(regions.RegionError) as exc:
        regions.extract(repo, [
            {"id": "R1", "file": "src/thing.py", "op": "create"},
        ], PROFILE)
    assert "already exists" in str(exc.value)


def test_create_makes_missing_parent_directories(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/deep/nested/mod.py", "op": "create"},
    ], PROFILE)
    regions.apply(regs, {"R1": "X = 1\n"})
    assert (repo / "src" / "deep" / "nested" / "mod.py").exists()


def test_create_still_enforces_the_language(repo):
    """A profile that edits .py must not be talked into writing a .cs file."""
    with pytest.raises(regions.RegionError):
        regions.extract(repo, [
            {"id": "R1", "file": "src/thing.cs", "op": "create"},
        ], PROFILE)


# --------------------------------------------------------------------------
# insert
# --------------------------------------------------------------------------
def test_insert_adds_after_the_anchored_block_without_replacing_it(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/thing.py", "op": "insert", "anchor": "def alpha"},
    ], PROFILE)
    assert regs[0].op == "insert"
    regions.apply(regs, {"R1": "\n\ndef beta():\n    return 2"})

    text = (repo / "src" / "thing.py").read_text()
    assert "def alpha():\n    return 1" in text, "the anchored block was replaced, not kept"
    assert "def beta" in text
    assert "def omega" in text, "the rest of the file survived"
    # Order matters: the new code goes after alpha and before omega.
    assert text.index("def alpha") < text.index("def beta") < text.index("def omega")


def test_insert_leaves_the_file_alone_when_the_body_is_empty(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/thing.py", "op": "insert", "anchor": "def alpha"},
    ], PROFILE)
    touched = regions.apply(regs, {"R1": ""})
    assert touched == []
    assert (repo / "src" / "thing.py").read_text() == EXISTING


def test_insert_can_add_a_module_level_name(repo):
    """The §6 trap 4 case, which bit F1-F6 on defect work: a region beginning at a
    `def` had no legal way to add a module-level import, so F5 emitted one
    mid-file. Anchoring the insert at the import block fixes that."""
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/thing.py", "op": "insert", "anchor": "import os", "kind": "line"},
    ], PROFILE)
    regions.apply(regs, {"R1": "\nimport json"})
    text = (repo / "src" / "thing.py").read_text()
    assert text.index("import json") < text.index("def alpha")


# --------------------------------------------------------------------------
# a feature is usually all three at once
# --------------------------------------------------------------------------
def test_one_ticket_can_create_insert_and_replace(repo):
    """The requirement: a new feature is new files AND additions to existing
    ones, in one unit of work."""
    regs = regions.extract(repo, [
        {"id": "NEW", "file": "src/feature.py", "op": "create"},
        {"id": "HOOK", "file": "src/thing.py", "op": "insert", "anchor": "import os", "kind": "line"},
        {"id": "EDIT", "file": "src/thing.py", "op": "replace", "anchor": "def omega"},
    ], PROFILE)
    touched = regions.apply(regs, {
        "NEW": "def feature():\n    return 'new'\n",
        "HOOK": "\nfrom .feature import feature",
        "EDIT": "def omega():\n    return feature()",
    })

    assert set(touched) == {"src/feature.py", "src/thing.py"}
    assert (repo / "src" / "feature.py").exists()
    text = (repo / "src" / "thing.py").read_text()
    assert "from .feature import feature" in text
    assert "return feature()" in text
    assert "def alpha():\n    return 1" in text, "the untouched function changed"


def test_an_unknown_op_is_refused_by_name(repo):
    """A typo must not silently fall back to replace and overwrite a file."""
    with pytest.raises(regions.RegionError) as exc:
        regions.extract(repo, [
            {"id": "R1", "file": "src/thing.py", "op": "raplace", "anchor": "def alpha"},
        ], PROFILE)
    assert "raplace" in str(exc.value)


def test_line_endings_survive_an_insert(tmp_path):
    """CRLF sources must not be rewritten wholesale -- the defect that made a
    two-line patch a whole-file diff."""
    (tmp_path / "src").mkdir()
    p = tmp_path / "src" / "crlf.py"
    p.write_bytes(b"import os\r\n\r\n\r\ndef alpha():\r\n    return 1\r\n")
    regs = regions.extract(tmp_path, [
        {"id": "R1", "file": "src/crlf.py", "op": "insert", "anchor": "import os", "kind": "line"},
    ], PROFILE)
    regions.apply(regs, {"R1": "\nimport json"})
    raw = p.read_bytes()
    assert b"\r\n" in raw
    assert b"\n\n" not in raw.replace(b"\r\n", b"@"), "a bare LF was introduced"


def test_created_files_use_the_platform_default_but_stay_consistent(repo):
    regs = regions.extract(repo, [
        {"id": "R1", "file": "src/made.py", "op": "create"},
    ], PROFILE)
    regions.apply(regs, {"R1": "a = 1\nb = 2\n"})
    raw = (repo / "src" / "made.py").read_bytes()
    assert raw.count(b"\n") >= 2
    # One terminator style throughout, whichever it is.
    assert raw.count(b"\r\n") in (0, raw.count(b"\n"))


# --------------------------------------------------------------------------
# a created file must reach the patch, or the fix ships without its own code
# --------------------------------------------------------------------------
#
# `Workspace.diff()` is `git diff`, which does not show untracked files. The red
# phase already learned this once: a new test file was invisible to the diff, so
# the exported patch carried the fix WITHOUT the test that proved it. A created
# source file has the same problem and the consequence is worse -- the patch would
# reference a module that the patch itself does not add.
def _git_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "thing.py").write_text(EXISTING, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


def test_a_created_file_appears_in_the_exported_patch(tmp_path):
    from agent_loop import workspace

    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "O36CREATE") as ws:
        regs = regions.extract(ws.root, [
            {"id": "NEW", "file": "src/feature.py", "op": "create"},
        ], PROFILE)
        regions.apply(regs, {"NEW": "def feature():\n    return 'new'\n"})
        ws.stage_new_files([r.file for r in regs if r.op == regions.CREATE])

        patch = ws.diff()
        assert "src/feature.py" in patch, f"the created file is missing from the patch:\n{patch}"
        assert "def feature" in patch


def test_promote_carries_a_created_file_into_the_live_repo(tmp_path):
    from agent_loop import workspace

    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "O36PROMOTE") as ws:
        regs = regions.extract(ws.root, [
            {"id": "NEW", "file": "src/feature.py", "op": "create"},
        ], PROFILE)
        regions.apply(regs, {"NEW": "def feature():\n    return 'new'\n"})
        ws.stage_new_files([r.file for r in regs if r.op == regions.CREATE])
        ws.promote(["src/feature.py"])

    assert (repo / "src" / "feature.py").exists(), "promote did not create the file"
    assert "def feature" in (repo / "src" / "feature.py").read_text()


def test_stage_new_files_ignores_a_path_that_was_not_created(tmp_path):
    from agent_loop import workspace

    repo = _git_repo(tmp_path)
    with workspace.open_workspace(repo, "O36MISSING") as ws:
        # Must not raise: a create region whose body the model never emitted
        # leaves no file behind, and that is a no-op, not an error.
        ws.stage_new_files(["src/never_written.py"])


# --------------------------------------------------------------------------
# the implementer has to be TOLD it is authoring a new file
# --------------------------------------------------------------------------
def test_a_create_region_does_not_report_a_nonsense_line_range():
    """start=0/end=-1 renders as "1-0", which would go into the implement prompt
    and into `--list`. A range is meaningless for a file that does not exist."""
    r = regions.Region(
        id="NEW", file="src/new.py", path=Path("src/new.py"), anchor="",
        kind="indent", start_line=0, end_line=-1, text="", op=regions.CREATE,
    )
    assert r.lines_1based == "new file", r.lines_1based


def test_the_implement_prompt_says_new_file_and_asks_for_whole_content(repo):
    from agent_loop.loop import build_implement_prompt

    regs = regions.extract(repo, [
        {"id": "NEW", "file": "src/feature.py", "op": "create", "note": "the new module"},
    ], PROFILE)
    prompt = build_implement_prompt(
        {"id": "T1", "title": "t", "defect": "d", "spec": "s"}, regs, PROFILE,
    )
    assert "new file" in prompt.lower()
    assert "1-0" not in prompt
    # It must be unambiguous that the whole file is wanted, not a fragment: the
    # region body IS the file.
    assert "entire" in prompt.lower() or "whole" in prompt.lower()


def test_the_implement_prompt_marks_an_insert_as_additive(repo):
    """An insert region shows the anchored block as context, and the model must
    not re-emit it -- doing so would duplicate the function it anchored to."""
    from agent_loop.loop import build_implement_prompt

    regs = regions.extract(repo, [
        {"id": "HOOK", "file": "src/thing.py", "op": "insert", "anchor": "def alpha"},
    ], PROFILE)
    prompt = build_implement_prompt(
        {"id": "T1", "title": "t", "defect": "d", "spec": "s"}, regs, PROFILE,
    )
    assert "insert" in prompt.lower() or "add after" in prompt.lower()
    assert "do not" in prompt.lower(), "must say not to re-emit the anchored block"


# --------------------------------------------------------------------------
# O34's discriminator is WRONG for a feature, and this is where they meet
# --------------------------------------------------------------------------
#
# O34 refuses a red test that never reached an assertion, on the grounds that it
# died in its own scaffolding. For a FEATURE that reasoning inverts: the natural
# red test imports a module that does not exist yet, so it fails with ImportError
# or AttributeError -- and that IS the defect being demonstrated. Without this
# exception the gate added three commits before O36 would refuse every feature on
# arrival.
NOT_YET_KINDS = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")


@pytest.mark.parametrize("kind", NOT_YET_KINDS)
def test_a_missing_name_is_legitimate_red_for_a_feature(kind):
    from agent_loop import gates

    assert gates.reached_an_assertion({kind}) is False, "unchanged for a defect fix"
    assert gates.reached_an_assertion({kind}, feature=True) is True, (
        f"{kind} is exactly what a feature's red test raises before the code exists"
    )


def test_a_feature_still_cannot_pass_off_an_unrelated_crash_as_red():
    """The exception is scoped to names that do not exist yet. A TypeError inside
    a stub is a broken test whether the work is a feature or a fix."""
    from agent_loop import gates

    assert gates.reached_an_assertion({"TypeError"}, feature=True) is False
    assert gates.reached_an_assertion({"ZeroDivisionError"}, feature=True) is False


def test_an_assertion_is_still_the_best_evidence_either_way():
    from agent_loop import gates

    assert gates.reached_an_assertion({"AssertionError"}, feature=True) is True
    assert gates.reached_an_assertion(set(), feature=True) is None


def test_a_ticket_that_creates_a_file_is_a_feature_ticket():
    """The caller should not have to be told twice. A ticket carrying a `create`
    region is authoring code that does not exist, which is the whole condition
    the exception turns on."""
    from agent_loop import gates

    assert gates.is_feature_ticket({"regions": [{"id": "R1", "op": "create", "file": "a.py"}]})
    assert not gates.is_feature_ticket({"regions": [{"id": "R1", "file": "a.py"}]})
    assert not gates.is_feature_ticket({})
    assert gates.is_feature_ticket({"kind": "feature"}), "an explicit marker also counts"
