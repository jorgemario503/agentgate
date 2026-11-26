"""Maintainer-managed guidance through the real request path.

The spec requires proof through the runner's *normal* request path, not a unit
test of the resolver. These tests install a scripted provider underneath the real
`DeepInfraAgentClient`, so the assertion is on the messages that would actually be
sent to the model — including the ephemeral context that carries the instruction.

Everything is deterministic: a temp AgentDB store, a fixed workspace, no network,
no model calls.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.context.managed_guidance import (
    ManagedGuidanceSelection,
    ResolvedScope,
    parse_selections,
    resolve_managed_guidance,
)

REPO = "komper"


def _live_count(store):
    """AgentDB's public ambiguity check."""
    return store.count_live_by_key
TASK = "bug_fix"
GUIDANCE_KEY = "memory/guidance/komper/bug-fix"
INSTRUCTION = "Verify an upgrade from a database created by the previous release."


# ---------------------------------------------------------------------------
# Fixtures: a real store, a real runner, a scripted provider
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """Captures the outgoing request and replies with a plain assistant turn."""

    def __init__(self):
        self.requests = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return _ScriptedCompletion()

    @property
    def last_messages(self):
        return self.requests[-1]["messages"] if self.requests else []

    def text_sent(self) -> str:
        """Every character of every message sent, as one blob."""
        return json.dumps(self.last_messages)


class _ScriptedCompletion:
    def model_dump(self, exclude_none=True):
        return {
            "choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }],
        }


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real AgentDB with a deny-by-default policy, as AgentGate configures."""
    from agentdb import AgentDB

    policy = tmp_path / "permissions.yaml"
    policy.write_text(
        "default_policy: deny\n"
        "agents:\n"
        "  agentgate:\n"
        "    can_read:\n      - 'memory/**'\n"
        "    can_write:\n      - 'memory/**'\n"
    )
    db = AgentDB(memory_dir=str(tmp_path / "agentdb"), permissions_path=str(policy))

    # A maintainer publishes the instruction as an ordinary record.
    db.write(
        key=GUIDANCE_KEY, value=INSTRUCTION, origin="human_approved",
        agent_id="agentgate", session_id=None,
        repository_id=REPO, task_type=TASK,
    )
    return db


@pytest.fixture
def scripted(monkeypatch):
    provider = ScriptedProvider()

    import runners.deepinfra as di

    class _PatchedClient:
        def __init__(self, *a, **kw):
            pass

        chat = property(lambda self: provider)

        async def close(self):
            pass

    monkeypatch.setattr(di, "AsyncOpenAI", lambda *a, **kw: _PatchedClient())
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test")
    return provider


def _client(scripted, *, context_window=None):
    """A real DeepInfraAgentClient with the scripted provider underneath."""
    import runners.deepinfra as di

    return di.DeepInfraAgentClient(
        model="test-model",
        api_key="test",
        tools={},
        system_prompt="SYSTEM",
        session_id="t1",
        session_store=di.JsonFileSessionStore(directory="./_test_sessions"),
        context_window=context_window,
        max_tool_iterations=1,
    )


def _send(client, prompt, ephemeral=None):
    """Drive one real request through the runner's normal path."""
    async def go():
        await client.query(prompt, ephemeral_context=ephemeral)
        async for _ in client.receive_response():
            pass
    asyncio.run(go())
    return client


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------

def test_matching_scope_yields_one_instruction(store):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    resolved = resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    assert resolved is not None
    assert resolved.text == INSTRUCTION


@pytest.mark.parametrize("repo,task", [
    ("other-repo", TASK),      # wrong repository
    (REPO, "feature"),         # wrong task type
])
def test_wrong_scope_yields_nothing(store, repo, task):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(repo, task, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


@pytest.mark.parametrize("scope", [
    ResolvedScope(None, TASK, "agentgate"),
    ResolvedScope(REPO, None, "agentgate"),
    ResolvedScope(None, None, "agentgate"),
])
def test_unresolved_scope_yields_nothing(store, scope):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    assert resolve_managed_guidance(
        selections=selections, scope=scope, read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


def test_denied_read_yields_nothing(store):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    # An identity the policy does not permit to read memory/**.
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "stranger"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


def test_withdrawn_mapping_yields_nothing(store):
    """Withdrawal is removing the selection; the record may still exist."""
    assert resolve_managed_guidance(
        selections=(),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


def test_a_rescoped_record_yields_nothing(store, tmp_path):
    """The mapping naming a key is not enough — the record must still apply."""
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    store.write(
        key=GUIDANCE_KEY, value=INSTRUCTION, origin="human_approved",
        agent_id="agentgate", repository_id="some-other-repo", task_type=TASK,
    )
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


def test_ambiguous_mapping_yields_nothing(store):
    selections = (
        ManagedGuidanceSelection(REPO, TASK, GUIDANCE_KEY),
        ManagedGuidanceSelection(REPO, TASK, "memory/guidance/second"),
    )
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    ) is None


# ---------------------------------------------------------------------------
# Through the real request path
# ---------------------------------------------------------------------------

def test_the_instruction_reaches_the_outgoing_request(store, scripted):
    """The matching task receives exactly one short instruction."""
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    resolved = resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )

    client = _client(scripted)
    _send(client, "fix the migration", ephemeral=resolved.text)

    sent = scripted.text_sent()
    assert INSTRUCTION in sent, "the instruction did not reach the provider request"
    # Exactly once: not repeated across the system prompt and the ephemeral block.
    assert sent.count(INSTRUCTION) == 1


def test_no_guidance_when_scope_does_not_match(store, scripted):
    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope("other-repo", TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    client = _client(scripted)
    _send(client, "fix the migration", ephemeral=resolved.text if resolved else None)
    assert INSTRUCTION not in scripted.text_sent()


def test_a_resume_does_not_replay_stale_guidance(store, scripted):
    """Scope is re-resolved per request; a resume carries no remembered copy."""
    client = _client(scripted)
    _send(client, "first task", ephemeral=INSTRUCTION)

    # The maintainer withdraws: the next request resolves nothing.
    selections = parse_selections("")
    resolved = resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    assert resolved is None
    _send(client, "resumed task", ephemeral=None)

    # The newest request must be clean, even though an earlier one had guidance.
    assert INSTRUCTION not in json.dumps(scripted.last_messages)


def test_the_instruction_is_absent_from_saved_history(store, scripted, tmp_path):
    """Guidance rides the ephemeral context and is never persisted."""
    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    history = tmp_path / "history"
    import runners.deepinfra as di

    client = di.DeepInfraAgentClient(
        model="test-model", api_key="test", tools={}, system_prompt="SYSTEM",
        session_id="t1",
        session_store=di.JsonFileSessionStore(directory=str(history)),
        context_window=None, max_tool_iterations=1,
    )
    _send(client, "fix the migration", ephemeral=resolved.text)
    client.session_store.save(client.session_id, client.messages)

    saved = history / "t1.json"
    assert saved.exists()
    assert INSTRUCTION not in saved.read_text(), (
        "guidance was written into saved conversation history"
    )


def test_a_scope_switch_omits_the_old_instruction(store, scripted):
    """Switching repository mid-session drops guidance written for the old one."""
    first = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    assert first is not None

    # Same session, now working in another repository.
    switched = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope("another-repo", TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=_live_count(store),
    )
    assert switched is None

    client = _client(scripted)
    _send(client, "now working elsewhere", ephemeral=None)
    assert INSTRUCTION not in scripted.text_sent()


# ---------------------------------------------------------------------------
# Ambiguity: the upstream gap this integration exposed
# ---------------------------------------------------------------------------

def test_two_live_siblings_make_the_selection_ambiguous(store):
    """A repeat write() leaves two live records under one key.

    `read()` returns whichever it encounters and gives no signal that it chose, so
    the host cannot tell it is reading *the* maintainer's record. The configured
    pointer is therefore treated as ambiguous and no guidance is sent.

    `AgentDB.count_live_by_key()` is the narrow upstream addition that makes this
    detectable; without it, AgentGate would have to inspect the store directly.
    """
    from core.context.managed_guidance import (
        ResolvedScope, parse_selections, resolve_managed_guidance,
    )

    store.write(
        key=GUIDANCE_KEY, value="a different instruction",
        origin="human_approved", agent_id="agentgate",
        repository_id="other-repo", task_type=TASK,
    )
    # In isolation mode the count is authorized, so pass the identity.
    assert store.count_live_by_key(GUIDANCE_KEY, agent_id="agentgate") == 2

    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        read_record=store.read,
        count_live_by_key=store.count_live_by_key,
    )
    assert resolved is None, "an ambiguous selection delivered guidance"


def test_archiving_the_sibling_restores_the_selection(store):
    from core.context.managed_guidance import (
        ResolvedScope, parse_selections, resolve_managed_guidance,
    )

    sibling = store.write(
        key=GUIDANCE_KEY, value="a different instruction",
        origin="human_approved", agent_id="agentgate",
        repository_id="other-repo", task_type=TASK,
    )
    scope = ResolvedScope(REPO, TASK, "agentgate")
    assert resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=scope, read_record=store.read,
        count_live_by_key=store.count_live_by_key,
    ) is None

    store._store.archive_record(sibling.id)
    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=scope, read_record=store.read,
        count_live_by_key=store.count_live_by_key,
    )
    assert resolved is not None and resolved.text == INSTRUCTION
