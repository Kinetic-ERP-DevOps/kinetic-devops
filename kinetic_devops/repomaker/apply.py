"""RepoMaker apply entrypoint."""

from __future__ import annotations

from typing import List, Optional

from kinetic_devops.repomaker import apply_engine


def _load_apply_script_module():
    """Compatibility shim retained for existing tests and callers."""
    return apply_engine


def main(argv: Optional[List[str]] = None) -> int:
    module = _load_apply_script_module()
    return module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
