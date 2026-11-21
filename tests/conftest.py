"""Shared pytest configuration.

`core/config.py` calls `load_dotenv()` at import time. python-dotenv resolves the
file by walking up from the calling module, not from the working directory, so
every test run would otherwise inherit the developer's local `.env`. That makes
test outcomes depend on whose machine runs them: a variable set locally can flip
a default that a test asserts on (and even change which tests are skipped).

python-dotenv honours `PYTHON_DOTENV_DISABLED`, which is the supported way to
turn discovery off. It MUST be set here at module import time, not inside a
fixture: pytest imports `conftest.py` before it imports test modules, so this
assignment runs before any test module imports `core.config` and triggers
`load_dotenv()`. A session-scoped fixture would run too late — collection has
already imported `core.*` by then, so `.env` would already be in `os.environ`.

With dotenv disabled, tests must declare the environment they need with
`monkeypatch.setenv`, so they assert behaviour rather than whatever happens to be
configured locally.

Why not restore the previous value in a fixture: the suite is a single process
and every test relies on dotenv being off; leaving it set for the session is the
point. Restoring it would let a later-imported module reload `.env` mid-run.
"""

import os

# Must run at conftest import, before test modules import core.config.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
