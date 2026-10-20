"""Shared CLI entrypoint matrix for regression tests."""

import os

_TRUE = {"1", "true", "yes", "on"}


def native_checks_enabled() -> bool:
    """Whether to run checks that execute or load newly built/installed native code:
    the console-script .exe launchers uv writes, and native extension libraries
    such as cryptography's (encrypted vault). On managed machines, enterprise endpoint security
    may block such newly built, unsigned files, so local runs
    defer these checks by default. They run in CI (CI=true), in Windows Sandbox
    (scripts/sandbox_test.py), or when asked for: `tests.test_runner --native`
    or KINETIC_NATIVE_CHECKS=1. KINETIC_SKIP_CONSOLE_SCRIPT_TESTS=1 always skips."""
    def flag(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in _TRUE

    if flag("KINETIC_SKIP_CONSOLE_SCRIPT_TESTS"):
        return False
    return flag("KINETIC_NATIVE_CHECKS") or flag("CI")


def scaled_timeout(seconds: float) -> float:
    """Subprocess timeout for CLI --help tests, multiplied by the optional
    KINETIC_TEST_TIMEOUT_SCALE (e.g. 3) for slow or heavily monitored
    machines. Unset or invalid means no change."""
    try:
        scale = float(os.environ.get("KINETIC_TEST_TIMEOUT_SCALE", "") or 1)
    except ValueError:
        scale = 1.0
    return seconds * max(scale, 1.0)

CLI_MODULES = [
    {"module": "access_scope", "script": "access_scope.py", "router": "scope"},
    {"module": "analysis", "script": "analysis.py", "router": "analysis"},
    {"module": "auth", "script": "auth.py", "router": "auth"},
    {"module": "baq", "script": "baq.py", "router": "baq"},
    {"module": "boreader", "script": "boreader.py", "router": None},
    {"module": "efx", "script": "efx.py", "router": "efx"},
    {"module": "efx_library", "script": "efx_library.py", "router": None},
    {"module": "export_all", "script": "export_all.py", "router": "export"},
    {"module": "file_service", "script": "file_service.py", "router": None},
    {"module": "find_sensitive_data", "script": "find_sensitive_data.py", "router": "find"},
    {"module": "metafx", "script": "metafx.py", "router": "meta"},
    {"module": "report_service", "script": "report_service.py", "router": "report"},
    {"module": "repo_maker", "script": "repo_maker.py", "router": "repomaker"},
    {"module": "solutions", "script": "solutions.py", "router": "solutions"},
    {"module": "trim_trace_paths", "script": "trim_trace_paths.py", "router": None},
    {"module": "zdatatable", "script": "zdatatable.py", "router": "zdatatable"},
]

ROUTER_TOOLS = [item for item in CLI_MODULES if item["router"]]
