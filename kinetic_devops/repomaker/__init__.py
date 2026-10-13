"""RepoMaker tool family.

This package provides modular entrypoints:
- apply: config-driven branch protection rollout for existing repositories
- reposmith: branch-protection smoke lifecycle validation
"""

from kinetic_devops.repomaker.apply import main as apply_main
from kinetic_devops.repomaker.reposmith import main as reposmith_main

__all__ = ["apply_main", "reposmith_main"]
