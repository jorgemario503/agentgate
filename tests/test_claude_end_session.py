"""Claude must finalize the AgentDB session on close, like the other runners."""

import pytest

from runners.claude import ClaudeRunner


class FakeMiddleware:
    def __init__(self):
        self.ended = []

    async def end_session(self, session_id, jsonl_path, summary=None,
                          run_consolidation=False, llm_fn=None):
        self.ended.append((session_id, jsonl_path))


class FakeClient:
    async def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_close_client_ends_agentdb_session():
    runner = ClaudeRunner()
    runner._agentdb_middleware = FakeMiddleware()
    runner._session_workspace_paths["thread-1"] = "/tmp/ws-1"
    runner._clients["thread-1"] = FakeClient()
    runner._locks["thread-1"] = None

    await runner.close_client("thread-1")

    assert runner._agentdb_middleware.ended == [
        ("thread-1", "/tmp/ws-1/session_summary.jsonl")
    ]
    # state is cleaned up
    assert "thread-1" not in runner._clients
    assert "thread-1" not in runner._session_workspace_paths


@pytest.mark.asyncio
async def test_close_client_without_workspace_still_closes():
    runner = ClaudeRunner()
    runner._agentdb_middleware = FakeMiddleware()
    runner._clients["thread-2"] = FakeClient()

    await runner.close_client("thread-2")

    assert runner._agentdb_middleware.ended == []
    assert "thread-2" not in runner._clients


@pytest.mark.asyncio
async def test_close_client_without_agentdb_does_not_raise():
    runner = ClaudeRunner()
    runner._clients["thread-3"] = FakeClient()

    await runner.close_client("thread-3")

    assert "thread-3" not in runner._clients
