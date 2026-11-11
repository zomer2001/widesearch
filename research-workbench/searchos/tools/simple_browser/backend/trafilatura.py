"""Local article extraction with the downloaded HTML retained for provenance."""
from __future__ import annotations

import asyncio

from searchos.tools.simple_browser.backend.aiohttp import AiohttpBackend
from searchos.tools.simple_browser.backend.base import FetchResult


class TrafilaturaBackend:
    cache_namespace = "trafilatura-article-v1"

    def __init__(self):
        import trafilatura  # noqa: F401
        self._http = AiohttpBackend()

    async def fetch(self, url: str, *, query: str = "", timeout: float = 20.0) -> FetchResult:
        result = await self._http.fetch(url, timeout=timeout)
        if not result.ok:
            return result
        return await asyncio.to_thread(extract_article, result)

    async def fetch_many(self, urls, *, query="", timeout=20.0):
        return await asyncio.gather(*(self.fetch(url, timeout=timeout) for url in urls))

    async def close(self):
        await self._http.close()


def extract_article(result: FetchResult) -> FetchResult:
    from trafilatura import extract
    text = extract(result.html, url=result.url, output_format="markdown",
                   include_links=True, include_tables=True, include_comments=False)
    if text and text.strip():
        result.markdown = text
        result.metadata = {**result.metadata, "fetch_backend": "trafilatura"}
    else:
        result.metadata = {**result.metadata, "fallback_reason": "未识别到正文，保留网页文本"}
    return result
