"""Native LangGraph research workflow with the common DeepSeek transport."""
import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import patch

from widesearch_experiment.common import ROOT, atomic_json, safe_error
from widesearch_experiment.model_runtime import content_text, searchos_model
from widesearch_experiment.services import langchain_callback


@contextmanager
def model_scope(cfg):
    import langchain.chat_models.base as chat_models
    original_init = chat_models.init_chat_model

    def compatible_model(model=None, *, model_provider=None, **kwargs):
        # Every upstream role uses the same model. The 'openai:' prefix selects
        # an SDK-compatible transport, not an OpenAI model or credential.
        if model and model.removeprefix("openai:") != cfg["model"]:
            raise ValueError("Upstream role attempted to change the unified model")
        kwargs.setdefault("timeout", cfg["model_timeout_seconds"])
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault("max_tokens", cfg["max_output_tokens"])
        return searchos_model(cfg, **kwargs)

    def create(*args, **kwargs):
        if not args:
            kwargs.setdefault("model", f"openai:{cfg['model']}")
        return original_init(*args, **kwargs)

    # Keep LangChain's deferred bind_tools/with_structured_output/with_config
    # behavior. Replacing it with ordinary configurable_fields loses per-role
    # settings after with_structured_output. This pinned helper is scoped to
    # one isolated worker and restored when the native graph finishes.
    with patch.object(chat_models, "_init_chat_model_helper", compatible_model):
        yield create


def settings(cfg):
    value = {"allow_clarification": False, "search_api": "tavily",
             "max_concurrent_research_units": cfg["max_concurrent_research_units"],
             "max_researcher_iterations": cfg["max_researcher_iterations"],
             "max_react_tool_calls": cfg["max_react_tool_calls"]}
    for role in ("research", "compression", "final_report", "summarization"):
        value[f"{role}_model"] = f"openai:{cfg['model']}"
        value[f"{role}_model_max_tokens"] = cfg["max_output_tokens"]
    return value


def partial_response(state):
    # Research briefs and messages containing instructions are not evidence.
    if state.get("final_report"):
        return content_text(state["final_report"])
    return "\n\n".join(content_text(note) for note in state.get("notes", []) if note)


async def run(task, cfg, services, workdir):
    sys.path.insert(0, str(ROOT / "vendor/open_deep_research/src"))
    import open_deep_research.utils as utils
    import open_deep_research.deep_researcher as impl
    from langchain_core.messages import HumanMessage

    async def common_search(search_queries, max_results=5, topic="general", include_raw_content=True, config=None):
        async def one(query):
            hits = await services.search(query, max_results)
            contents = await asyncio.gather(*(services.fetch(hit["url"]) for hit in hits))
            return {"query": query, "results": [
                {"url": hit["url"], "title": hit["title"], "content": hit["snippet"], "raw_content": body}
                for hit, body in zip(hits, contents)]}
        return await asyncio.gather(*(one(query) for query in search_queries))

    config = settings(cfg)
    services.journal.emit("adapter_config", workflow="official deep_researcher", config=config,
                          search_transport="DeepSeek via common Tavily-shaped tool", revision="deepseek-baselines-v1")
    state = {}
    status, error = "success", None
    with model_scope(cfg) as create, \
         patch.object(utils, "tavily_search_async", common_search), \
         patch.object(utils, "init_chat_model", create), \
         patch.object(impl, "configurable_model", create(configurable_fields=("model", "max_tokens", "api_key"))):
        try:
            # Same graph execution as ainvoke, exposing committed top-level
            # states so collected notes survive a failed final synthesis.
            async for state in impl.deep_researcher.astream(
                {"messages": [HumanMessage(content=task["query"])]}, stream_mode="values",
                config={"configurable": config, "recursion_limit": 150,
                        "callbacks": [langchain_callback(services.journal)]}):
                atomic_json(workdir / "workflow_checkpoint.json", state)
        except asyncio.CancelledError:
            status = "timeout"
        except Exception as exc:
            status, error = "failed", safe_error(exc)
            services.journal.emit("execution_error", **error)
    atomic_json(workdir / "workflow_result.json", state)
    result = {"response": partial_response(state), "status": status,
              "response_source": "final_report" if state.get("final_report") else "completed_research_notes"}
    if error:
        result["error"] = error
    if status == "success" and not state.get("final_report"):
        result.update(status="parse_error", reason="no_final_report")
    return result
