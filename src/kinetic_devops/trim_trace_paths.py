"""Compatibility shim for src-layout migration.

Canonical implementation lives in kinetic_devops.trim_trace_paths.
"""

from kinetic_devops.trim_trace_paths import *  # noqa: F401,F403


if __name__ == "__main__":
    from kinetic_devops.trim_trace_paths import main

    raise SystemExit(main())
