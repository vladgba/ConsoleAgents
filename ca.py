#!/usr/bin/env python3
"""Launcher: `python ca.py ...` from anywhere (or put this folder on PATH and use `ca`)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from consoleagents.cli import main  # noqa: E402
except ImportError:
    from autoagents.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
