#!/usr/bin/env python
"""Thin wrapper for kinetic_devops.find_sensitive_data.

Keeps legacy script path stable while delegating to the package implementation.
"""

from kinetic_devops.find_sensitive_data import main


if __name__ == "__main__":
    main()
