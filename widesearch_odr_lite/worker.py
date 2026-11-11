"""Inject the Lite adapter only in its own worker process."""
import argparse
import asyncio
import sys

from widesearch_experiment.common import read_json
from . import SYSTEM
from .launcher import validate_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    request = read_json(args.request)
    validate_config(request["config"])
    if request["system"] != SYSTEM:
        raise ValueError("This worker only runs Open Deep Research Lite")
    from widesearch_experiment import services
    from widesearch_fetch_backup.services import BackupServices
    services.Services = BackupServices
    from . import adapter
    sys.modules[f"widesearch_experiment.adapters.{SYSTEM}_adapter"] = adapter
    from widesearch_experiment.runners.worker import execute
    asyncio.run(execute(request, args.directory))


if __name__ == "__main__":
    main()
