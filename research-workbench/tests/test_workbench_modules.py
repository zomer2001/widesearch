"""Optional research modules stay fixed, durable and connected to the real harness."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from api import innovation, workbench
from api.routes import history, local_search, search
from api.routes import workbench as routes

from searchos.socm import SearchState, WorkspaceManager


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench, "CONFIG_PATH", tmp_path / "private.json")
    config = workbench.Configuration()
    config.ollama.model = "test-model"
    workbench.save_config(config)
    for module in (search, local_search, history):
        monkeypatch.setattr(module, "WORKSPACE_ROOT", str(tmp_path / "sessions"))
    search.sessions.clear()
    yield
    search.sessions.clear()


def source():
    return {
        "mode": "local",
        "local": {
            "library_id": "library",
            "selection": "documents",
            "document_ids": ["doc"],
            "model_access": True,
        },
    }


def verified_service(monkeypatch):
    record = SimpleNamespace(
        document_id="doc",
        model_dump=lambda **_: {
            "document_id": "doc",
            "source_revision": "r1",
            "generation": 1,
        },
    )
    service = SimpleNamespace(
        get_library=lambda _: SimpleNamespace(library_id="library", index_root="/allowed/library"),
        _verified_blocks=lambda library, ids: [(record, None)] if ids == ["doc"] else [],
    )
    monkeypatch.setattr(local_search, "_service", lambda: service)
    return service


def no_web(*args, **kwargs):
    raise AssertionError("Local tasks must not initialize external search")


def test_module_request_rejects_unknown_ids():
    with pytest.raises(ValidationError):
        search.SearchRequest(query="q", modules=["pretend_feature"])


def test_manifest_is_canonical_immutable_and_version_checked(tmp_path):
    config = innovation.resolve_modules(["evidence_revision", "context_memory"])
    innovation.write_manifest(tmp_path, config)
    assert innovation.resolve_modules(None, workspace=tmp_path) == config
    assert (
        innovation.resolve_modules(["context_memory", "evidence_revision"], workspace=tmp_path)
        == config
    )
    with pytest.raises(HTTPException, match="新建研究"):
        innovation.resolve_modules([], workspace=tmp_path)
    (tmp_path / innovation.MANIFEST_NAME).write_text(
        json.dumps({**config, "implementation_version": "2"})
    )
    with pytest.raises(HTTPException):
        innovation.read_manifest(tmp_path)


@pytest.mark.asyncio
async def test_registry_defaults_off_and_lists_only_implemented_modules():
    result = await routes.get_modules()
    assert {module["id"] for module in result["modules"]} == {"evidence_revision", "context_memory", "adaptive_collection"}
    assert all(module["default_enabled"] is False for module in result["modules"])


@pytest.mark.asyncio
async def test_default_local_task_still_uses_direct_qa(monkeypatch):
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        return {"session_id": kwargs["session_id"], "status": "running"}

    monkeypatch.setattr(local_search, "launch_local_search", launch)
    monkeypatch.setattr(search, "init_search_provider", no_web)
    await search.create_search(search.SearchRequest(query="q", sources=source()))
    assert captured["module_config"]["enabled"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"tables": [{"table_id": "leaders", "entities": ["Demo"], "attrs": ["leader"]}]},
        {"entities": ["Demo"], "attrs": ["leader"]},
    ],
)
async def test_enabled_local_task_routes_to_research_with_verified_scope(monkeypatch, schema):
    verified_service(monkeypatch)
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        return search.SearchResponse(
            session_id=kwargs["session_id"],
            status="running",
            module_config=kwargs["state"].module_config,
        )

    monkeypatch.setattr(search, "_launch_search", launch)
    monkeypatch.setattr(search, "init_search_provider", no_web)
    result = await search.create_search(
        search.SearchRequest(
            query="Compare local sources",
            sources=source(),
            modules=["evidence_revision"],
            **schema,
        )
    )
    state = captured["state"]
    assert state.research_mode == "findings"
    assert state.source_scope["mode"] == "local"
    assert state.source_scope["index_root"] == "/allowed/library"
    assert [record["document_id"] for record in state.source_scope["documents"]] == ["doc"]
    assert result.module_config.enabled == ["evidence_revision"]
    assert all(task.agent_type == "local_search_agent" for task in state.frontier.questions)


@pytest.mark.asyncio
async def test_follow_up_inherits_modules_after_restart_and_rejects_change(monkeypatch):
    verified_service(monkeypatch)
    config = innovation.normalize({"enabled": ["context_memory", "evidence_revision"]})
    workspace = WorkspaceManager(search.WORKSPACE_ROOT, "existing")
    workspace.create()
    workspace.save_state(
        SearchState(
            intent="old",
            module_config=config,
            source_scope={
                "mode": "local",
                "library_id": "library",
                "selection": "documents",
                "document_ids": ["doc"],
                "documents": [],
            },
        )
    )
    innovation.write_manifest(workspace.path, config)
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        return search.SearchResponse(
            session_id=kwargs["session_id"],
            status="running",
            module_config=kwargs["state"].module_config,
        )

    monkeypatch.setattr(search, "_launch_search", launch)
    monkeypatch.setattr(search, "init_search_provider", no_web)
    result = await search.create_search(
        search.SearchRequest(query="follow-up", follow_up_to="existing")
    )
    assert result.module_config.enabled == ["evidence_revision", "context_memory"]
    assert captured["state"].source_scope["mode"] == "local"
    with pytest.raises(HTTPException) as caught:
        await search.create_search(
            search.SearchRequest(query="follow-up", follow_up_to="existing", modules=[])
        )
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_launch_binds_manifest_to_harness_without_search_credentials(monkeypatch):
    from searchos.harness import session
    from searchos.config.settings import settings

    captured = {}
    monkeypatch.setattr(settings, "max_parallel_agents", 3)

    class Harness:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, *args, **kwargs):
            assert kwargs["initial_state"].module_config == captured["module_config"]
            assert settings.max_parallel_agents == 1
            return SimpleNamespace(search_state=kwargs["initial_state"])

    monkeypatch.setattr(session, "SearchSession", Harness)
    monkeypatch.setattr(workbench, "build_models", lambda _: {"orchestrator": object()})
    monkeypatch.setattr(workbench, "configure_search", no_web)
    config = innovation.normalize({"enabled": ["context_memory"]})
    state = SearchState(
        intent="q",
        source_scope={"mode": "local", "documents": []},
        research_mode="findings",
        module_config=config,
    )
    result = await search._launch_search(
        req=search.SearchRequest(query="q", effort="medium"),
        session_id="real-route",
        state=state,
        context_preamble=None,
        follow_up=False,
        query="q",
    )
    await search.sessions[result.session_id]["task"]
    assert settings.max_parallel_agents == 3
    assert captured["module_config"] == config
    assert captured["research_mode"] == "findings"
    search.sessions.clear()
    restored = await history.load_history(result.session_id)
    assert restored["module_config"] == config
    assert restored["search_state"]["module_config"] == config
    assert search._get_live_state(result.session_id)["module_config"] == config


@pytest.mark.asyncio
async def test_legacy_evidence_overwrite_cannot_bypass_enhanced_revision_tracking():
    config = innovation.normalize({"enabled": ["context_memory"]})
    workspace = WorkspaceManager(search.WORKSPACE_ROOT, "tracked")
    workspace.create()
    state = SearchState(intent="q", module_config=config)
    workspace.save_state(state)
    innovation.write_manifest(workspace.path, config)
    state_file = workspace.path / "search_state.json"
    before = state_file.read_bytes()
    with pytest.raises(HTTPException) as caught:
        await search.resolve_evidence(
            "tracked",
            search.ResolveEvidenceRequest(
                table_id="table", entity="entity", attribute="attribute", evidence_id="evidence"
            ),
        )
    assert caught.value.status_code == 409
    assert state_file.read_bytes() == before


@pytest.mark.asyncio
async def test_branch_keeps_manifest_and_state_version():
    config = innovation.normalize({"enabled": ["evidence_revision", "context_memory"]})
    workspace = WorkspaceManager(search.WORKSPACE_ROOT, "source")
    workspace.create()
    state = SearchState(intent="original", module_config=config)
    workspace.save_state(state)
    innovation.write_manifest(workspace.path, config)
    history._write_branch_conversation(workspace.path, "original", "answer")
    workspace.save_turn_snapshot("original", state, {})
    result = await history.branch_history_turn("source", 0)
    assert result.module_config.model_dump() == config
    branch = Path(search.WORKSPACE_ROOT) / result.session_id
    assert innovation.read_manifest(branch) == config
    restored = await history.load_history(result.session_id)
    assert restored["turns"][0]["module_config"] == config


@pytest.mark.asyncio
async def test_direct_local_launcher_rejects_enabled_modules():
    with pytest.raises(HTTPException) as caught:
        await local_search.launch_local_search(
            session_id="wrong-route",
            query="q",
            library_id="l",
            document_ids=["d"],
            history=[],
            module_config={"enabled": ["evidence_revision"]},
        )
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_internal_execution_failure_keeps_partial_state_but_never_reports_completed(
    monkeypatch,
):
    from searchos.harness import session

    class Harness:
        def __init__(self, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            state = kwargs["initial_state"]
            workspace = WorkspaceManager(search.WORKSPACE_ROOT, kwargs["session_id"])
            workspace.write_output(
                "result.json", json.dumps({"answer": "Supported partial answer"})
            )
            return SimpleNamespace(search_state=state, execution_error="ValueError")

    monkeypatch.setattr(session, "SearchSession", Harness)
    monkeypatch.setattr(workbench, "build_models", lambda _: {"orchestrator": object()})
    monkeypatch.setattr(workbench, "configure_search", no_web)
    config = innovation.normalize({"enabled": ["context_memory"]})
    state = SearchState(intent="q", source_scope={"mode": "local"}, module_config=config)
    started = await search._launch_search(
        req=search.SearchRequest(query="q"),
        session_id="interrupted",
        state=state,
        context_preamble=None,
        follow_up=False,
        query="q",
    )
    await search.sessions[started.session_id]["task"]
    result = await search.get_search_result(started.session_id)
    assert result["status"] == "error"
    assert result["partial"] is True
    assert result["execution_error"] == "ValueError"
    assert result["answer"] == "Supported partial answer"
    assert result["search_state"]["module_config"] == config
    search.sessions.clear()
    restored = await history.load_history(started.session_id)
    assert restored["status"] == "error"
    assert restored["execution_error"] == "ValueError"
    assert "中断" in restored["error"]


@pytest.mark.asyncio
async def test_local_repair_restores_fixed_modules_without_web_configuration(monkeypatch):
    from searchos.socm import CellStatus

    config = innovation.normalize({"enabled": ["evidence_revision", "context_memory"]})
    state = SearchState(intent="q", source_scope={"mode": "local"}, module_config=config)
    state.coverage_map.initialize(["company"], ["revenue"])
    state.coverage_map.cells["_default/company.revenue"].status = CellStatus.UNCERTAIN
    workspace = WorkspaceManager(search.WORKSPACE_ROOT, "repair")
    workspace.create()
    workspace.save_state(state)
    innovation.write_manifest(workspace.path, config)
    captured = {}

    async def launch(**kwargs):
        captured.update(kwargs)
        return search.SearchResponse(
            session_id=kwargs["session_id"],
            status="running",
            module_config=kwargs["state"].module_config,
        )

    monkeypatch.setattr(search, "_launch_search", launch)
    monkeypatch.setattr(search, "init_search_provider", no_web)
    result = await search.repair_cells(
        "repair",
        search.RepairRequest(
            cells=[
                search.RepairCellRequest(
                    table_id="_default", entity="company", attribute="revenue"
                ),
            ]
        ),
    )
    assert result.module_config.model_dump() == config
    assert captured["follow_up"] is True
    assert captured["state"].module_config == config
    assert captured["targeted_repair_cells"] == ["_default/company.revenue"]
