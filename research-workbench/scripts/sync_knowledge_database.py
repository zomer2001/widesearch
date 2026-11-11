#!/usr/bin/env python3
"""Import evidence-bound saved knowledge into the local entity database, offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "web")]

from api.knowledge_database import DEFAULT_DATABASE, KnowledgeDatabase  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "knowledge_base")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--reader-root", type=Path)
    args = parser.parse_args(argv)
    result = KnowledgeDatabase(args.data_root, args.database).sync(args.reader_root)
    print(
        json.dumps(
            {"database": str(args.database), "counts": result["counts"], "sync": result["sync"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
