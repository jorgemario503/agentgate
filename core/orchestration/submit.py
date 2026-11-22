"""Request-only handoff submission.

This module implements the *only* orchestration-facing tool a model may call. Its whole
job is to turn a model's request into a validated, durably recorded **request** — never an
execution, an approval, or a state transition.

Why the boundary is drawn here
------------------------------
The spec forbids three things that are easy to add "helpfully" and hard to take back:

* a model-callable approval (``approve_handoff``) — an agent must never authorize itself;
* arbitrary agent spawning (``spawn_agent``) — the coordinator decides what runs;
* advancing a workflow from prose — a chat message never initiates review.

So this module *records intent*. The coordinator (a later milestone, separate process)
reads the record, applies policy, and is the thing that actually schedules work.

Idempotency
-----------
A retried ``milestone_submit`` (same ``idempotency_key``) must return the **original**
status and must not write a second record. Without this, a model that retries after a
network hiccup would silently create duplicate review work.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from core.orchestration.validate import validate_handoff

#: Filename used inside the registry dir for a persisted request.
_SUBMIT_SUFFIX = ".handoff.json"

#: Conservative id shape: enough for hashed/uuid ids, no path separators.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SubmitError(Exception):
    """Raised for malformed arguments or an unsafe registry path."""


def _safe_filename(idempotency_key: str) -> str:
    """Map an idempotency key to a filename, rejecting anything path-like.

    The key comes from the model, so it is untrusted: it must never be usable to
    escape the registry directory or overwrite an unrelated record.
    """
    if not isinstance(idempotency_key, str) or not _SAFE_ID.match(idempotency_key):
        raise SubmitError(
            "idempotency_key must be 1-128 chars of letters, digits, '.', '_', ':' or '-'"
        )
    return idempotency_key + _SUBMIT_SUFFIX


def _registry_dir(registry_dir: str | os.PathLike) -> Path:
    p = Path(registry_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so a crash never leaves a half-readable record."""
    data = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_request(
    args: dict[str, Any],
    *,
    repo_root: Path,
    source_role: str,
    workflow_id: str,
    requested_by: str,
) -> dict[str, Any]:
    """Build a handoff payload from tool arguments and host-supplied identity.

    Identity fields (``workflow_id``, ``source_role``, ``requested_by``) are supplied by
    the host, never taken from the model's arguments: a model must not be able to claim
    to be a different role or a different workflow.
    """
    refs = args.get("artifact_references") or []
    payload = {
        "schema_version": "1.0",
        "workflow_id": workflow_id,
        "milestone_id": str(args.get("milestone_id", "")),
        "attempt_id": str(args.get("attempt_id", "001")),
        "handoff_id": str(args.get("handoff_id") or f"HO-{int(time.time() * 1000)}"),
        "idempotency_key": str(args.get("idempotency_key", "")),
        "source_role": source_role,
        "target_role": str(args.get("target_role", "reviewer")),
        "requested_action": str(args.get("requested_action", "review_milestone")),
        "spec_version": str(args.get("spec_version", "1.0")),
        "base_commit": str(args.get("base_commit", "")),
        "candidate_commit": str(args.get("candidate_commit", "")),
        "artifact_references": refs,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_by": requested_by,
        "authorization_status": "pending_human_approval",
    }
    return payload


def submit_handoff(
    args: dict[str, Any],
    *,
    registry_dir: str | os.PathLike,
    repo_root: Path,
    source_role: str,
    workflow_id: str,
    requested_by: str,
) -> str:
    """Validate and persist a handoff request. Returns a human-readable status.

    Never spawns, approves, or transitions. The only side effect is one JSON file.
    """
    try:
        payload = build_request(
            args,
            repo_root=repo_root,
            source_role=source_role,
            workflow_id=workflow_id,
            requested_by=requested_by,
        )
    except SubmitError as exc:
        return f"REJECTED: {exc}"

    errors = validate_handoff(payload, repo_root=repo_root)
    if errors:
        # Return every problem at once: a model fixes faster with the full list.
        return "REJECTED: " + " | ".join(errors)

    try:
        name = _safe_filename(payload["idempotency_key"])
    except SubmitError as exc:
        return f"REJECTED: {exc}"

    reg = _registry_dir(registry_dir)
    target = reg / name

    if target.exists():
        # Idempotent replay: report the original status, write nothing.
        try:
            existing = json.loads(target.read_text())
            status = existing.get("authorization_status", "pending_human_approval")
            return (
                f"ALREADY_SUBMITTED: handoff {existing.get('handoff_id')} is {status}. "
                "No new request was created; wait for the coordinator."
            )
        except (OSError, json.JSONDecodeError):
            return "REJECTED: an unreadable record already exists for this idempotency_key"

    try:
        _atomic_write(target, payload)
    except OSError as exc:
        return f"REJECTED: could not persist handoff request ({exc.__class__.__name__})"

    return (
        f"SUBMITTED: handoff {payload['handoff_id']} recorded as "
        f"{payload['authorization_status']}. This is a request only — no agent was "
        "started and nothing was approved. A human must authorize the handoff."
    )
