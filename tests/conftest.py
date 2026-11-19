"""Shared pytest configuration.

`core/config.py` calls `load_dotenv()` at import time. python-dotenv resolves the
file by walking up from the calling module, not from the working directory, so
every test run silently inherits the developer's local `.env`. That makes test
outcomes depend on whose machine runs them: a variable set locally can flip a
default that a test asserts on.

python-dotenv honours `PYTHON_DOTENV_DISABLED`, which is the supported way to
turn discovery off. Setting it here keeps the suite hermetic: tests must declare
the environment they need with `monkeypatch.setenv`, so they assert behaviour
rather than whatever happens to be configured locally.
"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_dotenv():
    """Stop `load_dotenv()` from reading a real `.env` for the whole session."""
    previous = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    yield
    if previous is None:
        os.environ.pop("PYTHON_DOTENV_DISABLED", None)
    else:
        os.environ["PYTHON_DOTENV_DISABLED"] = previous
