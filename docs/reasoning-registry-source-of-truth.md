# Reasoning registry: keeping it current

The reasoning registry (`core/reasoning/registry.py`) is a hand-curated list of
`provider -> model-pattern -> ReasoningStyle`. It answers one question before a
request goes out: *does this model accept a reasoning control, and in what
shape?* When a model is absent, `/reasoning high` reports "model not in the
registry" and the level is sent anyway but marked `unverified`.

The registry goes stale whenever a provider ships a model it has never seen —
e.g. `deepseek-ai/DeepSeek-V4.1-Flash-1017` was not matched by any DeepInfra
pattern and fell through to "unknown".

This note records where a reliable, automatic source of truth lives, so the
registry can be refreshed from data instead of from someone's memory.

## Finding: provider catalogs expose reasoning capability as tags

DeepInfra's OpenAI-compatible catalog endpoint reports, per model, a
`metadata.tags` list:

```
GET https://api.deepinfra.com/v1/openai/models
Authorization: Bearer $DEEPINFRA_API_KEY
```

Each entry looks like:

```json
{
  "id": "openai/gpt-oss-120b",
  "metadata": {
    "context_length": 131072,
    "tags": ["chat", "prompt_cache", "reasoning_effort", "reasoning"],
    "pricing": { "...": "..." }
  }
}
```

The two tags that matter map directly onto the registry's `ReasoningStyle`:

| Catalog tag        | Meaning                                   | Registry style      |
|--------------------|-------------------------------------------|---------------------|
| `reasoning_effort` | Accepts a top-level `reasoning_effort`    | `reasoning_effort`  |
| `reasoning`        | Reasoner, but no parameter to select depth| `model_variant`     |
| *(neither)*        | No reasoning support signalled            | not registered      |

Sampled across the full catalog at time of writing (178 models):

- **36** carry `reasoning_effort` — these are the ones `/reasoning low|medium|high`
  can actually drive.
- **69** carry `reasoning` (a superset that includes the 36) — capable, but the
  level is not tunable through a parameter.
- The tags are a **stable enumerated vocabulary**, not free text, so matching on
  them is safe.

Example rows:

```
openai/gpt-oss-120b                 ['chat','prompt_cache','reasoning_effort','reasoning']
deepseek-ai/DeepSeek-V4.1-Flash-1017 ['chat','vlm','vision','prompt_cache','reasoning']
Qwen/Qwen3-30B-A3B                   ['reasoning_effort','reasoning']
```

Note DeepSeek-V4.1-Flash carries only `reasoning` (no `reasoning_effort`), so
the honest registry style for it is `model_variant` — reasoning-capable, but we
should not send a `reasoning_effort` parameter. This is exactly the distinction
the old hand-list could not express, which is why the model "was not found".

## What this enables

A refresh can be driven from the catalog rather than hand-edited:

1. Fetch `/models`.
2. For each model, read `metadata.tags`.
3. Map `reasoning_effort` -> style `reasoning_effort`, `reasoning` (only) ->
   style `model_variant`, otherwise skip.
4. Regenerate the `deepinfra` block of `REGISTRY` from that mapping.

## Why not make it fully automatic at runtime

The registry is intentionally a *static, reviewable* artifact:

- It is read on a hot path (`resolve()` runs per turn); a network call there
  would add latency and a failure mode to every reasoning request.
- A static list is what makes the "unknown -> warn, don't silently send"
  behaviour auditable: you can see exactly what we claim to support.

So the recommended shape is a **refresh tool**, not a live lookup:

- `scripts/refresh_reasoning_registry.py` (or a `--refresh-registry` mode on a
  maintenance entrypoint) that fetches the catalog, prints the diff against the
  current `REGISTRY`, and writes the updated `deepinfra` block on `--apply`.
- The generated block carries a header noting it was machine-generated and from
  which endpoint/date, so a reviewer can see provenance.

Fireworks already queries its catalog for `context_length`
(`runners/fireworks.py`, `set_model`), so the "call the provider catalog" pattern
is established; this extends the same idea to the reasoning tags.

## Recommendation

1. **Now:** document the tag mapping (this file) and add
   `deepseek-ai/DeepSeek-V4.1-Flash-1017` by hand as `model_variant`, matching
   its actual tag — until the tool exists.
2. **Next:** a small refresh tool that turns the catalog tags into the registry
   block, run deliberately (not at runtime), with its output reviewed like any
   other code change.

The tag vocabulary is the source of truth; the registry is a reviewed snapshot
of it.
