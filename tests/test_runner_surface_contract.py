"""The published runner surface must match what the contract document says.

Why this exists
---------------
`docs/runner-surface-contract.md` tells `agentorchestrator` what it may depend on. A document
that drifts from the code is worse than no document: it promises a stable surface that no longer
exists, and the second repo only finds out at runtime.

I got those numbers wrong twice while writing it — first listing pre-grouping flat module names,
then missing `import`-style statements in an AST walk. So the document is checked here.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "docs" / "runner-surface-contract.md"


def _contract_text() -> str:
    assert CONTRACT.is_file(), f"missing {CONTRACT}"
    return CONTRACT.read_text()


def _core_modules_imported_by_runners() -> set[str]:
    """Every `core.X` the runners import — both `from core.X import` and `import core.X`."""
    mods: set[str] = set()
    for path in (REPO / "runners").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("core."):
                    mods.add(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("core."):
                        mods.add(alias.name.split(".")[1])
    mods.discard("handoff")   # separate published contract (DEC-108)
    return mods


def test_contract_lists_exactly_the_modules_runners_import():
    """The module list in the contract must equal the real set."""
    text = _contract_text()

    # The contract names the modules in one inline code block of backticks.
    listed = set(re.findall(r"`([a-z_]+)`", text.split("### 3.1")[1].split("Twelve modules")[0]))

    actual = _core_modules_imported_by_runners()
    assert listed == actual, (
        "contract module list drifted from the code\n"
        f"  only in doc : {sorted(listed - actual)}\n"
        f"  only in code: {sorted(actual - listed)}"
    )


def test_contract_count_matches_the_list():
    """The stated number must match the enumerable list — the error I made twice."""
    text = _contract_text()
    actual = _core_modules_imported_by_runners()
    words = {10: "Ten", 11: "Eleven", 12: "Twelve", 13: "Thirteen", 14: "Fourteen"}
    expected_word = words.get(len(actual))
    assert expected_word, f"add {len(actual)} to the word map"
    assert f"{expected_word} modules" in text, (
        f"contract says a different count; runners import {len(actual)}"
    )


def test_contract_names_the_supported_runner_members():
    """Every member the contract calls supported must exist on the protocol or the runners."""
    from runners.base import Runner

    text = _contract_text()
    supported = text.split("### 2.1")[1].split("### 2.2")[0]
    for member in re.findall(r"`([a-z_]+)\(", supported):
        assert hasattr(Runner, member), (
            f"contract lists {member!r} as supported but Runner does not declare it"
        )


def test_set_model_gap_is_documented():
    """Duck-typed members must be named in the contract rather than discovered later."""
    from runners import deepinfra, fireworks, local

    for module in (deepinfra, fireworks, local):
        runner_cls = [c for c in vars(module).values()
                      if isinstance(c, type) and c.__name__.endswith("Runner")][0]
        if hasattr(runner_cls, "set_model"):
            assert "set_model" in _contract_text(), (
                "runners implement set_model but the contract does not mention it"
            )
            return
    pytest.skip("no runner implements set_model")
