# Open-Model Agent Skills — Evaluation

A reproducible, controlled DeepInfra evaluation of OFF vs A vs B. This is an
**evaluation harness**, not production telemetry. Objective validators score the
produced artifacts; `skill_read` calls are not counted as success.

## Harness

`scripts/eval_skills.py` drives the real `DeepInfraAgentClient` (the same
`build_system_prompt` + shared `build_tools` registry the runner uses, with an
auto-allow permission callback). Per run it:

- creates a temp workspace + a temp skill catalog (fixed skills),
- builds the system prompt with (or without) the skills manifest,
- passes `skills_context`/`skill_session` only when skills are enabled,
- runs scripted turns, recording `skill_read` calls per turn,
- captures per-API-call usage (`prompt_tokens`, `cached_tokens`,
  `completion_tokens`, `estimated_cost`) by wrapping the raw call,
- grades the workspace with a deterministic validator.

Run:

```
python -m scripts.eval_skills --dry-run
python -m scripts.eval_skills --modes off,a,b
# writes agent-sessions/_eval/skills_eval_results.json
```

## Model / settings

- Provider: DeepInfra, OpenAI-compatible endpoint from `core/providers.py`.
- Model: `deepseek-ai/DeepSeek-V4.1-Flash-1017` (the configured default; fixed
  across all modes so OFF vs A vs B compare the same model).
- Controlled: same tools, same workspace inputs, same user turns per mode. Only
  the skills configuration differs.

## Fixed skills (distinct + overlapping descriptions)

| skill | description | body spells out procedure |
|---|---|---|
| `csv-clean` | clean/normalize CSV: header, trim, dedupe, drop empty rows | yes |
| `json-validate` | validate JSON shape, report first error | yes |
| `sum-digits` | sum **digit characters** (12 → 1+2), not numeric tokens | yes |

## Scenarios (built + run live)

| id | turns | relevant skill | deadline | validator |
|---|---|---|---|---|
| `csv-tidy` | clean `data.csv` → `clean.csv` | csv-clean | 1 | header + single dedupe |
| `json-recover` | detect invalid `config.json` → `app_valid.txt` | json-validate | 1 | file == "invalid" |
| `no-skill-sum` | write 17+25 to `out.txt` | none | 1 | file contains 42 |
| `digit-sum` | sum digit chars of `notes.txt` → `digits.txt` | sum-digits | 1 | file == "21" |
| `digit-chars` | sum digit chars treating 12 as 1+2 → `dc.txt` | sum-digits | 1 | file == "15" |
| `csv-header-required` | normalize to header `x,y` → `norm.csv` | csv-clean | 1 | header + dedupe |

Each scenario records: relevant skills, deadline turn, expected validated
output; selection timing (`first_read_turn`), just-in-time activation, precision/
recall of `skill_read` names, false positives, and usage/cost.

> Limitation: the spec asks for **≥24** scenarios covering overlap, mid-task
> switch, compounding compaction/resume, misleading skills, etc. This run covers
> **6** representative scenarios (obvious, failure-recovery, no-skill, procedural
> constraint, format constraint, digit-char subtlety). The harness supports
> adding more; results below are honest for the 6 run, not a 24-case study.

## Metrics

- Task success: deterministic validator pass/fail per scenario.
- Selection: `skill_read` precision/recall vs the ground-truth relevant set;
  false-positive activations; `first_read_turn` vs deadline (just-in-time).
- Overhead: summed `prompt_tokens`, `cached_tokens`, `completion_tokens`,
  `estimated_cost`, and API `calls` per mode.

## Design notes vs the spec

- OFF vs A tests the value of model-selected skills.
- A vs B tests whether harness recommendations improve activation timing/recall.
  Skill bodies are frozen identical across A and B, so routing differences are
  separated from prompt-quality differences.
- No LLM judge; no arbitrary 95% threshold. Results and a maintainer decision are
  documented in RESULTS.md.
