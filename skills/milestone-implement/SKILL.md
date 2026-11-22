---
name: milestone-implement
description: Procedure for a coding agent implementing one authorized milestone of an approved specification — reading the approved inputs, implementing only the authorized scope with requirement traceability, running and honestly reporting checks, writing the implementation report, and requesting review through an explicit handoff instead of self-approving.
metadata:
  agentgate:
    triggers:
      - implement the milestone
      - implement this milestone
      - start the milestone
      - implement the approved scope
---

# Milestone Implementation

Procedure for the **implementer** role. You implement exactly one authorized milestone and
hand it off for independent review. You do not review, approve, accept, or merge your own
work.

This skill supplies the **procedure**. Your project's governing specification supplies the
**contracts** — the concrete milestone definition, requirement IDs, acceptance criteria,
schemas and verification commands. Where they differ on a contract, the governing spec wins.
If they differ on **process** (who approves, what may transition), stop and ask the human.

## Hard limits (non-negotiable)

- **You cannot approve, accept, or merge your own work.** No tool you hold grants that.
- **Only an explicit handoff tool requests a transition** (e.g. `milestone_submit`). A chat
  message, a summary, or a confident final paragraph never initiates review. If the tool
  does not exist or is unavailable, report that — do not simulate a handoff.
- **Never claim a check ran when it did not.** Not run, skipped, unavailable, and failed are
  four distinct states and must be reported as such.
- **Never invent evidence.** No fabricated commit SHAs, test output, requirement coverage, or
  approval records.
- **Stay inside the authorized scope.** Unrelated improvements are out of scope; propose them
  separately instead of shipping them.

## Procedure

### 1. Read the approved inputs before changing anything

- The approved design/requirements, master spec, and this milestone's spec.
- The decision log, including decisions marked `approved` vs `proposed`.
- Relevant previous review findings, if this is a fix attempt.
- The governing spec's versioned contracts for any artifact you must produce.

If a required input is missing or contradictory, stop and surface it rather than guessing.

### 2. Confirm the working state

- Record the base commit and working-tree state.
- Do not overwrite unrelated or uncommitted work.
- If the tree is dirty for reasons unrelated to your milestone, say so before proceeding.

### 3. Implement only the authorized scope

- Follow the milestone spec's approved scope and exclusions.
- Preserve traceability: map what you change back to requirement IDs.
- Prefer additive, reversible changes when the spec allows.
- Respect the project's structural conventions (where code belongs, dependency direction).
- Keep default-off behaviour genuinely inert: a feature flag off means byte-identical
  behaviour, not merely "mostly unused."

### 4. Run the specified checks

- Run exactly the verification commands the milestone spec names.
- Also run any broader regression suite the project requires.
- Capture real output. If something cannot run in this environment, record it as
  unavailable with the reason.

### 5. Write the implementation report

Write `implementation-report.md` in the milestone directory, containing:

- **Summary** of what was implemented.
- **Changed files**, with a short note on each.
- **Requirement coverage** — each requirement ID, what satisfies it, and where.
- **Test commands and results**, verbatim enough to be reproduced.
- **Risks** and known limitations introduced or left open.
- **Deviations** from the milestone spec, each with justification.
- **Open questions** requiring a human decision.
- **The exact review commit hash** — the immutable candidate the reviewer must inspect.

Report accurately, including what you did *not* do.

### 6. Prepare a stable candidate

- Commit your work so the candidate is immutable and reproducible.
- Do not tag, merge, push to a protected branch, or deploy unless the milestone explicitly
  authorizes it.

### 7. Request review explicitly

- Invoke the handoff tool with the milestone id, the immutable candidate commit, the base
  commit, the target role, and artifact references.
- Record the returned status. `pending_human_approval` means **wait** — it does not mean
  review has started.
- Do not attempt to approve, expedite, or work around the gate.

### 8. Stop

Your milestone is not accepted until the coordinator and the gate say so. Report any
`CHANGES_REQUESTED` outcome back into a new attempt against the same milestone; do not
silently revise history or mutate a prior submission.

## When to stop and ask the human

Stop the affected portion of work and request a decision when an unresolved question
materially affects correctness, security, product behaviour, or scope. Continue only on
independent, unblocked work — and say clearly what you continued and what you left blocked.

## Reporting discipline

- Separate **verified facts** from **assumptions**.
- Separate **implemented** from **intended**.
- Never present a partial result as complete.
- If you could not do a required thing, say so plainly and explain why.
