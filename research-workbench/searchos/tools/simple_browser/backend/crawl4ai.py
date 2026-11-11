"""Crawl4AI browser rendering; cache complete pages, never query-filtered excerpts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from searchos.tools.simple_browser.backend.aiohttp import AiohttpBackend
from searchos.tools.simple_browser.backend.base import (
    _HEADERS,
    _MAX_HTML_BYTES,
    FetchResult,
)

logger = logging.getLogger(__name__)


# Static endpoints where Playwright is strictly slower (~2s startup vs ~200ms
# fetch) with no rendering benefit — skip straight to aiohttp.
_FAST_PATH_HOSTS: tuple[str, ...] = (
    "raw.githubusercontent.com",
    "api.github.com",
    "arxiv.org",
    "export.arxiv.org",
    "openreview.net",
)


class Crawl4aiBackend:
    """Persistent local browser with bounded concurrency and full-page output."""

    cache_namespace = "crawl4ai-full-v2"

    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        fast_path_hosts: tuple[str, ...] = _FAST_PATH_HOSTS,
    ) -> None:
        # Eager ImportError so _build_default_backend can fall back.
        import crawl4ai as _c4  # noqa: F401

        self._max_concurrency = max_concurrency
        self._fast_path_hosts = fast_path_hosts
        self._sem = asyncio.Semaphore(max_concurrency)
        self._crawler: Any = None
        self._crawler_lock = asyncio.Lock()
        self._fast_backend = AiohttpBackend()

    async def _ensure_crawler(self) -> Any:
        if self._crawler is not None:
            return self._crawler
        async with self._crawler_lock:
            if self._crawler is not None:
                return self._crawler
            from crawl4ai import AsyncWebCrawler, BrowserConfig
            crawler = AsyncWebCrawler(config=BrowserConfig(
                headless=True,
                verbose=False,
                text_mode=False,
                light_mode=True,
                avoid_css=False,
                user_agent=_HEADERS["User-Agent"],
                user_agent_mode="custom",
                extra_args=[
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-software-rasterizer",
                    "--disable-dev-shm-usage",
                ],
            ))
            try:
                await crawler.__aenter__()
            except BaseException:
                try:
                    await crawler.__aexit__(None, None, None)
                except Exception:
                    pass
                raise
            self._crawler = crawler
            logger.info("crawl4ai crawler started (max_concurrency=%d)", self._max_concurrency)
            return crawler

    async def close(self) -> None:
        if self._crawler is not None:
            try:
                await self._crawler.__aexit__(None, None, None)
            except Exception:
                logger.debug("crawl4ai close raised", exc_info=True)
            finally:
                self._crawler = None
        await self._fast_backend.close()

    def _is_fast_path(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(host == h or host.endswith("." + h) for h in self._fast_path_hosts)

    async def fetch(
        self, url: str, *, query: str = "", timeout: float = 20.0,
    ) -> FetchResult:
        if self._is_fast_path(url):
            return await self._fast_backend.fetch(url, query=query, timeout=timeout)
        async with self._sem:
            return await self._fetch_via_crawler(url, query=query, timeout=timeout)

    async def fetch_html(self, url: str, *, timeout: float = 60.0) -> str:
        """Raw DOM HTML via the crawl4ai pipeline."""
        async with self._sem:
            r = await self._fetch_via_crawler(url, query="", timeout=timeout)
        return r.html or ""

    async def fetch_many(
        self, urls: list[str], *, query: str = "", timeout: float = 20.0,
    ) -> list[FetchResult]:
        # Each input owns its result, including repeated and redirected URLs.
        return await asyncio.gather(*(
            self.fetch(url, query=query, timeout=timeout) for url in urls
        ))

    async def _fetch_via_crawler(
        self, url: str, *, query: str, timeout: float,
    ) -> FetchResult:
        try:
            async with asyncio.timeout(timeout):
                crawler = await self._ensure_crawler()
                container = await crawler.arun(url=url, config=self._run_config(query, timeout))
                for result in _iter_container(container):
                    return self._to_result(result, fallback_url=url)
                return FetchResult(url=url, status=0, error="浏览器未返回内容")
        except TimeoutError:
            return FetchResult(url=url, status=-1, error="浏览器读取超时",
                               metadata={"fetch_backend": "crawl4ai"})
        except Exception as exc:
            logger.warning("crawl4ai fetch failed (%s)", type(exc).__name__)
            reason = ("本机浏览器尚未安装完成" if "Executable doesn't exist" in str(exc)
                      else f"浏览器读取失败（{type(exc).__name__}）")
            return FetchResult(url=url, status=0, error=reason,
                               metadata={"fetch_backend": "crawl4ai"})

    def _run_config(self, query: str, timeout: float) -> Any:
        from crawl4ai import CrawlerRunConfig, CacheMode
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        return CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=int(timeout * 1000),
            wait_until="networkidle",
            markdown_generator=DefaultMarkdownGenerator(),
            exclude_all_images=True,
            remove_overlay_elements=True,
            delay_before_return_html=0.3,
        )

    def _to_result(self, r: Any, *, fallback_url: str) -> FetchResult:
        if r is None:
            return FetchResult(url=fallback_url, status=0, error="null result")
        url = getattr(r, "redirected_url", None) or getattr(r, "url", None) or fallback_url
        success = getattr(r, "success", True)
        raw_status = int(getattr(r, "status_code", 200) or 200)

        if not success:
            err = getattr(r, "error_message", "") or "crawl failed"
            return FetchResult(url=url, status=raw_status or 0, error=err,
                               metadata={"fetch_backend": "crawl4ai"})

        # Treat any 3xx that resolved into real content as 200 — Baidu Baike
        # serves 302→HTML and consumers want ok=True.
        status = 200 if (200 <= raw_status < 400) else raw_status

        # Preserve full Markdown so a URL cache is independent of the question.
        md_obj = getattr(r, "markdown", "") or ""
        if hasattr(md_obj, "raw_markdown"):
            md = md_obj.raw_markdown or ""
        else:
            md = str(md_obj or "")

        meta = getattr(r, "metadata", None) or {}
        title = meta.get("title", "") if isinstance(meta, dict) else ""

        links_raw = getattr(r, "links", {}) or {}
        internal = links_raw.get("internal", []) if isinstance(links_raw, dict) else []
        external = links_raw.get("external", []) if isinstance(links_raw, dict) else []
        links: dict[str, str] = {}
        for i, item in enumerate(list(internal)[:50] + list(external)[:30]):
            href = item.get("href", "") if isinstance(item, dict) else str(item)
            if href:
                links[f"L{i}"] = href

        html = getattr(r, "html", "") or getattr(r, "cleaned_html", "") or ""
        if len(html) > _MAX_HTML_BYTES:
            html = html[:_MAX_HTML_BYTES]

        return FetchResult(
            url=url, title=title, markdown=md, html=html,
            links=links, status=status, metadata={"fetch_backend": "crawl4ai"},
        )


def _iter_container(container: Any) -> Any:
    """crawl4ai's CrawlResultContainer is iterable for arun_many() (N items)
    but not always for arun() (1 item) — tolerate both."""
    if hasattr(container, "success"):
        return [container]
    try:
        iter(container)
        return container
    except TypeError:
        return [container]
