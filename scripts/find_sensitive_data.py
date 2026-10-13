#!/usr/bin/env python
"""Thin wrapper for kinetic_devops.find_sensitive_data.

Keeps legacy script path stable while delegating to the package implementation.
"""

import os
import sys

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from kinetic_devops.find_sensitive_data import main


if __name__ == "__main__":
    main()
