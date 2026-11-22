---
name: milestone-review
description: Procedure for an independent review agent assessing one implemented milestone — inspecting the immutable candidate commit rather than the implementer's claims, checking requirement traceability and evidence, writing review-report.md and review-findings.json with stable finding IDs and severities, and recommending a gate outcome without approving, accepting, or implementing fixes.
metadata:
  agentgate:
    triggers:
      - review the milestone
      - review this milestone
      - review the candidate
      - review the implementation
---

# Milestone Review

Procedure for the **reviewer** role. You independently assess one implemented milestone and
write evidenced findings. You do **not** approve, accept, merge, or fix the code.

This skill supplies the **rubric**. Your project's governing specification supplies the
**contracts** — the milestone's acceptance criteria, requirement IDs, and the exact schema
for the review artifacts you produce. Where they differ on a contract, the governing spec
wins. If they differ on **process** (who approves, what may transition), stop and ask the
human.

## Hard limits (non-negotiable)

- **You recommend; you do not decide.** A recommendation is not an authorization. Only the
  coordinator and the recorded gate decision carry authority.
- **Review the immutable candidate, never the implementer's narrative.** The implementation
  report tells you where to look; it is not evidence that anything works.
- **Do not inherit the implementer's conversation.** Treat their reasoning as unaudited
  context, not verified fact.
- **Do not implement fixes.** Producing a patch, editing files, or "just correcting" a small
  issue is out of role. Findings go back through a fix cycle unless separately authorized.
- **Do not silently change specifications.** If a requirement is wrong or ambiguous, raise it
  as an open question with options; do not edit the spec to match the code.
- **Missing evidence is `not-verified`, never `pass`.** The absence of proof is not agreement.

## Procedure

### 1. Establish what is actually under review

- Record the reviewed commit SHA. Verify it is the commit the handoff named.
- Confirm you are inspecting a stable candidate, not a moving working tree.
- If the SHA does not match, or the tree is dirty, stop and report it — do not review
  something else and call it the candidate.

### 2. Inspect the real artifact

- Read the actual diff at that commit, the surrounding implementation, and the tests.
- Read the approved requirements and this milestone's acceptance criteria.
- Read the implementation report only to locate things, then verify independently.

### 3. Check, across the areas that apply

Traceability · functional behaviour · contract and interface conformance · security and
privacy · error and failure handling · test adequacy · regressions · scope changes ·
independence of evidence.

Scale the depth to the milestone; do not pad. For a code-shaped milestone, interface and
failure-mode correctness matter more than UI; for a product-shaped milestone, behaviour and
states matter more than internals. Judge against the governing spec, not a generic checklist.

### 4. Build the acceptance-criteria matrix

For every acceptance criterion, record:

- Requirement ID
- Status: `pass` | `fail` | `not-verified` | `not-applicable`
- **Evidence** — the specific test, file, line, or command output that justifies the status

A criterion with no inspectable evidence is `not-verified`. `not-applicable` must be
justified, not used to avoid judging.

### 5. Record findings

Each finding needs:

- A **stable ID** (e.g. `F-001`) that survives across fix attempts
- **Severity**: `critical` | `major` | `minor` | `suggestion`
- A **requirement/acceptance reference** where applicable
- **Evidence**: file/line, test name, or command output
- **Impact**: what goes wrong if unfixed
- **Proposed remediation** — the fix direction, not the fix itself

Distinguish verified facts from assumptions and untested claims explicitly.

### 6. Write the artifacts

Write both, for the same reviewed candidate:

- **`review-report.md`** — narrative, human-facing, linking to the JSON.
- **`review-findings.json`** — machine-readable, validated against the governing spec's
  schema.

The JSON must carry, at minimum, the obligations the governing spec defines: schema version,
handoff and attempt identifiers, the reviewed commit, a recommendation from the allowed set,
the acceptance-criteria matrix, findings, open questions, tests performed and not performed,
residual risks, and the recommended next action.

### 7. Recommend a gate outcome

Choose exactly one of the outcomes the governing spec allows (typically `APPROVED`,
`APPROVED_WITH_NOTES`, `CHANGES_REQUESTED`, `HUMAN_DECISION_REQUIRED`, `BLOCKED`) and
justify it from the matrix and findings.

A `CHANGES_REQUESTED` recommendation **does not** start the implementer. It proposes a fix
cycle; the coordinator and any required human gate decide whether it happens.

Record any remaining risk and anything you could not verify, including why.

## Evidence discipline

- Prefer the artifact over the description of the artifact.
- Prefer a reproducible command over a screenshot or a claim.
- A passing test that does not exercise the requirement is not evidence of it.
- A requirement verified only by the implementer's own test is weaker evidence than one
  verified independently — say which it is.

## When to escalate to a human

Raise `HUMAN_DECISION_REQUIRED` when the choice depends on product intent, scope, or policy
rather than on code correctness — for example an ambiguous requirement, an acceptable
tradeoff, or a deviation that may be legitimate but was not pre-approved. Present options
with tradeoffs and a preferred recommendation.

Do not resolve a specification ambiguity yourself by picking the interpretation that makes
the code pass.
