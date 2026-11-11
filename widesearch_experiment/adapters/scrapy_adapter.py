import asyncio
import os
import sys

from ..common import WORKSPACE, atomic_json, read_json
from .crawler_common import extract


async def run(task, cfg, services, workdir):
    # One deterministic original-query search, then fixed same-domain BFS.
    hits = await services.search(task["query"])
    request = {"urls": [h["url"] for h in hits], "max_pages": cfg["max_pages"], "max_depth": cfg["max_depth"]}
    atomic_json(workdir / "crawl_input.json", request)
    env = {**os.environ, "PYTHONPATH": str(WORKSPACE)}
    with (workdir / "scrapy.log").open("w") as log:
        child = await asyncio.create_subprocess_exec(sys.executable, "-m", "widesearch_experiment.adapters.scrapy_crawl",
                 str(workdir / "crawl_input.json"), str(workdir / "crawl_output.json"),
                 env=env, stdout=log, stderr=log)
        try:
            code = await child.wait()
        finally:
            if child.returncode is None:
                child.terminate()
                await child.wait()
    if code:
        raise RuntimeError("Scrapy child failed; inspect scrapy.log")
    collected = read_json(workdir / "crawl_output.json")
    for visit in collected["visits"]:
        services.journal.emit("page_end", **visit)
    for page in collected["pages"]:
        services.save_page(page["url"], page["content"])
    services.journal.emit("adapter_config", planner="none", strategy="fixed_query_then_same_domain_bfs",
                          extractor="official Crawl4AI LLMExtractionStrategy shared with B2")
    return await extract(task, collected["pages"], cfg, services, workdir)
