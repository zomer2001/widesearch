import argparse
import asyncio
import json

from widesearch_experiment.common import ROOT, SYSTEMS, config, read_json
from .launcher import DEFAULT_CONFIG, run_batch


def main():
    parser = argparse.ArgumentParser(description="DeepSeek baselines; only 'run' executes questions and judging")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--systems", nargs="+", choices=[*SYSTEMS, "ours_off"],
                     default=["open_deep_research", "mindsearch"])
    selection = run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--instances", nargs="+")
    selection.add_argument("--split", choices=["smoke2", "pilot10", "batch30", "full200"])
    run.add_argument("--trials", type=int, default=1)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    for command in ("status", "analyze"):
        cmd = commands.add_parser(command)
        cmd.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(run_batch(args.label, args.systems, args.split, config(args.config),
                              args.trials, args.concurrency, args.instances))
    elif args.command == "status":
        print(json.dumps(read_json(ROOT / "results" / args.label / "progress.json"), ensure_ascii=False, indent=2))
    else:
        from widesearch_experiment.analysis.summarize import summarize
        summarize(args.label)


if __name__ == "__main__":
    main()
