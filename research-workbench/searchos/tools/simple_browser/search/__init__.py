"""Search providers (paper §4.2): the search-API half of Simple Browser."""

from __future__ import annotations

from searchos.tools.simple_browser.search.base import SearchProvider, SearchResult

# 开源可选的搜索后端元数据（配置向导 / 文档共用）。
SEARCH_PROVIDER_INFO: dict[str, dict[str, str]] = {
    "openai_web_search": {
        "label": "OpenAI web_search（Responses 联网搜索）",
        "api_key_env": "OPENAI_API_KEY",
        "doc_url": "https://developers.openai.com/api/docs/guides/tools-web-search",
    },
    "ddgs": {
        "label": "免密钥搜索（DDGS 聚合搜索）",
        "api_key_env": "",
        "doc_url": "https://github.com/deedy5/ddgs",
    },
    "serper": {
        "label": "Serper.dev（Google 结果，推荐）",
        "api_key_env": "SERPER_API_KEY",
        "doc_url": "https://serper.dev",
    },
    "tavily": {
        "label": "Tavily（需 pip install 'searchos[tavily]'）",
        "api_key_env": "TAVILY_API_KEY",
        "doc_url": "https://tavily.com",
    },
    "ragflow": {
        "label": "RagFlow（蚂蚁内网接口，外部不可用）",
        "api_key_env": "RAGFLOW_USER_ID",
        "doc_url": "",
    },
}


def resolve_search_provider_name(name: str = "") -> str:
    """确定搜索后端：显式配置 > 按已有 key 自动推断 > ragflow（向后兼容）。"""
    import os

    if not name:
        name = os.environ.get("SF_SEARCH_PROVIDER", "")
    name = name.strip().lower()
    if name:
        if name not in SEARCH_PROVIDER_INFO:
            known = ", ".join(SEARCH_PROVIDER_INFO)
            raise ValueError(f"Unknown SF_SEARCH_PROVIDER={name!r}. Available: {known}")
        return name
    from searchos.config.settings import settings

    if os.environ.get("SERPER_API_KEY") or settings.serper_api_key:
        return "serper"
    if os.environ.get("TAVILY_API_KEY") or settings.tavily_api_key:
        return "tavily"
    return "ragflow"


def build_search_provider(name: str = "", *, api_key: str = "",
                          api_base: str = "https://api.openai.com/v1",
                          web_search_model: str = "gpt-4o",
                          allowed_domains: list[str] | None = None) -> SearchProvider:
    """按名字构造搜索后端；``name`` 为空时走 ``resolve_search_provider_name``。"""
    import os

    from searchos.config.settings import settings

    name = resolve_search_provider_name(name)
    if name == "openai_web_search":
        from searchos.tools.simple_browser.search.openai_web_search import OpenAIWebSearchProvider

        return OpenAIWebSearchProvider(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""), api_base=api_base,
            model=web_search_model, allowed_domains=allowed_domains,
        )
    if name == "ddgs":
        from searchos.tools.simple_browser.search.ddgs import DDGSProvider
        return DDGSProvider()
    if name == "serper":
        from searchos.tools.simple_browser.search.serper import SerperProvider

        return SerperProvider(
            api_key=os.environ.get("SERPER_API_KEY", "") or settings.serper_api_key,
        )
    if name == "tavily":
        from searchos.tools.simple_browser.search.tavily import TavilyProvider

        return TavilyProvider(
            api_key=os.environ.get("TAVILY_API_KEY", "") or settings.tavily_api_key,
        )
    from searchos.tools.simple_browser.search.ragflow import RagFlowProvider

    return RagFlowProvider()


__all__ = [
    "SEARCH_PROVIDER_INFO",
    "SearchProvider",
    "SearchResult",
    "build_search_provider",
    "resolve_search_provider_name",
]
