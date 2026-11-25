"""Open-model Agent Skills concern package.

The implementation lives in :mod:`core.skills.catalog`; this package re-exports
its public API so callers can keep importing from ``core.skills`` unchanged
(``from core.skills import discover, SkillSession, ...``). This follows the
concern-package layout used by ``core/tools/`` (group modules by concern, not by
file type), here as ``core/skills/``.
"""
from __future__ import annotations

from core.skills.catalog import (
    # limits / constants
    MAX_NAME_LEN,
    MAX_DESCRIPTION_LEN,
    MAX_COMPATIBILITY_LEN,
    MAX_FRONTMATTER_BYTES,
    MAX_BODY_BYTES,
    ALLOWED_FIELDS,
    SKILL_FILE_NAMES,
    SKILL_READ_TOOL,
    SKILL_LIST_TOOL,
    # catalog model
    SkillEntry,
    Catalog,
    EMPTY_CATALOG,
    # parsing / discovery
    parse_skill_file,
    discover,
    # manifest
    estimate_tokens,
    build_manifest,
    list_line,
    # read
    SkillReadError,
    read_skill_body,
    body_matches_fingerprint,
    # context builder
    SkillsContext,
    build_skills_context,
    skills_startup_report,
    # recommendations (Mode B)
    Recommendation,
    recommend,
    build_recommendation_nudge,
    # session lifecycle
    ActiveSkill,
    SkillSession,
    is_skill_read_tool,
    is_skill_tool,
    record_activation_from_result,
    SkillInvocation,
    InvocationError,
    parse_skill_invocation,
    restoration_message,
    explicit_activation_message,
    RESTORE_LABEL, EXPLICIT_LABEL,
    InvocationResolution, resolve_invocation,
)

__all__ = [
    "MAX_NAME_LEN", "MAX_DESCRIPTION_LEN", "MAX_COMPATIBILITY_LEN",
    "MAX_FRONTMATTER_BYTES", "MAX_BODY_BYTES", "ALLOWED_FIELDS",
    "SKILL_FILE_NAMES", "SKILL_READ_TOOL", "SKILL_LIST_TOOL",
    "SkillEntry", "Catalog", "EMPTY_CATALOG",
    "parse_skill_file", "discover",
    "estimate_tokens", "build_manifest", "list_line",
    "SkillReadError", "read_skill_body", "body_matches_fingerprint",
    "SkillsContext", "build_skills_context",
    "Recommendation", "recommend", "build_recommendation_nudge",
    "ActiveSkill", "SkillSession",
    "is_skill_read_tool", "is_skill_tool", "record_activation_from_result",
    "SkillInvocation", "InvocationError", "parse_skill_invocation",
    "restoration_message", "explicit_activation_message",
    "RESTORE_LABEL", "EXPLICIT_LABEL",
    "InvocationResolution", "resolve_invocation",
]
