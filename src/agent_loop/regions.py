"""
regions.py
==========
Locate an editable source region by declaration, not by line number.

Language-agnostic: the file suffixes and comment syntax come from the
Profile, not hardcoded. The brace-matching logic is the same -- it works
for C#, TypeScript, Go, and any brace-delimited language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._io import read_text_verbatim, write_text_verbatim
from .profiles import Profile


class RegionError(LookupError):
    """Anchor missing, ambiguous, or in a file this locator cannot parse."""


def guard_unsupported_syntax(path: Path, src: str, profile: Profile) -> None:
    """Refuse a file containing syntax the brace matcher would silently misread."""
    for token in profile.block_comment:
        if token in src:
            line = src[: src.index(token)].count("\n") + 1
            raise RegionError(
                f"{path.name}:{line} contains a block comment ({token!r}), which this "
                f"locator cannot parse safely. Upgrade regions.py to a real "
                f"parser (tree-sitter) before editing this file."
            )


def language_for(path: Path, profile: Profile) -> str:
    if path.suffix.lower() not in profile.file_suffixes:
        raise RegionError(
            f"no configured language for suffix {path.suffix!r} ({path}); "
            f"profile {profile.name!r} supports {profile.file_suffixes}"
        )
    return profile.language


def strip_code(line: str, profile: Profile) -> str:
    """Blank out line comments and string/char literal bodies for brace counting."""
    out, i, n = [], 0, len(line)
    line_comment = profile.line_comment
    while i < n:
        c = line[i]
        if c == line_comment[0] and line[i:i + len(line_comment)] == line_comment:
            break
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# What to DO to a region, as distinct from `kind`, which is how to FIND it.
# Kept on a separate axis on purpose: `kind` (decl/indent/line) is the locator
# strategy consumed by find_region, and folding an operation into it would give
# one field two meanings.
REPLACE, CREATE, INSERT = "replace", "create", "insert"
OPS = (REPLACE, CREATE, INSERT)


@dataclass
class Region:
    id: str
    file: str
    path: Path
    anchor: str
    kind: str
    start_line: int  # 0-based, inclusive
    end_line: int    # 0-based, inclusive
    text: str
    note: str = ""
    # `replace` is the default so every existing ticket keeps its behaviour.
    op: str = REPLACE

    @property
    def lines_1based(self) -> str:
        # A create region has no span: start=0/end=-1 renders as "1-0", which
        # would appear in the implement prompt and in `--list` as if it were a
        # real range.
        if self.op == CREATE:
            return "new file"
        return f"{self.start_line + 1}-{self.end_line + 1}"


def _show(text: str, width: int) -> str:
    """Render one preview line, MARKING truncation rather than hiding it.

    Observed live: a preview cut mid-identifier at
    `TranslateSymbol(leaderInstrument.FullNam` was copied by the model as its
    next anchor and COMPLETED from imagination -- `FullName, relationship)`
    where the file says `FullName, rel)`. A preview that looks like whole code
    invites that. The marker says outright that the text is partial, so it
    cannot be mistaken for something copyable.
    """
    if len(text) <= width:
        return text
    return text[:width] + " ...[TRUNCATED, not a copyable anchor]"


def _nearest_lines(lines: List[str], anchor: str, k: int = 5) -> str:
    """Real lines from the file, ranked by similarity to a failed anchor.

    Why this exists, from two live feature runs: the plan model is asked to
    supply exact-match anchors into files it has never been shown. Layout
    context (O31) answers "where does new code go" and nothing answers "what
    text is actually in this file", so the model anchors from memory. It spent
    five rounds hunting `LoadCopierConfig` in a file whose method is
    `LoadFromDisk`, and two more inventing parameter names. No amount of
    re-prompting fixes a guess about text it cannot see.

    `anchor not found` is the right moment to answer it: the file is already in
    hand, so the failure can carry the candidates instead of just the verdict.
    """
    import difflib

    # `re:` is a mode marker, not content. Stripping it is normalisation only:
    # a mutation that keeps the prefix does not change any observed ranking, so
    # this line is deliberately NOT claimed as tested behaviour. It is here
    # because scoring a flag as text is wrong on its face, not because a test
    # pins it.
    needle = anchor[3:] if anchor.startswith("re:") else anchor
    needle = needle.strip()

    # ONE filter, the similarity floor below -- not two. Two earlier guards
    # lived here (`s in ("{", "}", "};")`, then a `len(s) < 4` length floor) and
    # mutation testing killed both: the first was unreachable because every one
    # of those strings is shorter than 4 characters, and the second was
    # redundant because a brace scores far below the floor against any realistic
    # anchor. Overlapping guards where no test pins either one are how a
    # threshold silently stops meaning anything.
    scored: List[Tuple[float, int, str]] = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        # Pure character similarity, deliberately. A bonus for lines containing
        # the anchor's leading identifier was written here first and then
        # DELETED: it survived mutation, and on all three anchors from the live
        # runs it produced byte-identical output. An untested weight that
        # changes nothing is a knob for a later reader to mis-tune.
        ratio = difflib.SequenceMatcher(None, needle.lower(), s.lower()).ratio()
        scored.append((ratio, i, s))

    if not scored:
        return ""
    scored.sort(key=lambda t: (-t[0], t[1]))
    best = [t for t in scored[:k] if t[0] > 0.3]
    if not best:
        return ""
    return "; ".join(f"L{i + 1}: {_show(s, 160)}" for _, i, s in best)


def _ambiguous_hits_preview(lines: List[str], hits: List[int]) -> str:
    """Render ambiguous anchor hits so they can actually be told apart.

    A fixed 60-char truncation is not enough. C# overloads -- and Python
    functions with long first parameters -- routinely share far more than 60
    characters of prefix, so every hit rendered IDENTICALLY and the feedback
    told the caller "these two are different" while showing the same string
    twice. A model handed that cannot lengthen the anchor usefully; the one
    observed live invented a parameter list that did not exist, turning a
    recoverable ambiguity into `anchor not found`.

    So the window is computed from the hits: extend past their common prefix
    far enough that the part which DIFFERS is visible. Line numbers are
    included because they are the one thing that always distinguishes a hit.
    """
    shown = hits[:4]
    stripped = [lines[i].strip() for i in shown]

    common = 0
    if len(stripped) > 1:
        shortest = min(len(s) for s in stripped)
        while common < shortest and len({s[common] for s in stripped}) == 1:
            common += 1

    width = max(60, min(200, common + 40))
    parts = [f"L{i + 1}: {_show(s, width)}" for i, s in zip(shown, stripped)]

    if len(hits) > len(shown):
        parts.append(f"... and {len(hits) - len(shown)} more")

    # Truly identical lines cannot be disambiguated by lengthening the anchor
    # at all. Saying so is more useful than printing the same text twice and
    # leaving the caller to infer that widening is hopeless.
    if len(set(stripped)) == 1:
        parts.append(
            "these lines are IDENTICAL -- a longer anchor cannot separate them; "
            "use 're:' with surrounding context, or anchor on the enclosing "
            "declaration instead"
        )
    return "; ".join(parts)


def find_region(lines: List[str], anchor: str, kind: str = "decl",
                profile: Profile | None = None) -> Tuple[int, int]:
    """Return 0-based inclusive (start_line, end_line) for `anchor`."""
    # An anchor is matched against ONE line at a time, so a newline inside it can
    # never match anything. Observed live: a plan asked for
    # `...TranslateSymbol(...)\n        {` and kept it for three rounds, because
    # `anchor not found` is true but says nothing about WHY it is unsatisfiable,
    # and a model cannot guess its way out of an impossible request. `kind=decl`
    # already expands a signature line to its whole block, so the signature line
    # alone is what the caller wanted.
    if "\n" in anchor:
        head = anchor.split("\n", 1)[0].strip()
        detail = f" Use only the line that OPENS the region, i.e. {head!r}." if head else ""
        nearest = _nearest_lines(lines, head) if head else ""
        if nearest:
            detail += f" Real lines in this file, closest first: {nearest}"
        raise RegionError(
            f"anchor spans multiple lines and can never match: {anchor!r}. Anchors are "
            f"matched one line at a time, and `kind=decl` already expands from the "
            f"opening line to the end of the block." + detail
        )

    if anchor.startswith("re:"):
        pat: Optional[re.Pattern] = re.compile(anchor[3:])
        hits = [i for i, ln in enumerate(lines) if pat.search(ln)]
    else:
        hits = [i for i, ln in enumerate(lines) if anchor in ln]
    if not hits:
        nearest = _nearest_lines(lines, anchor)
        if nearest:
            raise RegionError(
                f"anchor not found: {anchor!r}. Real lines in this file, closest first "
                f"-- anchor on one of these EXACTLY, or on a unique substring of one: {nearest}"
            )
        raise RegionError(f"anchor not found: {anchor!r}")
    if len(hits) > 1:
        preview = _ambiguous_hits_preview(lines, hits)
        raise RegionError(f"anchor not unique ({len(hits)} hits): {anchor!r} -> {preview}")

    start = hits[0]
    if kind == "line":
        return start, start

    strip_fn = lambda ln: strip_code(ln, profile) if profile else strip_code_default(ln)

    if kind == "indent":
        return _find_indent_block(lines, start, strip_fn)

    try:
        return _brace_block(lines, start, strip_fn)
    except RegionError:
        raise RegionError(f"unbalanced braces from anchor: {anchor!r}")


def _find_indent_block(lines: List[str], start: int, strip_fn) -> Tuple[int, int]:
    """Find the extent of an indentation-based block (Python, YAML, etc.).

    The anchor line is the declaration (e.g., 'def foo():' or 'for x in y:').
    The block extends until a line at the same or lesser indentation level
    that is not blank and not a comment.

    If the anchor line itself ends with a colon (e.g., 'def foo():'), the
    block starts on the NEXT non-blank line and extends until the indentation
    returns to the anchor's level or above.
    """
    if start >= len(lines):
        raise RegionError(f"anchor at end of file")

    anchor_code = strip_fn(lines[start])
    anchor_indent = len(anchor_code) - len(anchor_code.lstrip())

    # A declaration whose signature spans several lines opens its block on the
    # line where the brackets balance, not on the anchor line:
    #
    #     def check_lint(
    #         cmd: str,
    #         repo: Path,
    #     ) -> GateResult:      <-- the block actually opens here
    #
    # Testing the anchor line alone made every such region collapse to a single
    # line, and `--list` reported it as OK. The implementer was then handed
    # `def check_lint(` with no body, and splicing its replacement over that one
    # line orphaned the old parameters -- a guaranteed syntax error, discovered
    # only at the compile gate. Multi-line signatures are ordinary Python, so
    # this silently broke a large fraction of plausible tickets.
    header_end = start
    depth = 0
    for i in range(start, len(lines)):
        code = strip_fn(lines[i])
        depth += code.count("(") + code.count("[") + code.count("{")
        depth -= code.count(")") + code.count("]") + code.count("}")
        header_end = i
        if depth <= 0:
            break

    # Check whether the header (however many lines it took) opens a block.
    stripped = strip_fn(lines[header_end]).rstrip()
    if stripped.endswith(":"):
        start_body = header_end
        # The block body starts on the next non-blank, non-comment line.
        # The body's indentation must be > anchor_indent.
        # The block ends at the last consecutive line with indent > anchor_indent
        # (skipping blank lines and comments).
        end = start_body
        for i in range(start_body + 1, len(lines)):
            code = strip_fn(lines[i])
            if not code.strip():
                continue  # blank line — part of the block
            if code.lstrip().startswith("#"):
                # Comment-only line at body indent is part of the block
                line_indent = len(code) - len(code.lstrip())
                if line_indent > anchor_indent:
                    end = i
                    continue
                else:
                    break
            line_indent = len(code) - len(code.lstrip())
            if line_indent > anchor_indent:
                end = i
            else:
                break
        if end == start_body:
            raise RegionError(f"no indented body found after anchor at line {start + 1}")
        # The region spans the anchor line through the body, so a multi-line
        # signature is included in what the implementer sees and replaces.
        return start, end

    # Not a block opener — just the anchor line itself
    return start, start


def _brace_block(lines: List[str], start: int, strip_fn) -> Tuple[int, int]:
    """Extent of a brace-delimited declaration starting at `start`."""
    depth, seen_open = 0, False
    for i in range(start, len(lines)):
        for ch in strip_fn(lines[i]):
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
                if seen_open and depth == 0:
                    return start, i
    raise RegionError(f"unbalanced braces from line {start + 1}")


def extract_named_block(src: str, name: str, profile: Profile) -> Optional[str]:
    """Return the full source of the declaration named `name`, or None.

    Used to show reviewers the acceptance tests they are asked to judge. The
    match must be the DECLARATION, not a call site: matching a bare `name(`
    finds the invocation first and ships the wrong text to the reviewer.

    Language handling comes from the profile -- `block_kind` picks between an
    indentation block and a brace block, and the declaration patterns cover
    both `def`/`class` and modifier+return-type forms. The predecessor was
    C#-only, so on a Python profile this always returned nothing and the
    reviewer was asked to judge test adequacy with no tests in front of it.
    """
    lines = src.splitlines()
    strip_fn = lambda ln: strip_code(ln, profile)
    patterns = [
        # Python / Ruby style: def name(, class Name:
        re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\s*[\(:]"),
        # Go / Rust / JS style: func name(, fn name(, function name(
        re.compile(rf"^\s*(?:pub\s+)?(?:func|fn|function)\s+{re.escape(name)}\s*[\(<]"),
        # C# / Java style: at least one modifier, then a return type, then name(
        re.compile(
            r"^[ \t]*(?:(?:private|public|internal|protected|static|async|override"
            r"|virtual|final|sealed|abstract|extern|unsafe)\s+)+"
            rf"[\w<>\[\],\.\*\?]+\s+{re.escape(name)}\s*\("
        ),
    ]
    for pat in patterns:
        idx = next((i for i, ln in enumerate(lines) if pat.search(ln)), None)
        if idx is None:
            continue
        try:
            if profile.block_kind == "indent":
                start, end = _find_indent_block(lines, idx, strip_fn)
            else:
                start, end = _brace_block(lines, idx, strip_fn)
        except RegionError:
            continue
        return "\n".join(lines[start: end + 1])
    return None


def strip_code_default(line: str) -> str:
    """Fallback strip_code for when no profile is available (backward compat)."""
    out, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def read_source(path: Path) -> Tuple[List[str], str, bool]:
    """Read a file as (lines, newline, had_trailing_newline).

    Line splitting must be identical here and in `apply`, or the line indices
    a region records will not address the same lines it later writes back.
    `str.splitlines()` is NOT usable for that: it also breaks on \\x0b, \\x0c
    and \\u2028, so a form feed anywhere in the file shifts every index after
    it and the splice lands in the wrong place.
    """
    raw = read_text_verbatim(path)
    newline = "\r\n" if "\r\n" in raw else "\n"
    had_trailing_newline = raw.endswith(("\n", "\r"))
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if had_trailing_newline:
        lines.pop()
    return lines, newline, had_trailing_newline


def extract(repo: Path, specs: List[Dict[str, Any]], profile: Profile) -> List[Region]:
    """Resolve every region spec in a ticket against the current tree."""
    out: List[Region] = []
    for spec in specs:
        path = repo / spec["file"]
        op = spec.get("op", REPLACE)
        if op not in OPS:
            # Named, not defaulted: a typo that fell back to `replace` would
            # overwrite a file the ticket meant to create or extend.
            raise RegionError(
                f"{spec['id']}: unknown op {op!r}. Expected one of {', '.join(OPS)}."
            )

        if op == CREATE:
            # Nothing to locate: the point of this op is that the file is not
            # there yet, which is exactly what plan mode could not express.
            if path.exists():
                raise RegionError(
                    f"{spec['id']}: op=create but {spec['file']} already exists. "
                    f"Use op=replace or op=insert to change a file that is there."
                )
            # The language check still applies -- a Python profile must not be
            # talked into authoring a .cs file.
            language_for(path, profile)
            out.append(
                Region(
                    id=spec["id"], file=spec["file"], path=path, anchor="",
                    kind=spec.get("kind", profile.block_kind),
                    start_line=0, end_line=-1, text="",
                    note=spec.get("note", ""), op=CREATE,
                )
            )
            continue

        if not path.exists():
            raise RegionError(f"{spec['id']}: file does not exist: {spec['file']}")
        language_for(path, profile)
        lines, _, _ = read_source(path)
        guard_unsupported_syntax(path, "\n".join(lines), profile)
        kind = spec.get("kind", profile.block_kind)
        if kind in ("method", "block"):
            kind = "decl"
        start, end = find_region(lines, spec["anchor"], kind, profile)
        text = "\n".join(lines[start: end + 1])
        out.append(
            Region(
                id=spec["id"],
                file=spec["file"],
                path=path,
                anchor=spec["anchor"],
                kind=kind,
                start_line=start,
                end_line=end,
                text=text,
                note=spec.get("note", ""),
                op=op,
            )
        )
    return out


def apply(regions: List[Region], blocks: Dict[str, str]) -> List[str]:
    """Splice replacements in, per file, bottom-up so earlier spans stay valid.

    Three operations, per region: `replace` overwrites the located span, `insert`
    adds after it, and `create` writes a whole new file.
    """
    touched: List[str] = []
    by_file: Dict[Path, List[Region]] = {}
    for r in regions:
        if r.op == CREATE:
            body = blocks.get(r.id)
            if not body or not body.strip():
                continue
            r.path.parent.mkdir(parents=True, exist_ok=True)
            text = body if body.endswith("\n") else body + "\n"
            # A brand-new file has no existing terminator to preserve, so it is
            # written verbatim in the one style the model emitted rather than
            # being normalised -- write_text_verbatim is what keeps a later
            # `replace` on this same file from rewriting every line.
            write_text_verbatim(r.path, text)
            touched.append(r.file)
        else:
            by_file.setdefault(r.path, []).append(r)
    for path, regs in by_file.items():
        # Read with newline="" so the file's real line terminators survive.
        # read_text() universalises them to "\n" and write_text() then rewrites
        # the WHOLE file in the platform terminator -- which turns a two-line
        # patch into a whole-file diff on any repo whose files disagree with the
        # running platform (CRLF sources edited on Linux, or the reverse).
        lines, newline, had_trailing_newline = read_source(path)
        changed = False
        for r in sorted(regs, key=lambda x: x.start_line, reverse=True):
            body = blocks.get(r.id)
            if r.op == INSERT:
                # Additive: the anchored block STAYS and the new code follows it.
                # An empty body is a no-op rather than a deletion -- the model
                # declining to add anything must not remove what is there.
                if not body or not body.strip():
                    continue
                new_lines = body.replace("\r\n", "\n").split("\n")
                lines[r.end_line + 1: r.end_line + 1] = new_lines
                changed = True
                continue
            if body is None or body.rstrip() == r.text.rstrip():
                continue
            lines[r.start_line: r.end_line + 1] = body.replace("\r\n", "\n").split("\n")
            changed = True
        if changed:
            out = newline.join(lines) + (newline if had_trailing_newline else "")
            write_text_verbatim(path, out)
            touched.append(regs[0].file)
    return touched