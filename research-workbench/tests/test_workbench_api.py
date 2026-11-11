"""Private credentials, task-scoped engines and durable failure regressions."""

import asyncio
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from api import workbench
from api.routes import history, local_search, search
from api.routes import workbench as routes

from searchos.config.model_context import activate_models, set_default_model_factory
from searchos.config.models import get_model_for
from searchos.socm import SearchState


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private" / "config.json")
    for module in (search, local_search, history):
        monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "sessions"))
    search.sessions.clear()
    set_default_model_factory(None)
    config = workbench.Configuration()
    config.ollama.model = "test-local-model"
    workbench.save_config(config)
    yield
    search.sessions.clear()
    set_default_model_factory(None)


@pytest.mark.asyncio
async def test_public_settings_never_echo_secrets_and_blank_fields_preserve_them(monkeypatch):
    async def installed(_config):
        return [{"id": "test-local-model", "label": "test-local-model"}], ""

    monkeypatch.setattr(workbench, "ollama_models", installed)
    saved = await routes.put_settings(
        {
            "deepseek": {"api_key": "test-secret-model"},
            "search": {"serper_api_key": "test-secret-search"},
        }
    )
    assert saved["deepseek"]["api_key_configured"]
    assert saved["search"]["serper_key_configured"]
    assert "test-secret" not in json.dumps(saved)
    assert stat.S_IMODE(workbench.CONFIG_PATH.stat().st_mode) == 0o600
    await routes.put_settings({"deepseek": {"api_key": ""}, "search": {"serper_api_key": "  "}})
    assert workbench.load_config().deepseek.api_key == "test-secret-model"
    assert workbench.load_config().search.serper_api_key == "test-secret-search"


def test_invalid_setting_errors_do_not_include_credentials():
    with pytest.raises(HTTPException) as caught:
        workbench.merged_config(
            {"deepseek": {"api_base": "secret-invalid-address", "api_key": "hidden-key"}}
        )
    assert "hidden-key" not in str(caught.value)
    assert "secret-invalid-address" not in str(caught.value)


@pytest.mark.asyncio
async def test_settings_cannot_change_during_active_task():
    search.sessions["busy"] = {"status": "running"}
    with pytest.raises(HTTPException) as caught:
        await routes.put_settings({"engine": "cli"})
    assert caught.value.status_code == 409
    assert workbench.load_config().engine == "ollama"


@pytest.mark.asyncio
async def test_follow_up_restores_engine_model_and_endpoint_from_saved_session(monkeypatch):
    config = workbench.load_config()
    config.engine = "deepseek"
    workbench.save_config(config)
    prior = {
        "engine": "ollama",
        "engine_model": "original-model",
        "api_base": "http://127.0.0.1:12434/v1",
    }
    selected = await workbench.select_engine(None, prior)
    assert (selected.engine, selected.model, selected.api_base) == (
        "ollama",
        "original-model",
        prior["api_base"],
    )


@pytest.mark.asyncio
async def test_task_models_are_isolated_across_concurrent_contexts():
    first, second = object(), object()

    async def run(model):
        with activate_models({"orchestrator": model}):
            await asyncio.sleep(0)
            return get_model_for("orchestrator")

    results = await asyncio.gather(run(first), run(second))
    assert results == [first, second]


def test_web_research_requires_an_explicit_public_search_key():
    config = workbench.load_config()
    config.search.provider = "serper"
    workbench.save_config(config)
    with pytest.raises(HTTPException) as caught:
        workbench.configure_search(required=True)
    assert caught.value.status_code == 400
    assert "Serper" in caught.value.detail
    assert "本地资料" in caught.value.detail


@pytest.mark.asyncio
async def test_failed_task_persists_engine_and_redacted_error_for_history(monkeypatch):
    from searchos.harness import session

    config = workbench.load_config()
    config.deepseek.api_key = "private-model-key"
    workbench.save_config(config)
    token = object()

    class Harness:
        def __init__(self, **kwargs):
            assert kwargs["model_overrides"]["orchestrator"] is token

        async def run(self, *args, **kwargs):
            assert get_model_for("orchestrator") is token
            raise RuntimeError("upstream failed with private-model-key")

    monkeypatch.setattr(session, "SearchSession", Harness)
    monkeypatch.setattr(workbench, "configure_search", lambda **kwargs: None)
    monkeypatch.setattr(workbench, "build_models", lambda _: {"orchestrator": token})
    monkeypatch.setattr(search.skills_catalog, "effective_skill_kwargs", lambda _: {})
    response = await search._launch_search(
        req=search.SearchRequest(query="example", engine="deepseek"),
        session_id="task-failure",
        state=SearchState(intent="example"),
        context_preamble=None,
        follow_up=False,
        query="example",
    )
    await search.sessions[response.session_id]["task"]
    search.sessions.clear()  # Simulate an API restart before reopening history.
    meta = history._session_meta(Path(search.WORKSPACE_ROOT) / response.session_id)
    assert meta["engine"] == "deepseek"
    assert meta["status"] == "error"
    assert "private-model-key" not in json.dumps(meta)
    restored = await history.load_history(response.session_id)
    assert restored["engine"] == "deepseek" and restored["status"] == "error"


@pytest.mark.asyncio
async def test_stop_before_background_task_starts_is_durable(monkeypatch):
    from searchos.harness import session

    class Harness:
        def __init__(self, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            await asyncio.sleep(60)

    monkeypatch.setattr(session, "SearchSession", Harness)
    monkeypatch.setattr(workbench, "configure_search", lambda **kwargs: None)
    monkeypatch.setattr(workbench, "build_models", lambda _: {"orchestrator": object()})
    monkeypatch.setattr(search.skills_catalog, "effective_skill_kwargs", lambda _: {})
    response = await search._launch_search(
        req=search.SearchRequest(query="stop", engine="ollama"),
        session_id="task-stop",
        state=SearchState(intent="stop"),
        context_preamble=None,
        follow_up=False,
        query="stop",
    )
    task = search.sessions[response.session_id]["task"]
    await search.stop_search(response.session_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert search.sessions[response.session_id]["status"] == "error"
    metadata = workbench.read_metadata(Path(search.WORKSPACE_ROOT) / response.session_id)
    assert metadata["error"] == "任务已停止"


@pytest.mark.asyncio
async def test_local_mode_binds_selected_model_without_search_credentials(monkeypatch):
    token = object()
    called = []

    class Service:
        async def answer(self, *args, **kwargs):
            called.append(kwargs["model"])
            return SimpleNamespace(
                answer="Grounded local answer",
                read_observations=[],
                tool_counts={},
                source_documents=[],
                citation_count=0,
                citation_status="no_citations",
                citations=[],
                input_tokens_estimate=0,
                model_calls=1,
                reading_stop_reason="answered",
            )

    monkeypatch.setattr(workbench, "build_model", lambda selection, role: token)
    monkeypatch.setattr(local_search, "_service", Service)
    response = await local_search.launch_local_search(
        session_id="task-local",
        query="local question",
        library_id="library",
        document_ids=["doc"],
        history=[],
        engine="ollama",
    )
    await search.sessions[response["session_id"]]["task"]
    assert called == [token]
    assert search.sessions[response["session_id"]]["status"] == "completed"
    assert response["engine"] == "ollama"
