# Context efficiency (native tool-result compression)

Status: **Phase 1 implemented, default OFF.** Optional feature; nothing changes
unless you set a flag. See `docs/spec/` for the full multi-phase design.

## What it does

When a tool returns a **bulky text result** (long logs, large JSON, minified
assets), AgentGate can compress it before the model sees it, keeping the exact
original on disk and retrievable. This saves input tokens on the stateless
runners (local / fireworks / deepinfra) that re-send full history each turn.

It is **targeted and conservative**:

| Content | Behaviour |
|---|---|
| Repetitive logs | Collapse similar lines; keep first/last of a run, **all** error/fatal/traceback/exit lines and surrounding context |
| Large JSON arrays/objects | Annotated summary with counts + representative/anomalous records |
| Dense lines (minified JS/CSS, base64) | Keep head + tail, elide the middle with a count |
| Source code, diffs, tracebacks | **Conservative** — never lossy-reworded; tracebacks keep the terminal exception |
| System / user / assistant messages | **Never touched** |

Compression is **not redaction**. Secrets are not scrubbed by this feature.

## Safety properties (enforced by tests)

- **Fail open.** Any error returns the original tool text unchanged; a tool turn
  never breaks because of compression (`compact_fallback`).
- **Original always kept.** If the original cannot be stored, the text is *not*
  compressed (no lossy output under a false retrieval promise).
- **Prefix-stable.** A tool message is transformed once, when first appended; it
  is never rewritten when later turns arrive, so the provider cache prefix holds.
- **Retrieval is authorized.** `tool_result_read` checks the caller's session
  **and** workspace on every read; an id from another session returns nothing.
  Ids are opaque, path-confined, restart-persistent, and reads are bounded.
- **No new network/model calls.** Pure, deterministic, in-process.

## Configuration

In `.env` (or the environment):

```dotenv
AGENTGATE_TOOL_COMPRESSION_ENABLED=true   # master switch (default false)
AGENTGATE_TOOL_COMPRESSION_MIN_CHARS=4000 # only results >= this are considered
AGENTGATE_TOOL_ARTIFACT_DIR=              # blank -> <session-base>/_artifacts
```

With the flag off, outbound requests are byte-for-byte identical to before.

## Example: compressed tool output and retrieval

A `Bash` result of 3,000 log lines with one `FATAL` becomes (elided middle):

```
INFO step 0 ok latency=0ms
…[1499 similar lines elided: info step # ok latency=#ms]…
INFO step 1500 ok latency=3ms
FATAL disk full at /var/data
INFO step 1501 ok latency=4ms
…[1498 similar lines elided: info step # ok latency=#ms]…
INFO step 2999 ok latency=6ms

[AgentGate compressed tool result | original=<64-hex-id> | original_chars=58,918 |
 emitted_chars=612 | transforms=log_collapse |
 retrieve=tool_result_read('<64-hex-id>', start, limit)]
```

The model can recover any part exactly:

```
tool_result_read(artifact_id="<64-hex-id>", offset=0, limit=20000)
```

## Where it lives

| Module | Role |
|---|---|
| `core/tool_compression.py` | Pure transforms + `maybe_compress()`; never raises |
| `core/artifacts.py` | Bounded, scoped, restart-persistent original store |
| `core/tool_pipeline.py` | Shared runner adapter + `tool_result_read` handler |
| `core/config.py` | Flags (`AGENTGATE_TOOL_COMPRESSION_*`) |
| `runners/{local,fireworks,deepinfra}.py` | One call at the tool-result boundary |

## Tests

```bash
python -m pytest tests/test_artifacts.py tests/test_tool_compression.py \
                 tests/test_tool_pipeline.py tests/test_runner_tool_compression.py -q
```

Covers: disabled == identity, log/JSON/dense/traceback transforms, fail-open,
store-unavailable refusal, retrieval round-trip, cross-session isolation,
path/symlink security, metadata + prefix stability.
