# Design Proposal: Code Mode + Graph-Native Exploration for Developer Mode

> **Status:** proposal, not implemented
> **Date:** 2026-08-16
> **Inspiration:** [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Code Mode + codebase-memory-mcp graph
> **Scope:** Developer mode EXPLORE phase. Patch mode is not affected.

---

## 1. Problem

Developer mode's EXPLORE phase uses the same sequential JSON tool-calling
pattern that DSH's Code Mode was designed to replace. The model交互ively
calls `search_code`, `read_file`, and `trace_call_path`, and every
intermediate result dumps into the context window:

```
Turn 1: search_code("parse_date")       → 50 matches (~2000 chars) in context
Turn 2: read_file("src/dates.py")        → 100 lines (~4000 chars) in context
Turn 3: trace_call_path("parse_date")    → trace result (~1500 chars) in context
Turn 4: read_file("src/utils.py", 50)    → 100 lines (~4000 chars) in context
Turn 5: edit_file(...)                    → the actual edit
```

That's **4 round-trips to the model** and ~11,500 chars of intermediate data
in context, to find 5 lines and make one edit. Each round-trip costs:
- Network latency (10-60s per model call)
- Output tokens for the tool-call decision
- Input tokens for the tool result on the next turn
- A turn from the max-turns budget

### What we have but don't use well

The **codebase-memory-mcp graph** is already wired into developer mode as
a tool (`trace_call_path` in `developer/tools.py:253`). But it's called
interactively — one function at a time, results in context — which is the
least efficient way to use a knowledge graph. The graph knows the entire
call structure of the codebase; the model should be able to traverse it
programmatically without spending turns.

### What DSH's Code Mode does

DSH's Code Mode lets the model write a single TypeScript program that calls
tools as async functions. The program runs in a sandboxed worker thread.
Only the `return` value comes back to the model's context. The intermediate
data (500 filenames, 2000-line file contents) exists only in the sandbox's
memory.

```typescript
// Model writes this once, sandbox runs it
const files = sdk.listFiles('src/');
const target = files.find(f => f.includes('auth.ts'));
const content = sdk.readFile(target);
if (content.includes('legacyLogin')) {
    sdk.editFile(target, content.replace('legacyLogin', 'v2Login'));
    return "Updated auth.ts";
}
return "No changes needed";
```

**Key DSH design decisions (from source analysis):**

1. **Error is a field, not a rejection.** `CodeRunResult.error` carries
   failure; `run()` rejects only for contract misuse. A failed program is
   the caller's job to report, not an exception path.

2. **Orthogonal failure taxonomy.** `timeout`, `abort`, `worker-exit`,
   `invalid-output`, `output-limit` are independent outcomes. A budget
   expiry is not an exception; an abort is not a timeout.

3. **Busy-time budget, not wall-time.** DSH meters `eventLoopUtilization()`
   (busy time), not wall time. A program awaiting a slow tool binding
   accrues nothing. A hot loop accrues whether or not a decoy dispatch is
   in flight. Wall-time is a backstop for "awaiting a promise nobody will
   resolve."

4. **Fresh-per-run isolation.** No pooling, no cross-run state. A program's
   world dies with its worker. State bleed is unrepresentable.

5. **Hostile-peer assumption.** The worker runs model code. Every inbound
   message from the worker port is re-validated and rebuilt field-by-field.
   A forged extra field never rides along.

6. **Bindings, not imports.** The model's code receives tool functions as
   injected bindings (a `tools` namespace), not via `import`. The sandbox
   controls what the program can call; the program cannot `import os` or
   `open()`.

7. **Type-strip before spawn.** TypeScript types are stripped
   position-preserving (removed syntax becomes whitespace) so runtime error
   line numbers match the model's source. A syntax failure never spawns a
   worker.

---

## 2. Proposal

### 2.1 A `run_code` tool for developer mode EXPLORE

Add a new tool `run_code` available in the EXPLORE phase only. The model
writes a Python script that calls SDK functions; the sandbox executes it;
the return value goes into context.

```python
# Model writes this once, sandbox runs it
callers = sdk.trace_call_path("parse_date", direction="inbound", depth=2)
results = []
for caller in callers:
    content = sdk.read_file(caller.file)
    lines = [f"{caller.file}:{i}: {l}" for i, l in enumerate(content.splitlines()) if "parse_date" in l]
    if lines:
        results.extend(lines)
return f"Found {len(results)} call sites:\n" + "\n".join(results[:20])
```

One model call, one execution, **one return string in context**. The 50
search results and the file contents never touch the context window.

### 2.2 The SDK

The sandbox exposes a restricted SDK as Python functions injected into the
script's globals. The SDK wraps the existing developer-mode tools + the
codebase-memory-mcp graph client:

| SDK function | Backed by | What it does |
|---|---|---|
| `sdk.read_file(path, start=1, end=None)` | `developer/tools.py:_read_file` | Read a file, windowed |
| `sdk.search_code(pattern, file_pattern=None)` | `developer/tools.py:_search_code` | Grep + graph-augmented ranking |
| `sdk.trace_call_path(function_name, direction="both", depth=2)` | `developer/tools.py:_trace_call_path` | Graph: who calls / what it calls |
| `sdk.search_graph(query, **kwargs)` | `mcp_client.call_tool("search_graph", ...)` | Graph: full-text search over functions/classes |
| `sdk.get_code_snippet(qualified_name)` | `mcp_client.call_tool("get_code_snippet", ...)` | Graph: read function/class source by qualified name |
| `sdk.get_architecture(aspects=None)` | `mcp_client.call_tool("get_architecture", ...)` | Graph: package-level overview |

**Why this is better than interactive `trace_call_path`:**

The graph knows the entire call structure. A script can:
- Trace callers of callers (depth=3) in one call
- Read each caller's source via `get_code_snippet`
- Filter for lines that match a pattern
- Return only the relevant 5 lines

Interactively, this is 5+ turns. With `run_code`, it's 1 turn. And the
graph queries run at CPU speed (the MCP client uses stdio, no network),
not model latency.

### 2.3 The sandbox

**Design:** `subprocess.run([python, "-c", script])` with restricted globals.

**Restriction mechanism:** AST-based import blocking + injected SDK.

```python
import ast

_BLOCKED_NODES = (
    ast.Import,       # import os
    ast.ImportFrom,   # from os import system
)

_BLOCKED_BUILTINS = {
    "open", "exec", "eval", "compile", "__import__",
    "globals", "locals", "vars", "dir",
    "input", "breakpoint", "exit", "quit",
}

def validate_script(source: str) -> list[str]:
    """Return a list of errors, or empty if the script is safe to run."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    errors = []
    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            names = [n.name for n in node.names]
            errors.append(f"import not allowed: {', '.join(names)}")
        # Check for attribute access on blocked builtins
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_BUILTINS:
                errors.append(f"builtin not allowed: {node.func.id}")
    return errors
```

**Execution:**

```python
def run_script(source: str, repo: Path, profile: Profile, timeout: int = 30) -> dict:
    """Run a model-written script in a restricted sandbox.

    Returns {"return": str, "stdout": str, "error": str | None}.
    """
    errors = validate_script(source)
    if errors:
        return {"return": "", "stdout": "", "error": "; ".join(errors)}

    # Build the SDK with the repo and profile bound
    sdk = _build_sdk(repo, profile)

    # Restricted globals: no builtins access to open/exec/eval
    safe_builtins = {
        k: v for k, v in __builtins__.__dict__.items()
        if k not in _BLOCKED_BUILTINS
    }
    safe_globals = {
        "__builtins__": safe_builtins,
        "sdk": sdk,
        "print": _make_print_captor(),  # captures print() output
    }

    # Run in a subprocess for hard isolation (timeout, no shared state)
    # The script's return value is captured via a sentinel.
    wrapped = (
        "import sys\n"
        f"__result = exec(open(sys.argv[1]).read(), "
        f"{{'__builtins__': {safe_builtins_repr}, 'sdk': sdk}})\n"
        f"print('__RESULT__', repr(__result))\n"
    )
    # ... or simpler: exec in-process with a hard timeout via signal/threading
```

**Two isolation options:**

| Option | Pros | Cons |
|---|---|---|
| **In-process exec** (thread + timeout) | Simple, no subprocess overhead, SDK is real Python objects | A hostile script could crash the process; no memory isolation |
| **Subprocess** (`subprocess.run`) | Hard isolation, killable, no state bleed | SDK must be serialized across the process boundary |

**Recommendation:** subprocess. DSH's "fresh-per-run, no cross-run state"
principle applies. The SDK is injected via a bootstrap script that imports
the agent_loop package and constructs the SDK objects — the model's script
is appended as the body. A 30-second timeout kills the process. No network
(the MCP client uses stdio to a local server, not HTTP).

### 2.4 What stays a discrete tool call

`edit_file`, `run_build`, `run_tests`, and `write_test` stay as discrete
tool calls, not SDK functions. Reasons:

1. **Edits must be tracked, gated, and reverted.** The loop's worktree
   isolation, phase transitions (explore→edit), and file-scope checks
   all key off `edit_file` calls. Making edits happen inside a script
   would break the phase machine.

2. **Build/test results must go into context.** The model needs to see
   compiler errors and test failures to know what to fix. These are
   inherently context-visible results, not intermediate data to filter.

3. **TDD phase machine.** The red→explore→edit transition fires on the
   first `edit_file` call. This is a control-flow event, not a data
   query.

`run_code` is available in EXPLORE phase only. Once the model calls
`edit_file`, it transitions to EDIT phase and `run_code` is no longer
offered. This matches DSH's separation: Code Mode for exploration, native
tools for mutation.

### 2.5 Graph-native exploration

The key insight from combining Code Mode + the codebase-memory-mcp graph:

**The graph is a query engine, not a text search.** Used interactively
(one `trace_call_path` per turn), it returns text that dumps into context.
Used programmatically (inside a script), it's a traversal engine that can
answer structural questions in one execution:

```python
# "What functions call parse_date, and which of them are called by
#  anything in the auth module?"
callers = sdk.trace_call_path("parse_date", direction="inbound", depth=1)
auth_callers = []
for c in callers:
    inbound = sdk.trace_call_path(c.name, direction="inbound", depth=1)
    if any("auth" in caller.file for caller in inbound):
        source = sdk.get_code_snippet(c.qualified_name)
        auth_callers.append(f"{c.file}:{c.name}\n{source[:500]}")
return f"Auth-path callers of parse_date:\n" + "\n".join(auth_callers)
```

This is a 2-hop graph traversal with source reading and filtering. Done
interactively, it's 6-8 turns. Done in a script, it's 1 turn with a
10-line return value.

### 2.6 The `return` contract

The script's return value is a string that goes into the model's context
as the tool result. DSH uses `CodeJsonValue` (structured JSON); we use
plain strings because:

1. Our existing tool results are strings (e.g., `read_file` returns
   formatted text, `search_code` returns `file:line: content` lines).
2. The model is good at formatting text for its own consumption.
3. A string return is the simplest contract — no serialization boundary
   to cross in the subprocess.

If the script doesn't call `return`, the captured `print()` output is
returned instead (matching DSH's `logs` field). If neither, an error.

---

## 3. DSH patterns we should borrow

From the source analysis of `deepseek-harness`:

### 3.1 Error as a field, not a rejection

DSH's `CodeRunResult` carries `error` as a field; `run()` rejects only for
contract misuse. Our `run_code` tool should return a structured result:

```python
{
    "return": "",           # the script's return value (string)
    "stdout": "...",        # captured print() output
    "error": {              # present iff the script failed
        "kind": "timeout",  # timeout | abort | exception | invalid-output
        "message": "compute budget exhausted (30s)"
    }
}
```

A timeout is not an exception. A script that throws is not an abort. The
tool result always returns to the model — the model sees the error and
self-corrects, same as any other tool failure.

### 3.2 Busy-time budget

DSH meters busy time (ELU), not wall time, because a program awaiting a
slow tool binding accrues nothing. For our Python subprocess, we use
wall-time as the budget (30s) because Python doesn't have an ELU
equivalent. But the principle matters: if the SDK's graph queries take
10 seconds (MCP stdio round-trip), that should not count against the
model's compute budget. The 30s timeout is a hard ceiling, not a
compute meter.

### 3.3 Fresh-per-run

No pooling, no cross-run state. Each `run_code` call spawns a fresh
subprocess. State bleed is unrepresentable. This is cheap on Windows
(python startup ~200ms) and correct by construction.

### 3.4 Hostile-peer assumption

The model's script is untrusted. The AST validator blocks `import`,
`open`, `exec`, `eval`, `compile`, `__import__`. The subprocess has no
network access (the MCP client uses stdio, but the script doesn't have
access to the MCP client directly — it goes through the SDK). The
subprocess runs with the same user permissions as the loop, so file
reads are bounded by the OS, not by us.

### 3.5 Compaction: checkpoint framing

DSH's compaction wraps summaries in `<compacted-summary>` tags with a
checkpoint preamble so consumers recognize them. Our compaction
(`compaction.py`) uses truncation markers. The DSH pattern of a
structured summary directive (8 sections: Primary Request, Key Concepts,
Files and Code, Errors and Fixes, Pending Jobs, Current Work, Next Step,
Critical Context) is worth borrowing for Phase 4b compaction — it gives
the summarizer a deterministic structure instead of freeform prose.

### 3.6 SDK codegen from tool schemas

DSH generates the model-visible SDK (TypeScript `.d.ts` or Python `.pyi`)
from the tool registry's JSON schemas. The SDK is injected into the prompt
as a `tools:sdk` section. We should do the same: generate the SDK
signature from the tool definitions in `developer/tools.py` so the model
sees exactly what it can call, with types and descriptions.

---

## 4. What NOT to borrow

### 4.1 Cordis plugin architecture

DSH's "everything is a plugin" with capability seams, scoped registrations,
and 4 dispatch modes (waterfall/serial/emit/parallel) is 79 packages of
TypeScript infrastructure. Our loop is a single Python package with a
fixed pipeline. The plugin architecture is the right design for a
general-purpose harness; it is overkill for a batch pipeline that does
implement→gate→review→arbitrate→apply.

### 4.2 Session event log as single source of truth

DSH's append-only event log where messages are derived, not stored, is
architecturally superior to our dual-write (round records + artifacts +
ledger). But adopting it is a rearchitecture of the entire loop, not a
developer-mode enhancement. File as a future direction, not a current
proposal.

### 4.3 V8 worker thread isolation

DSH uses Node.js `Worker` with `resourceLimits.maxOldGenerationSizeMb`,
empty env, and captured stdout/stderr. We use a Python subprocess. The
isolation is weaker (no heap limit, no empty env by default) but
sufficient for a development tool that runs locally, not a multi-tenant
service.

---

## 5. Implementation plan

### 5.1 Files

| File | Lines (est.) | What |
|---|---|---|
| `developer/code_sandbox.py` | ~250 | AST validator, SDK builder, subprocess executor, result capture |
| `developer/tools.py` | +40 | `run_code` tool schema, dispatch to `code_sandbox.run_script` |
| `developer/driver.py` | +15 | Add `run_code` to `EXPLORE_TOOLS`, wire dispatch |
| `tests/acceptance/test_code_sandbox.py` | ~120 | Tests: AST validation, SDK functions, timeout, error handling |

### 5.2 Changes to existing files

**`developer/driver.py`:**
- Add `"run_code"` to `EXPLORE_TOOLS`
- Add dispatch case in the tool execution loop
- The `run_code` result is a string, same as any other tool result

**`developer/tools.py`:**
- Add `run_code` tool schema (two params: `code` string, `description` string)
- Add dispatch: `elif tool_name == "run_code": return _run_code(repo, args, profile)`

**`developer/code_sandbox.py` (new):**
- `validate_script(source) -> list[str]` — AST-based import/builtin blocking
- `build_sdk(repo, profile) -> Sdk` — constructs the SDK object with bound repo/profile
- `run_script(source, repo, profile, timeout=30) -> dict` — subprocess execution
- `Sdk` class with `read_file`, `search_code`, `trace_call_path`, `search_graph`, `get_code_snippet`, `get_architecture`

### 5.3 Tests

1. **AST validation blocks imports:** `import os` → error
2. **AST validation blocks builtins:** `open("file")` → error
3. **SDK read_file works:** script reads a file, returns content
4. **SDK search_code works:** script searches, returns matches
5. **SDK trace_call_path works:** script traces, returns callers (mocked MCP)
6. **Timeout kills the process:** infinite loop → timeout error
7. **Script exception is captured:** `1/0` → error with traceback
8. **Return value is captured:** `return "hello"` → `{"return": "hello"}`
9. **Print output is captured:** `print("hi"); return "done"` → both captured
10. **No access to real builtins:** `open(__file__)` → blocked

### 5.4 MCP integration

The SDK's graph functions (`trace_call_path`, `search_graph`,
`get_code_snippet`, `get_architecture`) call the codebase-memory-mcp
client directly, same as `developer/tools.py:_trace_call_path` does now.
The difference is that they're called inside a script, not as a
text-protocol tool call. The MCP client is a stdio-based subprocess
that the agent_loop already manages (`mcp_client.py`).

The sandbox subprocess does NOT have direct access to the MCP client.
The SDK functions are closures that call the MCP client in the parent
process and pass results back to the subprocess via stdout. This keeps
the MCP client's state in the parent, not in the sandbox.

**Implementation detail:** the sandbox runs as a subprocess, but the SDK
functions need to call the MCP client in the parent. Two options:

1. **Pre-resolve:** the script is analyzed for SDK calls, all graph
   queries are resolved before execution, results are injected as
   constants. (Simple but can't handle dynamic queries.)

2. **IPC bridge:** the subprocess communicates SDK calls back to the
   parent via stdout/stdin JSON lines. The parent resolves them against
   the MCP client and sends results back. (More complex but supports
   dynamic queries.)

**Recommendation:** option 2 (IPC bridge). The subprocess writes a JSON
line to stdout for each SDK call; the parent reads it, resolves it, and
writes the result back as a JSON line to stdin. The SDK functions block
on reading the response. This is the same pattern as DSH's worker port,
simplified to JSON lines.

---

## 6. Expected impact

### 6.1 Context savings

A typical EXPLORE phase that currently takes 4-6 turns and ~12,000 chars
of intermediate data would take 1-2 turns and ~500 chars (the return
value). That's a **10-24x reduction in context usage** for the explore
phase.

### 6.2 Latency savings

Each turn costs 10-60s of model latency. Collapsing 4 explore turns to 1
saves 30-180s per ticket. The sandbox execution itself is <1s for file
reads and <5s for graph queries (MCP stdio round-trip).

### 6.3 Graph utilization

The codebase-memory-mcp graph is currently used as a text search (one
function at a time, results in context). With `run_code`, it becomes a
traversal engine — the model can write 2-hop and 3-hop queries that
answer structural questions ("who calls the callers of parse_date?")
in one execution instead of 6-8 interactive turns.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Model writes a hostile script | AST validator blocks imports/builtins; subprocess isolation; 30s timeout |
| Script crashes the subprocess | Caught as `error.kind: "exception"`; model sees traceback and self-corrects |
| Graph queries are slow (MCP round-trip) | 30s timeout; graph queries are typically <5s |
| Model writes code that doesn't use the SDK | `return` value is empty; model sees "no output" and adapts |
| IPC bridge adds complexity | Start with option 1 (pre-resolve) for MVP; upgrade to option 2 when dynamic queries are needed |
| Phase machine breaks | `run_code` is EXPLORE-only; calling `edit_file` still transitions to EDIT |

---

## 8. Future directions (not in this proposal)

1. **Append-only event log** (DSH's session log pattern): replace
   dual-write (round records + artifacts + ledger) with a single
   append-only event log. Messages are derived, not stored. Replay is
   re-derivation.

2. **Monotonic guards** for all extension points: generalize the arbiter's
   BLOCKER rule (can't dismiss a BLOCKER and recommend SHIP) to all
   waterfall-style decisions. A guard can deny but never re-allow.

3. **`concludesTurn` data flag**: instead of `final = "APPROVE"; break`,
   attach a `concludes_turn: True` flag to the panel/arbiter result
   object. Cleaner control flow, composable with future extension points.

4. **Compaction checkpoint framing**: adopt DSH's 8-section summary
   directive for Phase 4b compaction. Deterministic structure instead of
   freeform prose.

5. **Code Mode for plan mode**: the planner decomposes a feature into
   tickets. A script could analyze the codebase graph to propose
   decomposition boundaries (files, functions, dependency edges) in one
   execution instead of interactive exploration.