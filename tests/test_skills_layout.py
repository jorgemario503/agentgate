"""Milestone 01 / ORCH-SKILL-001..004 — role skills are discoverable in the real catalog.

These tests assert against the *shipped* `skills/` directory, not a temp fixture:
the milestone requirement is that an operator-configured catalog discovers the three
role skills with no diagnostics. A temp-dir fixture would prove the catalog works but
would not prove the repository is in the required layout.

Deliberately offline and deterministic — no runner, no network, no model calls.
"""
from pathlib import Path

from core.skills.catalog import discover, read_skill_body

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

ROLE_SKILLS = {
    "product-spec-driven": ("governing spec", "phase 3", "phase 4"),
    "milestone-implement": ("milestone_submit", "own work", "implementation-report.md"),
    "milestone-review": ("review-findings.json", "review-report.md", "not-verified"),
}


def _catalog():
    return discover(SKILLS_DIR)


def _by_name(catalog):
    return {e.name: e for e in catalog.entries}


def test_role_skills_are_discovered():
    """ORCH-SKILL-001: the three role skills appear in the catalog."""
    names = set(_by_name(_catalog()))
    missing = sorted(set(ROLE_SKILLS) - names)
    assert not missing, f"role skills not discovered: {missing}"
    for name in ROLE_SKILLS:
        assert name in names


def test_role_skills_produce_no_diagnostics():
    """ORCH-SKILL-001/002: discovery reports no problems for the role skills.

    A malformed SKILL.md is silently excluded by discover(), so asserting only on
    presence would miss a broken frontmatter. This also guards ORCH-SKILL-002
    (folder name must equal frontmatter name) because a mismatch emits a diagnostic.
    """
    catalog = _catalog()
    offending = [
        d for d in catalog.diagnostics
        if any(name in d for name in ROLE_SKILLS)
    ]
    assert not offending, f"diagnostics for role skills: {offending}"


def test_skill_directory_layout_matches_name():
    """ORCH-SKILL-002: each skill lives at skills/<name>/SKILL.md."""
    for name in ROLE_SKILLS:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), f"missing skills/{name}/SKILL.md"


def test_skill_bodies_are_loadable_and_substantive():
    """ORCH-SKILL-003: bodies load and carry the expected procedure content."""
    catalog = _catalog()
    for name, needles in ROLE_SKILLS.items():
        body = read_skill_body(catalog, name)
        assert len(body.encode("utf-8")) > 1000, f"{name} body looks empty"
        lowered = body.lower()
        for needle in needles:
            assert needle in lowered, f"{name} body missing expected content: {needle!r}"


def test_descriptions_are_present_and_bounded():
    """ORCH-SKILL-003: descriptions exist and stay within the catalog limit."""
    from core.skills.catalog import MAX_DESCRIPTION_LEN

    for entry in _catalog().entries:
        if entry.name not in ROLE_SKILLS:
            continue
        assert entry.description.strip(), f"{entry.name} has an empty description"
        assert len(entry.description) <= MAX_DESCRIPTION_LEN


def test_role_skills_declare_triggers():
    """Optional but asserted: triggers are wired for Mode B recommendations."""
    for entry in _catalog().entries:
        if entry.name not in ROLE_SKILLS:
            continue
        assert entry.triggers, f"{entry.name} declares no metadata.agentgate.triggers"


def test_implementer_skill_forbids_self_approval():
    """The implement role must not imply it can approve or merge its own work."""
    body = read_skill_body(_catalog(), "milestone-implement").lower()
    assert "cannot approve" in body or "do not review, approve" in body
    assert "never claim a check ran when it did not" in body


def test_reviewer_skill_forbids_implementing():
    """The review role must not author code fixes."""
    body = read_skill_body(_catalog(), "milestone-review").lower()
    assert "do not implement fixes" in body
    assert "recommend" in body
