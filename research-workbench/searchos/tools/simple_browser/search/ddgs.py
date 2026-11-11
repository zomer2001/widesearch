"""Keyless public-engine search. Snippets remain discovery hints, not page evidence."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from searchos.tools.simple_browser.search.base import SearchProvider, SearchResult


class DDGSProvider(SearchProvider):
    def __init__(self):
        self._limit = asyncio.Semaphore(2)

    @property
    def name(self) -> str:
        return "ddgs"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if not query.strip():
            return []
        count = min(20, max(1, max_results))
        async with self._limit:
            async with asyncio.timeout(25):
                rows = await asyncio.to_thread(self._search, query, count)
        results, seen = [], set()
        for row in rows:
            url = str(row.get("href") or row.get("url") or "").strip()
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or url in seen):
                continue
            seen.add(url)
            results.append(SearchResult(title=str(row.get("title") or ""), url=url,
                                        snippet=str(row.get("body") or "")))
            if len(results) == count:
                break
        return results

    @staticmethod
    def _search(query: str, count: int) -> list[dict]:
        from ddgs import DDGS
        with DDGS(timeout=15, verify=True) as client:
            return client.text(query, max_results=count, backend="auto")
