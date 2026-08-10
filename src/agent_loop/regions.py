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

    @property
    def lines_1based(self) -> str:
        return f"{self.start_line + 1}-{self.end_line + 1}"


def find_region(lines: List[str], anchor: str, kind: str = "decl",
                profile: Profile | None = None) -> Tuple[int, int]:
    """Return 0-based inclusive (start_line, end_line) for `anchor`."""
    if anchor.startswith("re:"):
        pat: Optional[re.Pattern] = re.compile(anchor[3:])
        hits = [i for i, ln in enumerate(lines) if pat.search(ln)]
    else:
        hits = [i for i, ln in enumerate(lines) if anchor in ln]
    if not hits:
        raise RegionError(f"anchor not found: {anchor!r}")
    if len(hits) > 1:
        preview = "; ".join(lines[i].strip()[:60] for i in hits[:4])
        raise RegionError(f"anchor not unique ({len(hits)} hits): {anchor!r} -> {preview}")

    start = hits[0]
    if kind == "line":
        return start, start

    strip_fn = lambda ln: strip_code(ln, profile) if profile else strip_code_default(ln)

    if kind == "indent":
        return _find_indent_block(lines, start, strip_fn)

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

    # Check if this line ends with a colon (Python block opener)
    stripped = anchor_code.rstrip()
    if stripped.endswith(":"):
        # The block body starts on the next non-blank, non-comment line.
        # The body's indentation must be > anchor_indent.
        # The block ends at the last consecutive line with indent > anchor_indent
        # (skipping blank lines and comments).
        end = start
        for i in range(start + 1, len(lines)):
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
        if end == start:
            raise RegionError(f"no indented body found after anchor at line {start + 1}")
        return start, end

    # No colon — just the anchor line itself
    return start, start


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


def extract(repo: Path, specs: List[Dict[str, Any]], profile: Profile) -> List[Region]:
    """Resolve every region spec in a ticket against the current tree."""
    out: List[Region] = []
    for spec in specs:
        path = repo / spec["file"]
        if not path.exists():
            raise RegionError(f"{spec['id']}: file does not exist: {spec['file']}")
        language_for(path, profile)
        src = path.read_text(encoding="utf-8")
        guard_unsupported_syntax(path, src, profile)
        lines = src.splitlines()
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
            )
        )
    return out


def apply(regions: List[Region], blocks: Dict[str, str]) -> List[str]:
    """Splice replacements in, per file, bottom-up so earlier spans stay valid."""
    touched: List[str] = []
    by_file: Dict[Path, List[Region]] = {}
    for r in regions:
        by_file.setdefault(r.path, []).append(r)
    for path, regs in by_file.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        for r in sorted(regs, key=lambda x: x.start_line, reverse=True):
            body = blocks.get(r.id)
            if body is None or body.rstrip() == r.text.rstrip():
                continue
            lines[r.start_line: r.end_line + 1] = body.splitlines()
            changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            touched.append(regs[0].file)
    return touched