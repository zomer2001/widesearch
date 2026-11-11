import argparse
import asyncio
import json

from .common import ROOT, SYSTEMS, config, read_json


def main():
    p = argparse.ArgumentParser(description="Query-only WideSearch collection experiments")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("selftest")
    run = commands.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--systems", nargs="+", choices=[*SYSTEMS, "ours_off"], default=list(SYSTEMS))
    run.add_argument("--split", choices=["smoke2", "pilot10", "batch30", "full200"], default="smoke2")
    run.add_argument("--instances", nargs="+")
    run.add_argument("--trials", type=int, default=1)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--config")
    for name in ("status", "analyze"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--label", required=True)
    args = p.parse_args()
    if args.command == "prepare":
        from .prepare import prepare
        prepare()
    elif args.command == "selftest":
        from .evaluation.selftest import main as selftest
        selftest()
    elif args.command == "run":
        from .runners.batch import run_batch
        cfg = config(args.config)
        asyncio.run(run_batch(args.label, args.systems, args.split, cfg, args.trials,
                              args.concurrency or cfg["concurrency"], args.instances))
    elif args.command == "status":
        print(json.dumps(read_json(ROOT / "results" / args.label / "progress.json"), ensure_ascii=False, indent=2))
    elif args.command == "analyze":
        from .analysis.summarize import summarize
        summarize(args.label)


if __name__ == "__main__":
    main()
