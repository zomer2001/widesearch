"""Prefer local article extraction; render short/failed pages in a browser."""
from __future__ import annotations

import asyncio
import re

from searchos.tools.simple_browser.backend.base import FetchResult
from searchos.tools.simple_browser.backend.trafilatura import TrafilaturaBackend


class AutomaticBackend:
    cache_namespace = "auto-trafilatura-crawl4ai-v1"

    def __init__(self):
        self._light = TrafilaturaBackend()
        self._browser = None

    async def fetch(self, url: str, *, query: str = "", timeout: float = 20.0) -> FetchResult:
        started = asyncio.get_running_loop().time()
        light = await self._light.fetch(url, timeout=max(1, timeout * .4))
        script_notice = re.search(
            r"(?:enable javascript|javascript (?:is )?(?:required|disabled)|interactive scripts did not run)",
            light.markdown, re.IGNORECASE,
        )
        if (light.ok and len(light.markdown.strip()) >= 200
                and light.metadata.get("fetch_backend") == "trafilatura" and not script_notice):
            light.metadata["requested_backend"] = "auto"
            return light
        reason = ("网页提示脚本未加载，尝试浏览器渲染" if script_notice else
                  "网页文本较短，尝试浏览器渲染" if light.ok else f"直接读取失败（{light.status}）")
        try:
            if self._browser is None:
                from searchos.tools.simple_browser.backend.crawl4ai import Crawl4aiBackend
                self._browser = Crawl4aiBackend()
            remaining = max(1, timeout - (asyncio.get_running_loop().time() - started))
            rendered = await self._browser.fetch(url, timeout=remaining)
        except Exception as exc:
            rendered = FetchResult(url=url, status=0, error=type(exc).__name__)
        if (rendered.ok and rendered.markdown.strip()
                and (not light.ok or script_notice or len(rendered.markdown.strip()) >= len(light.markdown.strip()))):
            rendered.metadata.update(requested_backend="auto", fallback_reason=reason)
            return rendered
        placeholder = not light.markdown.strip() or re.fullmatch(
            r"(?:loading[. …]*|加载中[.。 …]*|please enable javascript[. …]*)",
            light.markdown.strip(), re.IGNORECASE,
        )
        if light.ok and placeholder:
            return FetchResult(url=light.url, status=0, error=rendered.error or "网页仅返回加载提示",
                               metadata={"requested_backend": "auto", "fallback_reason": reason})
        light.metadata.update(requested_backend="auto", fallback_reason=reason,
                              browser_error=rendered.error or "渲染未增加可读正文")
        return light

    async def fetch_many(self, urls, *, query="", timeout=20.0):
        return await asyncio.gather(*(self.fetch(url, timeout=timeout) for url in urls))

    async def close(self):
        await self._light.close()
        if self._browser is not None:
            await self._browser.close()
