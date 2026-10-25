#!/usr/bin/env python3
"""Legacy launcher alias: forwards to ca.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from consoleagents.cli import main  # noqa: E402
except ImportError:
    from autoagents.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
