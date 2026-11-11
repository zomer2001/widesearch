"""Single bounded Crawl4AI read; no LLM extraction or follow-up crawling."""
import argparse
import asyncio
from pathlib import Path

from widesearch_experiment.common import atomic_json, safe_error


async def read(url, output, timeout):
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    result = {"title": "", "content": "", "status": 0, "error": "", "backend": "crawl4ai"}
    try:
        async with asyncio.timeout(timeout):
            async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False, light_mode=True)) as crawler:
                container = await crawler.arun(url=url, config=CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS, page_timeout=max(1000, int(timeout * 1000)),
                    wait_until="domcontentloaded", delay_before_return_html=0.5,
                    exclude_all_images=True, remove_overlay_elements=True, verbose=False))
                page = container._results[0] if getattr(container, "_results", None) else container
                markdown = getattr(page, "markdown", None)
                content = getattr(markdown, "raw_markdown", "") or (markdown if isinstance(markdown, str) else "")
                result.update(title=(getattr(page, "metadata", {}) or {}).get("title", ""),
                              content=content if getattr(page, "success", False) else "",
                              status=getattr(page, "status_code", 0) or 0,
                              resolved_url=getattr(page, "redirected_url", None) or url,
                              error="" if getattr(page, "success", False) and content.strip() else "browser_read_failed")
                if getattr(page, "html", ""):
                    output.with_suffix(".html").write_text(page.html)
    except Exception as exc:
        result["error"] = safe_error(exc)["type"]
    atomic_json(output, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(read(args.url, args.output, args.timeout))
