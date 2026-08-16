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
    for backend in ("anthropic", "openai", "ollama", "gemini", "github", "agy"):
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


def describe_exception(exc: Optional[Exception]) -> str:
    """Render a transport failure with the part that says WHAT WENT WRONG.

    `str(HTTPError)` is "HTTP Error 400: Bad Request" -- the status line only. The
    response BODY is where the reason lives, and for a 400 it is usually complete
    and actionable. Observed live: a run died on

        {"error":"max_tokens (96000) exceeds model's maximum output tokens
                  (65536) for model qwen3.5"}

    and reported `HTTPError: HTTP Error 400: Bad Request`. Everything needed to fix
    the run in one edit was in a body nobody printed.
    """
    if exc is None:
        return "no exception recorded"
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            # Readable only once, and only if no backend consumed it first.
            raw = exc.read()
            body = raw.decode("utf-8", "replace").strip()[:500] if raw else ""
        except Exception:  # noqa: BLE001 - a body is a bonus, never a new failure
            body = ""
        head = f"HTTPError {exc.code}: {exc.reason}"
        return f"{head} -- {body}" if body else head
    return f"{type(exc).__name__}: {exc}"


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
    # thinking off.
    #
    # AN EFFORT LEVEL IS NOT A DIAL HERE. ollama accepts think="low"/"medium"/
    # "high" for some models, so this passes the value through unchanged -- but
    # MEASURED on the reviewer bench, 2026-08-11, one review prompt, three
    # models, every level:
    #
    #   minimax-m3    off 91559 chars | medium 59435 | high 43734  (all APPROVE, 0 findings)
    #   deepseek-v4-pro  off 0 chars, 37 findings | low 65887, 2 findings
    #                    medium 189061 chars -> NO ANSWER | high HTTP 500
    #   kimi-k2.7-code   off 0 chars | low 201035 -> NO ANSWER | high 166890, 3 findings
    #
    # The ordering is not monotonic and four of the level arms exhausted the
    # output budget on reasoning and returned nothing. These are truthy strings,
    # so on a model that does not implement levels they simply mean THINKING ON,
    # which is strictly worse than the False these callers want. Only False does
    # anything reliable. Do not reach for a level to "reduce" reasoning.
    #
    # Nor can it be done from the prompt: an arm instructing "think in AT MOST 3
    # short bullet points" produced the LARGEST burn in the sweep -- 216211
    # chars and no answer. The reasoning channel is not the answer channel, so
    # an instruction about how to answer has no purchase on it.
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
            # "Raise max_tokens" was the only advice here, and it is not
            # reliable: reasoning expands to fill whatever it is given. Measured
            # on one ticket, same model, same prompt --
            #   64000 -> 282935 chars of thinking, empty content
            #   96000 -> 435641 chars of thinking, empty content
            # 4.42 and 4.54 characters per token. The budget is not a control on
            # reasoning, it is only where the model gets cut off. The measured
            # fix for the identical failure in the reviewer role was think=False
            # (159s and no verdict, versus 21s and ten findings), so name that
            # first and offer the budget second (O60).
            raise ProviderError(
                f"{model} exhausted its output budget on reasoning: {detail} "
                f"It produced no answer at all, so it did not run out of room to "
                f"write one -- it never started. Set think=False for this role "
                f"before raising max_tokens above {max_tokens}: reasoning expands "
                f"to fill the budget, so a larger one may buy only more of it."
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


AGY_BIN_DEFAULT = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "agy", "bin", "agy.exe"
)
# CreateProcess caps a Windows command line at 32767 characters, and agy takes
# the prompt as an ARGUMENT. Refuse above this rather than truncate: a silently
# shortened arbiter prompt would drop the end of the diff and the model would
# rule on a patch it was never shown.
_AGY_PROMPT_LIMIT = 30000


def _call_agy(model, messages, temperature, max_tokens, timeout, num_ctx, think=None, cache=False):
    """Antigravity's `agy` CLI: a SUBPROCESS, not an HTTP endpoint.

    Worth using despite that, because it authenticates through the Antigravity
    subscription rather than an AI Studio key -- so it reaches gemini-3.1-pro
    without the free tier's input-token quota, and it exposes models the direct
    API path does not (`agy models`): claude-opus-4-6-thinking,
    claude-sonnet-4-6, gpt-oss-120b-medium.

    Reasoning effort is part of the model NAME here (gemini-3.1-pro-high), not
    a separate flag, so `think` is not used. This is why the direct-API arms
    measured Google's default effort and the agy arms do not: same model,
    different setting, not comparable.

    Two safety choices, both deliberate:

    * `--sandbox` and a scratch cwd. agy is an AGENT with file and terminal
      tools, not a completions endpoint. Run in the repo it would be able to
      edit the code under review.
    * `--dangerously-skip-permissions` is required for a non-interactive run --
      without it a permission prompt blocks until the timeout. It is only
      acceptable BECAUSE of the sandbox and the scratch directory above.
    """
    import subprocess
    import tempfile

    binpath = os.getenv("AGY_BIN", AGY_BIN_DEFAULT)
    if not os.path.exists(binpath):
        raise ProviderError(
            f"agy: CLI not found at {binpath}. Set AGY_BIN, or install Antigravity. "
            f"`agy models` lists the ids this backend accepts."
        )

    prompt = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
    if len(prompt) > _AGY_PROMPT_LIMIT:
        raise ProviderError(
            f"agy: prompt is {len(prompt)} chars, over the {_AGY_PROMPT_LIMIT} limit "
            f"imposed by the Windows command line (agy takes the prompt as an "
            f"argument). Refusing rather than truncating -- a shortened prompt would "
            f"silently drop the end of the diff. Use an HTTP backend for inputs this "
            f"large."
        )

    env = os.environ.copy()
    env["PATH"] = os.path.dirname(binpath) + os.pathsep + env.get("PATH", "")
    cmd = [
        binpath, "--sandbox", "--dangerously-skip-permissions",
        f"--model={model}", f"--print-timeout={timeout}s", "-p", prompt,
    ]
    t0 = time.time()
    # NOT TemporaryDirectory(): agy keeps a file open in its working directory,
    # so the context manager's cleanup raises WinError 32 -- AFTER a successful
    # call -- and the exception discards a completion that had already arrived.
    # Clean up best-effort instead; a leftover temp dir is a smaller problem
    # than losing the answer.
    scratch = tempfile.mkdtemp(prefix="agy-")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 30,
            env=env, cwd=scratch,
        )
    except subprocess.TimeoutExpired:
        raise ProviderError(f"agy: {model} timed out after {timeout}s")
    except OSError as exc:
        raise ProviderError(f"agy: could not run {binpath}: {exc}")
    finally:
        # Belt and braces. `ignore_errors=True` swallows OSError, which covers
        # the observed WinError 32, but this runs in a `finally`: ANY exception
        # escaping here replaces a completed answer with a cleanup failure.
        # Nothing about removing a temp directory is worth that.
        import shutil
        try:
            shutil.rmtree(scratch, ignore_errors=True)
        except Exception:  # noqa: BLE001 - see above
            pass

    text = (proc.stdout or "").strip()
    if not text:
        raise ProviderError(
            f"agy: {model} returned nothing (exit {proc.returncode}). "
            f"stderr: {(proc.stderr or '')[:400]}"
        )
    return Completion(
        text=text,
        model=f"agy:{model}",
        # agy's print mode reports no usage, so these are UNKNOWN, not zero.
        # Cost and token lines for an agy arm are therefore not comparable with
        # an HTTP arm's.
        input_tokens=0,
        output_tokens=0,
        stop_reason="stop",
        secs=round(time.time() - t0, 1),
        raw={"returncode": proc.returncode, "stderr": (proc.stderr or "")[:2000]},
    )


_BACKENDS = {
    "ollama": _call_ollama,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
    "github": _call_github,
    "agy": _call_agy,
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

    Pre-dispatch reasoning budget check (Wave 4.3, E-P1a): when think=True,
    the model spends chain-of-thought tokens from the SAME budget as the
    answer. A budget sized for the expected output becomes a budget shared
    with an unbounded reasoning prefix, and the model can return EMPTY CONTENT
    having spent the whole budget reasoning. This was measured on the
    implementer: 125,070 chars of reasoning, empty content, the run died.
    A warning is printed when think=True and max_tokens is below 32K (the
    measured minimum for a reasoning model to produce both reasoning and a
    non-trivial answer). This does not refuse the call -- the budget may be
    intentionally small for a short task -- but it makes the hazard visible
    before the call is spent, not after.
    """
    # None means "whatever config says", so the transport defaults live in one
    # place with the rest of the tunables instead of in this signature.
    _p = config.get().provider
    temperature = _p.temperature if temperature is None else temperature
    max_tokens = _p.default_max_tokens if max_tokens is None else max_tokens
    timeout = _p.timeout_secs if timeout is None else timeout
    num_ctx = _p.num_ctx if num_ctx is None else num_ctx
    max_retries = _p.max_retries if max_retries is None else max_retries

    # Pre-dispatch reasoning budget check (E-P1a). Measured: the implementer
    # spent 125,070 chars (~31K tokens) on reasoning and returned empty content
    # with a 48K budget. A reasoning model needs headroom for BOTH reasoning
    # and the answer; a budget that is tight for the answer alone will be
    # consumed by reasoning. The warning makes the hazard visible before the
    # call is spent, not after.
    #
    # Uses sys.stderr (not print/stdout) so concurrent panel reviewers do not
    # interleave their warnings on stdout. stderr is line-buffered and the
    # warning is one line, so thread interleaving is less likely to corrupt it.
    _REASONING_MIN_BUDGET = 32000
    if think and max_tokens < _REASONING_MIN_BUDGET:
        import sys as _sys
        _sys.stderr.write(
            f"  WARNING: {model_spec} think=True with max_tokens={max_tokens} "
            f"(below {_REASONING_MIN_BUDGET}). On a reasoning model, chain-of-thought "
            f"is spent from the same budget as the answer, so the model may spend "
            f"the whole budget reasoning and return empty content. Raise max_tokens "
            f"in the same edit you set think=True.\n"
        )
        _sys.stderr.flush()

    backend, model = split_model(model_spec)
    fn = _BACKENDS[backend]
    last: Optional[Exception] = None
    t0 = time.time()
    # The count of attempts ACTUALLY made, not the ceiling. A non-retryable error
    # breaks out of this loop after one call, and the message used to claim
    # `max_retries` anyway -- so a deterministic 400 was reported as "failed after
    # 3 attempts". That sends the reader looking for a flaky endpoint instead of a
    # bad request, and it is the loop lying about its own behaviour.
    attempts = 0
    for attempt in range(max_retries):
        attempts = attempt + 1
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
    plural = "attempt" if attempts == 1 else "attempts"
    raise ProviderError(
        f"{model_spec} failed after {attempts} {plural}: {describe_exception(last)}"
    )
