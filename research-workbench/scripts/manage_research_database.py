#!/usr/bin/env python3
"""Initialize, verify or document the independent seven-dimension database."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from searchos.knowledge_store.database import (  # noqa: E402
    DEFAULT_CATALOG,
    DEFAULT_DATABASE,
    DEFAULT_EXPORT,
    ResearchDatabase,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "check", "export"])
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT)
    args = parser.parse_args()
    store = ResearchDatabase(args.database)
    report = store.initialize(args.catalog) if args.action == "init" else store.verify()
    if args.action in {"init", "export"}:
        store.export(args.export_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
