#!/usr/bin/env python
"""
tests/test_runner.py - Unified test runner for Kinetic SDK test suite

Usage:
    python -m tests.test_runner
    python -m tests.test_runner --validate
    python tests/test_runner.py
"""
import sys
import os
import unittest
import logging
import argparse
from pathlib import Path


# Resolve repository root (parent of tests/)
_repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(_repo_root))
os.chdir(str(_repo_root))


# Setup logging to `tests/test_results.log` and stdout
_log_file = _repo_root / "tests" / "test_results.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def validate_environment():
    """Validate that the environment is ready for testing."""
    logger.info("=" * 70)
    logger.info("ENVIRONMENT VALIDATION")
    logger.info("=" * 70)

    errors = []
    warnings = []

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    logger.info(f"Python version: {py_version}")
    if sys.version_info < (3, 8):
        errors.append(f"Python 3.8+ required, found {py_version}")

    # Check basic repo structure
    required_dirs = [
        'kinetic_devops',
        'tests',
        'scripts'
    ]
    for dir_name in required_dirs:
        dir_path = _repo_root / dir_name
        if dir_path.exists():
            logger.info(f"[OK] Directory: {dir_name}")
        else:
            errors.append(f"Missing directory: {dir_name}")

    # Check a few key files that the tests expect
    required_files = [
        'kinetic_devops/__init__.py',
        'kinetic_devops/auth.py',
        'kinetic_devops/base_client.py',
        'kinetic_devops/baq.py',
        'kinetic_devops/boreader.py',
        'kinetic_devops/file_service.py',
    ]
    for file_name in required_files:
        if (_repo_root / file_name).exists():
            logger.info(f"[OK] File: {file_name}")
        else:
            errors.append(f"Missing file: {file_name}")

    # Try importing the SDK package
    try:
        import kinetic_devops  # noqa: F401
        logger.info(f"[OK] kinetic_devops module imports successfully")
    except Exception as e:
        errors.append(f"Cannot import kinetic_devops: {e}")

    # Optional dependency check
    try:
        import requests  # noqa: F401
        logger.info(f"[OK] requests module available")
    except Exception:
        warnings.append("requests module not found (may be needed at runtime)")

    logger.info("")

    if errors:
        logger.error(f"[FAIL] {len(errors)} environment error(s) found:")
        for error in errors:
            logger.error(f"  - {error}")
        return False

    if warnings:
        logger.warning(f"[WARN] {len(warnings)} environment warning(s):")
        for warning in warnings:
            logger.warning(f"  - {warning}")

    if not errors:
        logger.info("[PASS] Environment validation successful")

    logger.info("=" * 70)
    logger.info("")
    return not bool(errors)


def run_tests(validate_only: bool = False):
    """Discover and run tests under `tests/` and log results."""
    if not validate_environment():
        logger.error("[FATAL] Environment validation failed. Aborting tests.")
        return 1

    if validate_only:
        return 0

    logger.info("=" * 70)
    logger.info("KINETIC SDK TEST SUITE")
    logger.info("=" * 70)

    loader = unittest.TestLoader()
    suite = loader.discover(str(_repo_root / "tests"), pattern='test_*.py')

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    logger.info("=" * 70)
    logger.info(f"Tests run: {result.testsRun}")
    logger.info(f"Failures: {len(result.failures)}")
    logger.info(f"Errors: {len(result.errors)}")
    logger.info(f"Skipped: {len(result.skipped)}")

    if result.testsRun == 0:
        logger.error("[FAIL] ZERO TESTS DISCOVERED")
        logger.info("=" * 70)
        return 1

    if result.wasSuccessful():
        logger.info("[PASS] ALL TESTS PASSED")
        logger.info("=" * 70)
        return 0
    else:
        logger.error("[FAIL] SOME TESTS FAILED")
        logger.info("=" * 70)
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Kinetic SDK Test Runner")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run environment validation only"
    )
    args = parser.parse_args()
    sys.exit(run_tests(validate_only=args.validate))
