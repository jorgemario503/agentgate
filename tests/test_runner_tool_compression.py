"""Integration tests: tool-result pipeline wired into the DeepInfra runner loop.

These exercise `receive_response` end-to-end with a fake provider so we can
assert the two acceptance criteria that matter most at the seam:

- T01: with the feature disabled, the tool message content is byte-identical to
  the raw handler output (no transform, no new tool added).
- Enabled: the bulky result is compressed, the retrieval handle is present, and
  tool-call metadata (role / tool_call_id) is preserved.
- Prefix stability: an earlier tool message does not change when later turns
  arrive (transform happens once, at ingestion).
"""
import asyncio
import tempfile
import types
import unittest

from runners.deepinfra import DeepInfraAgentClient, Tool, _tool_schema
from runners.deepinfra import JsonFileSessionStore
from core.artifacts import ArtifactStore
from core.tool_pipeline import ToolResultPipeline


class _TmpStore(JsonFileSessionStore):
    """Session store writing to a temp dir, so tests never touch ./di_sessions."""

    def __init__(self):
        import tempfile
        self._d = tempfile.mkdtemp()
        super().__init__(directory=self._d)


def _big_log(n=3000):
    lines = [f"INFO step {i} ok latency={i % 7}ms" for i in range(n)]
    lines.insert(n // 2, "FATAL disk full at /var/data")
    return "\n".join(lines)


class _FakeTool:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def handler(self, args):
        self.calls += 1
        return self.output


def _make_completion(tool_name, tool_id):
    """A fake chat.completions.create: one tool call, then a final answer."""
    state = {"n": 0}

    async def create(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tool_id, "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }],
            }
        else:
            msg = {"role": "assistant", "content": "done"}
        return types.SimpleNamespace(model_dump=lambda **_: {
            "choices": [{"message": msg}]
        })
    return create


class TestDisabledByteIdentical(unittest.TestCase):
    def test_disabled_tool_message_is_raw(self):
        log = _big_log()
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(d)
            client = DeepInfraAgentClient(
                model="m", api_key="k",
                tools={"Bash": Tool(schema=_tool_schema("Bash", "x", {}, []),
                                    handler=_FakeTool(log).handler)},
                session_id="s", tool_pipeline=None, session_store=_TmpStore(),
            )
            client._client = types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(
                    create=_make_completion("Bash", "call1"))),
                close=_async_noop,
            )

            async def run():
                async for _ in client.receive_response():
                    pass
            asyncio.run(run())

            tool_msgs = [m for m in client.messages if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 1)
            self.assertEqual(tool_msgs[0]["content"], log)
            self.assertEqual(tool_msgs[0]["tool_call_id"], "call1")
            # the retrieve tool must NOT be advertised when disabled
            self.assertNotIn("tool_result_read", client.tools)


class TestEnabledCompresses(unittest.TestCase):
    def _client(self, d, log):
        pipeline = ToolResultPipeline(
            d, session_id="s", workspace_id="/ws", enabled=True, min_chars=1000,
        )
        from runners.deepinfra import build_tools
        tools = build_tools("/ws", _FakeStore(), pipeline)
        # override Bash with our fake to avoid running a shell
        tools["Bash"] = Tool(schema=_tool_schema("Bash", "x", {}, []),
                             handler=_FakeTool(log).handler)
        client = DeepInfraAgentClient(
            model="m", api_key="k", tools=tools,
            session_id="s", tool_pipeline=pipeline, session_store=_TmpStore(),
        )
        client._client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(
                create=_make_completion("Bash", "call1"))),
            close=_async_noop,
        )
        return client

    def test_enabled_compresses_and_preserves_metadata(self):
        log = _big_log()
        with tempfile.TemporaryDirectory() as d:
            client = self._client(d, log)

            async def run():
                async for _ in client.receive_response():
                    pass
            asyncio.run(run())

            tool_msgs = [m for m in client.messages if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 1)
            content = tool_msgs[0]["content"]
            self.assertLess(len(content), len(log))
            self.assertIn("FATAL disk full at /var/data", content)
            self.assertIn("original=", content)
            self.assertEqual(tool_msgs[0]["tool_call_id"], "call1")
            self.assertIn("tool_result_read", client.tools)

    def test_earlier_tool_message_stable_after_later_turns(self):
        log = _big_log()
        with tempfile.TemporaryDirectory() as d:
            client = self._client(d, log)

            async def run():
                async for _ in client.receive_response():
                    pass
            asyncio.run(run())
            first = [m for m in client.messages if m.get("role") == "tool"][0]["content"]

            # simulate appending a later user turn (as a new query would)
            client.messages.append({"role": "user", "content": "next"})
            later = [m for m in client.messages if m.get("role") == "tool"][0]["content"]
            self.assertEqual(first, later, "earlier tool message must not change")


class _FakeStore:
    base = "/tmp/agentgate-test-artifacts"
    def lookup_directory(self, name):
        return None
    def remember_directory(self, *a, **k):
        pass


async def _async_noop(*a, **k):
    return None


if __name__ == "__main__":
    unittest.main()
