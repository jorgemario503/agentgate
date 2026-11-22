"""Milestone 03 / ORCH-AUTHZ-001..006 — fail-closed gate authorization."""
import pytest

from core.commands import is_admin, is_allowed
from core.orchestration.authz import NotAuthorized, is_gate_approver, require_gate_approver


class _P:
    allowed_user_ids = frozenset()
    admin_user_ids = frozenset()


def _gate(platform="slack", users=("U-APPROVER",)):
    return {"gate_id": "G-1",
            "approvers": {"platform": platform, "user_ids": list(users)}}


# --------------------------------------------------------------------------
# ORCH-AUTHZ-001: two distinct checks
# --------------------------------------------------------------------------

def test_bot_use_and_gate_approval_are_separate_functions():
    assert is_allowed(_P(), "anyone") is True          # bot check: open
    assert is_gate_approver(_gate(), platform="slack", user_id="anyone") is False


# --------------------------------------------------------------------------
# ORCH-AUTHZ-002: fails closed (contrast with is_allowed, which fails open)
# --------------------------------------------------------------------------

def test_empty_allowlist_fails_open_for_bot_use():
    assert is_allowed(_P(), "random-user") is True


def test_empty_approvers_fails_closed():
    assert is_gate_approver(_gate(users=()), platform="slack",
                            user_id="U-APPROVER") is False


def test_missing_approvers_fails_closed():
    assert is_gate_approver({"gate_id": "G-1"}, platform="slack",
                            user_id="U-APPROVER") is False


@pytest.mark.parametrize("platform,user", [(None, "U-APPROVER"), ("slack", None),
                                           (None, None), ("", "")])
def test_incomplete_identity_fails_closed(platform, user):
    assert is_gate_approver(_gate(), platform=platform, user_id=user) is False


# --------------------------------------------------------------------------
# ORCH-AUTHZ-003: admin is not a fallback
# --------------------------------------------------------------------------

def test_admin_flag_is_not_used_by_default():
    """is_admin exists, but is_gate_approver must not consult it implicitly."""
    assert is_admin(type("P", (), {"admin_user_ids": frozenset({"U-APPROVER"})})(),
                    "U-APPROVER") is True
    assert is_gate_approver(_gate(users=("U-OTHER",)), platform="slack",
                            user_id="U-APPROVER") is False


def test_admin_fallback_only_when_explicitly_requested():
    g = _gate(users=("U-OTHER",))
    assert is_gate_approver(g, platform="slack", user_id="U-ADMIN") is False
    assert is_gate_approver(g, platform="slack", user_id="U-ADMIN",
                            allow_admins=("U-ADMIN",)) is True


# --------------------------------------------------------------------------
# ORCH-AUTHZ-004: platform scoping
# --------------------------------------------------------------------------

def test_wrong_platform_denied():
    assert is_gate_approver(_gate(platform="slack"), platform="telegram",
                            user_id="U-APPROVER") is False


def test_correct_platform_allowed():
    assert is_gate_approver(_gate(platform="slack"), platform="slack",
                            user_id="U-APPROVER") is True


# --------------------------------------------------------------------------
# ORCH-AUTHZ-005: read yes, resolve no
# --------------------------------------------------------------------------

def test_non_approver_raise_is_clear():
    with pytest.raises(NotAuthorized) as exc:
        require_gate_approver(_gate(), platform="slack", user_id="U-NOBODY")
    assert "not an approver" in str(exc.value)


def test_approver_does_not_raise():
    require_gate_approver(_gate(), platform="slack", user_id="U-APPROVER")
