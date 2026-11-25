"""Open-model Agent Skills: discovery, manifest, on-demand read, recommendations.

This module is the *only* place that knows about skill catalogs. It is pure:
no runner, provider, AgentDB or Claude knowledge. The three open-model runners
(local / fireworks / deepinfra) each call these helpers from their existing
client build seam; nothing here is duplicated per runner.

Design boundaries:

- Format: the Agent Skills standard — a directory per skill containing a
  ``SKILL.md`` with YAML frontmatter (``name``, ``description`` required;
  ``license``, ``compatibility``, ``metadata``, ``allowed-tools`` optional).
  Parsing reuses the already-installed PyYAML with ``safe_load`` plus bounded
  input; no new runtime dependency (``skills-ref`` is demonstration-only).
- Discovery is read-only and offline: frontmatter is parsed, bodies are never
  loaded eagerly, and no code is executed or fetched.
- The manifest is compact, deterministic, budgeted and reports omissions. The
  body is returned *exactly* by ``skill_read`` as tool content — never
  truncated or lossily compressed, and it never edits system instructions.
- ``allowed-tools`` is informational only; it never widens permissions.

Feature is OFF by default. When disabled, callers must not call into this
module at all, so outgoing requests are byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Conformance limits (mirror the Agent Skills reference validator).
# ---------------------------------------------------------------------------

MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
MAX_COMPATIBILITY_LEN = 500

#: Hard cap on how many bytes of a SKILL.md we will parse. A skill whose file
#: exceeds this is excluded rather than read unbounded into memory.
MAX_FRONTMATTER_BYTES = 64 * 1024

#: Hard cap on a returned skill body. Bodies are returned exactly; this only
#: guards against pathological files. Exceeding it is an error, not a truncation
#: (the spec forbids silent/lossy body handling).
MAX_BODY_BYTES = 512 * 1024

ALLOWED_FIELDS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}

SKILL_FILE_NAMES = ("SKILL.md", "skill.md")

#: Approximate chars-per-token used elsewhere in the codebase (core/history,
#: runners). Kept local so the manifest budget is self-contained.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Catalog data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillEntry:
    """Immutable catalog metadata for one skill. The body is NOT held here."""

    name: str
    description: str
    path: Path              # resolved canonical SKILL.md path
    fingerprint: str        # sha256 of the raw SKILL.md bytes
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: Optional[str] = None
    #: Deterministic keyword triggers (from metadata.agentgate.triggers), used by
    #: Mode B. Empty when none configured. Never derived from the body/description.
    triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Catalog:
    """A deterministic, immutable view of the skills directory.

    ``entries`` is sorted by name; ``fingerprint`` is a stable digest of the
    whole catalog (name + body fingerprint), so a session can pin it and detect
    change on resume. ``diagnostics`` lists excluded/invalid skills (redacted).
    """

    entries: tuple[SkillEntry, ...]
    root: Optional[Path]
    fingerprint: str
    diagnostics: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(e.name for e in self.entries)

    def get(self, name: str) -> Optional[SkillEntry]:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def __bool__(self) -> bool:
        return bool(self.entries)


EMPTY_CATALOG = Catalog(entries=(), root=None, fingerprint="", diagnostics=())


# ---------------------------------------------------------------------------
# Frontmatter parsing and validation
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _validate_name(name: object, skill_dir: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(name, str) or not name.strip():
        return ["field 'name' must be a non-empty string"]
    name = _normalize(name.strip())
    if len(name) > MAX_NAME_LEN:
        errors.append(f"name exceeds {MAX_NAME_LEN} chars")
    if name != name.lower():
        errors.append("name must be lowercase")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name cannot start or end with a hyphen")
    if "--" in name:
        errors.append("name cannot contain consecutive hyphens")
    if not all(c.isalnum() or c == "-" for c in name):
        errors.append("name may only contain letters, digits and hyphens")
    if _normalize(skill_dir.name) != name:
        errors.append(f"directory name '{skill_dir.name}' must match skill name")
    return errors


def _validate_description(desc: object) -> list[str]:
    if not isinstance(desc, str) or not desc.strip():
        return ["field 'description' must be a non-empty string"]
    if len(desc) > MAX_DESCRIPTION_LEN:
        return [f"description exceeds {MAX_DESCRIPTION_LEN} chars"]
    return []


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body). Raises ValueError when malformed."""
    if not content.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter (---)")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter not closed with ---")
    return parts[1], parts[2].strip()


def _parse_triggers(metadata: object) -> tuple[tuple[str, ...], list[str]]:
    """Extract and validate ``metadata.agentgate.triggers``.

    Returns ``(triggers, warnings)``. Triggers are deterministic keyword phrases
    used by Mode B. Invalid/overly broad entries are dropped with a warning; a
    malformed ``agentgate``/``triggers`` shape yields no triggers (never raises).
    The whole ``metadata`` mapping stays str->str for catalog fidelity; triggers
    are additionally surfaced as a typed tuple here.
    """
    warnings: list[str] = []
    if not isinstance(metadata, dict):
        return (), warnings
    ag = metadata.get("agentgate")
    if ag is None:
        return (), warnings
    if not isinstance(ag, dict):
        warnings.append("metadata.agentgate must be a mapping; triggers ignored")
        return (), warnings
    raw = ag.get("triggers")
    if raw is None:
        return (), warnings
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        warnings.append("metadata.agentgate.triggers must be a list; ignored")
        return (), warnings

    seen: set[str] = set()
    triggers: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            warnings.append("non-string trigger ignored")
            continue
        norm = " ".join(item.strip().lower().split())
        if not norm:
            continue
        if len(norm) < 3:
            warnings.append(f"trigger too short '{norm}' ignored")
            continue
        if len(norm) > 120:
            warnings.append(f"trigger too long '{norm[:20]}...' ignored")
            continue
        if norm in seen:
            continue
        seen.add(norm)
        triggers.append(norm)
    return tuple(triggers), warnings


def parse_skill_file(text: str, skill_dir: Path) -> tuple[SkillEntry, list[str]]:
    """Parse and validate one SKILL.md.

    Returns ``(entry, errors)``. When ``errors`` is non-empty the entry is not
    trustworthy and the caller should exclude the skill; ``entry`` is still
    returned so callers can report the name if it was readable. Never raises.
    """
    errors: list[str] = []
    try:
        fm_text, _body = _split_frontmatter(text)
        data = yaml.safe_load(fm_text)
    except (ValueError, yaml.YAMLError) as e:
        return _blank_entry(skill_dir), [f"unparseable frontmatter: {e}"]
    if not isinstance(data, dict):
        return _blank_entry(skill_dir), ["frontmatter must be a YAML mapping"]

    extra = set(data.keys()) - ALLOWED_FIELDS
    if extra:
        errors.append(f"unexpected frontmatter fields: {', '.join(sorted(extra))}")

    if "name" not in data:
        errors.append("missing required field 'name'")
    else:
        errors.extend(_validate_name(data["name"], skill_dir))
    if "description" not in data:
        errors.append("missing required field 'description'")
    else:
        errors.extend(_validate_description(data["description"]))

    compat = data.get("compatibility")
    if compat is not None and (not isinstance(compat, str) or len(compat) > MAX_COMPATIBILITY_LEN):
        errors.append(f"compatibility must be a string ≤{MAX_COMPATIBILITY_LEN} chars")

    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append("metadata must be a mapping")
        metadata = None

    # Triggers are optional keyword hints for Mode B; malformed ones are dropped
    # inside _parse_triggers without invalidating the skill.
    triggers, _trigger_warnings = _parse_triggers(metadata)

    entry = SkillEntry(
        name=_normalize(str(data.get("name", skill_dir.name)).strip()),
        description=str(data.get("description", "")).strip(),
        path=skill_dir / "SKILL.md",
        fingerprint="",  # filled by discovery (needs raw bytes)
        license=_opt_str(data.get("license")),
        compatibility=compat if isinstance(compat, str) else None,
        metadata={str(k): str(v) for k, v in (metadata or {}).items()},
        allowed_tools=_opt_str(data.get("allowed-tools")),
        triggers=triggers,
    )
    return entry, errors


def _opt_str(value: object) -> Optional[str]:
    return str(value) if isinstance(value, (str, int, float)) else None


def _blank_entry(skill_dir: Path) -> SkillEntry:
    return SkillEntry(name=skill_dir.name, description="", path=skill_dir / "SKILL.md", fingerprint="")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _fingerprint_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def resolve_skills_dir(skills_dir: "str | os.PathLike | None") -> "str | None":
    """Expand ``~`` so an operator can point at a shared folder.

    A skills catalog is often shared outside this repository
    (``~/agent-skills``, a team mount). ``Path`` does not expand ``~`` itself, so
    without this the literal directory ``~/agent-skills`` is looked for and not
    found — a silently empty catalog that looks like "no skills configured".

    Absolute paths are returned unchanged; relative paths stay relative (they are
    resolved against the process CWD by ``discover``, and the startup report
    prints what that resolved to).
    """
    if not skills_dir:
        return skills_dir
    try:
        text = os.fspath(skills_dir)
    except TypeError:
        return skills_dir
    return os.path.expanduser(text)


def discover(skills_dir: str | os.PathLike | None) -> Catalog:
    """Build an immutable catalog from an operator-configured directory.

    Never raises: a missing/unreadable root yields an empty catalog with a
    diagnostic. Each entry is validated; invalid ones are excluded (with a
    redacted diagnostic naming the directory and reason). Duplicate names keep
    the first in sorted order and record a diagnostic for the rest.
    """
    if not skills_dir:
        return EMPTY_CATALOG
    root = Path(skills_dir)
    if not root.exists():
        # The full path, not just the basename: "not found" is only actionable
        # if the operator can see WHERE it was looked for, and a path that is
        # missing cannot leak a skill body.
        return Catalog((), None, "", (f"skills dir not found: {os.path.abspath(root)}",))
    if not root.is_dir():
        return Catalog((), None, "", (f"skills dir is not a directory: {os.path.abspath(root)}",))

    try:
        root_resolved = root.resolve(strict=True)
    except OSError as e:
        return Catalog((), None, "", (f"skills dir unresolvable: {e.__class__.__name__}",))

    entries: list[SkillEntry] = []
    diagnostics: list[str] = []
    seen: set[str] = set()

    try:
        child_dirs = sorted((p for p in root_resolved.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError as e:
        return Catalog((), root_resolved, "", (f"could not list skills dir: {e.__class__.__name__}",))

    for child in child_dirs:
        entry, problems = _discover_one(child, root_resolved)
        if problems:
            diagnostics.extend(problems)
            continue
        if entry is None:  # no SKILL.md present — silently skip (not a skill)
            continue
        if entry.name in seen:
            diagnostics.append(f"duplicate skill name '{entry.name}' in {child.name} (ignored)")
            continue
        seen.add(entry.name)
        entries.append(entry)

    entries.sort(key=lambda e: e.name)
    catalog_fp = _fingerprint_bytes(
        "\n".join(f"{e.name}:{e.fingerprint}" for e in entries).encode()
    )
    return Catalog(tuple(entries), root_resolved, catalog_fp, tuple(diagnostics))


def _discover_one(child: Path, root_resolved: Path) -> tuple[Optional[SkillEntry], list[str]]:
    """Resolve, validate and fingerprint a single skill directory.

    Returns ``(None, [])`` when the directory simply has no SKILL.md.
    Returns ``(None, [problem])`` on any safety or validation failure.
    """
    skill_md = next((child / n for n in SKILL_FILE_NAMES if (child / n).exists()), None)
    if skill_md is None:
        return None, []

    # Containment: the resolved SKILL.md must stay inside the skills root, so a
    # symlink pointing outside (or a directory symlink) is refused.
    try:
        resolved = skill_md.resolve(strict=True)
    except OSError:
        return None, [f"unresolvable skill file in {child.name}"]
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None, [f"skill '{child.name}' escapes skills root (symlink?)"]

    try:
        size = resolved.stat().st_size
    except OSError:
        return None, [f"could not stat skill file in {child.name}"]
    if size > MAX_FRONTMATTER_BYTES:
        return None, [f"skill '{child.name}' SKILL.md exceeds {MAX_FRONTMATTER_BYTES} bytes"]

    try:
        raw = resolved.read_bytes()
    except OSError:
        return None, [f"could not read skill file in {child.name}"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, [f"skill '{child.name}' SKILL.md is not valid UTF-8"]

    entry, problems = parse_skill_file(text, child)
    if problems:
        return None, [f"skill '{child.name}' invalid: {'; '.join(problems)}"]
    # Replace the empty fingerprint with the real one; keep the canonical path.
    entry = SkillEntry(
        name=entry.name,
        description=entry.description,
        path=resolved,
        fingerprint=_fingerprint_bytes(raw),
        license=entry.license,
        compatibility=entry.compatibility,
        metadata=entry.metadata,
        allowed_tools=entry.allowed_tools,
        triggers=entry.triggers,
    )
    return entry, []


# ---------------------------------------------------------------------------
# Manifest (stable, budgeted, deterministic)
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def build_manifest(catalog: Catalog, *, max_tokens: int) -> str:
    """Render the compact skills manifest for the stable prompt section.

    The manifest lists each skill's name and trigger description (never the
    body) plus ``skill_read`` guidance, inside an ``<available_skills>`` block
    in the standard format. It is deterministic (sorted) and respects a token
    budget. If the full set does not fit, it emits the skills that fit plus an
    explicit omission notice telling the model how to discover the rest.

    Returns "" for an empty catalog (callers then add nothing).
    """
    if not catalog.entries:
        return ""

    header = (
        "## Available skills\n"
        "You have access to the skills listed below. Call the `skill_read` tool "
        "with a skill's name to load its full instructions *only when* it is "
        "relevant to the current task. Do not guess a skill's contents; read it "
        "first. If no skill fits, proceed without one.\n"
    )
    footer = "\n</available_skills>"
    open_tag = "<available_skills>"

    def render(entry: SkillEntry) -> str:
        return (
            "<skill>\n"
            f"<name>{html.escape(entry.name)}</name>\n"
            f"<description>{html.escape(entry.description)}</description>\n"
            "</skill>"
        )

    # Budget is measured on the rendered block (header + tags + skills).
    base_overhead = estimate_tokens(header) + estimate_tokens(open_tag + footer)

    rendered: list[str] = []
    used = base_overhead
    omitted = 0
    for entry in catalog.entries:
        block = render(entry)
        cost = estimate_tokens(block)
        if used + cost > max_tokens:
            omitted += 1
            continue
        rendered.append(block)
        used += cost

    parts = [header, open_tag, *rendered]
    if omitted:
        names_hint = (
            f"\n<omitted count=\"{omitted}\">"
            f"{omitted} additional skill(s) are not shown to save context. "
            "Use `skill_list` to see all skill names, then `skill_read`."
            "</omitted>"
        )
        parts.append(names_hint)
    parts.append(footer)
    return "\n".join(parts)


def list_line(catalog: Catalog) -> str:
    """One-line list of every skill name, for the ``skill_list`` tool."""
    if not catalog.entries:
        return "No skills are available."
    return "\n".join(f"- {e.name}: {e.description}" for e in catalog.entries)


# ---------------------------------------------------------------------------
# Exact body read (traversal-safe)
# ---------------------------------------------------------------------------

class SkillReadError(Exception):
    pass


def read_skill_body(catalog: Catalog, name: str) -> str:
    """Return the exact SKILL.md body for a catalogued skill.

    Name is validated against the catalog (never used as a raw path), so a
    traversal attempt simply fails the lookup. The resolved path is re-checked
    for containment against the catalog root, so a file swapped for a symlink
    after discovery is refused. Body is returned verbatim (no truncation); a
    body over ``MAX_BODY_BYTES`` raises rather than being cut.
    """
    entry = catalog.get(name)
    if entry is None:
        raise SkillReadError(f"unknown skill '{name}' — use skill_list to see available skills")

    if catalog.root is not None:
        try:
            resolved = entry.path.resolve(strict=True)
            resolved.relative_to(catalog.root)
        except (OSError, ValueError):
            raise SkillReadError(f"skill '{name}' is no longer safely readable")

    try:
        size = entry.path.stat().st_size
    except OSError as e:
        raise SkillReadError(f"could not read skill '{name}': {e.__class__.__name__}")
    if size > MAX_BODY_BYTES:
        raise SkillReadError(f"skill '{name}' body exceeds {MAX_BODY_BYTES} bytes")

    try:
        raw = entry.path.read_bytes()
    except OSError as e:
        raise SkillReadError(f"could not read skill '{name}': {e.__class__.__name__}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillReadError(f"skill '{name}' is not valid UTF-8")

    # Verify the body still matches the catalog fingerprint. If the file changed
    # under the pinned catalog, refuse rather than silently serve new content.
    if entry.fingerprint and _fingerprint_bytes(raw) != entry.fingerprint:
        raise SkillReadError(
            f"skill '{name}' changed on disk since it was catalogued — restart the "
            "session to reload the catalog"
        )

    _fm, body = _split_frontmatter(text)
    return body.strip()


def body_matches_fingerprint(catalog: Catalog, name: str) -> bool:
    """True if the on-disk body still matches the catalogued fingerprint."""
    entry = catalog.get(name)
    if entry is None:
        return False
    try:
        return _fingerprint_bytes(entry.path.read_bytes()) == entry.fingerprint
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Skills context (built once per process, shared across runners)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillsContext:
    """Everything a runner needs to wire skills for one process (shared catalog).

    ``enabled`` is False when the feature is off OR the catalog is empty, in
    which case runners add no manifest, no tools and no state — disabled mode
    is byte-for-byte unchanged. ``mode`` is "model" (A) or "recommend" (B).
    """

    enabled: bool
    catalog: Catalog
    manifest: str
    mode: str
    diagnostics: tuple[str, ...] = ()
    #: What the operator asked for, kept so a fallback can be reported rather
    #: than silently applied. Empty when skills are disabled.
    requested_mode: str = ""
    #: The directory discovery actually used, after ``~`` expansion and CWD
    #: resolution. Reported at startup so a relative setting is never ambiguous.
    resolved_dir: str = ""


def build_skills_context(
    *,
    enabled: bool,
    skills_dir: str | os.PathLike | None,
    manifest_tokens: int,
    selection_mode: str,
) -> SkillsContext:
    """Discover the catalog and render the manifest once, for all runners.

    Called from each runner's client-build seam so the catalog + manifest are
    identical across local/fireworks/deepinfra. Falls back to disabled on any
    discovery problem (discovery itself never raises; this is belt-and-braces).
    """
    if not enabled:
        return SkillsContext(False, EMPTY_CATALOG, "", selection_mode, (),
                            requested_mode=selection_mode or "")
    skills_dir = resolve_skills_dir(skills_dir)
    _resolved = os.path.abspath(skills_dir) if skills_dir else ""
    try:
        catalog = discover(skills_dir)
    except Exception as e:  # discovery is written not to raise; never take the runner down
        return SkillsContext(
            False, EMPTY_CATALOG, "", selection_mode,
            (f"skill discovery failed: {e.__class__.__name__}",),
            requested_mode=selection_mode or "",
            resolved_dir=_resolved,
        )
    if not catalog.entries:
        # No usable skills: behave exactly as disabled.
        return SkillsContext(
            False, catalog, "", selection_mode, catalog.diagnostics,
            requested_mode=selection_mode or "",
            resolved_dir=_resolved,
        )
    manifest = build_manifest(catalog, max_tokens=manifest_tokens)
    mode = selection_mode if selection_mode in ("model", "recommend") else "model"
    return SkillsContext(
        True, catalog, manifest, mode, catalog.diagnostics,
        requested_mode=selection_mode or "",
        resolved_dir=_resolved,
    )


def skills_startup_report(context: "SkillsContext") -> list[str]:
    """Operator-facing lines describing how skills resolved at startup.

    Skills fail quietly by design: an invalid skill is excluded, an unknown
    selection mode falls back, a missing directory yields an empty catalog. That
    is right for a request path — a bad skill file must never cost a turn — but
    it leaves the operator with no way to see that their configuration did not
    take effect. These lines are printed once at startup, in the same shape as
    ``core.config.additional_dirs_report``.

    Returns an empty list when skills are disabled: nothing was consulted, so
    there is nothing to report.
    """
    if not context.enabled and not context.diagnostics:
        # Nothing was consulted, or nothing went wrong: no report. When the
        # catalog is empty *because* skills were rejected, the diagnostics are
        # the whole point and must still be shown.
        return []

    lines: list[str] = []
    _where = f", dir {context.resolved_dir}" if context.resolved_dir else ""
    lines.append(
        f"Skills: {len(context.catalog.entries)} available "
        f"({', '.join(e.name for e in context.catalog.entries)}), "
        f"mode '{context.mode}'{_where}"
    )

    # An unrecognised selection mode silently becomes "model". Say so rather than
    # letting a typo look like a deliberate choice.
    if context.requested_mode and context.requested_mode != context.mode:
        lines.append(
            f"Skills: selection mode {context.requested_mode!r} is not recognised "
            f"(use 'model' or 'recommend') -- falling back to '{context.mode}'"
        )

    for diagnostic in context.diagnostics:
        lines.append(f"Skills: {diagnostic}")
    return lines


# ---------------------------------------------------------------------------
# Mode B: deterministic harness-assisted recommendations (no LLM/embeddings)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    name: str
    score: float
    reasons: tuple[str, ...]


#: Tokens too generic to indicate skill relevance on their own.
_STOPWORDS = frozenset(
    "the a an and or of to in on for with this that is are be do does how what "
    "when where which it its i you we they my your our please can could would "
    "me use using run make get set want need help".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9_+-]*", text.lower()) if t not in _STOPWORDS]


def _normalize_phrase(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _phrase_in_request(phrase: str, request_norm: str) -> bool:
    """True if `phrase` occurs in `request_norm` on word boundaries.

    Both are pre-normalized (lowercase, collapsed whitespace). Boundaries are
    checked with non-alphanumeric edges so "ticket" does not match "tickets"
    when the trigger is the bare word, but a multi-word phrase like
    "create a ticket" still matches inside a longer sentence.
    """
    if not phrase:
        return False
    start = 0
    while True:
        idx = request_norm.find(phrase, start)
        if idx == -1:
            return False
        before = request_norm[idx - 1] if idx > 0 else " "
        after_idx = idx + len(phrase)
        after = request_norm[after_idx] if after_idx < len(request_norm) else " "
        if not before.isalnum() and not after.isalnum():
            return True
        start = idx + 1


def recommend(
    catalog: Catalog,
    *,
    user_request: str,
    recent_errors: str = "",
    limit: int = 2,
) -> list[Recommendation]:
    """Deterministic trigger-phrase recommendations (Mode B). [] to abstain.

    Matches ONLY the configured ``metadata.agentgate.triggers`` phrases against
    the current user request — never the skill body or description, and never
    tool results or history. Case/whitespace normalized, word-boundary matched.
    If several skills match, at most ``limit`` are returned, ordered by match
    specificity (longer/more-specific phrase first) then skill name. No match
    yields [] (abstention). ``recent_errors`` is accepted for interface
    stability but intentionally does NOT widen matching (spec §4: match only the
    user request).

    Never injects a body; a recommendation is only a nudge to call ``skill_read``.
    """
    req = _normalize_phrase(user_request)
    if not req:
        return []

    results: list[Recommendation] = []
    for entry in catalog.entries:
        matched: list[str] = []
        for trig in entry.triggers:
            if _phrase_in_request(trig, req):
                matched.append(trig)
        if not matched:
            continue
        # Specificity: the longest matched trigger dominates (more words/chars
        # = more specific), and a higher match count adds a small tie-break.
        best_len = max(len(m) for m in matched)
        score = best_len + (len(matched) - 1) * 0.001
        reasons = tuple(sorted(matched, key=lambda t: (-len(t), t))[:3])
        results.append(Recommendation(entry.name, round(score, 3), reasons))

    # Stable ordering: higher specificity score first, then skill name.
    results.sort(key=lambda r: (-r.score, r.name))
    return results[:limit]


def build_recommendation_nudge(recs: list[Recommendation]) -> str:
    """Turn recommendations into a short, model-visible, volatile nudge.

    This goes in the per-turn ephemeral context (NOT the cached stable prompt),
    and only ever references skills by name — it never inserts a body.
    """
    if not recs:
        return ""
    lines = ["## Suggested skills for this turn"]
    lines.append(
        "These skills may be relevant. Call `skill_read` to load one if it fits; "
        "ignore them if not."
    )
    for r in recs:
        why = ", ".join(r.reasons) if r.reasons else "request terms"
        lines.append(f"- {r.name} (match: {why})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-session activation tracking (multi-turn continuity)
# ---------------------------------------------------------------------------

#: Tool names for skill loading/listing. Kept here so runners and tests agree.
SKILL_READ_TOOL = "skill_read"
SKILL_LIST_TOOL = "skill_list"


@dataclass
class ActiveSkill:
    name: str
    fingerprint: str
    activated_turn: int


class SkillSession:
    """Tracks which skills were actually *activated* (body appended) this session.

    Truth about presence is derived from the actual outgoing message list, not
    from internal state: ``body_present_in`` scans the real messages, so a body
    dropped by trimming/compaction is correctly reported as absent even if the
    activation record remains. This is what makes reactivation safe.

    The class never stores bodies (they live in the messages); it only keeps
    name + fingerprint + activation turn, so state stays tiny.
    """

    def __init__(self, catalog: Optional[Catalog] = None) -> None:
        self._active: dict[str, ActiveSkill] = {}
        self._reactivated: set[str] = set()
        self._catalog = catalog
        # Name of the most recent skill reactivated by reactivation_block(), so
        # callers can label the lower-trust restore message with the right skill.
        self.last_reactivated_name: Optional[str] = None

    def bind_catalog(self, catalog: Catalog) -> None:
        """Bind the catalog used for body-presence scanning and reactivation."""
        self._catalog = catalog

    def record_activation(self, name: str, fingerprint: str, turn: int) -> None:
        """Record that a skill body was successfully appended as tool content."""
        self._active[name] = ActiveSkill(name, fingerprint, turn)

    @property
    def active_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    def body_present_in(self, messages: list[dict], name: str) -> bool:
        """True if this skill's body is currently in the outgoing messages.

        Detected by scanning ``role: tool`` contents for the verbatim body of
        the skill (loaded from the catalog). Scanning the real messages is what
        the spec requires (do not infer presence from activation state).
        """
        if self._catalog is None or self._catalog.get(name) is None:
            # No catalog bound (or unknown skill): fall back to a name marker.
            marker = f"[skill:{name}]"
            return any(
                m.get("role") == "tool" and marker in str(m.get("content", ""))
                for m in messages
            )
        try:
            body = read_skill_body(self._catalog, name)
        except SkillReadError:
            return False
        return any(
            m.get("role") == "tool" and body in str(m.get("content", ""))
            for m in messages
        )

    def missing_bodies(self, messages: list[dict]) -> list[str]:
        """Active skills whose body is no longer present in the outgoing messages."""
        return [n for n in self.active_names if not self.body_present_in(messages, n)]

    def reactivation_block(
        self, messages: list[dict], catalog: Catalog, turn: int
    ) -> str:
        """One-time restoration content for a still-active skill dropped by trimming.

        Task-boundary rule (spec §6): restoration is only produced within the
        *same* task the skill was activated in — i.e. ``turn`` must equal the
        activation turn. On a later user turn a previous task's skill is NOT
        auto-restored (the model can ``skill_read`` again if it still needs it).
        This prevents indefinite injection of an old skill after the task
        changes, without an LLM task detector.

        Returns plain content (no role). Callers MUST inject it as a lower-trust
        message (see ``restoration_message``), never as a system message — the
        skill body keeps its lower-priority origin (spec §5).
        """
        if not self._active or catalog is None:
            return ""
        blocks: list[str] = []
        for name in list(self.active_names):
            if name in self._reactivated:
                continue
            act = self._active[name]
            # Only restore within the same task (turn) the skill was activated in.
            if turn != act.activated_turn:
                continue
            # Verify the on-disk body still matches what was activated; if it
            # changed, do not silently re-inject different content.
            if act.fingerprint and not body_matches_fingerprint(catalog, name):
                self._reactivated.add(name)  # give up gracefully; model can re-read
                continue
            if self.body_present_in(messages, name):
                continue
            try:
                body = read_skill_body(catalog, name)
            except SkillReadError:
                self._reactivated.add(name)
                continue
            self._reactivated.add(name)
            self.last_reactivated_name = name
            blocks.append(body)
        return "\n\n".join(blocks)

    def explicit_activation(self, name: str, catalog: Catalog, turn: int) -> Optional[str]:
        """Return the exact body for an explicit ``//skill`` activation.

        Records the activation so normal lifecycle rules (no-repeat, one-time
        reactivation within the task) apply afterwards. Returns the body, or
        None if it cannot be read safely.
        """
        entry = catalog.get(name) if catalog is not None else None
        if entry is None:
            return None
        try:
            body = read_skill_body(catalog, name)
        except SkillReadError:
            return None
        self.record_activation(name, entry.fingerprint, turn)
        return body


# ---------------------------------------------------------------------------
# Mode C: explicit user invocation  //skill-name <task...>
# ---------------------------------------------------------------------------

#: Skill names are lowercase, alnum + hyphens (same rule as validation).
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    task: str


@dataclass(frozen=True)
class InvocationError:
    """A ``//something`` prefix that is NOT a valid invocation.

    Returned only when the message clearly looks like an invocation attempt
    (leading ``//word``) but the name is unknown or malformed, so the caller can
    surface a clear error instead of silently running it as a normal prompt.
    """

    name: str
    reason: str
    suggestion: Optional[str] = None


def parse_skill_invocation(text: str, catalog: Catalog) -> "SkillInvocation | InvocationError | None":
    """Parse ``//<skill-name> <task>`` at the start of a user message.

    Returns:
    - ``SkillInvocation`` for a valid, catalogued skill,
    - ``InvocationError`` when the text looks like an invocation but the name is
      unknown/malformed (so the bot can report a clear error),
    - ``None`` when the text is not an invocation attempt at all (normal prompt).

    Contract (spec §4): only a leading ``//word`` (after optional whitespace) is
    considered; ``//`` later in the message, or inside code/quotes, is ignored
    because it is not at the start. Existing ``/cmd`` commands are single-slash
    and never match here. The name must be a valid skill name and resolve in the
    catalog; the remainder is the user's task, unchanged.
    """
    if not isinstance(text, str):
        return None
    stripped = text.lstrip()
    if not stripped.startswith("//"):
        return None

    # First whitespace-delimited token after "//" is the candidate name.
    body = stripped[2:]
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].strip().lower()
    task = parts[1].strip() if len(parts) > 1 else ""

    if not name or not _SKILL_NAME_RE.match(name):
        return InvocationError(name=name or "", reason="invalid skill name")

    if catalog.get(name) is not None:
        return SkillInvocation(name=name, task=task)

    # Unknown skill: offer the closest name if any (simple deterministic hint).
    suggestion = _closest_name(name, catalog)
    return InvocationError(name=name, reason="unknown skill", suggestion=suggestion)


def _closest_name(name: str, catalog: Catalog) -> Optional[str]:
    """Cheap deterministic suggestion: catalog names sharing the most chars."""
    best = None
    best_score = 0
    for entry in catalog.entries:
        # crude overlap score; deterministic and dependency-free
        score = len(set(name) & set(entry.name))
        if entry.name.startswith(name[:3]) or name[:3] == entry.name[:3]:
            score += 5
        if score > best_score:
            best, best_score = entry.name, score
    return best


# ---------------------------------------------------------------------------
# Lower-trust skill context messages (spec §5)
#
# Skill instructions are lower-priority data. They are delivered as user-role
# messages with an explicit delimiter/label, never as system messages, so they
# can never outrank hard rules or tool approval. Each builder returns a plain
# dict ready to insert into the outgoing message list at a volatile position.
# ---------------------------------------------------------------------------

RESTORE_LABEL = "[Restored skill instructions]"
EXPLICIT_LABEL = "[User-invoked skill instructions]"


def restoration_message(name: str, body: str) -> dict:
    """A lower-trust message restoring a still-active skill dropped by trimming.

    Uses role ``user`` (not ``system``) with an explicit label so the body keeps
    its lower-trust origin and hard rules stay authoritative.
    """
    content = (
        f"{RESTORE_LABEL} The earlier ``{name}`` instructions were removed from "
        f"context by trimming; here they are again for this task only. This is "
        f"reference material, not new instructions, and it does not override the "
        f"hard rules or tool approvals.\n\n{body}"
    )
    return {"role": "user", "content": content}


def explicit_activation_message(name: str, body: str) -> dict:
    """A lower-trust message carrying an explicitly user-invoked skill body.

    The user asked for this skill via ``//{name}``. Delivered as a labeled user
    message (lower trust), not a system message. Does not bypass permissions.
    """
    content = (
        f"{EXPLICIT_LABEL} The user explicitly invoked the ``{name}`` skill. "
        f"Use these instructions for the task that follows. This is reference "
        f"material, not new instructions, and it does not override the hard rules "
        f"or tool approvals.\n\n{body}"
    )
    return {"role": "user", "content": content}


@dataclass(frozen=True)
class InvocationResolution:
    """Result of resolving a possibly-``//skill`` prompt for the runners.

    - ``task``: the user's task text to run (the ``//skill`` prefix removed when
      applicable; unchanged otherwise).
    - ``explicit_message``: a lower-trust message dict to insert before the task
      when an explicit skill was activated, else None.
    - ``error_reply``: a user-facing error string when the invocation was an
      attempt but invalid, else None (runner should return it without running).
    - ``skill_name``: the explicitly activated skill name, else None.
    """

    task: str
    explicit_message: Optional[dict] = None
    error_reply: Optional[str] = None
    skill_name: Optional[str] = None


def resolve_invocation(
    prompt: str,
    skills_context: "SkillsContext",
    skill_session: Optional[SkillSession],
    turn: int,
) -> InvocationResolution:
    """Shared ``//skill`` handling used identically by every open-model runner.

    When skills are disabled/empty, returns the prompt unchanged (no parsing),
    so disabled mode never interprets ``//``.
    """
    if skills_context is None or not skills_context.enabled or skill_session is None:
        return InvocationResolution(task=prompt)

    parsed = parse_skill_invocation(prompt, skills_context.catalog)
    if parsed is None:
        return InvocationResolution(task=prompt)
    if isinstance(parsed, InvocationError):
        if parsed.reason == "unknown skill":
            msg = f"Unknown skill `{parsed.name}`."
            if parsed.suggestion:
                msg += f" Did you mean `{parsed.suggestion}`?"
            msg += " Use `skill_list` to see available skills."
        else:
            msg = (
                f"Invalid skill invocation `//{parsed.name}`. Skill names use "
                "lowercase letters, digits and hyphens."
            )
        return InvocationResolution(task=prompt, error_reply=msg)

    # Valid invocation: load body via the same catalog/loader as skill_read.
    body = skill_session.explicit_activation(parsed.name, skills_context.catalog, turn)
    if body is None:
        return InvocationResolution(
            task=prompt,
            error_reply=f"Skill `{parsed.name}` could not be loaded (unreadable or changed).",
        )
    return InvocationResolution(
        task=parsed.task,
        explicit_message=explicit_activation_message(parsed.name, body),
        skill_name=parsed.name,
    )


def is_skill_read_tool(name: str) -> bool:
    return name == SKILL_READ_TOOL


def is_skill_tool(name: str) -> bool:
    return name in (SKILL_READ_TOOL, SKILL_LIST_TOOL)


def record_activation_from_result(
    session: SkillSession,
    catalog: Catalog,
    name: str,
    args: dict,
    result_text: str,
    turn: int,
) -> None:
    """Record an activation iff this was a successful ``skill_read`` call.

    A skill is only "active" when its body was actually appended as tool
    content, never when merely recommended, and never when the read errored.
    """
    if name != SKILL_READ_TOOL or not isinstance(result_text, str):
        return
    if result_text.startswith("ERROR:"):
        return
    skill_name = args.get("name") if isinstance(args, dict) else None
    if not isinstance(skill_name, str):
        return
    entry = catalog.get(skill_name)
    if entry is None:
        return
    session.record_activation(skill_name, entry.fingerprint, turn)
