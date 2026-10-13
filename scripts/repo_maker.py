#!/usr/bin/env python
"""RepoMaker script entrypoint.

Primary executable for unified GitHub/Forgejo branch-protection smoke flow.
"""

from __future__ import annotations

from kinetic_devops import repo_maker


if __name__ == "__main__":
    raise SystemExit(repo_maker.main())
