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


def _modules_named_in_contract() -> set[str]:
    """The module names the contract lists in section 3.1.

    Sliced on section headings, not on the word-form count: changing "Twelve" to "Thirteen"
    silently moved the boundary once and made the parser read the wrong prose.
    """
    text = _contract_text()
    section = text.split("### 3.1")[1].split("### 3.2")[0]
    # The list is a run of `name`, `name`, ... in one paragraph.
    first = section.split("Measured from the source (not assumed):")[1]
    # Stop at the closing note: it names the four *old* flat modules, which are not in the set.
    first = first.split("Note `context` covers")[0]
    return set(re.findall(r"`([a-z_]+)`", first))


def test_contract_lists_exactly_the_modules_runners_import():
    """The module list in the contract must equal the real set."""
    listed = _modules_named_in_contract()
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


# --------------------------------------------------------------------------
# Section 3.2: the dependency claims must match the actual imports.
#
# Two rounds of getting numbers wrong in this same document is enough. These parse the source
# rather than trusting the prose.
# --------------------------------------------------------------------------

def _module_level_third_party() -> dict[str, set[str]]:
    """Map third-party package -> files importing it at module level (runners + shipped core)."""
    import ast

    watch = {"openai", "httpx", "claude_agent_sdk", "dotenv", "yaml", "agentdb"}
    found: dict[str, set[str]] = {}
    paths = list((REPO / "runners").glob("*.py"))
    paths += [REPO / "core" / "config.py", REPO / "core" / "skills" / "catalog.py",
              REPO / "core" / "agentdb.py"]
    for path in paths:
        if not path.is_file():
            continue
        for node in ast.parse(path.read_text()).body:      # module level ONLY
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                mods.extend(a.name.split(".")[0] for a in node.names)
            for mod in mods:
                if mod in watch:
                    found.setdefault(mod, set()).add(path.name)
    return found


def test_documented_base_dependencies_are_imported_at_module_level():
    """openai, httpx, dotenv and pyyaml are claimed as base deps -- prove they are needed."""
    found = _module_level_third_party()
    for pkg in ("openai", "httpx", "dotenv", "yaml"):
        assert pkg in found, f"contract claims {pkg} is required, but nothing imports it"


def test_claude_sdk_is_module_level_but_confined():
    """It IS a module-level import -- but only in claude.py, which nothing auto-imports."""
    found = _module_level_third_party()
    assert found.get("claude_agent_sdk") == {"claude.py"}, found.get("claude_agent_sdk")
    # And runners/__init__.py must stay empty, or importing `runners` would pull the SDK.
    init = (REPO / "runners" / "__init__.py").read_text().strip()
    assert not init, "runners/__init__.py must stay empty so `import runners` needs no SDK"


def test_agentdb_is_not_module_level():
    """The contract says it is imported lazily; that is why it is an extra."""
    found = _module_level_third_party()
    assert "agentdb" not in found, (
        "core/agentdb.py imports the agentdb package at module level; "
        "the contract says it is lazy, so either the doc or the code is wrong"
    )
