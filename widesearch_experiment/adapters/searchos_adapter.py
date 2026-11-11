import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

from ..common import ROOT, atomic_json, read_json
from ..conversion import table_markdown
from ..services import langchain_callback
from ..model_runtime import searchos_model


def state_tables(state):
    """Use the system's own eval export, including full evidence values."""
    from searchos.harness.report.eval_table_export import build_eval_table
    return build_eval_table(state)


async def run(task, cfg, services, workdir, *, ours=False, adaptive=True):
    source = ROOT / cfg["_ours_snapshot"] if ours else ROOT / "vendor/searchos"
    sys.path.insert(0, str(source))
    os.environ.update({"SF_BROWSER_BACKEND": "aiohttp", "SF_SEARCH_PROVIDER": "serper",
                       "SF_ENABLE_P1_EVIDENCE_REASONING": "false", "SF_ENABLE_P2_CONTEXT_MEMORY": "false",
                       "SF_ENABLE_ADAPTIVE_COLLECTION": "false"})
    from searchos.config.run_config import EffectiveRunConfig
    from searchos.config.settings import settings
    from searchos.harness.session import SearchSession, close_browser_service
    from searchos.tools.simple_browser.search.base import SearchProvider, SearchResult
    from searchos.tools.simple_browser.state import set_browser_provider
    from searchos.tools.simple_browser.backend.base import BrowserService, FetchResult
    from searchos.tools.simple_browser.backend.aiohttp import AiohttpBackend

    class CommonSearch(SearchProvider):
        name = cfg["search_provider"]
        allows_snippet_fallback = False

        async def search(self, query, max_results=10):
            results = await services.search(query, max_results)
            return [SearchResult(**r) for r in results]

    class ObservedReader(AiohttpBackend):
        async def fetch(self, url, **kwargs):
            if cfg.get("fetch_provider") == "deepseek":
                page = await services.deepseek_fetch(url)
                links = dict.fromkeys(re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", page["content"]))
                return FetchResult(url=url, title=page["title"], markdown=page["content"],
                                   links={f"L{i}": link for i, link in enumerate(links)},
                                   status=page["status"], error=page["error"])
            services.journal.emit("page_start", url=url)
            r = await super().fetch(url, **kwargs)
            services.journal.emit("page_end", url=url, success=r.ok and bool(r.markdown), status=r.status)
            if r.ok:
                services.save_page(url, r.markdown)
            return r

    set_browser_provider(CommonSearch())
    BrowserService.set_instance(BrowserService(ObservedReader()))
    model = searchos_model(cfg, max_tokens=cfg["max_output_tokens"],
                           timeout=cfg["model_timeout_seconds"], max_retries=0,
                           callbacks=[langchain_callback(services.journal)])
    # Some optional skill helpers resolve a role lazily rather than using the
    # seven session constructor overrides. Bind that public factory as well.
    import searchos.config.models as model_registry
    model_registry.get_model_for = lambda *args, **kwargs: model
    roles = ("orchestrator", "judge", "extraction", "synthesis", "skill_evolver", "post_mortem", "sub_agent")
    # Copy static shipped skills to the attempt directory, so evolution cannot leak across runs.
    skill_dir = workdir / "skill_library"
    original = source / "searchos/skills/library"
    if original.exists():
        shutil.copytree(original, skill_dir, dirs_exist_ok=True)
    else:
        skill_dir.mkdir(exist_ok=True)
    run_cfg = EffectiveRunConfig.from_settings(for_eval=True)
    if ours:
        run_cfg = run_cfg.with_modules({"enabled": ["adaptive_collection"] if adaptive else []})
    kwargs = dict(max_time_s=cfg["research_timeout_seconds"], workspace_root=str(workdir / "sessions"),
                  skill_library_path=str(skill_dir), model_overrides={role: model for role in roles},
                  run_config=run_cfg, research_mode="table", skip_synthesis=True)
    session = SearchSession(**kwargs)
    services.journal.emit("adapter_config", source=str(source), run_config=run_cfg.snapshot(),
                          static_skills_copied=True, browser=cfg.get("fetch_provider", "native_aiohttp"), initial_schema="none",
                          all_model_roles=cfg["model"], skip_synthesis=True)
    last = {"text": "", "count": -1}

    def snapshot(state):
        try:
            text = state_tables(state)
            count = services.journal.counts()["search_calls"]
            if text and (text != last["text"] or count != last["count"]):
                event = services.journal.emit("table_snapshot", search_calls=count)
                directory = workdir / "snapshots"
                directory.mkdir(exist_ok=True)
                (directory / f"{event['seq']:06}.md").write_text(text)
                last.update(text=text, count=count)
        except Exception:
            # Telemetry cannot alter the collection algorithm.
            pass

    from searchos.socm.workspace import WorkspaceManager
    original_save = WorkspaceManager.save_state

    def observed_save(workspace, state):
        original_save(workspace, state)
        snapshot(state)

    WorkspaceManager.save_state = observed_save

    try:
        result = await session.run(task["query"], session_id="session")
        workspace = Path(result.workspace_path)
        output = workspace / "output"
        response = ""
        if (output / "eval_table.md").exists():
            response = (output / "eval_table.md").read_text()
        if not response and (output / "result.json").exists():
            response = read_json(output / "result.json").get("answer", "")
        if not response:
            for message in reversed(result.final_messages):
                if message.get("role") in {"assistant", "ai"} and isinstance(message.get("content"), str):
                    response = message["content"]
                    break
        if not response:
            response = state_tables(result.search_state)
        atomic_json(workdir / "searchos_result.json", {"token_usage": result.token_usage,
                    "browser_usage": result.browser_usage, "execution_error": result.execution_error,
                    "final_messages": result.final_messages, "coverage": result.coverage_score})
        atomic_json(workdir / "final_state.json", result.search_state.model_dump(mode="json"))
        return {"response": response, "status": "failed" if result.execution_error else "success",
                "execution_error": result.execution_error, "native_usage": result.token_usage}
    finally:
        WorkspaceManager.save_state = original_save
        await close_browser_service()
