import sys

from ..common import ROOT, atomic_json
from .crawler_common import extract


async def run(task, cfg, services, workdir):
    sys.path.insert(0, str(ROOT / "vendor/crawl4ai"))
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode
    from crawl4ai.adaptive_crawler import AdaptiveCrawler, AdaptiveConfig
    hits = await services.search(task["query"])
    pages = {}
    attempts = 0
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        original = crawler.arun

        async def observed(url, config=None, **kwargs):
            nonlocal attempts
            if attempts >= cfg["max_pages"]:
                raise RuntimeError("Configured page bound reached")
            attempts += 1
            services.journal.emit("page_start", url=url)
            if config:
                config = config.clone(cache_mode=CacheMode.BYPASS, page_timeout=30000, verbose=False)
            result = await original(url=url, config=config, **kwargs)
            r = result._results[0] if getattr(result, "_results", None) else result
            text = getattr(getattr(r, "markdown", None), "raw_markdown", "") or ""
            success = bool(getattr(r, "success", False)) and bool(text)
            services.journal.emit("page_end", url=url, success=success, status=getattr(r, "status_code", None))
            if success:
                pages[url] = {"url": url, "content": text}
                services.save_page(url, text)
            return result

        crawler.arun = observed
        for hit in hits:
            if attempts >= cfg["max_pages"]:
                break
            adaptive = AdaptiveCrawler(crawler=crawler, config=AdaptiveConfig(
                strategy="statistical", max_pages=max(1, cfg["max_pages"] - attempts), max_depth=cfg["max_depth"]))
            state = await adaptive.digest(start_url=hit["url"], query=task["query"])
            services.journal.emit("adaptive_crawl_state", seed=hit["url"], metrics=state.metrics,
                                  crawl_order=state.crawl_order)
    atomic_json(workdir / "crawl_output.json", list(pages.values()))
    services.journal.emit("adapter_config", strategy="official AdaptiveCrawler statistical",
                          cache="BYPASS", preview_requests="not counted as full page reads")
    return await extract(task, list(pages.values()), cfg, services, workdir)
