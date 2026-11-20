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

A refresh is driven from the catalog rather than hand-edited:

1. Fetch `/models`.
2. For each model, read `metadata.tags`.
3. Map `reasoning_effort` -> style `reasoning_effort`, `reasoning` (only) ->
   style `model_variant`, otherwise skip.
4. Write the result to `core/reasoning/registry_generated.py`, which
   `registry.py` appends *after* its hand-written patterns.

## The tool

`scripts/refresh_reasoning_registry.py` implements the refresh:

```bash
python scripts/refresh_reasoning_registry.py           # show the diff
python scripts/refresh_reasoning_registry.py --apply   # write the fragment
```

It fetches the catalog (key resolved through `core.providers`, same as a bot),
maps tags to styles, and diffs against the current generated fragment, printing
`+`/`-`/`~` lines. `--apply` rewrites `core/reasoning/registry_generated.py`
with a provenance header (endpoint + UTC timestamp). The fragment is imported by
`registry.py` and layered **under** the hand-written patterns, so an explicit
human entry always wins.

## Why not make it fully automatic at runtime

The registry is intentionally a *static, reviewable* artifact:

- It is read on a hot path (`resolve()` runs per turn); a network call there
  would add latency and a failure mode to every reasoning request.
- A static list is what makes the "unknown -> warn, don't silently send"
  behaviour auditable: you can see exactly what we claim to support.

So the catalog is consulted **deliberately**, by the tool, and its output is
reviewed like any other code change — not on every request.

Fireworks already queries its catalog for `context_length`
(`runners/fireworks.py`, `set_model`), so the "call the provider catalog" pattern
is established; this extends the same idea to the reasoning tags.

## Recommendation

1. **Now (done):** the tag mapping is documented here and encoded in
   `scripts/refresh_reasoning_registry.py`; `core/reasoning/registry_generated.py`
   is generated from the live catalog. The DeepSeek V3/V4 families are no longer
   hand-maintained — the generated fragment records their real tags (V3.1 carries
   `reasoning_effort`, V4.x carries `reasoning` only).
2. **Ongoing:** re-run the tool when a provider ships new models, review the
   diff, commit the regenerated fragment. The tag vocabulary is the source of
   truth; the registry is a reviewed snapshot of it.

## Execution notes (how a resolved style reaches the request)

`resolve(provider, model)` -> `build_request_kwargs(resolution, level, model)`
is shared by all three open-model runners. The resulting kwargs are merged into
the outgoing request by one of two transports:

| Runner    | Transport                         | Merge                      |
|-----------|-----------------------------------|----------------------------|
| local     | raw `httpx` (`json=payload`)      | `payload.update(kwargs)`   |
| deepinfra | OpenAI SDK `chat.completions.create(**kwargs)` | `kwargs.update(...)` |
| fireworks | OpenAI SDK `chat.completions.create(**kwargs)` | `kwargs.update(...)` |

For the `reasoning_effort` style (`{"reasoning_effort": level}`) this is correct
on all three: it is a top-level JSON field on the wire, a first-class SDK
parameter, and a top-level key under raw httpx alike.

The `extra_body` style (`{"extra_body": {"reasoning": {"effort": level}}}`) is
valid only for the SDK transports, where `extra_body` is an SDK kwarg the client
unwraps into the request body. On the raw-httpx **local** runner the same dict
would be sent as a literal `extra_body` JSON field, which is wrong. This is
currently **latent**: no registry entry uses the `extra_body` style. If one is
added for a model served by the local runner, the merge for that transport must
be adjusted (unwrap `extra_body` into the payload) rather than sending it
verbatim.

The verify path (`_record_reasoning` -> `extract_reasoning_content` ->
`classify_outcome`) is symmetric across all three runners.
