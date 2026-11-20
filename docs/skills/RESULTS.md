# Open-Model Agent Skills — Results

Feature is **OFF by default**. This document records the evidence and the
release decision.

## Baseline tests (pre-change)

At source commit `8410073` (merge-base `2001f33`): `509 passed, 3 skipped`
(see the Phase-0 note in DECISIONS.md). After the change:

- Full suite: **543 passed, 3 skipped** = 546 tests (`python -m pytest -q`).
- Baseline (excluding the 3 new files): **509 passed, 3 skipped** (512 tests).
- New tests: **34** across
  - `tests/test_skills.py` (24): S01–S13,
  - `tests/test_skills_runner_integration.py` (7): S11/S12/S14/S15 outgoing-message checks,
  - `tests/test_skills_guards.py` (3): S17 no Claude/AgentDB surface, S18 disabled parity.

Deterministic/skipped coverage: the pre-existing 3 skips are live probes
(gated off), unchanged.

## Deterministic coverage mapping (spec §10)

| ID | Covered by |
|---|---|
| S01 disabled equivalence | test_skills TestDisabledEquivalence |
| S02 conformance/malformed | TestConformance |
| S03 duplicates | TestDuplicates |
| S04 no eager body | TestNoEager |
| S05 deterministic manifest+budget+hidden | TestManifest |
| S06 exact body, no compression | TestBody (+ loop bypass test) |
| S07 traversal/symlink | TestPathSafety |
| S08 allowed-tools no perm change | TestAllowedTools |
| S09 same registry all runners | TestSharedRegistry |
| S10 prefix stable | TestPrefixStable |
| S11 no repeat while present | TestSessionLifecycle + integration |
| S12 reactivation on trim | TestSessionLifecycle + integration |
| S13 cold resume/changed fingerprint | TestSessionLifecycle |
| S14 unrelated abstention | TestModeBAbstention |
| S15 B never injects bodies/mutates prefix | TestModeBAbstention + integration |
| S16 errors/missing root/malformed | TestConformance + discover() |
| S17 no Claude/AgentDB | test_skills_guards |
| S18 no compression/approval regressions | test_skills_guards |

## Mechanism verification (end to end, real client)

- Manifest appears in the system prompt; `skill_read`/`skill_list` are in the
  tool set only when enabled.
- A successful `skill_read` places the exact body once; while present there is
  no repeat; after trimming, exactly one reactivation block is injected as
  volatile context and the stable system prefix is unchanged.
- Path traversal returns a clean `ERROR:`; the body is not truncated by
  `output_cap`.

## Controlled DeepInfra evaluation (live)

Model `deepseek-ai/DeepSeek-V4.1-Flash-1017`, 6 scenarios × {OFF, A, B}.

| mode | task ok | skill_reads | false-pos | prompt tok | cached tok | cost (USD) | API calls |
|---|---|---|---|---|---|---|---|
| OFF | 6/6 | 0 | 0 | 89,635 | 80,256 | 0.00211 | 31 |
| A   | 6/6 | 0 | 0 | 115,815 | 105,088 | 0.00234 | 27 |
| B   | 6/6 | 0 | 0 | 122,273 | 118,784 | 0.00152 | 23 |

### Observations

- **No objective task-success difference** between OFF, A and B on this set:
  the model solved every scenario directly.
- **The model never called `skill_read`** in either A or B, even with the
  manifest present and tool advertised. It treated these tasks as solvable
  without procedural guidance.
- **Mode A adds manifest overhead** (~+29% prompt tokens vs OFF here) with no
  outcome gain — a pure cost on these tasks.
- **Mode B cost less this run** only because it happened to take fewer API
  calls (23 vs 31); that is run variance, not a skills benefit. B made **no
  recommendations that led to a read**, so it did not exercise its routing value.

### Caveats

- Small sample (6 scenarios, single run per cell, one model). Not statistically
  powered; no significance claim.
- Scenarios were designed to be objectively validatable and to reward the skill
  procedure (digit-character summation vs numeric summation; explicit CSV
  header), yet a strong model still passed without reading. Choosing tasks a
  model *cannot* do without the skill is the hard part and is left as future
  work (see "Open questions").

## Verdict on Mode B

B does **not** merit its added complexity on current evidence: it produced no
recommendation that changed behavior and no measurable benefit over A. It is
implemented behind `AGENTGATE_SKILLS_SELECTION_MODE=recommend` and is fully
tested, but the evidence does not justify making it the default.

## Release decision

**Keep the feature OFF by default.**

Gate check (spec §11):
- All deterministic tests + security invariants pass. ✅
- No harmful regressions; disabled path byte-for-byte unchanged. ✅
- Objective task-success improvement vs OFF: **not demonstrated** on this set. ❌
- B benefit over A: **not demonstrated**. ❌

Per the spec's own rule ("If skills do not improve outcomes, keep disabled and
report limitations"), the default stays `AGENTGATE_SKILLS_ENABLED=false`. The
capability is available for operators who want it and for further evaluation.

## Open questions / next steps to re-open the gate

1. Expand to ≥24 scenarios, including cases where a base model provably fails
   without the skill (misleading defaults, long multi-turn compaction/resume,
   overlapping skills, mid-task switches, misleading/low-quality skills).
2. Use a model known to be weaker at the target procedures, so the skill's
   lift is observable; then compare A vs OFF on the failure set.
3. Only if a skill-dependent failure set reproduces, re-run A vs B to test
   whether B's recommendations improve just-in-time activation — otherwise ship
   A only, or nothing.
4. Consider shaping task outputs so the procedure is load-bearing (e.g. counts,
   ordering, exact serialization) where a "natural" approach differs.
