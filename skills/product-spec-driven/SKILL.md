---
name: product-spec-driven
description: Guide specification-first product and code development, approved milestone implementation, independent review, and governed agent-to-agent handoffs using versioned artifacts under docs/spec. Use for requirement discovery, governing specs, coding/review milestones, handoff decisions, and human approval gates.
metadata:
  agentgate:
    triggers:
      - write a spec
      - create a milestone plan
      - break this into milestones
      - spec-driven
      - handoff to review
---

# Spec-Driven Development

## Purpose and authority

Use this skill for **specification-first development of a product or codebase** — a new application, a new subsystem, or a substantial change to an existing system. Work in two stages: (1) define and approve the design/requirements, then (2) define and approve the master implementation specification and milestone plan. Implement only after the relevant specifications have been approved.

Treat approved requirements and master specifications as authoritative. Never silently alter requirements to fit an implementation. Surface ambiguity, conflicts, missing information, and proposed deviations with a recommendation and explicit human decision request when material.

## Governing spec inputs

Before Phase 1, locate and read the project's **governing specification** — typically the approved or working spec under `docs/spec/`, or whichever document the human names as authoritative.

This skill supplies the **process** (stages, roles, gates, artifact obligations, handoff discipline). A governing spec supplies the **project-specific contracts** (concrete schemas, field sets, milestone definitions, acceptance checks).

Rules for reconciling the two:

- Where the governing spec defines a contract (a JSON schema, a field set, a required artifact), **the governing spec is authoritative for that project** — including where it adds fields or sections this skill does not list.
- Where the governing spec is silent, this skill's guidance — including its illustrative field lists — is the working default.
- If the governing spec and this skill disagree on **process** (roles, gates, authorization), surface the conflict to the human rather than silently choosing.
- If no governing spec exists yet, this skill provides the scaffolding; treat the field lists below as the starting default and propose them for approval before relying on them.

Never assume the governing spec is a fixed file path across projects, and never infer its contents from this skill's examples.

## Roles

- **Human owner (product owner or technical owner):** owns vision/requirements, scope, priorities, and decisions; approves specifications and material changes.
- **Coding agent:** implements only the authorized milestone, runs checks, and prepares an evidence-based handoff. Does not self-approve.
- **Review agent:** independently compares the code and tests with the approved requirements and milestone acceptance criteria; records findings and a gate recommendation. Does not silently change specifications or implement fixes unless separately assigned.
- **Workflow coordinator (trusted host, not an agent skill):** validates handoff contracts, checks authorization and artifact identity, manages durable workflow state and bounded retries, and applies gate policy. Neither agent can grant itself new permissions or independently approve its work.

The initial deployment uses **same-host runners, explicit handoff submission, skill-defined procedures, and policy-gated acceptance**. Human approval of **agent-to-agent execution handoffs is enabled by default**, but is configurable by an authorized operator. This approval is separate from product/specification approval and milestone acceptance.

## Repository conventions

Keep all specification and milestone deliverables under `docs/spec/`.

Use a milestone directory named:

`docs/spec/{milestone#-spec-generic-name_milestone-generic-name}/{deliverable-name}`

Interpret the placeholders as follows:

- `milestone#`: a zero-padded sequential number, e.g. `01`, `02`, `03`.
- `spec-generic-name`: a concise, stable kebab-case identifier for the specification or workstream.
- `milestone-generic-name`: a concise, stable kebab-case milestone label.
- `deliverable-name`: a descriptive kebab-case filename with the appropriate extension.

Example:

`docs/spec/01-product-design_discovery/product-design-spec.md`

`docs/spec/02-mobile-foundation_app-shell/milestone-spec.md`

`docs/spec/02-mobile-foundation_app-shell/implementation-report.md`

`docs/spec/02-mobile-foundation_app-shell/review-report.md`

`docs/spec/02-mobile-foundation_app-shell/gate-decision.json`

`docs/spec/02-mobile-foundation_app-shell/handoff.json`

`docs/spec/02-mobile-foundation_app-shell/review-findings.json`

This skill lives at the repository's skill-discovery location supported by the agent runtime — `skills/product-spec-driven/SKILL.md`, discovered by AgentGate's catalog (`AGENTGATE_SKILLS_DIR`, default `skills/`). **Do not also assume a standalone Markdown file is automatically discovered by every agent runtime.** All project deliverables still belong in `docs/spec/`; the skill carries the procedure, `docs/spec/` carries the artifacts.

Do not rename milestone directories after approval without an explicit migration decision. Reference requirement IDs, milestone IDs, and exact commit hashes in reports.

`docs/spec/` is the **project's canonical deliverable location**. Handoff and review reports, decisions, and traceability links belong in the corresponding milestone directory. The coordinator may independently maintain ephemeral/durable operational state (events, leases, retry counters, execution logs) in AgentGate's private storage; that is **not** a second source of truth for approved project specs or milestone reports. Do not overwrite prior evidence or reviews: use unique attempt IDs and link the final human-facing deliverables to the applicable attempt. Treat output paths as validated, contained repository-relative paths.

## Phase 1 — Requirements and design (before coding)

Scope this phase to the project type. The objective is the same — an approved statement of *what must be true* before any implementation — but the elicitation differs.

**For a product/application-shaped project:**

1. Elicit the product vision, target users, jobs to be done, goals, success metrics, platforms, constraints, and exclusions.
2. Map personas, primary and secondary journeys, navigation, screens, state transitions, and key interactions.
3. Define screen-level behavior, loading/empty/error/offline/permission states, accessibility, localization, and privacy expectations.
4. Record business rules, assumptions, dependencies, non-goals, and measurable acceptance criteria.
5. Identify open questions. For each, give options, tradeoffs, a recommended default, and the owner of the decision.
6. Request human approval of the design baseline before fixing the master implementation plan.

**For a code/system-shaped project (subsystem, refactor, service, library, migration):**

1. Elicit the problem, the current behavior, the required behavior, and why the change is needed now.
2. Map the affected components, interfaces, data flows, contracts, and external dependencies.
3. Define interface and data contracts, error and failure behavior, compatibility/migration expectations, and observability needs.
4. Record invariants, assumptions, constraints, non-goals, risks, and measurable acceptance criteria.
5. Identify open questions with options, tradeoffs, a recommended default, and the decision owner.
6. Request human approval of the design baseline before fixing the master implementation plan.

Suggested deliverables scale to the project: product-shaped projects typically use `product-design-spec.md`, `user-journeys.md`, `screen-inventory.md`, `design-system.md`, `open-questions.md`, `decision-log.md`; code-shaped projects typically use `requirements-spec.md`, `interfaces-and-contracts.md`, `open-questions.md`, `decision-log.md`. Only create files that carry substantive content; links between files should use repository-relative paths.

## Phase 2 — Master specification and milestone plan

After design/requirements approval, define architecture, data models, API and integration contracts, authentication and authorization, security/privacy, storage and synchronization, observability, testing, deployment, and release criteria. Choose technology based on requirements, not habit.

Create a master specification and a dependency-ordered milestone plan. Each milestone must define:

- Unique ID, goal, scope, exclusions, prerequisites, and dependencies.
- Traceable requirement IDs and testable acceptance criteria.
- Expected code/configuration/documentation changes.
- Verification commands, test coverage, manual checks, and required evidence.
- Definition of done, handoff artifacts, and blocking conditions.
- Decisions reserved for the human reviewer.

Prefer vertical feature slices that can be verified end to end. Require human approval of the master baseline before implementation begins.

## Phase 3 — Coding-agent execution

For each authorized milestone:

1. Read the current approved product design, master spec, milestone spec, decision log, and relevant previous review findings.
2. Confirm the base commit and working-tree state; do not overwrite unrelated work.
3. Implement only the milestone's approved scope, preserving traceability to requirement IDs.
4. Run specified checks. Report failed, skipped, and unavailable checks accurately; never claim a test ran when it did not.
5. Write `implementation-report.md` with summary, changed files, requirement coverage, test commands/results, risks, deviations, open questions, and exact review commit hash.
6. Hand off a stable commit/PR for independent review. Do not mark the milestone approved.
7. Submit an **explicit** `milestone_submit` handoff referencing the approved milestone and an immutable candidate commit. A final chat message alone must not initiate a review. The tool must only request a coordinator-managed transition; it must not spawn a reviewer or authorize a workflow by itself.

If an unresolved decision materially affects correctness, security, product behavior, or scope, stop that portion of work and request a human decision. Continue only independent, unblocked work.

## Phase 4 — Independent review

The review agent must inspect the actual diff at the specified commit, relevant implementation, test evidence, and specifications. Do not rely solely on the coding agent's report. Review an immutable candidate commit/snapshot in an independent checkout or otherwise isolated workspace; do not review a moving working tree. Do not inherit the implementer's conversation history or write permissions. Approve only evidence actually inspected; a reviewer recommendation is not an execution authorization.

Check requirement traceability, functional behavior, UI/UX alignment, architecture/contracts, security/privacy, accessibility, error handling, test adequacy, regressions, and scope changes. Distinguish verified facts from assumptions and untested claims.

Write `review-report.md` and a machine-readable `review-findings.json` for the same reviewed candidate, with:

- Milestone ID, spec version, reviewed commit, and review date.
- Acceptance-criteria matrix: requirement ID, status (`pass`, `fail`, `not-verified`, `not-applicable`), and evidence.
- Findings with severity (`critical`, `major`, `minor`, `suggestion`), file/line or test references, impact, and recommended remediation.
- Unresolved questions with options, tradeoffs, preferred recommendation, and whether human approval is required.
- Remaining risks, tests performed, tests not performed, and a recommended gate outcome.

## Gate decisions and routing

Allowed outcomes:

- `APPROVED`: mandatory criteria verified; proceed if authorized.
- `APPROVED_WITH_NOTES`: only nonblocking findings remain, with owners and follow-up tracking.
- `CHANGES_REQUESTED`: return actionable findings to the coding agent, then re-review the new commit.
- `HUMAN_DECISION_REQUIRED`: pause affected work; present alternatives and recommendation to the human owner.
- `BLOCKED`: critical unmet criteria or missing evidence prevent approval.

The reviewer recommends; the **trusted coordinator** validates evidence and policy, and the designated gate authority records the decision. Do not infer human approval from silence. Never auto-advance past a required human gate.

**Separate two decisions:**

1. **Handoff authorization:** permission to start the next agent's work (implementer → reviewer, reviewer → implementer for fixes, or another configured step). This requires a recorded human approval **by default**. An authorized operator may explicitly disable this requirement for eligible transitions; if disabled, the coordinator must still validate predefined workflow policy and authorization. The requesting or reviewing agent cannot disable the gate.
2. **Milestone acceptance:** approval of the completed milestone after independent review. It is **policy-gated automatic acceptance** only when every mandatory deterministic criterion passes, no required human product/spec change gate remains, and the configured acceptance policy authorizes it. Otherwise require a human decision. Human handoff approval alone does not imply milestone acceptance.

Human product-design and master-spec approvals, plus material changes to approved specifications, are **always** human decisions and are never bypassed by an operator disabling handoff approvals.

The reviewer may recommend `APPROVED`, `APPROVED_WITH_NOTES`, `CHANGES_REQUESTED`, `HUMAN_DECISION_REQUIRED`, or `BLOCKED`. A `CHANGES_REQUESTED` outcome can propose a fix cycle but does not immediately invoke the implementer; coordinator validation and any required handoff authorization must occur first. Set a bounded fix-attempt limit (recommended initial value: 2), and escalate on exhaustion. No automatic merges, deployments, secret access expansions, or irreversible actions.

Write `gate-decision.json` as valid JSON with at least these fields:

```json
{
  "milestone_id": "02",
  "spec_version": "1.0",
  "reviewed_commit": "<full-git-commit-sha>",
  "outcome": "HUMAN_DECISION_REQUIRED",
  "decision_authority": "human-product-owner",
  "decision_status": "pending",
  "blocking_finding_ids": [],
  "open_question_ids": ["Q-001"],
  "next_action": "await-human-decision"
}
```

Use `decision_status` values `pending` or `recorded`; only mark `recorded` when the named authority has actually made the decision. Never fabricate commit hashes, approvals, or verification evidence.

## Agent-to-agent handoff contract (additive to spec artifacts)

The skill specifies the **content** of reports; it does not implement process orchestration. AgentGate must supply a trusted coordinator, explicitly authorized tools and durable state handling. Never interpret a Markdown skill as permission to launch a runner.

For every agent-to-agent execution request:

1. The implementer or reviewer explicitly requests a configured transition via an authorized tool (for example, `milestone_submit` or a coordinator-controlled transition request). Tool names are proposed interfaces, not claims that the current runner already supports them.
2. The host verifies requester identity, workflow role, approved milestone, allowed target role, candidate/base Git SHA, schema version, artifact paths and hashes, and a unique idempotency key. Reject missing/stale or unauthorized requests.
3. The coordinator writes a durable `handoff.json` and an event, with status `pending_human_approval` when the default human handoff gate is active. No target agent starts until approval is explicitly recorded by the trusted host.
4. After authorization, launch the target runner with a separate session, least-privilege tool policy, explicit skill instructions, the approved spec, immutable candidate and only relevant evidence.
5. The reviewer writes `review-report.md` and `review-findings.json`; the coordinator validates JSON/schema, exact reviewed SHA, evidence pointers and workflow state before using the recommendation.
6. If fixes are authorized, send immutable finding IDs and the appropriate milestone scope back to the implementer as a **new** attempt. Re-review the new candidate SHA. Never silently mutate a prior review.
7. Log every approval, denial, cancellation, timeout, retry and state transition with actor, timestamp and causation ID. Reject duplicate submissions without launching duplicate work. Recover safely from restarts.

**Required handoff fields — illustrative and non-exhaustive.** The approved governing spec's versioned schema is authoritative; this list only shows the obligations such a schema must cover. Expect fields such as: `schema_version`, `workflow_id`, `milestone_id`, `attempt_id`, `handoff_id`, `idempotency_key`, `source_role`, `target_role`, `requested_action`, `spec_version`, `base_commit`, `candidate_commit`, `artifact_references`, `created_at`, `requested_by`, and `authorization_status`. A governing spec MAY add fields (for example a `spec_id` distinguishing specs within one repository) or a richer digest contract; the coordinator validates against **that spec's schema**, not against this illustrative list. The trusted host supplies or verifies identities and statuses, not the model.

**Required reviewer-result fields — illustrative and non-exhaustive.** Typically: `schema_version`, `handoff_id`, `attempt_id`, `reviewed_commit`, `recommendation`, `acceptance_criteria`, `findings`, `open_questions`, `tests_performed`, `tests_not_performed`, `residual_risks`, `recommended_next_action`. Every finding has a stable ID, severity, requirement/acceptance reference where applicable, evidence, and proposed remediation. The governing spec may extend this set.

**Decision digest (process obligation).** Wherever a governing spec separates the human-facing conversation from an implementer's context, the coordinator MUST produce a **deterministic, bounded digest** built only from recorded structured decisions and validated findings. It must **not** be built from raw chat, and must **not** be authored or summarized by the reviewer or implementer agent. A human free-text note may cross only as a discrete, attributed, bounded field on a recorded decision. The governing spec defines the digest's exact fields and version; this skill requires that the digest exist, be deterministic, and be traceable to the records it was projected from.

Use a strict finite state machine for requested/authorized/running/reviewed/accepted/rejected/blocked transitions; do not route using unvalidated free-form model text. Maintain separate **handoff approval**, **review recommendation**, and **gate decision** records. The canonical milestone `gate-decision.json` must accurately identify its decision authority, policy and recorded status.

## Change control and traceability

- Give requirements stable IDs (`PRD-001`, `UX-001`, `SEC-001`) and decisions stable IDs (`DEC-001`).
- Version approved specifications; record what changed, why, who approved it, and which milestones are affected.
- Keep `decision-log.md` current; distinguish `proposed`, `approved`, `rejected`, and `deferred` decisions.
- A spec change after approval requires impact analysis and human approval before affected implementation proceeds.
- Reviewer findings must map back to acceptance criteria, code, tests, or a clearly labeled advisory recommendation.
- Prefer concise, actionable Markdown. Avoid duplicate sources of truth; link to canonical specifications rather than copying them.

## Startup behavior

At the beginning of a new project, **do not invent the vision, requirements, or design, and do not begin implementation**. Ask the human owner for the vision/goals (or, for a code-shaped project, the problem and required behavior), then elicit remaining design decisions in manageable groups. Deliver the design/requirements for review first; proceed to the master spec and milestone implementation only after approval.
