"""
kinetic_devops/cli/test_runner.py - Test runner entry point for the Kinetic SDK.

Run from the repository root, so the ``tests`` package is importable:
    uv run python -m kinetic_devops.cli.test_runner
"""
import sys
from pathlib import Path

try:
    from tests import test_runner as suite_runner
    _runner_import_error = None
except Exception as exc:  # pragma: no cover - only expected in broken runtime/bootstrap states
    suite_runner = None
    _runner_import_error = exc


def run_tests() -> int:
    """Delegate to the canonical test runner implementation under tests/."""
    if suite_runner is None:
        print(
            f"[FATAL] Unable to import tests.test_runner from '{Path.cwd()}' (run from the repository root): "
            f"{_runner_import_error}",
            file=sys.stderr,
        )
        return 1

    return suite_runner.run_tests()


if __name__ == "__main__":
    sys.exit(run_tests())
