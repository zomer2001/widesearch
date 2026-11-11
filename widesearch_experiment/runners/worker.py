import argparse
import asyncio
import importlib
import os
import time
from pathlib import Path

from ..common import atomic_json, now, read_json, safe_error, validate_task
from ..conversion import convert
from ..services import Journal, Services


async def execute(request, directory):
    task, cfg, system = request["task"], request["config"], request["system"]
    validate_task(task)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chdir(directory)
    # No user workbench state, session cache or inherited model/search settings.
    for key in list(os.environ):
        if key.startswith("SF_"):
            del os.environ[key]
    os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(directory / "crawl4ai_cache")
    journal = Journal(directory)
    services = Services(cfg, journal)
    start = time.monotonic()
    record = {"instance_id": task["instance_id"], "language": task["language"], "system": system,
              "trial": request["trial"], "attempt": request["attempt"], "started_at": now(),
              "response": "", "status": "running", "config_fingerprint": request["config_fingerprint"]}
    atomic_json(directory / "result.json", record)
    try:
        name = "ours" if system == "ours_off" else system
        adapter = importlib.import_module(f"widesearch_experiment.adapters.{name}_adapter")
        kwargs = {"adaptive": False} if system == "ours_off" else {}
        output = await asyncio.wait_for(adapter.run(task, cfg, services, directory, **kwargs),
                                        timeout=cfg["research_timeout_seconds"] + 45)
        record.update(output)
    except (TimeoutError, asyncio.TimeoutError):
        record["status"] = "timeout"
    except Exception as exc:
        record.update(status="failed", error=safe_error(exc))
        journal.emit("execution_error", **safe_error(exc))
        # Local debug trace with known credentials removed; not sent anywhere.
        import traceback
        trace = traceback.format_exc()
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if os.environ.get(key):
                trace = trace.replace(os.environ[key], "[REDACTED]")
        (directory / "error.log").write_text(trace)
    record.update(elapsed_seconds=round(time.monotonic() - start, 3), **journal.counts())
    atomic_json(directory / "result.json", record)
    try:
        conversion = await asyncio.wait_for(convert(task["query"], record["response"], directory / "prediction", services),
                                            timeout=cfg["conversion_timeout_seconds"])
        record["conversion"] = conversion
        if conversion["status"] != "ok" and record["status"] == "success":
            record["status"] = "parse_error"
    except Exception as exc:
        record["conversion"] = {"status": "conversion_error", "error": safe_error(exc)}
        record["status"] = "conversion_error" if record["status"] == "success" else record["status"]
        (directory / "prediction").mkdir(exist_ok=True)
        (directory / "prediction/prediction.md").write_text("")
    record.update(ended_at=now(), total_elapsed_seconds=round(time.monotonic() - start, 3))
    atomic_json(directory / "result.json", record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    asyncio.run(execute(read_json(args.request), args.directory))
