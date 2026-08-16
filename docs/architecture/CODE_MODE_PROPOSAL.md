# Design Proposal: Code Mode + Graph-Native Exploration for Developer Mode

> **Status:** proposal, not implemented (revised after first review)
> **Date:** 2026-08-16
> **Inspiration:** [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Code Mode + codebase-memory-mcp graph
> **Scope:** Developer mode EXPLORE phase. Patch mode is not affected.

---

## 1. Problem

Developer mode's EXPLORE phase uses the same sequential JSON tool-calling
pattern that DSH's Code Mode was designed to replace. The model interactively
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

5. **Semi-trusted peer, not hostile.** The worker runs model code with
   bash-equivalent authority. Containment is about process isolation and
   state bleed, not a security boundary against a determined adversary.
   Deployments that need hard isolation use a container-class backend.

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

The sandbox exposes a restricted SDK as Python functions. The SDK wraps the
existing developer-mode tools + the codebase-memory-mcp graph client. The
SDK functions are **stubs in the subprocess that marshal calls over a
JSON-line IPC bridge to the parent** process, which resolves them against
the real tool implementations and the MCP client. The subprocess never has
direct access to the MCP client, the file system, or the network — only
to the IPC bridge.

| SDK function | Backed by (parent) | What it does |
|---|---|---|
| `sdk.read_file(path, start=1, end=None)` | `developer/tools.py:_read_file` | Read a file, windowed. Note: the existing `_read_file` accepts `start_line`/`end_line` (not `start`/`end`); the SDK adapter renames. |
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

Interactively, this is 5+ turns. With `run_code`, it's 1 turn. The graph
queries run via stdio IPC to the MCP client — a per-call round-trip cost
measured in milliseconds, not the 10-60s of a model round-trip.

### 2.3 The sandbox

**Threat model:** the model's script is **semi-trusted**. It is written by
a coding model that may produce buggy logic, not by a determined adversary.
The sandbox provides **process isolation** (no shared state, killable,
timeout-bounded) and **namespace restriction** (no `import`, no `open()`,
no network). It does NOT provide a security boundary against a hostile
script — a determined script could escape via Python introspection. For
a hard boundary, a container-class backend (chroot/Landlock/Seatbelt) would
be needed, which is out of scope for this proposal. The blast radius of a
sandbox escape is: any file the user can read. This is the same authority
the existing `read_file` tool already grants.

**Design:** subprocess with AST-based restriction + restricted builtins +
JSON-line IPC bridge for SDK calls.

**AST validator (revised — closes the `getattr` bypass):**

The validator blocks three categories of access:

1. **Import statements** — `import` and `from X import Y` are blocked
   unconditionally. No module may be loaded.

2. **Dangerous builtins by name** — `open`, `exec`, `eval`, `compile`,
   `__import__`, `globals`, `locals`, `vars`, `dir`, `input`, `breakpoint`,
   `exit`, `quit`, **`getattr`, `setattr`, `delattr`, `type`**,
   `__build_class__`. These are blocked both as direct calls (`open(...)`)
   and as attribute access targets (`__builtins__.open`).

3. **Introspection chains** — the validator walks `ast.Attribute` chains
   and rejects any that reach: `__builtins__`, `__class__`, `__bases__`,
   `__subclasses__`, `__globals__`, `__dict__`, `__mro__`, `__dir__`,
   `__getattr__`. This blocks the `().__class__.__base__.__subclasses__()`
   escape that recovers `os`/`subprocess`/`_io.FileIO`.

```python
import ast

_BLOCKED_NODES = (
    ast.Import,       # import os
    ast.ImportFrom,   # from os import system
)

# Blocked as direct calls AND as attribute access targets
_BLOCKED_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__",
    "globals", "locals", "vars", "dir",
    "input", "breakpoint", "exit", "quit",
    "getattr", "setattr", "delattr", "type",
    "__build_class__",
})

# Blocked attribute names — any chain reaching these is rejected
_BLOCKED_ATTRS = frozenset({
    "__builtins__", "__class__", "__bases__", "__subclasses__",
    "__globals__", "__dict__", "__mro__", "__dir__", "__getattr__",
    "__getattribute__",
})


def validate_script(source: str) -> list[str]:
    """Return a list of errors, or empty if the script is safe to run.

    Blocks: imports, dangerous builtins, and introspection chains.
    This is a namespace restriction, not a security boundary — a
    determined adversary can escape Python's object model. The blast
    radius is documented in the threat model above.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    errors: list[str] = []
    for node in ast.walk(tree):
        # 1. Block all import statements
        if isinstance(node, _BLOCKED_NODES):
            names = [n.name for n in node.names]
            errors.append(f"import not allowed: {', '.join(names)}")
            continue

        # 2. Block dangerous builtins called by name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _BLOCKED_NAMES:
                errors.append(f"builtin not allowed: {node.func.id}")

        # 3. Block attribute access into introspection chains
        if isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_ATTRS:
                errors.append(
                    f"attribute access not allowed: .{node.attr} "
                    f"(introspection chain blocked)"
                )
    return errors
```

**Restricted builtins (revised — uses `import builtins`, not `__builtins__`):**

```python
import builtins as _builtins

def _safe_builtins() -> dict:
    """Construct a safe builtins dict from the real builtins module.

    Uses `import builtins` (the module), not `__builtins__` (which is a
    dict proxy in non-__main__ modules and raises AttributeError on
    .__dict__). Filters out dangerous names.
    """
    return {
        k: v for k, v in vars(_builtins).items()
        if k not in _BLOCKED_NAMES
    }
```

### 2.4 Return capture (revised — wraps script in a function)

The model's script is wrapped in a function so `return` is legal. The
wrapper calls the function, captures the return value, and emits it as a
sentinel line on stdout. The parent parses the sentinel.

**The wrapper (generated by the parent, not written by the model):**

```python
# Parent constructs this wrapper around the model's script body.
# The model's script is inserted as the body of __user_script.
# `return` is legal inside __user_script because it's a function body.

WRAPPER_TEMPLATE = '''
import json, sys

# --- safe builtins (injected by parent) ---
{safe_builtins_assignment}

# --- SDK stubs (injected by parent) ---
{sdk_stubs}

# --- model script (wrapped in a function so return is legal) ---
def __user_script():
{model_script_indented}

# --- execute and capture return value ---
try:
    __result = __user_script()
except Exception as e:
    import traceback
    print("__ERROR__" + json.dumps({{
        "kind": "exception",
        "message": str(e),
        "traceback": traceback.format_exc(),
    }}))
    sys.exit(0)

# Emit the return value as a sentinel line.
print("__RESULT__" + json.dumps({{"value": __result}}))
'''
```

**How the parent runs it:**

```python
def run_script(source: str, repo: Path, profile: Profile, timeout: int = 30) -> dict:
    """Run a model-written script in a restricted sandbox.

    Returns {"return": str, "stdout": str, "error": dict | None}.
    """
    errors = validate_script(source)
    if errors:
        return {"return": "", "stdout": "", "error": {
            "kind": "invalid-script", "message": "; ".join(errors)
        }}

    # Indent the model script to be the body of __user_script
    indented = "\n".join("    " + line for line in source.splitlines())
    wrapper = WRAPPER_TEMPLATE.format(
        safe_builtins_assignment=_render_safe_builtins(),
        sdk_stubs=_render_sdk_stubs(),
        model_script_indented=indented,
    )

    # Run in a subprocess with timeout
    proc = subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True, text=True,
        timeout=timeout,
        encoding="utf-8", errors="replace",
        cwd=str(repo),
        env={},  # empty env — no PATH, no network config
    )

    # Parse the sentinel from stdout
    stdout = proc.stdout
    return_value = ""
    error = None

    for line in stdout.splitlines():
        if line.startswith("__RESULT__"):
            payload = line[len("__RESULT__"):]
            return_value = json.loads(payload).get("value", "")
        elif line.startswith("__ERROR__"):
            payload = line[len("__ERROR__"):]
            error = json.loads(payload)

    if proc.returncode != 0 and not error:
        error = {"kind": "crash", "message": proc.stderr[:2000]}

    return {"return": str(return_value), "stdout": stdout, "error": error}
```

**Distinguishing error kinds:**

| `error.kind` | Cause | What the model sees |
|---|---|---|
| `invalid-script` | AST validator blocked import/builtin/attribute | The validator's error message |
| `exception` | Script raised an exception | Traceback (first 2000 chars) |
| `crash` | Subprocess exited non-zero without sentinel | stderr (first 2000 chars) |
| `timeout` | `subprocess.run` raised `TimeoutExpired` | "compute budget exhausted (30s)" |

A `TimeoutExpired` is caught by the caller and converted to the same
`error` dict shape.

### 2.5 What stays a discrete tool call

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

**Phase machine and `run_code` (revised — addresses the race):**

`run_code` is available in EXPLORE phase only. The offered-tool list is
rebuilt at the **start of each turn** from the current phase, and a
`run_code` call is dispatched only if the phase at dispatch time is
`explore`. If the model calls `edit_file` and `run_code` in the same
turn, the tool dispatcher processes calls in order: `edit_file` fires
the phase transition to `edit`, and the subsequent `run_code` is
**rejected with a message** ("not available in edit phase") rather
than executed. This is the same mechanism already used for rejected
tool calls (`developer/driver.py:274-300`).

### 2.6 Graph-native exploration

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
for caller in callers:
    inbound = sdk.trace_call_path(caller.name, direction="inbound", depth=1)
    if any("auth" in caller.file for caller in inbound):
        source = sdk.get_code_snippet(caller.qualified_name)
        auth_callers.append(f"{c.file}:{c.name}\n{source[:500]}")
return f"Auth-path callers of parse_date:\n" + "\n".join(auth_callers)
```

This is a 2-hop graph traversal with source reading and filtering. Done
interactively, it's 6-8 turns. Done in a script, it's 1 turn with a
10-line return value.

### 2.7 The `return` contract

The script's return value is a string that goes into the model's context
as the tool result. DSH uses `CodeJsonValue` (structured JSON); we use
plain strings because:

1. Our existing tool results are strings (e.g., `read_file` returns
   formatted text, `search_code` returns `file:line: content` lines).
2. The model is good at formatting text for its own consumption.
3. A string return avoids crossing a JSON serialization boundary in the
   IPC — the return value is already a string by the time the parent
   parses the `__RESULT__` sentinel.

If the script doesn't call `return` (or returns `None`), the captured
`print()` output (stdout minus the sentinel lines) is returned instead.
If neither, an error ("no output and no return value").

---

## 3. DSH patterns we should borrow

From the source analysis of `deepseek-harness`:

### 3.1 Error as a field, not a rejection

DSH's `CodeRunResult` carries `error` as a field; `run()` rejects only for
contract misuse. Our `run_code` tool returns a structured result:

```python
{
    "return": "",           # the script's return value (string)
    "stdout": "...",        # captured print() output (minus sentinels)
    "error": {              # present iff the script failed
        "kind": "timeout",  # timeout | crash | exception | invalid-script
        "message": "compute budget exhausted (30s)"
    }
}
```

A timeout is not an exception. A script that throws is not a crash. The
tool result always returns to the model — the model sees the error and
self-corrects, same as any other tool failure.

### 3.2 Busy-time budget

DSH meters busy time (ELU), not wall time, because a program awaiting a
slow tool binding accrues nothing. For our Python subprocess, we use
wall-time as the budget (30s) because Python doesn't have an ELU
equivalent. But the principle matters: if the SDK's graph queries take
10 seconds (MCP stdio round-trip), the 30s budget is a hard ceiling on
the whole execution, not a compute meter. The model's context budget is
not charged for time spent in the sandbox.

### 3.3 Fresh-per-run

No pooling, no cross-run state. Each `run_code` call spawns a fresh
subprocess. State bleed is unrepresentable. Python startup on Windows is
~500ms-1s (higher than the ~200ms estimate for Linux); this is acceptable
given the 30-180s of model latency it saves.

### 3.4 Semi-trusted peer (revised — not "hostile-peer")

DSH's Code Mode runs model code with bash-equivalent authority. Its
isolation is containment, not a security boundary — `worker.terminate()`
stops the thread but not OS processes it spawned. The DSH agent note
explicitly says: "Deployments that need a hard multi-tenant boundary need
a container-class backend for both code and bash."

We adopt the same posture. The sandbox provides:
- **Namespace restriction** (no `import`, no `open()`, no `getattr`/`type`
  introspection chains)
- **Process isolation** (fresh subprocess, killable, timeout-bounded)
- **No environment** (empty `env={}` — no PATH, no network config)
- **Restricted builtins** (filtered from the real `builtins` module)

It does NOT provide:
- A security boundary against a determined adversary (Python's object
  model is escapable via introspection chains that the AST validator may
  not catch)
- Network isolation at the OS level (a script that bypasses the validator
  could `import socket`)
- Filesystem isolation (the subprocess runs with user permissions and
  can read any file the user can read — same as `read_file` already grants)

**The blast radius of a sandbox escape is: any file the user can read.**
This is explicitly the same authority the existing `read_file` tool grants.
A script that escapes the namespace restriction gains nothing it couldn't
get by calling `read_file` interactively. The sandbox is about preventing
accidental damage (a buggy script that writes files, spawns processes, or
hangs), not about defending against a model that's actively trying to
escape.

### 3.5 Compaction: checkpoint framing

DSH's compaction wraps summaries in `<compacted-summary>` tags with a
checkpoint preamble so consumers recognize them. Our compaction
(`compaction.py`) uses truncation markers. The DSH pattern of a
structured summary directive (8 sections: Primary Request, Key Concepts,
Files and Code, Errors and Fixes, Pending Jobs, Current Work, Next Step,
Critical Context) is worth borrowing for Phase 4b compaction — it gives
the summarizer a deterministic structure instead of freeform prose. This
is a future direction, not part of this proposal; it would need
measurement against the existing truncation approach before adoption.

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
| `developer/code_sandbox.py` | ~350 | AST validator, safe builtins, wrapper template, subprocess executor, IPC bridge, SDK stubs |
| `developer/tools.py` | +40 | `run_code` tool schema, dispatch to `code_sandbox.run_script` |
| `developer/driver.py` | +15 | Add `run_code` to `EXPLORE_TOOLS`, wire dispatch, reject after phase flip |
| `tests/acceptance/test_code_sandbox.py` | ~200 | Tests (see §5.3) |

### 5.2 Changes to existing files

**`developer/driver.py`:**
- Add `"run_code"` to `EXPLORE_TOOLS`
- Add dispatch case in the tool execution loop
- The `run_code` result is a string, same as any other tool result
- The existing rejected-tool mechanism handles the phase-flip race: if
  `edit_file` transitions to EDIT and `run_code` was called in the same
  turn, `run_code` is rejected with "not available in edit phase"

**`developer/tools.py`:**
- Add `run_code` tool schema (two params: `code` string, `description` string)
- Add dispatch: `elif tool_name == "run_code": return _run_code(repo, args, profile)`

**`developer/code_sandbox.py` (new):**
- `validate_script(source) -> list[str]` — AST validator (imports, builtins, attribute chains)
- `_safe_builtins() -> dict` — filtered builtins from `import builtins`
- `run_script(source, repo, profile, timeout=30) -> dict` — subprocess execution with wrapper
- `WRAPPER_TEMPLATE` — wraps model script in `def __user_script(): ...`
- SDK stubs that marshal calls over JSON-line IPC to the parent

### 5.3 Tests

All tests will be landed in the same PR as the implementation. The
proposal makes no behavioral claim that is not verified by a test:

1. **AST validation blocks imports:** `import os` → error
2. **AST validation blocks builtins by name:** `open("file")` → error
3. **AST validation blocks `getattr` bypass:** `getattr(__builtins__, 'open')` → error
4. **AST validation blocks introspection chains:** `().__class__.__base__.__subclasses__()` → error
5. **AST validation blocks `type` calls:** `type("x", (), {})` → error
6. **Safe builtins construction works:** `import builtins` in the test, filter, verify `open` is absent
7. **SDK read_file works:** script reads a file via `sdk.read_file`, returns content
8. **SDK search_code works:** script searches via `sdk.search_code`, returns matches
9. **SDK trace_call_path works:** script traces via `sdk.trace_call_path`, returns callers (mocked MCP)
10. **Timeout kills the process:** `while True: pass` → timeout error
11. **Script exception is captured:** `1/0` → error with traceback
12. **Return value is captured:** `return "hello"` → `{"return": "hello"}`
13. **Print output is captured:** `print("hi"); return "done"` → both captured
14. **No return value → print output used:** `print("hi")` (no return) → `{"return": "hi"}`
15. **Empty env:** subprocess launched with `env={}`, verify no PATH leakage
16. **Phase-flip rejection:** `run_code` called after `edit_file` in same turn → rejected

### 5.4 MCP integration (revised — IPC bridge is the only mechanism)

The SDK's graph functions (`trace_call_path`, `search_graph`,
`get_code_snippet`, `get_architecture`) call the codebase-memory-mcp
client in the **parent process**, not in the subprocess. The subprocess
has SDK stubs that marshal calls over a JSON-line IPC bridge:

```
Subprocess stdout → parent reads JSON line → parent resolves SDK call
                                                      ↓
Subprocess stdin  ← parent writes JSON line ← parent sends result
```

**Protocol:**

Each SDK call from the subprocess writes a JSON line to stdout:
```json
{"__sdk_call__": "read_file", "args": {"path": "src/dates.py", "start": 1}}
```

The parent reads this line, resolves it against the real implementation
(`_read_file(repo, args)` or `mcp_client.call_tool(...)`), and writes the
result back to the subprocess's stdin as a JSON line:
```json
{"__sdk_result__": "File: src/dates.py (lines 1-100 of 250)\n    1: ..."}
```

The SDK stub in the subprocess blocks on reading this line. This is the
same pattern as DSH's worker port, simplified to JSON lines over stdio.

**Why the IPC bridge (not closures):**

Closures cannot cross a `subprocess.run` boundary. The subprocess is a
separate Python process — it has its own memory space, its own module
state, its own `__builtins__`. The SDK functions in the subprocess are
stubs that communicate with the parent via the IPC bridge. The parent
resolves the calls against the real tool implementations and the MCP
client. This keeps the MCP client's state in the parent, not in the
sandbox.

**Why not pre-resolve:**

Pre-resolving (analyzing the script for SDK calls and resolving them
before execution) doesn't work for dynamic queries — the script's SDK
calls depend on runtime values (e.g., `sdk.read_file(caller.file)` where
`caller` comes from a prior `trace_call_path` result). The IPC bridge
supports dynamic queries at the cost of a per-call round-trip (typically
<10ms for file reads, <100ms for MCP stdio queries).

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
reads and <5s for graph queries (MCP stdio round-trip, measured in
milliseconds per call).

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
| Model writes a script that escapes the namespace | AST validator blocks imports, builtins, and introspection chains. Blast radius is documented: same as `read_file` (any file the user can read). |
| Model writes a hostile script that imports `socket` | The validator blocks `import` unconditionally. `socket` cannot be imported. A bypass via introspection chains is possible but gains only what `read_file` already grants. |
| Script crashes the subprocess | Caught as `error.kind: "crash"` or `error.kind: "exception"`; model sees traceback and self-corrects |
| Graph queries are slow (MCP round-trip) | 30s timeout; graph queries are typically <5s |
| Model writes code that doesn't use the SDK | `return` value is empty; model sees "no output" and adapts |
| Phase machine race | `run_code` is rejected if phase has flipped to `edit` by the time it dispatches (same mechanism as existing rejected tools) |
| Subprocess startup latency on Windows | ~500ms-1s per call; acceptable given 30-180s of model latency saved |

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
   freeform prose. Needs measurement against the existing approach.

5. **Code Mode for plan mode**: the planner decomposes a feature into
   tickets. A script could analyze the codebase graph to propose
   decomposition boundaries (files, functions, dependency edges) in one
   execution instead of interactive exploration.

6. **Container-class sandbox backend**: for deployments that need hard
   isolation (multi-tenant, untrusted models), add a container backend
   (chroot on Linux, restricted token on Windows) behind the same `run_code`
   interface. The current proposal is semi-trusted; this upgrades it to
   hostile-peer without changing the SDK.