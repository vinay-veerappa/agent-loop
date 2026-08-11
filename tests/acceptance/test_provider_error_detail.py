"""A transport failure must report what happened, and how many tries it took.

Live: a plan run died with

    qwen3.5:cloud failed after 3 attempts: HTTPError: HTTP Error 400: Bad Request

Two lies in one line. `_retryable` correctly excludes 400, so exactly ONE call
was made -- "3 attempts" sends the reader hunting a flaky endpoint. And the
response body, which said

    max_tokens (96000) exceeds model's maximum output tokens (65536) for qwen3.5

was discarded, so the one thing that would have fixed the run in a single edit
was thrown away.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from agent_loop import providers


def _http_error(code: int, body: bytes = b"", reason: str = "Bad Request"):
    return urllib.error.HTTPError(
        url="http://x/api/chat", code=code, msg=reason, hdrs=None, fp=io.BytesIO(body)
    )


def test_http_error_body_is_included():
    body = b'{"error":"max_tokens (96000) exceeds model\'s maximum output tokens (65536)"}'
    out = providers.describe_exception(_http_error(400, body))
    assert "400" in out
    assert "65536" in out, "the actionable part of the body was dropped"


def test_http_error_without_a_body_still_reads_cleanly():
    out = providers.describe_exception(_http_error(503, b"", reason="Service Unavailable"))
    assert "503" in out
    assert "Service Unavailable" in out
    assert out.rstrip().endswith("Service Unavailable"), "a dangling separator was left"


def test_a_body_that_cannot_be_read_is_not_a_new_failure():
    class _Hostile(urllib.error.HTTPError):
        def read(self, *a, **k):
            raise OSError("stream already consumed")

    exc = _Hostile("http://x", 400, "Bad Request", None, io.BytesIO(b""))
    out = providers.describe_exception(exc)  # must not raise
    assert "400" in out


def test_non_http_exceptions_keep_their_type():
    out = providers.describe_exception(TimeoutError("timed out"))
    assert "TimeoutError" in out and "timed out" in out


def test_a_non_retryable_error_reports_one_attempt(monkeypatch):
    calls = {"n": 0}

    def _backend(*a, **k):
        calls["n"] += 1
        raise _http_error(400, b'{"error":"max_tokens too big"}')

    monkeypatch.setitem(providers._BACKENDS, "ollama", _backend)

    with pytest.raises(providers.ProviderError) as exc:
        providers.chat("ollama:stub", [{"role": "user", "content": "x"}], max_retries=3)

    assert calls["n"] == 1, "a 400 must not be retried"
    msg = str(exc.value)
    assert "1 attempt" in msg, f"claimed the wrong attempt count: {msg}"
    assert "3 attempts" not in msg
    assert "max_tokens too big" in msg


def test_a_retryable_error_reports_every_attempt(monkeypatch):
    calls = {"n": 0}

    def _backend(*a, **k):
        calls["n"] += 1
        raise _http_error(503, b"overloaded", reason="Service Unavailable")

    monkeypatch.setitem(providers._BACKENDS, "ollama", _backend)
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)

    with pytest.raises(providers.ProviderError) as exc:
        providers.chat("ollama:stub", [{"role": "user", "content": "x"}], max_retries=3)

    assert calls["n"] == 3
    assert "3 attempts" in str(exc.value)
