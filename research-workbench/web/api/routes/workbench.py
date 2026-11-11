"""Local workbench configuration and connection diagnostics."""

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from pydantic import Field
from typing import Literal
import asyncio
import time

from api import innovation, workbench
from api.deps import sessions

router = APIRouter(prefix="/api/workbench")


@router.get("/modules")
async def get_modules():
    return {"schema_version": 1, "modules": list(innovation.MODULES)}


@router.get("/settings")
async def get_settings():
    return await workbench.public_settings()


@router.put("/settings")
async def put_settings(patch: dict = Body(...)):
    if any(item.get("status") == "running" for item in sessions.values()):
        raise HTTPException(409, "请等待当前任务结束或先停止任务，再修改连接设置")
    config = workbench.merged_config(patch)
    workbench.save_config(config)
    workbench.configure_search()
    from searchos.tools.simple_browser.backend.base import BrowserService
    previous = BrowserService._instance
    BrowserService.reset()
    if previous is not None:
        await previous.close()
    return await workbench.public_settings(config)


class ProbeRequest(BaseModel):
    engine: workbench.Engine


@router.post("/probe")
async def probe_engine(request: ProbeRequest):
    return await workbench.probe(request.engine)


class SearchProbeRequest(BaseModel):
    provider: workbench.SearchProviderName | None = None
    query: str = Field(default="Python official website", min_length=1, max_length=2000)
    api_key: str = Field(default="", repr=False, max_length=4096)
    web_search_model: str | None = Field(default=None, min_length=1, max_length=200)
    web_search_allowed_domains: list[str] | None = Field(default=None, max_length=100)


@router.post("/search-probe")
async def probe_search(request: SearchProbeRequest):
    """Test draft settings without saving; OpenAI search invokes its search model."""
    config = workbench.load_config()
    draft = config.search.model_copy(deep=True)
    if request.provider:
        draft.provider = request.provider
    if request.api_key.strip() and draft.provider in {"serper", "tavily"}:
        setattr(draft, f"{draft.provider}_api_key", request.api_key.strip())
    proposed = draft.model_dump()
    for key in ("web_search_model", "web_search_allowed_domains"):
        value = getattr(request, key)
        if value is not None:
            proposed[key] = value
    try:
        draft = workbench.Search.model_validate(proposed)
    except ValueError:
        raise HTTPException(422, "搜索模型或限定网站设置无效") from None
    provider = workbench.search_provider(draft, openai=config.openai)
    started = time.monotonic()
    try:
        async with asyncio.timeout(60 if draft.provider == "openai_web_search" else 28):
            results = await provider.search(request.query, max_results=3)
    except TimeoutError:
        raise HTTPException(504, "搜索超时，请稍后重试或切换搜索服务")
    except Exception as exc:
        raise HTTPException(503, f"搜索服务暂不可用（{type(exc).__name__}），请检查网络后重试") from None
    return {"ok": bool(results), "provider": provider.name,
            "elapsed_s": round(time.monotonic() - started, 2),
            "message": f"搜索成功，返回 {len(results)} 条网页链接" if results else "搜索已完成，但没有返回结果，请换一个关键词",
            "results": [{"title": result.title, "url": result.url, "snippet": result.snippet}
                        for result in results]}


class CrawlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    backend: Literal["auto", "aiohttp", "crawl4ai", "trafilatura", "jina"] | None = None


@router.post("/crawl")
async def crawl_page(request: CrawlRequest):
    """Read a public URL directly; no model or search service is used."""
    from searchos.tools.simple_browser.backend.base import BrowserService, build_backend
    from searchos.tools.simple_browser.image_reading import validate_public_url

    backend = None
    started = time.monotonic()
    try:
        await validate_public_url(request.url)
        name = request.backend or workbench.load_config().search.browser_backend
        backend = build_backend(name)
        service = BrowserService(backend)
        async with asyncio.timeout(65):
            result = await service.fetch(request.url, timeout=60)
        if result.url:
            await validate_public_url(result.url)
        return {"ok": result.ok and bool(result.markdown.strip()), "title": result.title,
                "url": result.url, "status": result.status, "error": result.error,
                "characters": len(result.markdown), "elapsed_s": round(time.monotonic() - started, 2),
                "metadata": result.metadata, "preview": result.markdown[:6000] if result.ok else ""}
    except TimeoutError:
        raise HTTPException(504, "网页读取超时，请换一个网址重试")
    except ImportError:
        raise HTTPException(503, "本机缺少所选抓取组件，请先安装网页采集依赖")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        if backend is not None:
            await backend.close()
