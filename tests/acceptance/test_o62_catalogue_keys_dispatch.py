"""O62: six catalogue entries could not be dispatched to the backend they name.

FOUND BY RUNNING THE REVIEWER SWEEP, not by reading. Twenty catalogue models
were sent one review prompt. `gemini-3.6-flash`, `gemini-3.5-flash`,
`gemini-3.1-pro-preview`, `claude-opus-5`, `claude-sonnet-5` and
`claude-haiku-4-5` all came back:

    HTTPError 404: Not Found -- {"error":"model ... not found"}

That is an OLLAMA error. `split_model` (providers.py) treats any spec without a
known backend prefix as ollama, so all six were being asked of the local ollama
host, which has never heard of them. The catalogue notes say "Requires
ANTHROPIC_API_KEY" / "Requires GEMINI_API_KEY", so they were INTENDED to be
reachable -- and the `agy:` entries in the same catalogue already carry their
prefix, so the convention existed and these six broke it.

The cost of the bug is not that they failed. It is that they failed with a
message that says the MODEL DOES NOT EXIST, which reads as a typo in the name.
The true cause -- wrong backend, and then a missing key -- is invisible. Anyone
putting `claude-opus-5` in a config would have concluded the id was stale.

The fix is the key, not the dispatcher: inferring a backend from a name stem
cannot work, because `agy:claude-sonnet-4-6` and `anthropic:claude-sonnet-5` are
the same vendor reached two ways, and only the prefix distinguishes them.
"""
from __future__ import annotations

import pytest

from agent_loop.config import MODEL_CATALOG
from agent_loop.providers import _BACKENDS, split_model


def test_every_catalogue_key_names_a_real_backend():
    """A prefix that is not in _BACKENDS is silently swallowed as an ollama name."""
    for spec in MODEL_CATALOG:
        backend, bare = split_model(spec)
        assert backend in _BACKENDS, f"{spec} dispatches to unknown backend {backend}"
        assert bare, f"{spec} splits to an empty model name"


# The rule that generalises, so a SEVENTH entry added tomorrow is caught.
#
# The first draft of this test read the `note` field for "ANTHROPIC_API_KEY" etc.
# It caught the three anthropic entries and MISSED all three gemini ones,
# because their key requirement is stated in a `#` comment above the block
# rather than in the note field. It would have passed while the defect stood in
# half the cases -- so it is replaced rather than kept alongside.
#
# This reads the VENDOR STEM instead, which is a fact about the name and not
# about how carefully someone wrote prose. Ollama's own catalogue entries are
# kimi / glm / qwen / deepseek / mistral / gemma; none collides with these.
_VENDOR_ONLY_OFF_OLLAMA = ("claude", "gemini", "gpt-", "o1-", "o3-")


@pytest.mark.parametrize("spec", sorted(MODEL_CATALOG))
def test_a_vendor_hosted_model_does_not_route_to_ollama(spec):
    stem = spec.split(":")[-1].lower()
    if not any(stem.startswith(v) for v in _VENDOR_ONLY_OFF_OLLAMA):
        pytest.skip("not a vendor-hosted name")
    backend, _ = split_model(spec)
    assert backend != "ollama", (
        f"{spec!r} is a vendor-hosted model but dispatches to ollama, which "
        f"answers 404 'model not found' -- a message that blames the name. "
        f"Give the key its backend prefix."
    )


def test_the_six_that_were_broken_are_reachable_as_named():
    """Named explicitly, so a rename that 'fixes' the rule by deleting the
    entries is not mistaken for a fix."""
    for spec in (
        "gemini:gemini-3.6-flash",
        "gemini:gemini-3.5-flash",
        "gemini:gemini-3.1-pro-preview",
        "anthropic:claude-opus-5",
        "anthropic:claude-sonnet-5",
        "anthropic:claude-haiku-4-5",
    ):
        assert spec in MODEL_CATALOG, f"{spec} is missing from the catalogue"
        assert split_model(spec)[0] != "ollama"
