"""Suite-wide fixtures.

The CF-40 startup probe makes one real provider call per configured model from
`cli.main()`, and more than a dozen tests drive `main()`. None of them may reach
a real provider: on a box with Ollama running they would silently spend calls,
and in CI they would each wait on a connection. The probe's own tests turn it
back on with a stubbed `chat`.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_startup_probe(monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_NO_PROBE", "1")
