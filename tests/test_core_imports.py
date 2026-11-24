"""Every `core.*` import in the source tree must resolve.

Why this exists
---------------
Grouping flat modules into packages is a rename across hundreds of import sites, and Python
fails *lazily*: a stale `from core.artifacts import ...` only breaks when that line executes. A
file guarded by an `if TYPE_CHECKING` or a lazily-imported branch can hide a broken path for a
long time.

This walks the real source tree and imports each module, so a half-applied move fails here rather
than in production.
"""
import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGES = ("core", "runners", "bots")


def _modules() -> list[str]:
    names = []
    for pkg in PACKAGES:
        for path in (REPO / pkg).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            rel = path.relative_to(REPO).with_suffix("")
            names.append(".".join(rel.parts))
    return sorted(names)


@pytest.mark.parametrize("module", _modules())
def test_module_imports(module):
    """A stale internal import path fails import — catch it here, not at runtime."""
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # Third-party or env-dependent deps are out of scope; missing *core* modules are not.
        if exc.name and exc.name.startswith(("core", "runners", "bots")):
            raise AssertionError(
                f"{module} imports a module that does not exist: {exc.name}"
            ) from exc
        pytest.skip(f"{module} needs an unavailable third-party dependency: {exc.name}")


def test_no_references_to_removed_flat_modules():
    """Structural: the moved modules must not be imported by their old flat paths."""
    moved = ("tool_compression", "tool_pipeline", "artifacts", "dedup", "repo_map", "cache_key")
    offenders = []
    for pkg in PACKAGES:
        for path in (REPO / pkg).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text()
            for name in moved:
                if f"core.{name}" in text and f"core.context.{name}" not in text:
                    offenders.append(f"{path.relative_to(REPO)}: core.{name}")
    assert not offenders, "stale flat imports remain:\n  " + "\n  ".join(offenders)
