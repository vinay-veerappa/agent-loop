"""
providers.py
============
Thin multi-provider chat shim for the patch loop. No third-party dependencies.

Why not LiteLLM: this loop makes ~3 calls per round against a handful of models.
A ~200-line shim buys the same retry/cost/provider coverage without pulling a
large transitive dependency tree into the project venv.

Every backend returns the same `Completion`, so the loop never branches on
provider. Transport failures raise `ProviderError` and are distinguishable from
a model that answered — the old loop conflated the two, which meant one dead
reviewer could permanently block APPROVE.

Model naming: "<backend>:<model>", e.g.
    ollama:kimi-k2.7-code:cloud
    anthropic:claude-opus-5
    openai:gpt-oss-120b            (any OpenAI-compatible /v1/chat/completions)
A bare name with no recognised prefix defaults to ollama, so existing ticket
files and CLI flags keep working unchanged.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ANTHROPIC_VERSION = "2023-06-01"

# USD per 1M tokens (input, output). Anthropic rates as of 2026-06-24; Ollama
# cloud models are billed by subscription, so they cost nothing per token here.
PRICING: Dict[str, Tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Anthropic rejects temperature/top_p/top_k with a 400 on every current model
# (Opus 5, Sonnet 5, Fable 5, Opus 4.7+). The loop asks for temperature=0.1 to
# keep the implementer deterministic; on these models that request is dropped
# rather than sent, and determinism is bought with effort=low instead.
_SAMPLING_REJECTED = re.compile(
    r"^claude-(fable-5|mythos-5|opus-5|opus-4-(7|8)|sonnet-5)"
)


class ProviderError(RuntimeError):
    """Transport-level failure after retries. NOT a verdict from a model."""


@dataclass
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = ""
    secs: float = 0.0
    # Reasoning models bill their chain of thought as output tokens. Tracked so
    # a reviewer that spends its whole budget thinking is visible in the logs.
    thinking_chars: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        bare = self.model.split(":", 1)[-1] if self.model.startswith("anthropic:") else self.model
        rate = PRICING.get(bare)
        if not rate:
            return 0.0
        # Cache reads bill at ~0.1x input; treat uncached input at full rate.
        return (
            self.input_tokens * rate[0] + self.cache_read_tokens * rate[0] * 0.1
        ) / 1e6 + self.output_tokens * rate[1] / 1e6

    def usage_line(self) -> str:
        cost = f" ${self.cost_usd:.4f}" if self.cost_usd else ""
        think = f" think={self.thinking_chars}c" if self.thinking_chars else ""
        return (
            f"{self.model} {self.secs:.1f}s "
            f"in={self.input_tokens} out={self.output_tokens}{think}{cost}"
        )


def split_model(spec: str) -> Tuple[str, str]:
    """'anthropic:claude-opus-5' -> ('anthropic', 'claude-opus-5').

    Ollama model names contain colons themselves ('kimi-k2.7-code:cloud'), so
    only a known backend prefix is treated as one.
    """
    for backend in ("anthropic", "openai", "ollama"):
        if spec.startswith(backend + ":"):
            return backend, spec[len(backend) + 1 :]
    return "ollama", spec


def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # 408 timeout, 409 conflict, 429 rate limit, 5xx server. A 400/401/404
        # is a bug in our request and will fail identically on every retry.
        return exc.code in (408, 409, 429) or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _ollama_host() -> str:
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    if not host.startswith("http"):
        host = f"http://{host}"
    host = host.replace("0.0.0.0", "127.0.0.1")
    if host.count(":") == 1:
        host = f"{host}:11434"
    return host


def _call_ollama(model, messages, temperature, max_tokens, timeout, num_ctx, think=None):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # num_predict was previously omitted, so max_tokens was silently ignored
        # and the budget was whatever the server defaulted to.
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": max_tokens},
    }
    # think=False disables chain-of-thought on reasoning models. Worth doing
    # wherever the caller wants a structured answer rather than deliberation:
    # on a T2-sized review deepseek-v4-pro spends ~90k chars thinking and then
    # has no budget left to answer, versus 21s and a full set of findings with
    # thinking off. `think="low"` is not honoured -- it reasons at full length.
    if think is not None:
        payload["think"] = think
    data = _post(
        f"{_ollama_host()}/api/chat", payload, {"Content-Type": "application/json"}, timeout
    )
    msg = data.get("message", {}) or {}
    text = msg.get("content", "") or ""
    thinking = msg.get("thinking", "") or ""

    # Reasoning models return their chain of thought in `thinking` and the
    # answer in `content`. When the output budget is exhausted before the model
    # stops reasoning, `content` comes back empty -- which reads as "the model
    # returned nothing" and is impossible to diagnose from the artifact.
    # deepseek-v4-pro did exactly this on every T2 review round: 40k chars of
    # thinking, zero content. Report it as what it is.
    if not text.strip() and thinking.strip():
        raise ProviderError(
            f"{model} exhausted its output budget on reasoning: "
            f"{len(thinking)} chars of thinking, empty content "
            f"(eval_count={data.get('eval_count')}, done_reason={data.get('done_reason')}). "
            f"Raise max_tokens above {max_tokens}."
        )
    return Completion(
        text=text,
        model=model,
        input_tokens=data.get("prompt_eval_count", 0) or 0,
        output_tokens=data.get("eval_count", 0) or 0,
        stop_reason=data.get("done_reason", "") or "",
        thinking_chars=len(thinking),
        raw=data,
    )


def _call_anthropic(model, messages, temperature, max_tokens, timeout, num_ctx, think=None):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ProviderError(
            "ANTHROPIC_API_KEY is not set. Export a key, or run `ant auth login` "
            "and export `ant auth print-credentials --access-token`."
        )
    # The Messages API takes `system` as a top-level parameter, not a role.
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]

    payload: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": turns}
    if system:
        payload["system"] = system
    if not _SAMPLING_REJECTED.match(model):
        payload["temperature"] = temperature

    data = _post(
        "https://api.anthropic.com/v1/messages",
        payload,
        {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        timeout,
    )
    stop = data.get("stop_reason", "") or ""
    # Safety classifiers decline with HTTP 200 + stop_reason=refusal and an
    # empty content array. Reading content[0] unconditionally would IndexError.
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if stop == "refusal":
        cat = (data.get("stop_details") or {}).get("category")
        raise ProviderError(f"{model} declined the request (refusal, category={cat})")
    usage = data.get("usage", {}) or {}
    return Completion(
        text=text,
        model=f"anthropic:{model}",
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
        stop_reason=stop,
        raw=data,
    )


def _call_openai(model, messages, temperature, max_tokens, timeout, num_ctx, think=None):
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.getenv("OPENAI_API_KEY", "")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = _post(f"{base}/chat/completions", payload, headers, timeout)
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage", {}) or {}
    return Completion(
        text=(choice.get("message") or {}).get("content", "") or "",
        model=f"openai:{model}",
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        stop_reason=choice.get("finish_reason", "") or "",
        raw=data,
    )


_BACKENDS = {"ollama": _call_ollama, "anthropic": _call_anthropic, "openai": _call_openai}


def chat(
    model_spec: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 16000,
    timeout: int = 900,
    num_ctx: int = 32768,
    max_retries: int = 3,
    think: Optional[bool] = None,
) -> Completion:
    """Single completion, retried on transport failure with jittered backoff.

    Raises ProviderError if every attempt fails. Callers MUST distinguish this
    from a low-quality answer: a reviewer that could not be reached has not
    voted, and must not be counted as a dissent.
    """
    backend, model = split_model(model_spec)
    fn = _BACKENDS[backend]
    last: Optional[Exception] = None
    t0 = time.time()
    for attempt in range(max_retries):
        try:
            out = fn(model, messages, temperature, max_tokens, timeout, num_ctx, think)
            out.secs = round(time.time() - t0, 1)
            return out
        except ProviderError:
            raise  # refusal / missing key: retrying changes nothing
        except Exception as exc:  # noqa: BLE001 - classified by _retryable
            last = exc
            if not _retryable(exc) or attempt == max_retries - 1:
                break
            time.sleep(min(2**attempt + random.uniform(0, 1), 30))
    raise ProviderError(f"{model_spec} failed after {max_retries} attempts: {type(last).__name__}: {last}")
