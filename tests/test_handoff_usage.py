"""Milestone 01 / ORCH-USAGE-001..007 — normalized provider usage capture.

These tests use **fake completion dicts**, so they verify our extraction and
emission logic, not provider behaviour. Whether each provider actually populates
`cached_tokens` requires a live call and is recorded as evidence, not asserted here
(milestone-spec §10).

The critical invariant under test: a missing count is `None`, never `0`. Zero is a
measured value; absence is not. Collapsing them would under-report every rollup.
"""
import io
import logging

import pytest

from core.handoff.usage import (
    UsageRecord,
    normalize_usage,
    safe_emit,
)

FULL_USAGE = {
    "usage": {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 96, "cache_write_tokens": 4},
    }
}


# --------------------------------------------------------------------------
# ORCH-USAGE-001/002: counts and cached detail are captured
# --------------------------------------------------------------------------

def test_captures_core_counts():
    r = normalize_usage(FULL_USAGE, runner="deepinfra", model_id="m", session_key="k")
    assert r.prompt_tokens == 120
    assert r.completion_tokens == 30
    assert r.total_tokens == 150


def test_captures_cached_and_cache_write():
    r = normalize_usage(FULL_USAGE, runner="deepinfra", model_id="m", session_key="k")
    assert r.cached_tokens == 96
    assert r.cache_write_tokens == 4


def test_has_provider_usage_true_when_any_count_present():
    r = normalize_usage(FULL_USAGE, runner="x", model_id="m", session_key="k")
    assert r.has_provider_usage is True


# --------------------------------------------------------------------------
# ORCH-USAGE-003: absent is None, never zero
# --------------------------------------------------------------------------

def test_missing_usage_is_none_not_zero():
    r = normalize_usage({}, runner="x", model_id="m", session_key="k")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "cached_tokens", "cache_write_tokens"):
        assert getattr(r, field) is None, f"{field} must be None when absent"


def test_zero_is_preserved_as_measured_zero():
    r = normalize_usage({"usage": {"prompt_tokens": 0}}, runner="x",
                        model_id="m", session_key="k")
    assert r.prompt_tokens == 0
    assert r.prompt_tokens is not None


def test_missing_cached_tokens_stays_none_even_when_other_counts_present():
    r = normalize_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                        runner="x", model_id="m", session_key="k")
    assert r.prompt_tokens == 10
    assert r.cached_tokens is None


def test_non_dict_usage_is_treated_as_absent():
    r = normalize_usage({"usage": "garbage"}, runner="x", model_id="m", session_key="k")
    assert r.prompt_tokens is None
    assert r.has_provider_usage is False


def test_negative_and_bool_values_rejected():
    r = normalize_usage({"usage": {"prompt_tokens": -5, "completion_tokens": True}},
                        runner="x", model_id="m", session_key="k")
    assert r.prompt_tokens is None
    assert r.completion_tokens is None  # bool is not accepted as a count


# --------------------------------------------------------------------------
# ORCH-USAGE-006: identity fields present for attribution
# --------------------------------------------------------------------------

def test_record_carries_identity_fields():
    r = normalize_usage(FULL_USAGE, runner="deepinfra", model_id="meta/llama",
                        session_key="01:implementer", ts_start=1.0, ts_end=2.0)
    assert r.runner == "deepinfra"
    assert r.model_id == "meta/llama"
    assert r.session_key == "01:implementer"
    assert r.outcome == "ok"
    assert r.ts_start == 1.0 and r.ts_end == 2.0


def test_record_is_frozen_and_serialisable():
    r = normalize_usage(FULL_USAGE, runner="x", model_id="m", session_key="k")
    with pytest.raises(Exception):
        r.prompt_tokens = 1  # frozen
    d = r.as_dict()
    assert d["prompt_tokens"] == 120 and d["runner"] == "x"


# --------------------------------------------------------------------------
# ORCH-USAGE-007: a failing sink never breaks the turn
# --------------------------------------------------------------------------

def test_safe_emit_swallows_sink_exception(caplog):
    def boom(_record):
        raise RuntimeError("disk full")

    rec = normalize_usage(FULL_USAGE, runner="x", model_id="m", session_key="k")
    with caplog.at_level(logging.WARNING):
        safe_emit(boom, rec)  # must not raise
    assert "usage sink failed" in caplog.text


def test_safe_emit_with_none_sink_is_noop():
    safe_emit(None, normalize_usage({}, runner="x", model_id="m", session_key="k"))


def test_safe_emit_delivers_record():
    seen = []
    rec = normalize_usage(FULL_USAGE, runner="x", model_id="m", session_key="k")
    safe_emit(seen.append, rec)
    assert seen == [rec]


# --------------------------------------------------------------------------
# ORCH-USAGE-005/004: runners are inert without a sink, and emit when given one
# These use the real client classes with a stubbed transport-free path: we call
# the client's _emit_usage directly, which is exactly what the request path does.
# --------------------------------------------------------------------------

def _make_client(module_name, cls_name, **kwargs):
    import importlib
    mod = importlib.import_module(module_name)
    cls = getattr(mod, cls_name)
    return cls, kwargs


@pytest.mark.parametrize("module_name,cls_name", [
    ("runners.deepinfra", "DeepInfraAgentClient"),
    ("runners.fireworks", "FireworksAgentClient"),
    ("runners.local", "LocalAgentClient"),
])
def test_client_without_sink_is_inert(module_name, cls_name):
    """ORCH-USAGE-005: no sink configured means _emit_usage does nothing."""
    mod = __import__(module_name, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    # Construct minimally without touching the network: we only exercise _emit_usage,
    # so build the object via __new__ and set the attributes it reads.
    obj = cls.__new__(cls)
    obj._usage_sink = None
    obj.model = "test-model"
    obj.session_id = "01:implementer"
    obj._emit_usage(FULL_USAGE)  # must not raise and must not emit


@pytest.mark.parametrize("module_name,cls_name,runner_name", [
    ("runners.deepinfra", "DeepInfraAgentClient", "deepinfra"),
    ("runners.fireworks", "FireworksAgentClient", "fireworks"),
    ("runners.local", "LocalAgentClient", "local"),
])
def test_client_emits_normalized_record(module_name, cls_name, runner_name):
    """ORCH-USAGE-001/006: each client emits one attributed record per response."""
    mod = __import__(module_name, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    seen = []
    obj = cls.__new__(cls)
    obj._usage_sink = seen.append
    obj.model = "test-model"
    obj.session_id = "01:implementer"

    obj._emit_usage(FULL_USAGE)

    assert len(seen) == 1
    rec = seen[0]
    assert isinstance(rec, UsageRecord)
    assert rec.runner == runner_name
    assert rec.model_id == "test-model"
    assert rec.session_key == "01:implementer"
    assert rec.prompt_tokens == 120
    assert rec.cached_tokens == 96


@pytest.mark.parametrize("runner_mod,runner_cls", [
    ("runners.deepinfra", "DeepInfraRunner"),
    ("runners.fireworks", "FireworksRunner"),
    ("runners.local", "LocalRunner"),
])
def test_runner_exposes_sink_setter(runner_mod, runner_cls):
    """ORCH-USAGE-005: every open runner can install and clear a sink."""
    mod = __import__(runner_mod, fromlist=[runner_cls])
    cls = getattr(mod, runner_cls)
    assert hasattr(cls, "set_usage_sink")


def test_run_prompt_signature_unchanged():
    """ORCH-USAGE-004: the sink must not widen run_prompt's public contract."""
    import inspect
    from runners.deepinfra import DeepInfraRunner
    params = list(inspect.signature(DeepInfraRunner.run_prompt).parameters)
    assert params[:5] == ["self", "thread_id", "workspace_path", "prompt",
                          "resume_session_id"]
    assert "usage_sink" not in params
    assert "usage" not in str(inspect.signature(DeepInfraRunner.run_prompt).return_annotation)
