"""Local fetching, cache provenance and no-key preview regressions."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from api import workbench
from api.routes.workbench import CrawlRequest, crawl_page
from searchos.tools.simple_browser.backend.base import BrowserService, FetchResult
from searchos.tools.simple_browser.cache import DiskFetchCache


def test_article_extraction_retains_source_and_removes_navigation():
    pytest.importorskip("trafilatura")
    from searchos.tools.simple_browser.backend.trafilatura import extract_article
    body = "资料采集系统需要保留真实来源、完整正文和抓取时间，以便核验引用。" * 25
    html = f"<html><head><title>采集测试</title></head><body><nav>登录 注册 帮助</nav><article><h1>采集测试</h1><p>{body}</p></article></body></html>"
    result = extract_article(FetchResult(url="https://example.com/a", html=html))
    assert body in result.markdown
    assert "登录 注册 帮助" not in result.markdown
    assert result.html == html
    assert result.metadata["fetch_backend"] == "trafilatura"


@pytest.mark.asyncio
async def test_auto_renders_short_page_and_reports_actual_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace())
    from searchos.tools.simple_browser.backend.automatic import AutomaticBackend
    backend = AutomaticBackend()
    class Light:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="Loading")
    class Browser:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="动态页面正文" * 40,
                               metadata={"fetch_backend": "crawl4ai"})
    backend._light, backend._browser = Light(), Browser()
    result = await backend.fetch("https://example.com")
    assert result.metadata["requested_backend"] == "auto"
    assert result.metadata["fetch_backend"] == "crawl4ai"
    assert result.metadata["fallback_reason"]
    assert "动态页面正文" in result.markdown


@pytest.mark.asyncio
async def test_auto_retains_usable_light_content_when_browser_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace())
    from searchos.tools.simple_browser.backend.automatic import AutomaticBackend
    backend = AutomaticBackend()
    class Light:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="真实短公告", metadata={"fetch_backend": "trafilatura"})
    class Browser:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", status=-1, error="timeout")
    backend._light, backend._browser = Light(), Browser()
    result = await backend.fetch("https://example.com")
    assert result.markdown == "真实短公告"
    assert result.metadata["fetch_backend"] == "trafilatura"
    assert result.metadata["browser_error"] == "timeout"


def test_cache_keeps_fetch_metadata_and_isolates_modes(tmp_path):
    first = DiskFetchCache(tmp_path, namespace="trafilatura")
    second = DiskFetchCache(tmp_path, namespace="crawl4ai")
    result = FetchResult(url="https://example.com", markdown="body", html="<p>body</p>",
                         metadata={"fetch_backend": "trafilatura", "fetched_at": "original-time"})
    first.put(result)
    assert first.peek(result.url).metadata == result.metadata
    assert second.peek(result.url) is None


@pytest.mark.asyncio
async def test_browser_service_marks_cache_hit_without_replacing_original_time(tmp_path, monkeypatch):
    from searchos.config.settings import settings
    monkeypatch.setattr(settings, "browser_disk_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "browser_disk_cache_enabled", True)
    class Backend:
        cache_namespace = "test"
        calls = 0
        async def fetch(self, url, **kwargs):
            self.calls += 1
            return FetchResult(url=url, markdown="actual source")
    backend = Backend()
    service = BrowserService(backend)
    first = await service.fetch("https://example.com")
    second = await service.fetch("https://example.com")
    assert backend.calls == 1
    assert second.metadata["cache_hit"]
    assert second.metadata["fetched_at"] == first.metadata["fetched_at"]


@pytest.mark.asyncio
async def test_preview_uses_selected_backend_without_search_key(monkeypatch, tmp_path):
    import searchos.tools.simple_browser.backend.base as base
    import searchos.tools.simple_browser.image_reading as visual
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private.json")
    config = workbench.Configuration()
    workbench.save_config(config)
    checked, selected = [], []
    async def validate(url): checked.append(url)
    class Backend:
        async def fetch(self, url, **kwargs):
            return FetchResult(url=url, title="真实网页", markdown="真实网页正文", metadata={"fetch_backend": "crawl4ai"})
        async def close(self): selected.append("closed")
    def build(name):
        selected.append(name)
        return Backend()
    monkeypatch.setattr(visual, "validate_public_url", validate)
    monkeypatch.setattr(base, "build_backend", build)
    from searchos.config.settings import settings
    monkeypatch.setattr(settings, "browser_disk_cache_enabled", False)
    result = await crawl_page(CrawlRequest(url="https://example.com", backend="crawl4ai"))
    assert result["ok"] and result["preview"] == "真实网页正文"
    assert selected == ["crawl4ai", "closed"]
    assert checked == ["https://example.com", "https://example.com"]


@pytest.mark.asyncio
async def test_preview_rejects_private_address_before_building_browser(monkeypatch):
    from fastapi import HTTPException
    import searchos.tools.simple_browser.image_reading as visual
    async def reject(url): raise ValueError("不允许读取私有地址")
    monkeypatch.setattr(visual, "validate_public_url", reject)
    with pytest.raises(HTTPException) as caught:
        await crawl_page(CrawlRequest(url="http://127.0.0.1"))
    assert caught.value.status_code == 400


def test_workbench_accepts_only_real_fetch_modes():
    for name in ["auto", "crawl4ai", "trafilatura", "aiohttp", "jina"]:
        assert workbench.Search(browser_backend=name).browser_backend == name
    with pytest.raises(ValueError):
        workbench.Search(browser_backend="search_engine")

@pytest.mark.asyncio
async def test_loading_placeholder_is_not_a_success_when_rendering_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace())
    from searchos.tools.simple_browser.backend.automatic import AutomaticBackend
    backend = AutomaticBackend()
    class Light:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="Loading...")
    class Browser:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", status=-1, error="timeout")
    backend._light, backend._browser = Light(), Browser()
    result = await backend.fetch("https://example.com")
    assert not result.ok and result.error == "timeout"


@pytest.mark.asyncio
async def test_long_javascript_fallback_still_uses_browser(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace())
    from searchos.tools.simple_browser.backend.automatic import AutomaticBackend
    backend = AutomaticBackend()
    class Light:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="interactive scripts did not run " + "fallback " * 80,
                               metadata={"fetch_backend": "trafilatura"})
    class Browser:
        async def fetch(self, *args, **kwargs):
            return FetchResult(url="https://example.com", markdown="真实动态正文" * 40,
                               metadata={"fetch_backend": "crawl4ai"})
    backend._light, backend._browser = Light(), Browser()
    result = await backend.fetch("https://example.com")
    assert result.metadata["fetch_backend"] == "crawl4ai"
    assert "脚本" in result.metadata["fallback_reason"]
