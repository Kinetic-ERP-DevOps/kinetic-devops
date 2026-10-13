"""
tests/README.md - Kinetic SDK Test Suite Documentation
"""

# Kinetic SDK Test Suite

## Quick Start

Run all tests with validation:
```bash
uv run python -m tests.test_runner
# or
uv run python tests/test_runner.py
# or
uv run python -m kinetic_devops.cli.test_runner
```

Run specific test module:
```bash
uv run python -m unittest tests.test_imports
uv run python -m unittest tests.test_cli
uv run python -m unittest tests.test_redaction
uv run python -m unittest tests.test_base_client_redaction
```

Run with verbose output:
```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

Run canonical pytest gate (configured in pyproject):
```bash
uv run python -m pytest -q
```

## Environment Validation

Quick validation of SDK environment:
```bash
uv run python scripts/validate.py
```

## Test Runner

The canonical test runner is `tests/test_runner.py`, which:
- Validates the environment (Python version, required directories/files, SDK imports).
- Discovers and runs all tests under `tests/test_*.py`.
- Logs results to `tests/test_results.log`.
- Returns exit code 0 on success, non-zero on failure (useful for CI/CD).

The CLI module `python -m kinetic_devops.cli.test_runner` runs the same test discovery and result reporting flow.

## Test Results

All test results are logged to `tests/test_results.log` for CI/CD integration.

## What's Tested

### test_imports.py
- Service class imports (KineticBAQService, KineticBOReaderService, etc.)
- Service method availability
- Base client and config manager availability

### test_cli.py
- CLI help output for each service module
- Argument parser functionality

### test_redaction.py
- Heuristic redaction behavior for sensitive keys
- Escaped nested JSON redaction paths
- Whitespace and formatting variation handling

### test_base_client_redaction.py
- `KineticCore.log_wire()` zero-trust output redaction
- `KineticBaseClient.execute_request()` success and failure wire-log behavior
- Runtime identity and URL sanitization in error paths

### sdk_kinetic/test_basic.py
- Basic package smoke test for importability

## Pre-commit Hook

To enable pre-commit testing:

**Windows (PowerShell):**
```powershell
./scripts/install-hook.ps1
```

**Linux/macOS:**
```bash
chmod +x scripts/hooks/pre-commit
git config core.hooksPath scripts/hooks
```

The pre-commit hook will automatically run the sensitive-data gate and test suite before allowing commits.

## Environment Validation

The test runner includes automatic environment validation that checks:
- Python version (3.8+)
- Required directories (kinetic_devops, tests, scripts)
- Required files (all service modules)
- Module imports
- Required dependencies (requests)

## Test Coverage

Run `uv run python -m pytest -q` for the current pass/fail count -- this file doesn't keep a static
table of test counts since it goes stale the moment a test is added (this one has, repeatedly).

### Module/Script/CLI Coverage Matrices

Three files enforce an **exact-match** registry between declared modules and what's actually on disk,
so nothing new ships without at least a smoke test:

- `tests/test_module_coverage_matrix.py` -- `PACKAGE_MODULE_MATRIX` (every `kinetic_devops/*.py`) and
  `SCRIPT_MODULE_MATRIX` (every `scripts/*.py`)
- `tests/cli_matrix.py` / `test_cli_matrix_consistency.py` -- `CLI_MODULES` (every directly
  CLI-runnable module) and the router-tool mapping in `kinetic_devops/__main__.py`

If you add or remove a module, script, or CLI entrypoint, update the matching registry -- see
[CONTRIBUTING.md](../CONTRIBUTING.md) for the full table of what maps to what.

## CI Integration

Test results are logged to `tests/test_results.log` and can be parsed by CI systems.

Example GitHub Actions integration:
```yaml
- name: Run Kinetic SDK Tests
  run: python tests/test_runner.py
```
