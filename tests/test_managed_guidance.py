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
    import tempfile

    import runners.deepinfra as di

    return di.DeepInfraAgentClient(
        model="test-model",
        api_key="test",
        tools={},
        system_prompt="SYSTEM",
        session_id="t1",
        session_store=di.JsonFileSessionStore(directory=tempfile.mkdtemp()),
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
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
    ) is None


@pytest.mark.parametrize("scope", [
    ResolvedScope(None, TASK, "agentgate"),
    ResolvedScope(REPO, None, "agentgate"),
    ResolvedScope(None, None, "agentgate"),
])
def test_unresolved_scope_yields_nothing(store, scope):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    assert resolve_managed_guidance(
        selections=selections, scope=scope, select_unique=store.select_unique,
    ) is None


def test_denied_read_yields_nothing(store):
    selections = parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}")
    # An identity the policy does not permit to read memory/**.
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "stranger"),
        select_unique=store.select_unique,
    ) is None


def test_withdrawn_mapping_yields_nothing(store):
    """Withdrawal is removing the selection; the record may still exist."""
    assert resolve_managed_guidance(
        selections=(),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
    ) is None


def test_ambiguous_mapping_yields_nothing(store):
    selections = (
        ManagedGuidanceSelection(REPO, TASK, GUIDANCE_KEY),
        ManagedGuidanceSelection(REPO, TASK, "memory/guidance/second"),
    )
    assert resolve_managed_guidance(
        selections=selections,
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
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
        select_unique=store.select_unique,
    )
    assert first is not None

    # Same session, now working in another repository.
    switched = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope("another-repo", TASK, "agentgate"),
        select_unique=store.select_unique,
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
    # Two live siblings: select_unique declines to choose.
    assert store.select_unique(GUIDANCE_KEY, agent_id="agentgate") is None

    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        select_unique=store.select_unique,
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
        scope=scope, select_unique=store.select_unique,
    ) is None

    store._store.archive_record(sibling.id)
    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=scope, select_unique=store.select_unique,
    )
    assert resolved is not None and resolved.text == INSTRUCTION


# ---------------------------------------------------------------------------
# Against the released API, with generic memory also enabled
# ---------------------------------------------------------------------------

def test_guidance_coexists_with_generic_memory_recall(store, scripted):
    """The managed asset must not arrive through generic recall as well.

    Generic memory is enabled on the same turn: an ordinary record that matches
    the query is injected through the memory path, and the managed instruction
    arrives exactly once through its own.
    """
    store.write(
        key="memory/note/komper", value="the deploy checklist covers index usage",
        origin="agent_inferred", agent_id="agentgate", session_id="s-1",
        repository_id=REPO, task_type=TASK,
    )
    resolved = resolve_managed_guidance(
        selections=parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"),
        scope=ResolvedScope(REPO, TASK, "agentgate"),
        select_unique=store.select_unique,
    )
    assert resolved is not None

    client = _client(scripted)
    # Both paths active on the same turn.
    _send(client, "deploy checklist", ephemeral=resolved.text)

    sent = scripted.text_sent()
    assert sent.count(INSTRUCTION) == 1, "managed guidance did not arrive exactly once"


def test_recalled_memory_is_stripped_of_the_managed_record(store):
    """A managed record recovered by generic recall is removed before injection.

    The record is a normal governed record, so `retrieve()` can return it. The
    guarantee is not that it is unretrievable — it is that it does not *reach the
    model* by that second path. The runner strips it from recalled memory before
    assembling the turn.
    """
    from core.context.managed_guidance import strip_guidance_record_lines

    hits = store.retrieve("Verify upgrade database previous release",
                          agent_id="agentgate", limit=20)
    recalled = [r for r in hits.records if r.key == GUIDANCE_KEY]
    assert recalled, "expected generic recall to surface the managed record"

    # Recall renders records as "- key: value"; the namespace is dropped wholesale.
    rendered = f"- {GUIDANCE_KEY}: {INSTRUCTION}"
    assert strip_guidance_record_lines([rendered]) == []


def test_stripping_keeps_other_recalled_records(store):
    from core.context.managed_guidance import strip_guidance_record_lines

    block = (
        "- memory/note/komper: the deploy checklist covers index usage\n"
        f"- {GUIDANCE_KEY}: {INSTRUCTION}"
    )
    kept = strip_guidance_record_lines([block])
    assert len(kept) == 1
    assert "deploy checklist" in kept[0]
    assert GUIDANCE_KEY not in kept[0]


def test_an_older_agentdb_omits_guidance_rather_than_guessing(monkeypatch):
    """Without select_unique, the feature is omitted — not degraded to read()."""
    import runners.deepinfra as di

    runner = di.DeepInfraRunner.__new__(di.DeepInfraRunner)
    runner._agentdb = object()          # an 0.8.0 store: no select_unique

    import core.config as config

    monkeypatch.setattr(
        config, "managed_guidance_selections",
        lambda: (parse_selections(f"{REPO}:{TASK}:{GUIDANCE_KEY}"), []),
    )
    assert runner._managed_guidance_text("t1", "/ws", "/tmp") == ""


# ---------------------------------------------------------------------------
# Through run_prompt(): the real entry point, generic recall enabled
# ---------------------------------------------------------------------------

def _platform(tmp_path, store):
    """A PlatformConfig whose session store lives under tmp_path."""
    from core.sessions import SessionStore
    from runners.base import PlatformConfig

    base = tmp_path / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    return PlatformConfig(session_store=SessionStore(base=str(base)), agent_id="agentgate")


def _runner_with_agentdb(scripted, store, monkeypatch, *, repo, task):
    """A DeepInfraRunner wired to a real AgentDB, as the bots wire it."""
    import core.config as config
    import runners.deepinfra as di

    monkeypatch.setattr(config, "AGENTGATE_MANAGED_GUIDANCE",
                        f"{repo}:{task}:{GUIDANCE_KEY}")
    monkeypatch.setattr(config, "AGENTGATE_MANAGED_GUIDANCE_REPOSITORY", repo)
    monkeypatch.setattr(config, "AGENTGATE_MANAGED_GUIDANCE_TASK_TYPE", task)
    monkeypatch.setattr(config, "AGENTGATE_MANAGED_GUIDANCE_AGENT_ID", "agentgate")
    # Generic memory recall must be on, so the exclusion is actually exercised.
    monkeypatch.setattr(config, "AGENTDB_ENABLED", True)

    runner = di.DeepInfraRunner(model="test-model")
    return runner


async def _drive(runner, platform, thread_id, workspace, prompt):
    async def post_fn(text):
        return None

    return await runner.run_prompt(
        thread_id=thread_id, workspace_path=workspace, prompt=prompt,
        resume_session_id=None, post_fn=post_fn, platform=platform,
    )


def _run_once(runner, store, tmp_path, prompt, *, thread_id="t1", repo=REPO):
    """Wire AgentDB into the runner and drive exactly one real turn.

    The workspace is a real repository named after ``repo``, so the session has the
    work-item evidence a real one would: a session in ``komper`` is that work item,
    and a session in another repository is not.
    """
    import asyncio

    platform = _platform(tmp_path, store)
    runner._agentdb = store
    runner.init_agentdb(store, platform)
    # Named exactly as the repository, since the resolved root's basename is what
    # identifies the work item.
    workspace = tmp_path / repo
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    asyncio.run(_drive(runner, platform, thread_id, str(workspace), prompt))
    return workspace


def test_run_prompt_delivers_the_instruction_once(store, scripted, tmp_path, monkeypatch):
    """Matching binding, authorized caller, generic recall on: one instruction."""
    store.write(
        key="memory/note/komper", value="the deploy checklist covers index usage",
        origin="agent_inferred", agent_id="agentgate", session_id="s-1",
        repository_id=REPO, task_type=TASK,
    )
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=TASK)
    _run_once(runner, store, tmp_path, "deploy checklist")

    sent = scripted.text_sent()
    assert sent.count(INSTRUCTION) == 1, (
        f"expected the instruction exactly once, found {sent.count(INSTRUCTION)}"
    )


@pytest.mark.parametrize("repo,task,label", [
    (REPO, None, "unresolved task scope"),
    (None, TASK, "unresolved repository"),
    ("another-repo", TASK, "scope mismatch"),
])
def test_run_prompt_sends_nothing_without_a_matching_binding(
    store, scripted, tmp_path, monkeypatch, repo, task, label,
):
    """Unresolved scope and scope mismatch both yield zero."""
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=repo, task=task)
    _run_once(runner, store, tmp_path, "deploy checklist")
    assert INSTRUCTION not in scripted.text_sent(), label


def test_run_prompt_sends_nothing_after_withdrawal(store, scripted, tmp_path, monkeypatch):
    """Withdrawal (the record gone) yields zero on the next real turn."""
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=TASK)
    _run_once(runner, store, tmp_path, "deploy checklist")
    assert INSTRUCTION in scripted.text_sent()

    store.delete(GUIDANCE_KEY, agent_id="agentgate", trust_zone="human_approved")
    _run_once(runner, store, tmp_path, "deploy checklist")
    assert INSTRUCTION not in scripted.text_sent(), "guidance survived withdrawal"


def test_run_prompt_keeps_guidance_out_of_recall_even_when_not_selected(
    store, scripted, tmp_path, monkeypatch,
):
    """The namespace is excluded from recall regardless of the binding.

    With the binding unresolved, no instruction is selected — but the record must
    still not arrive through generic recall.
    """
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=None)
    _run_once(runner, store, tmp_path, "Verify upgrade database previous release")
    assert INSTRUCTION not in scripted.text_sent()


# ---------------------------------------------------------------------------
# Work-item binding: one bot, several repositories
# ---------------------------------------------------------------------------

def test_the_same_bot_does_not_send_guidance_to_another_repository(
    store, scripted, tmp_path, monkeypatch,
):
    """Finding 1: the binding is per work item, not per process.

    The process is configured for komper:bug_fix. A session working in another
    repository on the same bot must receive nothing.
    """
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=TASK)

    # A session in a different repository.
    _run_once(runner, store, tmp_path, "deploy checklist", thread_id="other", repo="another-repo")
    assert INSTRUCTION not in scripted.text_sent(), (
        "the configured repository's instruction reached a different repository"
    )

    # The same bot, the matching work item, still gets it once.
    _run_once(runner, store, tmp_path, "deploy checklist", thread_id="komper")
    assert scripted.text_sent().count(INSTRUCTION) == 1


def test_a_session_with_no_resolved_repository_gets_nothing(
    store, scripted, tmp_path, monkeypatch,
):
    """No work-item evidence and no configuration means no guidance."""
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=TASK)

    import asyncio
    platform = _platform(tmp_path, store)
    runner._agentdb = store
    runner.init_agentdb(store, platform)
    bare = tmp_path / "scratch"
    bare.mkdir(exist_ok=True)          # not a repository
    asyncio.run(_drive(runner, platform, "t1", str(bare), "deploy checklist"))

    assert INSTRUCTION not in scripted.text_sent()


# ---------------------------------------------------------------------------
# Whole-record exclusion: a multi-line instruction must not leak
# ---------------------------------------------------------------------------

MULTILINE = (
    "Verify the upgrade from the previous release.\n"
    "Then run the migration twice.\n"
    "Finally compare the schemas."
)


def test_a_multiline_instruction_is_excluded_whole_from_recall(
    store, scripted, tmp_path, monkeypatch,
):
    """Finding 2: continuation lines are part of the record, not loose text.

    A line filter dropped only the bullet bearing the key, leaving the rest of the
    value in the request. The whole record is excluded now, and an ordinary memory
    in the same recall result still reaches the provider.
    """
    store.write(
        key=GUIDANCE_KEY, value=MULTILINE, origin="human_approved",
        agent_id="agentgate", session_id=None,
        repository_id=REPO, task_type=TASK,
    )
    store.write(
        key="memory/note/komper", value="deploy checklist covers index usage",
        origin="agent_inferred", agent_id="agentgate", session_id="s-1",
        repository_id=REPO, task_type=TASK,
    )
    runner = _runner_with_agentdb(scripted, store, monkeypatch, repo=REPO, task=None)
    # One query that matches BOTH records, so the exclusion is exercised against a
    # recall result that also carries an ordinary memory.
    _run_once(runner, store, tmp_path, "deploy checklist verify upgrade migration")

    sent = scripted.text_sent()
    # No selected guidance (task unset), so the whole instruction must be absent.
    assert "Then run the migration twice" not in sent, (
        "a continuation line of the managed instruction leaked into the request"
    )
    assert "Finally compare the schemas" not in sent
    assert "Verify the upgrade from the previous release" not in sent
    # The ordinary memory in the same recall result still arrives.
    assert "deploy checklist covers index usage" in sent


def test_multiline_exclusion_keeps_following_records():
    """A managed record's continuation must not swallow the next record."""
    from core.context.managed_guidance import strip_guidance_record_lines

    block = (
        "- memory/guidance/komper/bug-fix: line one\n"
        "  line two continues\n"
        "- memory/note/komper: an ordinary memory"
    )
    kept = strip_guidance_record_lines([block])
    assert len(kept) == 1
    assert "an ordinary memory" in kept[0]
    assert "line one" not in kept[0] and "line two" not in kept[0]
