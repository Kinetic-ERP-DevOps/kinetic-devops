"""
kinetic_devops/cli/test_runner.py - Test runner entry point for the Kinetic SDK.

Invokable as:
    uv run kinetic-test
    python -m kinetic_devops.cli.test_runner
    python kinetic_devops/cli/test_runner.py
"""
import sys
from pathlib import Path

# Resolve repository root: cli/ -> kinetic_devops/ -> repo root.
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

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
            f"[FATAL] Unable to import tests.test_runner from repository root '{_repo_root}': {_runner_import_error}",
            file=sys.stderr,
        )
        return 1

    return suite_runner.run_tests()


if __name__ == "__main__":
    sys.exit(run_tests())
