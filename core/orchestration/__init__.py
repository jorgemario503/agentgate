"""Deprecated alias for :mod:`core.handoff`.

``core.orchestration`` was renamed to ``core.handoff`` (DEC-404) because "orchestration"
now names the separate agentorchestrator repository, and keeping the word here invited
workflow logic to be added back into the runner.

**Remove this shim in 0.4.0.** It exists only so an already-published consumer —
agentorchestrator, which imports ``core.orchestration.validate`` — keeps working across
the 0.3.0 upgrade. 0.3.0 is the last release that ships it.

Migrate imports:

    from core.orchestration.validate import validate_gate_record   # old
    from core.handoff.validate import validate_gate_record          # new
"""
from __future__ import annotations

import sys
import warnings

from core.handoff import *          # noqa: F401,F403 - deliberate re-export
from core.handoff import __all__, __version__, PUBLISHED_API, TRANSITIONAL  # noqa: F401
from core import handoff as _handoff

# `from core.orchestration.validate import X` imports the *submodule*, which a
# package-level re-export does not provide. Register the real submodules under the old
# names so both import forms keep working.
for _name in ("validate", "submit", "usage"):
    _mod = __import__(f"core.handoff.{_name}", fromlist=["*"])
    sys.modules[f"{__name__}.{_name}"] = _mod
del _name, _mod

warnings.warn(
    "core.orchestration is deprecated and will be removed in 0.4.0; "
    "import from core.handoff instead",
    DeprecationWarning,
    stacklevel=2,
)
