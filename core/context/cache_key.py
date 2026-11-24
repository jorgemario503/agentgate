"""Session-scoped prompt cache keys, allowlisted per provider.

Providers cannot tell two conversations apart on their own, so they cache by
prompt prefix only. Both hosted backends accept a session identifier that pins
a conversation to its own cache lineage:

  - DeepInfra: ``prompt_cache_key`` in the request body
  - Fireworks: ``x-session-affinity`` header (also accepts prompt_cache_key)

Sending one keeps concurrent sessions on the same model and API key from
sharing a cache entry, and keeps each conversation's hit stream stable.

The list is keyed by **provider**, never by runner class, so Claude and any
future SDK backend are excluded by construction rather than by a flag someone
has to remember to clear.

The local runner talks to arbitrary OpenAI-compatible servers of varying
strictness, so it is opt-in: a strict server may reject an unknown field. When
that happens the caller retries without the key instead of failing the turn.
"""
from __future__ import annotations

import os
from typing import Any, Optional

#: Body field understood by DeepInfra (and Fireworks' compatible endpoint).
CACHE_KEY_FIELD = "prompt_cache_key"

#: Header Fireworks documents for session affinity.
CACHE_KEY_HEADER = "x-session-affinity"

#: Set to any non-empty value to send the key to the local runner as well.
LOCAL_OPT_IN_ENV = "AGENTGATE_PROMPT_CACHE_KEY"

#: provider -> how the key is transmitted.
_BODY_PROVIDERS = {"deepinfra"}
_HEADER_PROVIDERS = {"fireworks"}
_OPT_IN_PROVIDERS = {"ollama", "mlx"}

#: Substrings that identify a rejection *of the key itself*, so an unrelated
#: 400 is never silently retried.
_REJECTION_HINTS = (
    CACHE_KEY_FIELD,
    "cache_key",
    CACHE_KEY_HEADER,
    "unknown field",
    "unrecognized",
    "extra fields not permitted",
)


def cache_key_kwargs(provider: str, thread_id: Optional[str]) -> dict[str, Any]:
    """Return the request kwargs that pin `thread_id` to its own cache.

    Returns {} for providers outside the allowlist, and when there is no
    thread id to pin to. Claude and unknown providers are excluded by
    construction.
    """
    if not thread_id:
        return {}
    name = (provider or "").lower()

    if name in _BODY_PROVIDERS:
        return {"extra_body": {CACHE_KEY_FIELD: thread_id}}
    if name in _HEADER_PROVIDERS:
        return {"extra_headers": {CACHE_KEY_HEADER: thread_id}}
    if name in _OPT_IN_PROVIDERS:
        if os.environ.get(LOCAL_OPT_IN_ENV):
            return {CACHE_KEY_FIELD: thread_id}
        return {}
    return {}


def retry_without_cache_key(exc: BaseException) -> bool:
    """True when `exc` looks like a rejection of the cache-key parameter.

    Only then is a retry worth it; any other failure must propagate so it is
    not masked by a silent second attempt.
    """
    text = str(exc).lower()
    if not any(hint.lower() in text for hint in _REJECTION_HINTS):
        return False
    status = getattr(exc, "status_code", None)
    return status is None or status in (400, 422)
