"""
Gemini and GitHub Models reach the loop as first-class backends.

Both publish OpenAI-compatible chat-completions endpoints, so they need a base
URL and a key rather than a new transport. They are NOT done by pointing
OPENAI_BASE_URL at Google, because that variable is global: it would silently
redirect every `openai:` model in the same run.

No key is configured in this environment, so these tests stub the transport --
which is the point. The wiring is what has been wrong twice this session (O10,
O15), and it is testable without credentials.
"""
from unittest.mock import patch

import pytest

from agent_loop import providers
from agent_loop.providers import ProviderError, split_model


def _ok_response():
    return {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


@pytest.mark.parametrize("spec,backend,model", [
    ("gemini:gemini-3.1-pro", "gemini", "gemini-3.1-pro"),
    ("github:openai/gpt-5", "github", "openai/gpt-5"),
    ("ollama:kimi-k2.7-code:cloud", "ollama", "kimi-k2.7-code:cloud"),
    ("mistral-large-3:675b-cloud", "ollama", "mistral-large-3:675b-cloud"),
])
def test_prefixes_route_to_the_right_backend(spec, backend, model):
    assert split_model(spec) == (backend, model)


def test_gemini_posts_to_googles_openai_endpoint_with_its_own_key():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        seen["model"] = payload["model"]
        return _ok_response()

    with patch.dict("os.environ", {"GEMINI_API_KEY": "g-key"}, clear=False):
        with patch.object(providers, "_post", side_effect=fake_post):
            out = providers.chat("gemini:gemini-3.1-pro", [{"role": "user", "content": "x"}],
                                 max_tokens=100)
    assert seen["url"].startswith(providers.GEMINI_BASE)
    assert seen["url"].endswith("/chat/completions")
    assert seen["auth"] == "Bearer g-key"
    assert seen["model"] == "gemini-3.1-pro"
    assert out.model == "gemini:gemini-3.1-pro", "usage lines must name the service"


def test_gemini_does_not_read_the_openai_key_or_base():
    """The whole reason for a separate prefix: OPENAI_BASE_URL is global."""
    with patch.dict("os.environ", {"OPENAI_API_KEY": "o-key",
                                   "OPENAI_BASE_URL": "https://example.invalid/v1"},
                    clear=False):
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(ProviderError) as exc:
                providers.chat("gemini:gemini-3.1-pro", [{"role": "user", "content": "x"}])
    assert "GEMINI_API_KEY" in str(exc.value)


def test_a_missing_key_says_which_one_and_where_to_get_it():
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN",
                        "GITHUB_MODELS_TOKEN")}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(ProviderError) as g:
            providers.chat("gemini:gemini-3.1-pro", [{"role": "user", "content": "x"}])
        with pytest.raises(ProviderError) as h:
            providers.chat("github:openai/gpt-5", [{"role": "user", "content": "x"}])
    assert "AI Studio" in str(g.value)
    # The Copilot distinction is recorded where someone will actually hit it.
    assert "Copilot" in str(h.value)


def test_github_models_uses_a_pat_not_a_copilot_subscription():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        return _ok_response()

    with patch.dict("os.environ", {"GITHUB_MODELS_TOKEN": "pat"}, clear=False):
        with patch.object(providers, "_post", side_effect=fake_post):
            providers.chat("github:openai/gpt-5", [{"role": "user", "content": "x"}],
                           max_tokens=100)
    assert seen["url"].startswith(providers.GITHUB_MODELS_BASE)
    assert seen["auth"] == "Bearer pat"


def test_openai_backend_is_unchanged_by_the_new_prefixes():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["url"] = url
        return _ok_response()

    with patch.dict("os.environ", {"OPENAI_API_KEY": "o", "OPENAI_BASE_URL": "https://x.test/v1"},
                    clear=False):
        with patch.object(providers, "_post", side_effect=fake_post):
            out = providers.chat("openai:gpt-oss-120b", [{"role": "user", "content": "x"}],
                                 max_tokens=100)
    assert seen["url"] == "https://x.test/v1/chat/completions"
    assert out.model == "openai:gpt-oss-120b"
