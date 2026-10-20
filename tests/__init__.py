"""
tests/ - Test suite for Kinetic SDK.

Run all tests:
    uv run python -m tests.test_runner
    uv run python -m pytest

Run specific test module:
    uv run python -m unittest tests.test_imports
    uv run python -m unittest tests.test_cli
"""

import os

# Tests never touch the real OS keyring: the null backend stores nothing and
# returns None, so no stored credential is ever read, and no backend can block
# on a prompt (headless Linux/WSL asks for a "default" keyring password).
# Tests that need a stored value patch keyring themselves. Set before any test
# imports keyring; subprocesses started by tests inherit it.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
