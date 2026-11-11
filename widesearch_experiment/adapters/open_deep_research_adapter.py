import asyncio
import sys

from ..common import ROOT, atomic_json
from ..services import langchain_callback
from ..model_runtime import content_text, langchain_options


async def run(task, cfg, services, workdir):
    sys.path.insert(0, str(ROOT / "vendor/open_deep_research/src"))
    import open_deep_research.utils as utils
    from langchain.chat_models import init_chat_model as original_init
    from langchain_core.messages import HumanMessage

    # Replace only the provider transport underneath the official Tavily-shaped
    # search+page summarization tool. The LangGraph planner, supervisor, parallel
    # researchers, compression and final report nodes remain the official ones.
    async def common_search(search_queries, max_results=5, topic="general", include_raw_content=True, config=None):
        async def one(query):
            hits = await services.search(query, max_results)
            contents = await asyncio.gather(*(services.fetch(h["url"]) for h in hits))
            return {"query": query, "results": [{"url": h["url"], "title": h["title"],
                    "content": h["snippet"], "raw_content": content} for h, content in zip(hits, contents)]}
        return await asyncio.gather(*(one(q) for q in search_queries))

    def model_factory(*args, **kwargs):
        if not args:
            kwargs.setdefault("model", f"openai:{cfg['model']}")
        kwargs.update(langchain_options(cfg))
        kwargs.setdefault("timeout", cfg["model_timeout_seconds"])
        kwargs.setdefault("max_retries", 0)
        return original_init(*args, **kwargs)

    utils.tavily_search_async = common_search
    utils.init_chat_model = model_factory
    import open_deep_research.deep_researcher as impl
    impl.configurable_model = model_factory(configurable_fields=("model", "max_tokens", "api_key"))
    settings = {"allow_clarification": False, "search_api": "tavily",
                "max_concurrent_research_units": cfg["max_concurrent_research_units"],
                "max_researcher_iterations": cfg["max_researcher_iterations"],
                "max_react_tool_calls": cfg["max_react_tool_calls"]}
    for role in ("research", "compression", "final_report", "summarization"):
        settings[f"{role}_model"] = f"openai:{cfg['model']}"
        settings[f"{role}_model_max_tokens"] = cfg["max_output_tokens"]
    services.journal.emit("adapter_config", workflow="official deep_researcher", config=settings,
                          search_transport="unified search replacing tavily_search_async")
    result = await impl.deep_researcher.ainvoke({"messages": [HumanMessage(content=task["query"])]},
              config={"configurable": settings, "recursion_limit": 150,
                      "callbacks": [langchain_callback(services.journal)]})
    atomic_json(workdir / "workflow_result.json", result)
    return {"response": content_text(result.get("final_report", "")), "status": "success"}
