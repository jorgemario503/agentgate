# Live tests — operator & contributor guide

AgentGate ships two kinds of checks that touch a **real model provider**:

- **Live task-correctness probes** (`core/live_task_check.py`) — verify that a
  model can still answer questions whose answers depend on content that tool
  compression elided (by using retained lines or by retrieving the original).
- **Live cache-reuse probe** (same module, `run_live_cache_reuse`) — measures the
  real provider `cached_tokens` on a repeated-prefix second call.

Both are **opt-in and cost-guarded**: they never run under a plain `pytest` run
and only fire when you explicitly enable them. This guide explains how to run
them safely, what they cost, and how to read the results.

## Why they are opt-in

The default test suite is deterministic, offline, and free. Live probes hit a
real provider, spend real tokens, and depend on network + credentials, so they
are gated so that CI and normal development never incur cost or flakiness.

The wrapper in `tests/test_live_task_check.py` skips the probes unless
`AGENTGATE_LIVE_TESTS` is truthy. The budget-guard tests (which assert the guard
behaves) always run — they make no network calls.

## Enabling

```bash
export AGENTGATE_LIVE_TESTS=1              # 1/true/yes/on enables the probes
export AGENTGATE_LIVE_TOKEN_BUDGET=6000    # hard cumulative cap (default 6000)
```

The probes **reuse the same provider settings the runner already uses** — they
read `DEEPINFRA_API_KEY`, `DEEPINFRA_BASE_URL`, `DEEPINFRA_MODEL` through
`core.providers.settings_for("deepinfra")`. Nothing is hardcoded and no secrets
are logged.

Run them a few ways:

```bash
# Under pytest (the live test is skipped unless the flag is set):
AGENTGATE_LIVE_TESTS=1 python -m pytest tests/test_live_task_check.py -q

# Directly, one probe at a time:
python -c "from core.live_task_check import run_task_correctness as r; print(r())"
python -c "from core.live_task_check import run_live_cache_reuse as r; print(r())"
python -c "from core.live_task_check import run_multi_case as r; print(r())"

# As a script (runs the task-correctness probe and prints JSON; requires the flag):
AGENTGATE_LIVE_TESTS=1 python -m core.live_task_check
```

## The budget guard

Every request is **pre-checked** against the remaining budget before it is made:
if the worst-case cost (estimated prompt tokens + `MAX_COMPLETION_TOKENS`, 200)
exceeds what is left, the call is refused and recorded under `skipped` rather
than started. After each response, the provider-reported usage is recorded. When
the budget runs short the run **stops cleanly** and reports what it could not
complete — it does not overspend.

Raise `AGENTGATE_LIVE_TOKEN_BUDGET` if you want more coverage; lower it to bound
cost. Because pre-check is per-call, a budget smaller than a single worst-case
call refuses to start at all (by design).

## What each probe measures

| Probe | What it does | Key output fields |
|---|---|---|
| `run_task_correctness` | Compresses a 400-line log with one embedded `FATAL`, asks the model for the missing table, then checks retrieval | `cases[*].correct`, `cases[*].artifact_id_present`, `retrieval_exact.exact_match`, `used_tokens` |
| `run_live_cache_reuse` | Sends the same long stable prefix twice with different trailing questions; reads the 2nd call's `cached_tokens` | `turns[*].cached_tokens`, `cache_reuse_observed` |
| `run_multi_case` | Runs 4 needle cases (log / JSON anomaly / code identifier / dense line) across up to 3 registry-validated models | `results[*].{model,case,correct,changed}`, `used_tokens`, `skipped` |

`select_models()` only returns models the deepinfra reasoning registry knows
(`lookup(...) is not None`), so a probe never fires at an unknown/unlisted model.
The env model runs first.

## Reading the results

- `correct: true` on a case means the needle survived compression (visible in
  retained lines) **or** was recoverable — either way the feature did not destroy
  needed content.
- `retrieval_exact.exact_match: true` means the compressed original round-trips
  byte-exactly through `tool_result_read`.
- `cache_reuse_observed: true` means the provider genuinely served the second
  call from cache (`cached_tokens > 0`) — an honest verdict, not an assumption.
- `skipped` lists anything the budget guard refused; `used_tokens` is the real
  spend for the run.

## Cost & safety notes

- No secrets are printed; results contain only answers and token counts.
- Deterministic fixtures are generated locally (`_make_needle_log`, `_cases`);
  only the model call is live.
- The offline `python -m core.benchmark` harness is the free alternative for
  comparing configurations without any network calls; use live probes to confirm
  behavior against a real model.

## Where it lives

| File | Role |
|---|---|
| `core/live_task_check.py` | Probes + `TokenBudget` guard + model selection |
| `tests/test_live_task_check.py` | Gated pytest wrapper (skips unless `AGENTGATE_LIVE_TESTS`) |
| `core/benchmark.py` | Offline deterministic replay (`python -m core.benchmark`) |
| `.env.example` | Documents `AGENTGATE_LIVE_TESTS`, `AGENTGATE_LIVE_TOKEN_BUDGET` |
