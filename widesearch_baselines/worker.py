"""Adapter substitution is confined to a new worker process."""
import argparse
import asyncio
import importlib
import sys

from widesearch_experiment.common import read_json
from .launcher import validate_config


def install_adapter(system):
    if system in {"open_deep_research", "mindsearch"}:
        module = importlib.import_module(f"widesearch_baselines.{system}")
        sys.modules[f"widesearch_experiment.adapters.{system}_adapter"] = module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    request = read_json(args.request)
    validate_config(request["config"])
    install_adapter(request["system"])
    from widesearch_experiment.runners.worker import execute
    asyncio.run(execute(request, args.directory))


if __name__ == "__main__":
    main()
