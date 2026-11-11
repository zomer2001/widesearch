#!/usr/bin/env python3
"""Local entry point for packaging and reviewing Workbuddy extraction results."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "web")]

from api.knowledge_handoff.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
