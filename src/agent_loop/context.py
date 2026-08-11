"""
context.py
==========
Graph freshness check and passive context injection (Phase 3).

Phase 2: checks whether the codebase-memory-mcp graph is fresh at loop
startup. If stale, re-indexes.

Phase 3: builds a ranked, token-budgeted context slice for each region
and injects it into the implementer/reviewer/arbiter prompts. This is
the Aider-style passive injection pattern -- the LLM never calls graph
tools; it receives richer context.

The context is built by querying the codebase-memory-mcp graph for:
- Callees of the functions in the region (what does this code call?)
- Callers of the functions in the region (who depends on this code?)
- Tests that cover the region (what verifies this code?)
- Types/interfaces used in the region (what contracts does it rely on?)

The results are ranked by structural distance and truncated to the
profile's context_token_budget (default 3000 tokens).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .profiles import Profile


def _marker_path(repo: Path) -> Path:
    return repo / "logs" / "agent_loop" / ".graph_mtime"


def mark_graph_fresh(repo: Path) -> None:
    """Record that the graph has been indexed as of now.

    Nothing ever wrote this marker, so the freshness check compared every
    source file against 0.0 and reported "stale" on every ticket forever --
    a status line that is always the same carries no information.
    """
    path = _marker_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")


def check_graph_freshness(repo: Path, profile: Profile, timeout: int = 60) -> str:
    """Report whether the codebase-memory-mcp graph is fresh for this repo.

    Compares the mtime of the newest source file against a persisted marker.
    Returns 'fresh', 'stale (...)', 'no-project', or 'error: ...'.

    This REPORTS; it does not re-index. Re-indexing a large repo takes minutes,
    and silently spending that before every ticket is not a decision this
    function should make on the caller's behalf -- the returned string names the
    command to run instead.
    """
    if not profile.graph_project:
        return "no-project"
    try:
        newest = _newest_source_mtime(repo, profile)
        if newest is None:
            return "fresh"

        marker_path = _marker_path(repo)
        last_indexed = 0.0
        if marker_path.exists():
            try:
                last_indexed = float(marker_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                last_indexed = 0.0

        if newest > last_indexed:
            if not last_indexed:
                return (
                    f"never indexed by this loop (project {profile.graph_project!r}); "
                    "graph context may be stale. Re-index via codebase-memory-mcp "
                    "index_repository, then call context.mark_graph_fresh(repo)"
                )
            age = (newest - last_indexed) / 3600.0
            return f"stale ({age:.1f}h of edits since the last recorded index)"
        return "fresh"
    except Exception as exc:
        return f"error: {exc}"


def _newest_source_mtime(repo: Path, profile: Profile) -> Optional[float]:
    """Find the mtime of the newest source file in the repo."""
    newest = 0.0
    for suffix in profile.file_suffixes:
        for path in repo.rglob(f"*{suffix}"):
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            try:
                mtime = path.stat().st_mtime
                if mtime > newest:
                    newest = mtime
            except OSError:
                continue
    return newest if newest > 0 else None


# --------------------------------------------------------------------------
# Phase 3: Passive context injection
# --------------------------------------------------------------------------
# Estimated tokens: ~4 chars per token. Conservative so we stay under budget.
_CHARS_PER_TOKEN = 4


def build_context_slice(
    repo: Path,
    regions: Sequence[Any],
    profile: Profile,
) -> str:
    """Build a ranked, token-budgeted context slice for the prompts.

    Queries the codebase-memory-mcp graph (live MCP) for each region's
    callees, callers, tests, and types. Falls back to the cache file
    when the MCP server is not available.

    The context is returned as a formatted string to inject into the
    implementer/reviewer/arbiter prompts. When the graph is unavailable
    and no cache exists, returns an empty string.
    """
    if not profile.graph_project:
        return ""

    budget_chars = profile.context_token_budget * _CHARS_PER_TOKEN

    # Try live MCP queries first
    context = _build_context_via_mcp(regions, profile)
    if context:
        # Truncate to budget
        if len(context) > budget_chars:
            context = context[:budget_chars] + "\n... (truncated to token budget)"
        return context

    # Fall back to cache file
    parts: List[str] = []
    for region in regions:
        region_context = _build_region_context(repo, region, profile)
        if region_context:
            parts.append(region_context)
            if sum(len(p) for p in parts) >= budget_chars:
                break

    if not parts:
        return ""

    result = "\n".join(parts)
    if len(result) > budget_chars:
        result = result[:budget_chars] + "\n... (truncated to token budget)"
    return result


def _build_context_via_mcp(regions: Sequence[Any], profile: Profile) -> str:
    """Query the codebase-memory-mcp graph live for each region's context.

    Returns an empty string if the MCP server is not available.
    """
    try:
        from .mcp_client import get_mcp_client
        client = get_mcp_client()
        if not client:
            return ""
    except Exception:
        return ""

    parts: List[str] = []
    for region in regions:
        # Extract function/class names from the region's anchor
        names = _extract_names_from_region(region)
        if not names:
            continue

        callees: List[str] = []
        callers: List[str] = []
        tests: List[str] = []
        types: List[str] = []

        for name in names[:3]:  # limit to 3 names per region
            # Get callees (outbound)
            result = client.call_tool("trace_call_path", {
                "function_name": name,
                "direction": "outbound",
                "project": profile.graph_project,
                "depth": 1,
            })
            if result and not result.startswith("ERROR"):
                for line in result.splitlines():
                    # Extract function names from the trace result
                    stripped = line.strip()
                    if stripped and not stripped.startswith("[") and not stripped.startswith("{"):
                        # Extract the function name from lines like "name (qualified_name, hop=N)"
                        if "name" in line and ":" in line:
                            parts_match = line.split(":", 1)
                            if len(parts_match) > 1:
                                fn = parts_match[1].strip().split(",")[0].strip().strip('"')
                                if fn and fn not in callees:
                                    callees.append(fn)

            # Get callers (inbound)
            result = client.call_tool("trace_call_path", {
                "function_name": name,
                "direction": "inbound",
                "project": profile.graph_project,
                "depth": 1,
            })
            if result and not result.startswith("ERROR"):
                for line in result.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("[") and not stripped.startswith("{"):
                        if "name" in line and ":" in line:
                            parts_match = line.split(":", 1)
                            if len(parts_match) > 1:
                                fn = parts_match[1].strip().split(",")[0].strip().strip('"')
                                if fn and fn not in callers:
                                    callers.append(fn)

            # Search for tests that reference this name
            result = client.call_tool("search_code", {
                "pattern": name,
                "file_pattern": "*test*",
                "path_filter": "tests/",
                "project": profile.graph_project,
            })
            if result and not result.startswith("ERROR"):
                for line in result.splitlines()[:5]:
                    if "file" in line.lower() and name in line:
                        tests.append(line.strip()[:80])

        if callees or callers or tests:
            lines = [f"### Graph context for {region.id} ({region.file})"]
            if callees:
                lines.append(f"Callees ({len(callees)}): {', '.join(callees[:10])}")
            if callers:
                lines.append(f"Callers ({len(callers)}): {', '.join(callers[:10])}")
            if tests:
                lines.append(f"Tests ({len(tests)}): {', '.join(tests[:5])}")
            parts.append("\n".join(lines))

    return "\n".join(parts) if parts else ""


_ANCHOR_KEYWORDS = {
    "if", "for", "while", "try", "with", "else", "elif", "switch", "case",
    "return", "using", "namespace", "public", "private", "protected", "internal",
    "static", "async", "await", "override", "virtual", "sealed", "abstract",
    "void", "new", "lock", "class", "struct", "def", "func", "fn", "function",
}


def _extract_names_from_region(region: Any) -> List[str]:
    """Extract the symbol name(s) a region's anchor refers to.

    Language-neutral. The predecessor understood only `def`/`class` and
    otherwise passed the whole anchor through as a "name", so a C# anchor like
    `private void OnOrderUpdate(` was sent to the graph as the literal string
    "private void OnOrderUpdate" -- which matches nothing, so every graph query
    on the NT8 profile was wasted.
    """
    anchor = (getattr(region, "anchor", "") or "").strip()
    if anchor.startswith("re:"):
        anchor = anchor[3:]
    if not anchor:
        return []

    names: List[str] = []
    # Declaration keyword forms: def/class (Python), func/fn/function (Go, Rust, JS).
    m = re.search(r"(?:async\s+)?(?:def|class|struct|func|fn|function)\s+(\w+)", anchor)
    if m:
        names.append(m.group(1))
    # Signature form: the identifier immediately before "(" is the callee name,
    # whatever modifiers and return type precede it. An optional generic
    # parameter list sits between them (`TryCopy<T>(`).
    m = re.search(r"(\w+)\s*(?:<[^<>()]*>)?\s*\(", anchor)
    if m and m.group(1) not in _ANCHOR_KEYWORDS:
        names.append(m.group(1))
    if not names:
        # A bare symbol anchor (a field, a constant). Keep plausible
        # identifiers only; single letters come from regex escapes, not code.
        names += [
            tok for tok in re.findall(r"[A-Za-z_]\w*", anchor)
            if tok not in _ANCHOR_KEYWORDS and len(tok) >= 3
        ]

    out: List[str] = []
    for n in names:
        if n not in out and len(n) < 50:
            out.append(n)
    return out


def _build_region_context(repo: Path, region: Any, profile: Profile) -> str:
    """Build context for a single region: callees, callers, tests, types.

    This reads a pre-computed graph context cache file
    (logs/agent_loop/graph_context.json) that is populated by a separate
    script that queries the codebase-memory-mcp graph. This two-step design
    avoids hard-coupling the loop to the MCP client protocol; the graph can
    be queried by any tool that writes the cache file.

    When the cache file doesn't exist, returns an empty string (the loop
    works without graph context -- it's an enhancement, not a gate).
    """
    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    if not cache_path.exists():
        return ""

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    # The cache is keyed by region id. Each entry has:
    # { "callees": [...], "callers": [...], "tests": [...], "types": [...] }
    entry = cache.get(region.id)
    if not entry:
        return ""

    lines = [f"### Graph context for {region.id} ({region.file})"]

    callees = entry.get("callees", [])
    if callees:
        lines.append(f"Callees ({len(callees)}): {', '.join(callees[:10])}")

    callers = entry.get("callers", [])
    if callers:
        lines.append(f"Callers ({len(callers)}): {', '.join(callers[:10])}")

    tests = entry.get("tests", [])
    if tests:
        lines.append(f"Tests ({len(tests)}): {', '.join(tests[:5])}")

    types = entry.get("types", [])
    if types:
        lines.append(f"Types ({len(types)}): {', '.join(types[:5])}")

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


def write_context_cache(
    repo: Path,
    context_data: Dict[str, Any],
) -> None:
    """Write the graph context cache file.

    This is called by a separate script (or the MCP agent) that queries
    the codebase-memory-mcp graph and writes the results. The loop's
    build_context_slice() reads this file.
    """
    cache_path = repo / "logs" / "agent_loop" / "graph_context.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(context_data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# O31: context keyed on the REQUEST, for modes that have no regions yet
# --------------------------------------------------------------------------
#
# `build_context_slice` above needs `regions`, and plan mode exists to PRODUCE
# regions -- there was nothing to pass it, which is why plan_mode imported it and
# never called it. Brainstorm had no context of any kind. Both were reasoning
# about a codebase they had never seen: measured at in=264 and in=319 tokens on
# live runs.
#
# The mechanism here is the FILESYSTEM, not the graph. Docs mode's private
# builder returns "" unless codebase-memory-mcp is live, so copying its shape
# would have left both modes blind on every machine without the server running.
# The graph is added on top when it happens to be available.

# Words that look like identifiers but are just English. Kept small on purpose:
# the identifier SHAPE tests below do most of the filtering, and this set only
# has to catch prose that survives them.
_PROSE = {
    "the", "and", "for", "with", "from", "that", "this", "there", "then", "than",
    "when", "where", "which", "what", "into", "onto", "over", "under", "about",
    "should", "would", "could", "must", "will", "can", "cannot", "does", "not",
    "but", "are", "was", "were", "has", "have", "had", "its", "you", "your",
    "fix", "add", "remove", "update", "change", "make", "get", "set", "use",
    "user", "users", "code", "file", "files", "line", "lines", "test", "tests",
    "bug", "defect", "feature", "instead", "rather", "because", "every", "each",
    "output", "input", "error", "errors", "wrong", "right", "correct", "prints",
    "print", "call", "calls", "called", "return", "returns", "mode", "modes",
    "colours", "colors", "nicer",
}

# A path-shaped token: at least one directory separator and a file extension,
# optionally followed by :LINE.
_PATH_RE = re.compile(r"\b((?:[\w.\-]+/)+[\w.\-]+\.\w{1,6})(?::\d+)?")
# A bare filename with an extension, when no directory is given.
_BARE_FILE_RE = re.compile(
    r"\b([\w\-]+\.(?:py|cs|js|ts|tsx|go|rs|java|rb|md|json|ya?ml))\b"
)
_BACKTICK_RE = re.compile(r"`([^`\n]{1,120})`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE_EXTS = {"py", "cs", "js", "ts", "tsx", "go", "rs", "java", "rb", "md",
              "json", "yaml", "yml"}


def _looks_like_code(tok: str) -> bool:
    """Is this token shaped like an identifier rather than an English word?

    Recall matters more than precision here, and the reason is structural: a
    candidate that is not real simply finds no DEFINITION in the tree and is
    dropped, so the filesystem is the filter. Being strict, by contrast, loses
    the name silently.

    Measured: the first version required an underscore or a lower-to-upper
    transition, which rejects every single-word class name -- `Config`, `Vote`,
    `Finding`. On a live brainstorm run about `Config.roles` it therefore found
    nothing and added 25 tokens of context to a 264-token prompt.
    """
    if len(tok) < 3 or len(tok) > 60:
        return False
    if tok.lower() in _PROSE:
        return False
    if "_" in tok:
        return True
    # CamelCase / mixedCase: a lower-to-upper transition.
    if re.search(r"[a-z][A-Z]", tok):
        return True
    # A leading capital: a class name (`Config`) or a constant (`PROMOTABLE`).
    # Sentence-initial English words reach here too and cost one failed lookup.
    return tok[0].isupper()


def extract_intent_symbols(intent: str) -> List[str]:
    """Identifiers a request points at, best signal first.

    Backticked spans are trusted -- someone marked them as code. Otherwise a
    token must be SHAPED like an identifier (snake_case or CamelCase), because
    the alternative, taking every non-stopword, sends the dictionary to the
    searcher and buries the real names in noise.
    """
    out: List[str] = []

    def _add(tok: str) -> None:
        if tok and tok not in out and tok.lower() not in _PROSE and len(tok) >= 3:
            out.append(tok)

    # 1. Backticked: trusted, and split on non-identifier characters so
    #    `gates.run_tests()` contributes `run_tests`.
    for m in _BACKTICK_RE.finditer(intent):
        for part in _IDENT_RE.findall(m.group(1)):
            if part.lower() not in _FILE_EXTS:
                _add(part)

    # 2. Dotted qualified names in prose: take the member being talked about.
    for m in re.finditer(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", intent):
        seg = m.group(0).split(".")
        # A file path is not a symbol: `review_mode.py` means the module.
        _add(seg[-2] if seg[-1].lower() in _FILE_EXTS else seg[-1])

    # 3. Identifier-shaped bare tokens.
    for tok in _IDENT_RE.findall(intent):
        if _looks_like_code(tok):
            _add(tok)

    return out


def split_paths_and_symbols(intent: str):
    """(file paths, symbol names) named by a defect or feature description."""
    paths: List[str] = []
    for m in _PATH_RE.finditer(intent):
        if m.group(1) not in paths:
            paths.append(m.group(1))
    for m in _BARE_FILE_RE.finditer(intent):
        p = m.group(1)
        if not any(p in seen for seen in paths):
            paths.append(p)
    return paths, extract_intent_symbols(intent)


_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "logs",
              ".pytest_cache", "dist", "build", ".mypy_cache", ".ruff_cache"}


def _is_test_path(repo: Path, path: Path, profile: Profile) -> bool:
    rel = path.relative_to(repo).as_posix()
    if any(fnmatch.fnmatch(rel, pat) for pat in (profile.test_sources or ())):
        return True
    name = path.name.lower()
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", "_tests.py", "test.cs", "tests.cs"))
        or "tests" in path.parts
        or "test" in path.parts
    )


def _iter_sources(repo: Path, profile: Profile):
    """Source files, PRODUCTION FIRST.

    Ordering is load-bearing, not cosmetic: `_find_symbols` stops at two hits per
    name, and on a live run `roles` matched `roles = dict(base.roles)` in two test
    files and pushed the real declaration out of the budget entirely -- because
    `rglob` is alphabetical and the test tree happened to sort first. For
    localisation the production tree is the answer; a test is corroboration.
    """
    found: List[Path] = []
    for suffix in profile.file_suffixes or (".py",):
        for path in repo.rglob(f"*{suffix}"):
            if _SKIP_DIRS & set(path.parts):
                continue
            found.append(path)
    found.sort(key=lambda p: (_is_test_path(repo, p, profile), p.as_posix()))
    yield from found


def _definition_patterns(name: str):
    esc = re.escape(name)
    return (
        # def/class/func/struct NAME
        re.compile(
            rf"^\s*(?:@\w+\s*)?(?:async\s+)?"
            rf"(?:def|class|struct|func|fn|function|interface|enum)\s+{esc}\b"
        ),
        # A C#-style signature: modifiers, return type, NAME(
        re.compile(
            rf"^\s*(?:(?:public|private|protected|internal|static|override|virtual"
            rf"|sealed|abstract|async)\s+)*[\w<>\[\],.?]+\s+{esc}\s*"
            rf"(?:<[^<>()]*>)?\s*\("
        ),
        # An assignment or a typed field. Indentation is allowed on purpose: a
        # dataclass field like `Config.roles` lives inside the class body, and a
        # column-0-only pattern missed exactly the name the request pointed at.
        # `==` is excluded so a comparison is not read as a definition.
        re.compile(rf"^\s*{esc}\s*(?::[^=]|=(?!=))"),
    )


def _find_symbols(
    repo: Path,
    profile: Profile,
    names: Sequence[str],
    per_name: int = 2,
) -> Dict[str, List[str]]:
    """Where each symbol is DEFINED: name -> [`relpath:line  <the line>`].

    One walk for all names. The per-symbol version re-read the whole tree once
    per candidate, which was fine for thirty files and quadratic in the thing
    that grows -- and loosening extraction to catch `Config` multiplied the
    candidates.

    Definitions only. Every call site is a longer and less useful answer than
    the one place the thing is declared, and the budget is small.
    """
    if not names:
        return {}
    pats = {n: _definition_patterns(n) for n in names}
    hits: Dict[str, List[str]] = {n: [] for n in names}
    for path in _iter_sources(repo, profile):
        outstanding = [n for n in names if len(hits[n]) < per_name]
        if not outstanding:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        present = [n for n in outstanding if n in text]
        if not present:
            continue
        rel = path.relative_to(repo).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            for name in present:
                if len(hits[name]) >= per_name or name not in line:
                    continue
                if any(p.search(line) for p in pats[name]):
                    hits[name].append(f"{rel}:{i}  {line.strip()[:160]}")
    return {n: v for n, v in hits.items() if v}


def _is_useful_trace(res: Any) -> bool:
    """Does this graph answer say anything, or is it a failure wearing a 200?

    Observed live: `trace_call_path` returns `{"error":"function not found"}` for
    a name it does not know. That is a JSON body, not a string beginning with
    "ERROR", so the first version injected three of them into the prompt under
    the heading "Call paths" -- which does not merely waste tokens, it tells the
    model those symbols do not exist.
    """
    if not res:
        return False
    text = str(res).strip()
    if not text or text.startswith("ERROR"):
        return False
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            return True
        if isinstance(parsed, dict) and ("error" in parsed or not parsed):
            return False
        if isinstance(parsed, list) and not parsed:
            return False
    return True


def _graph_traces(profile: Profile, names: Sequence[str], limit: int = 3) -> List[str]:
    """Optional enrichment. Every failure path returns [], because a graph that
    is down must not take the filesystem findings down with it."""
    if not profile.graph_project:
        return []
    try:
        from .mcp_client import get_mcp_client
        client = get_mcp_client()
        if not client:
            return []
        out: List[str] = []
        for name in list(names)[:limit]:
            try:
                res = client.call_tool("trace_call_path", {
                    "function_name": name,
                    "direction": "both",
                    "project": profile.graph_project,
                    "depth": 1,
                })
            except Exception:
                continue
            if _is_useful_trace(res):
                out.append(f"- {name}: {str(res)[:400]}")
        return out
    except Exception:
        return []


def build_intent_context(
    repo: Path,
    profile: Profile,
    intent: str,
    max_symbols: int = 8,
) -> str:
    """Codebase context for a request that has no regions yet.

    Returns "" when nothing in the tree matches. An empty section is worse than
    no section: it costs tokens and reads as "the codebase has nothing to say
    about this", which is a claim this function is not entitled to make.
    """
    repo = Path(repo)
    if not repo.is_dir():
        return ""

    paths, symbols = split_paths_and_symbols(intent)
    if not paths and not symbols:
        return ""

    budget = max(200, profile.context_token_budget * _CHARS_PER_TOKEN)
    parts: List[str] = []

    named: List[str] = []
    for p in paths:
        if (repo / p).is_file():
            named.append(f"- {p}")
        else:
            named += [
                f"- {q.relative_to(repo).as_posix()}"
                for q in _iter_sources(repo, profile)
                if q.as_posix().endswith(p)
            ][:3]
    if named:
        parts.append(
            "### Files named in the request\n" + "\n".join(dict.fromkeys(named))
        )

    found = _find_symbols(repo, profile, symbols[:max_symbols])
    located = [
        f"- `{name}` -- {hit}"
        for name in symbols[:max_symbols]
        for hit in found.get(name, ())
    ]
    if located:
        parts.append("### Where those names are defined\n" + "\n".join(located))

    traces = _graph_traces(profile, symbols)
    if traces:
        parts.append(
            "### Call paths (from the code knowledge graph)\n" + "\n".join(traces)
        )

    if not parts:
        return ""

    body = "## Codebase context\n" + "\n\n".join(parts) + "\n"
    if len(body) > budget:
        # The notice is counted INSIDE the budget, not added on top of it. A
        # budget that the truncation message can overshoot is not a budget.
        notice = "\n... (truncated to the context budget)\n"
        body = body[: max(0, budget - len(notice))].rstrip() + notice
    return body