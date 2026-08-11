"""The interpreter a shelled-out test command must use.

Tests that build a `build_cmd`/`test_cmd` shell out to `python`, and PATH decides
which one that is -- not the interpreter running the suite. On the consumer's
3.12 venv bare `python` resolved to a 3.14 install with no pytest, and eight
developer-mode tests failed for that reason alone while the same suite was green
on the dev interpreter. That is the same shape as O9: a defect invisible on the
machine it was written on.

Quoted because `Workspace.run` uses `shell=True` and `sys.executable` may contain
spaces. Concatenate rather than f-string at the call sites -- several of these
commands carry a literal `{files}` placeholder that the gate substitutes.
"""
import sys

PY_EXE = f'"{sys.executable}"'
