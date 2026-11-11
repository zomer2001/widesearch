import argparse
import asyncio
import json

from widesearch_experiment.common import ROOT, config, read_json
from .launcher import DEFAULT_CONFIG, run_batch


def main():
    parser = argparse.ArgumentParser(description="ODR Lite: bounded collection; evaluation is opt-in")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--instances", nargs="+", required=True)
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    run.add_argument("--evaluate", action="store_true", help="Also run the unchanged official judge (extra API cost)")
    for name in ("status", "analyze"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.command == "run":
        cfg = {**config(args.config), "odr_evaluate": args.evaluate}
        asyncio.run(run_batch(args.label, args.instances, cfg))
    elif args.command == "status":
        print(json.dumps(read_json(ROOT / "results" / args.label / "progress.json"), ensure_ascii=False, indent=2))
    else:
        from widesearch_experiment.analysis.summarize import summarize
        summarize(args.label)


if __name__ == "__main__":
    main()
