"""Regression tests for zero-tests-discovered guard in test runners."""

import types
import unittest
from unittest.mock import patch

from tests import test_runner as suite_runner
from kinetic_devops.cli import test_runner as cli_runner


class _FakeResult:
    def __init__(self, tests_run: int, success: bool):
        self.testsRun = tests_run
        self.failures = []
        self.errors = []
        self.skipped = []
        self._success = success

    def wasSuccessful(self):
        return self._success


class _FakeTextRunner:
    def __init__(self, result_obj):
        self._result_obj = result_obj

    def run(self, _suite):
        return self._result_obj


class TestRunnerZeroTestsGuard(unittest.TestCase):
    """Ensure both runners fail when zero tests are discovered."""

    def test_tests_runner_fails_on_zero_tests(self):
        fake_result = _FakeResult(tests_run=0, success=True)
        fake_runner = _FakeTextRunner(fake_result)

        with patch.object(suite_runner, "validate_environment", return_value=True):
            with patch("tests.test_runner.unittest.TestLoader") as loader_cls:
                loader_cls.return_value.discover.return_value = types.SimpleNamespace()
                with patch("tests.test_runner.unittest.TextTestRunner", return_value=fake_runner):
                    exit_code = suite_runner.run_tests()

        self.assertEqual(exit_code, 1)

    def test_cli_runner_delegates_to_suite_runner(self):
        with patch.object(suite_runner, "run_tests", return_value=1) as run_tests:
            exit_code = cli_runner.run_tests()

        run_tests.assert_called_once_with()
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
