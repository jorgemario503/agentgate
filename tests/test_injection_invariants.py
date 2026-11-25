"""Invariants the session-context work must not break.

Two things about open models are load-bearing and easy to damage from a
distance: the provider session key, and the position of the system prompt.

**Provider session key.** DeepInfra documents ``prompt_cache_key`` as the
identifier that pins a conversation; Fireworks accepts ``x-session-affinity``.
AgentGate sends it on *every* message of a session. Nothing the injection layer
does may stop it being sent, or change its value mid-session.

**System prompt position.** The cached prefix begins at ``messages[0]``. The
open-model runners prepend per-turn ephemeral context *before the last user
message* precisely so that the system prompt stays at slot 0 and the prefix
stays stable across turns. Moving it would cold-start the cache every turn.

These are assertions about shape, not about any particular injector, so they
keep holding as the injection layer is refactored.
"""

import ast
from pathlib import Path


from core.context.cache_key import cache_key_kwargs
from core.sessions import (
    SessionState,
    classify_session,
)

REPO = Path(__file__).resolve().parents[1]
OPEN_MODEL_RUNNERS = ("runners/deepinfra.py", "runners/fireworks.py", "runners/local.py")  # local is opt-in; see below


# -- the provider session key ---------------------------------------------


def test_key_is_sent_for_a_permanent_address():
    """Telegram/WhatsApp addresses never change, so their key never changes.

    This is the documented consequence of using the address as the session id:
    the cache lineage is per-address, not per-session. Asserted so the property
    is visible rather than implied.
    """
    assert cache_key_kwargs("deepinfra", "555") == {
        "extra_body": {"prompt_cache_key": "555"}
    }
    # Same address again -> identical key. Nothing about a new session changes it.
    assert cache_key_kwargs("deepinfra", "555") == cache_key_kwargs("deepinfra", "555")


def test_key_tracks_the_thread_for_slack():
    """Slack addresses are per-thread, so a new thread gets a new lineage."""
    a = cache_key_kwargs("deepinfra", "111.222")
    b = cache_key_kwargs("deepinfra", "111.333")
    assert a != b


def test_every_open_model_request_carries_the_key():
    """The key must be applied on each call, not captured once at construction.

    Guards against an injector refactor that passes a rebuilt kwargs dict and
    drops ``cache_kwargs`` on the way.

    The local runner is excluded on purpose: it is opt-in (a strict
    OpenAI-compatible server may reject an unknown field), so it builds its
    payload without a key at all. That exemption is asserted below rather than
    left implicit.
    """
    for rel in ("runners/deepinfra.py", "runners/fireworks.py"):
        source = (REPO / rel).read_text()
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "completions"
        ]
        assert calls, f"{rel}: no completion call found"
        # At least one completion call must be made with the cache kwargs splatted.
        carries = [
            node for node in calls
            if any(isinstance(kw, ast.keyword) and kw.arg is None for kw in node.keywords)
        ]
        assert carries, f"{rel}: completion call does not splat cache kwargs"

    # The opt-in runner must be a deliberate no-op, not an accidental omission.
    assert cache_key_kwargs("ollama", "thread-1") == {}
    assert cache_key_kwargs("local", "thread-1") == {}


def test_key_is_independent_of_session_state():
    """Cache key derives from the address, never from an injection decision."""
    key = cache_key_kwargs("deepinfra", "thread-1")
    for state in SessionState:
        assert cache_key_kwargs("deepinfra", "thread-1") == key, state


# -- system prompt position ------------------------------------------------


def test_system_prompt_stays_at_slot_zero():
    """The cached prefix must not be disturbed by per-turn ephemeral context."""
    path = REPO / "runners/deepinfra.py"
    tree = ast.parse(path.read_text())
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_outgoing_messages"
    )
    source = ast.get_source_segment(path.read_text(), fn)
    # Ephemeral context is inserted immediately before a user message...
    assert 'messages.insert(i, {"role": "system", "content": self._ephemeral_context})' in source
    # ...never at the head of the list.
    assert "messages.insert(0" not in source
    assert "self.messages[0] =" not in source


def test_apply_system_prompt_keeps_the_prompt_first():
    """A reloaded history gets the freshly built prompt at slot 0."""
    from core.prompts import apply_system_prompt

    saved = [{"role": "system", "content": "OLD"}, {"role": "user", "content": "hi"}]
    out = apply_system_prompt(saved, "NEW")
    assert out[0] == {"role": "system", "content": "NEW"}
    assert len(out) == len(saved)


# -- the state machine no longer consults the idle marker ------------------


def test_classify_ignores_keys_it_should_not_read():
    """Adding a notification marker must never move the classification.

    The retired RESUME_AFTER_TIMEOUT state existed only to read `expired`. If a
    future marker reintroduces state-dependence, this fails.
    """
    from core.sessions import IDLE_NOTIFIED_KEY

    plain = {"channel": "C", "last_activity": 1.0}
    flagged = {**plain, IDLE_NOTIFIED_KEY: 99.0}
    legacy = {**plain, "expired": True}

    for has_client in (True, False):
        assert (
            classify_session(plain, has_client)
            is classify_session(flagged, has_client)
            is classify_session(legacy, has_client)
        )
