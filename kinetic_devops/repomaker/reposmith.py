"""RepoSmith smoke entrypoint.

RepoSmith is the validation part of RepoMaker: it runs the end-to-end
branch protection smoke lifecycle against a temporary repository.
"""

from __future__ import annotations

from typing import List, Optional

from kinetic_devops import repo_maker


def main(argv: Optional[List[str]] = None) -> int:
    return repo_maker.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
