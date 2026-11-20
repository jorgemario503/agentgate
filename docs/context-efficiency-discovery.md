# Discovery note — Phase 0 (context efficiency)

Phase 0 deliverable from `docs/spec/SPEC.md` §7: discover the real call paths
before changing anything, record concrete files/functions (not historical line
numbers), establish the baseline, and name the integration points.

## Baseline

- `python -m pytest -q` -> **421 passed** on entry (branch `main`, clean tree).
- Python 3.14 (project `.venv`); `openai` 2.54.0 present.
- Note: `AGENTS.md` says "expect 366"; the suite had grown. Not changed here.

## Confirmed code paths (verified in the working tree)

| Concern | Location | Finding |
|---|---|---|
| Tool-result creation | `runners/{deepinfra,local,fireworks}.py`, `receive_response()` tool loop | Byte-identical in all three except the log prefix. Sequence: `result_text = await self._run_handler(...)` -> `cap = self._max_tool_result_chars` -> optional `_summarize_tool_result` -> `self.messages.append({"role":"tool","tool_call_id":tc["id"],"content":result_text})`. |
| Output cap | `core/providers.py` `tool_output_cap` (default 8000); runner `_max_tool_result_chars()` (8k-100k) | Raw handler output already truncated at `_OUTPUT_CAP` inside the `_make_*` handlers. |
| Message shape | OpenAI-style dicts | `role`, `tool_call_id`, `content`, `tool_calls`; role and tool_call_id must be preserved. |
| Session/workspace | `core/sessions.py` `SessionStore`; `core/history.py` `JsonFileHistoryStore` (`{name}_history/<id>.json`); workspace under `agent-sessions/<platform>/<thread>/workspace` | `store.base` is the per-platform base; workspace path known at client build. |
| Compaction / trim | `_maybe_compact_history()` + `_trim_history()` per runner, before `_build_outgoing_messages()` | Operate on `self.messages`; transform must run at **append** time to keep the prefix stable. |
| Cache keys | `core/cache_key.py` `cache_key_kwargs(provider, thread_id)` | `prompt_cache_key` / `x-session-affinity`; per-session prefix must stay byte-stable. |
| Prompt assembly | `core/prompts.py`, `prompts/*.md`, `SYSTEM_PROMPT`/`FALLBACK_HARD_RULES` | System message is index 0 and never enters the tool-result path. |
| Permissions / approvals | `core/approvals.py`; `permission_decision` + `can_use_tool` gate before `_run_handler` | Compression runs *after* the gate, on the result; no effect on approval. |
| AgentDB | `core/agentdb.py` `AgentDBSurface`; `on_compact` callback | Injects into system prompt / ephemeral context; out of scope. |
| Config | `core/config.py` via `os.environ.get(NAME, default)` | Single place to add feature flags. |

## Integration decision

A single shared adapter in `core/` (`core/tool_pipeline.py`) called at the one
tool-result append boundary in each runner - not three divergent copies, per
`AGENTS.md` ("don't copy blocks across runners") and `docs/spec/ARCHITECTURE.md`.
Transform runs **once at ingestion** so earlier serialized messages never change
(prefix-cache invariant, acceptance criterion 4).

## Feature flags (all default off)

`AGENTGATE_TOOL_COMPRESSION_ENABLED`, `AGENTGATE_CROSS_TURN_DEDUP_ENABLED`,
`AGENTGATE_REPO_MAP_ENABLED` (+ min-chars / artifact-dir / map-budget knobs).

## Live evaluation gateway

`AGENTGATE_LIVE_TESTS=1` enables the opt-in DeepInfra probes; they reuse the
runner's existing `DEEPINFRA_*` environment variables and model id and run
under a hard cumulative token budget (`AGENTGATE_LIVE_TOKEN_BUDGET`).
