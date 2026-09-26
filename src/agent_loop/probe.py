"""Startup probe: ask every model a run will call whether it still exists.

CF-37 and CF-40 are the same defect twice. Ollama retired a configured model
(the second reviewer, then the arbiter), and the loop found out only when that
call failed INSIDE a run -- after the implementer had spent a round, and after
the gates had passed. The catalogue's `RETIRED` marker could not have caught
either: it records what someone already knew, and in both cases nobody did.

So each distinct model gets one tiny call before the run starts. The two
outcomes are deliberately asymmetric:

  * HTTP 410 Gone is a fact about the model, not the network: the provider is
    saying the name will never answer again. That refuses the run, and says
    which model and which role, because every round it would spend is wasted.
  * Anything else (timeout, 429, 5xx, connection refused) may be transient, and
    the loop already degrades correctly when a member cannot vote. That warns
    and the run proceeds -- blocking work on a flaky endpoint would make the
    probe the outage.

`AGENT_LOOP_NO_PROBE=1` or `--no-probe` skips it (offline runs, and the test
suite, whose `main()` callers must never reach a real provider).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from . import config
from .providers import ProviderError, chat

PROBE_PROMPT = [{"role": "user", "content": "Reply with the single word OK."}]


@dataclass
class ProbeResult:
    retired: List[Tuple[str, str]] = field(default_factory=list)      # (model, detail)
    unreachable: List[Tuple[str, str]] = field(default_factory=list)  # (model, detail)
    ok: List[str] = field(default_factory=list)


def is_retired(detail: str) -> bool:
    # providers.describe_exception renders an HTTPError as "HTTPError <code>: ...",
    # so the status code is the signal, never the provider's prose.
    return "HTTPError 410" in detail


def disabled() -> bool:
    return os.getenv("AGENT_LOOP_NO_PROBE", "").strip() not in ("", "0")


def _one(model: str) -> Tuple[str, str]:
    try:
        # One attempt: a 410 is not retryable anyway, and a probe that retries
        # a flaky endpoint three times is just a slower warning.
        p = config.get().provider
        chat(model, PROBE_PROMPT, max_tokens=p.probe_max_tokens, think=False,
             max_retries=1, timeout=p.probe_timeout_secs)
        return model, ""
    except ProviderError as exc:
        return model, str(exc) or type(exc).__name__


def probe(models: Sequence[str]) -> ProbeResult:
    distinct = list(dict.fromkeys(m for m in models if m))
    res = ProbeResult()
    if not distinct:
        return res
    with ThreadPoolExecutor(max_workers=len(distinct)) as pool:
        for model, detail in pool.map(_one, distinct):
            if not detail:
                res.ok.append(model)
            elif is_retired(detail):
                res.retired.append((model, detail))
            else:
                res.unreachable.append((model, detail))
    return res


def check(roles: Dict[str, Sequence[str]]) -> int:
    """Probe every model named in `roles` (role -> models). Print what was found.

    Returns 2 if any model is retired (the caller refuses the run), else 0.
    """
    by_model: Dict[str, List[str]] = {}
    for role, ms in roles.items():
        for m in ms:
            if m:
                by_model.setdefault(m, []).append(role)
    res = probe(list(by_model))
    for model, detail in res.unreachable:
        print(f"  PROBE WARNING: {model} ({', '.join(by_model[model])}) did not answer: "
              f"{detail[:300]}. The run proceeds; a member that cannot vote is not counted.")
    for model, detail in res.retired:
        print(f"  PROBE REFUSED: {model} ({', '.join(by_model[model])}) is RETIRED by its "
              f"provider: {detail[:300]}")
    if res.retired:
        print("  Reseat the role(s) above (config.py / agent_loop.config.json / --reviewers "
              "/ --arbiter / --implementer) and mark the catalogue entry RETIRED.")
        return 2
    return 0
