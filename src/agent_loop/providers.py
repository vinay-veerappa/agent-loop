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

from . import config

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
    cache_creation_tokens: int = 0  # billed at 1.25x input; reported separately from input_tokens
    stop_reason: str = ""
    secs: float = 0.0
    # Reasoning models bill their chain of thought as output tokens. Tracked so
    # a reviewer that spends its whole budget thinking is visible in the logs.
    thinking_chars: int = 0
    # Native tool calls, normalised to [{"name": str, "args": dict}]. A model
    # that answers with a tool call returns EMPTY `text` -- the call is the
    # answer -- so a caller that reads only `text` sees a blank turn and cannot
    # tell it from a dead model. Populated for the ollama and OpenAI backends;
    # Anthropic only emits tool_use when the request carries a `tools` array,
    # which this shim never sends.
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
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
            + self.cache_creation_tokens * rate[0] * 1.25
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
    for backend in ("anthropic", "openai", "ollama", "gemini", "github"):
        if spec.startswith(backend + ":"):
            return backend, spec[len(backend) + 1 :]
    return "ollama", spec


def _add_cache_control(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Place Anthropic cache breakpoints on a MULTI-TURN conversation.

    Caching is a prefix match: a breakpoint caches everything from the start of
    the request up to and including that block, and a later request reads the
    longest cached prefix it still matches byte-for-byte.

    Two breakpoints, for two different reasons:

    * `turns[0]` -- the implement prompt, carrying the ticket, the spec and the
      verbatim region source. `compaction.pin_count()` guarantees this message
      is byte-identical on every round of a ticket, so it is the ONLY span that
      survives Phase 4a rewriting the middle of the history. Marking only the
      newest turn (what this function did originally) produced an entry that
      was invalidated the first time 4a truncated a prior round, from which
      point every round paid a write premium and read nothing.

    * the newest user turn -- incremental reuse while the history is still
      append-only, which is the documented multi-turn pattern.

    Anthropic allows at most four breakpoints per request; with the system block
    marked by the caller that is three.
    """
    if not turns:
        return turns

    marks = set()
    if turns[0].get("role") == "user":
        marks.add(0)
    for i in range(len(turns) - 1, -1, -1):
        if turns[i].get("role") == "user":
            marks.add(i)
            break

    result: List[Dict[str, Any]] = []
    for i, turn in enumerate(turns):
        # Only str content is wrapped; a caller that already passed structured
        # blocks owns its own cache_control placement.
        if i in marks and isinstance(turn.get("content"), str):
            result.append({
                **turn,
                "content": [
                    {"type": "text", "text": turn["content"],
                     "cache_control": {"type": "ephemeral"}}
                ],
            })
        else:
            result.append(turn)
    return result


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


def _fit_num_ctx(messages, max_tokens: int, num_ctx: int) -> int:
    """Widen num_ctx so the prompt AND the requested output both fit.

    num_ctx bounds prompt + completion, num_predict bounds the completion
    alone, so a caller asking for 48000 output tokens under the old fixed
    32768 window was asking for something arithmetically impossible: the
    server silently truncated, and the loop read the truncation as "the model
    returned nothing". The implementer's 48k budget and the 40k round input
    budget are both larger than that window on their own.
    """
    prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    needed = int((prompt_tokens + max_tokens) * 1.15) + 1024  # headroom for the chat template
    if needed <= num_ctx:
        return num_ctx
    # Round up to the next 8K boundary; servers allocate KV cache in blocks.
    return ((needed + 8191) // 8192) * 8192


def _normalise_tool_calls(raw: Any) -> List[Dict[str, Any]]:
    """Flatten a provider's native tool-call array to [{"name", "args"}].

    Both ollama and OpenAI-compatible servers nest the call under `function`,
    but ollama returns `arguments` already decoded while OpenAI sends it as a
    JSON string. Accept either. An entry with no usable name is skipped rather
    than given an invented one: a tool call the loop cannot name is a call it
    cannot execute, and a placeholder would be dispatched as a real request.
    """
    calls: List[Dict[str, Any]] = []
    for tc in raw or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or tc
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or ""
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"name": name, "args": args if isinstance(args, dict) else {}})
    return calls


def _call_ollama(model, messages, temperature, max_tokens, timeout, num_ctx, think=None, cache=False):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # num_predict was previously omitted, so max_tokens was silently ignored
        # and the budget was whatever the server defaulted to.
        "options": {
            "temperature": temperature,
            "num_ctx": _fit_num_ctx(messages, max_tokens, num_ctx),
            "num_predict": max_tokens,
        },
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
    tool_calls = _normalise_tool_calls(msg.get("tool_calls"))

    # Reasoning models return their chain of thought in `thinking` and the
    # answer in `content`. When the output budget is exhausted before the model
    # stops reasoning, `content` comes back empty -- which reads as "the model
    # returned nothing" and is impossible to diagnose from the artifact.
    # deepseek-v4-pro did exactly this on every T2 review round: 40k chars of
    # thinking, zero content. Report it as what it is.
    #
    # But empty content is ALSO how a model replies with a tool call, and the
    # old check could not tell the two apart: it blamed the budget for any
    # empty answer that had any thinking attached. kimi-k2.7-code answering
    # developer mode's first turn with a native read_file call -- 21 tokens,
    # done_reason=stop -- was reported as a 48000-token budget exhaustion, and
    # the advice it printed ("raise max_tokens") could not have helped. Check
    # for the tool call first, and only claim truncation when the response was
    # actually truncated.
    if not text.strip() and not tool_calls and thinking.strip():
        eval_count = data.get("eval_count") or 0
        done_reason = data.get("done_reason") or ""
        detail = (
            f"{len(thinking)} chars of thinking, empty content "
            f"(eval_count={eval_count}, done_reason={done_reason})."
        )
        if done_reason == "length" or eval_count >= max_tokens:
            raise ProviderError(
                f"{model} exhausted its output budget on reasoning: {detail} "
                f"Raise max_tokens above {max_tokens}."
            )
        raise ProviderError(
            f"{model} returned neither content nor a tool call: {detail} "
            f"It stopped on its own well inside the {max_tokens}-token budget, "
            f"so raising max_tokens will not help; the prompt is the suspect."
        )
    return Completion(
        text=text,
        model=model,
        input_tokens=data.get("prompt_eval_count", 0) or 0,
        output_tokens=data.get("eval_count", 0) or 0,
        stop_reason=data.get("done_reason", "") or "",
        thinking_chars=len(thinking),
        tool_calls=tool_calls,
        raw=data,
    )


def _call_anthropic(model, messages, temperature, max_tokens, timeout, num_ctx, think=None, cache=False):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ProviderError(
            "ANTHROPIC_API_KEY is not set. Export a key, or run `ant auth login` "
            "and export `ant auth print-credentials --access-token`."
        )
    # The Messages API takes `system` as a top-level parameter, not a role.
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]

    # Prompt caching is OPT-IN per call, because a breakpoint is not free: a
    # cache write bills at 1.25x input, so marking a prompt that will never be
    # re-sent is a pure 25% surcharge. Every single-shot caller here -- the
    # review panel, the arbiter, plan/test/docs/brainstorm, the compactor --
    # builds a fresh prompt every time and can never read what it wrote, so
    # they leave `cache` False. Only a genuine multi-turn conversation (the
    # implementer across rounds) opts in, where break-even is two requests.
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": _add_cache_control(turns) if cache else turns,
    }
    if system:
        # A system breakpoint is also readable by the NEXT ticket on the same
        # profile: implementer_rules plus the output contract are byte-identical
        # across tickets. It silently does nothing when the system prompt is
        # under the model's minimum cacheable prefix, which is model-dependent
        # (512 tokens on Opus 5, 1024 on Opus 4.8/Sonnet 5, 4096 on Opus 4.6).
        payload["system"] = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache else system
        )
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
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
        stop_reason=stop,
        raw=data,
    )


def _call_openai(model, messages, temperature, max_tokens, timeout, num_ctx, think=None,
                 cache=False, base="", key="", label="openai"):
    base = (base or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
    key = key or os.getenv("OPENAI_API_KEY", "")
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
    message = choice.get("message") or {}
    return Completion(
        text=message.get("content", "") or "",
        model=f"{label}:{model}",
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        stop_reason=choice.get("finish_reason", "") or "",
        tool_calls=_normalise_tool_calls(message.get("tool_calls")),
        raw=data,
    )


# Google and GitHub both publish OpenAI-compatible chat-completions endpoints,
# so they need a base URL and a key rather than a new transport. They get their
# OWN prefix instead of being an OPENAI_BASE_URL trick for two reasons: the env
# var is global, so pointing it at Google silently redirects every `openai:`
# model in the same run; and a model named in a config file should say which
# service it comes from.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GITHUB_MODELS_BASE = "https://models.github.ai/inference"


def _call_gemini(model, messages, temperature, max_tokens, timeout, num_ctx, think=None, cache=False):
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise ProviderError(
            "gemini: set GEMINI_API_KEY (or GOOGLE_API_KEY) from Google AI Studio. "
            "Model ids are Google's own, e.g. gemini:gemini-3.1-pro."
        )
    return _call_openai(
        model, messages, temperature, max_tokens, timeout, num_ctx, think, cache,
        base=os.getenv("GEMINI_BASE_URL", GEMINI_BASE), key=key, label="gemini",
    )


def _call_github(model, messages, temperature, max_tokens, timeout, num_ctx, think=None, cache=False):
    """GitHub Models. RETIRED -- see the warning below; kept for the plumbing.

    Verified 2026-08-10 with a valid PAT: `models.github.ai/inference` returns
    HTTP 410 `github_models_retirement_brownout`, and the older
    `models.inference.ai.azure.com` host returns 404. The token authenticated
    fine against api.github.com, so this is the SERVICE, not the credential.

    Left in place because `_call_openai` now takes a base URL, key and label,
    so any OpenAI-compatible service is ~10 lines. Point
    GITHUB_MODELS_BASE_URL elsewhere and this becomes a generic connector.
    """
    key = os.getenv("GITHUB_MODELS_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not key:
        raise ProviderError(
            "github: set GITHUB_MODELS_TOKEN (or GITHUB_TOKEN) to a PAT with the "
            "models scope. NOTE GitHub Models was RETIRING as of 2026-08-10 (HTTP "
            "410 brownout), so expect this to fail even with a valid token. Also "
            "note a Copilot subscription is licensed for use through Copilot "
            "clients and is not a chat-completions endpoint for a harness like this."
        )
    return _call_openai(
        model, messages, temperature, max_tokens, timeout, num_ctx, think, cache,
        base=os.getenv("GITHUB_MODELS_BASE_URL", GITHUB_MODELS_BASE), key=key,
        label="github",
    )


_BACKENDS = {
    "ollama": _call_ollama,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
    "github": _call_github,
}


def chat(
    model_spec: str,
    messages: List[Dict[str, str]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[int] = None,
    num_ctx: Optional[int] = None,
    max_retries: Optional[int] = None,
    think: Optional[bool] = None,
    cache: bool = False,
) -> Completion:
    """Single completion, retried on transport failure with jittered backoff.

    Raises ProviderError if every attempt fails. Callers MUST distinguish this
    from a low-quality answer: a reviewer that could not be reached has not
    voted, and must not be counted as a dissent.
    """
    # None means "whatever config says", so the transport defaults live in one
    # place with the rest of the tunables instead of in this signature.
    _p = config.get().provider
    temperature = _p.temperature if temperature is None else temperature
    max_tokens = _p.default_max_tokens if max_tokens is None else max_tokens
    timeout = _p.timeout_secs if timeout is None else timeout
    num_ctx = _p.num_ctx if num_ctx is None else num_ctx
    max_retries = _p.max_retries if max_retries is None else max_retries

    backend, model = split_model(model_spec)
    fn = _BACKENDS[backend]
    last: Optional[Exception] = None
    t0 = time.time()
    for attempt in range(max_retries):
        try:
            out = fn(model, messages, temperature, max_tokens, timeout, num_ctx, think, cache)
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
