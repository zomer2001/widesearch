"""Production P2 opt-in wiring, conversation ownership and revision lifecycle."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from searchos.config.settings import settings
from searchos.harness.middleware import pipelines
from searchos.harness.middleware.models import TaskContext
from searchos.harness.p1_runtime import begin_run
from searchos.socm import FrontierTask, SearchState
from searchos.socm.workspace import WorkspaceManager


@pytest.fixture
def ws(tmp_path):
    workspace = WorkspaceManager(tmp_path, "pipeline")
    workspace.create()
    state = SearchState(intent="Compare official reports from 2025.")
    state.frontier.add(FrontierTask(id="task", question="Read the annual filing"))
    workspace.save_state(state)
    return workspace


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "role", ["web_search", "local_search", "explore", "writer", "orchestrator"]
)
def test_all_roles_choose_one_context_mechanism(monkeypatch, ws, enabled, role):
    monkeypatch.setattr(settings, "enable_p2_context_memory", enabled)
    monkeypatch.setattr(settings, "p2_context_window", 12000)
    monkeypatch.setattr(settings, "compress_old_tool_results", True)
    monkeypatch.setattr(pipelines, "_graph", lambda m, t, s, middleware, **kw: middleware)
    kwargs = {} if role == "orchestrator" else {
        "task": TaskContext(agent_id="worker", task_id="task")
    }
    chain = getattr(pipelines, role + "_pipeline")(
        SimpleNamespace(), [], workspace=ws, **kwargs
    )
    names = [type(item).__name__ for item in chain]
    assert names.count("CompactionContextMiddleware") == int(enabled)
    assert ("DynamicTrimMiddleware" in names) is not enabled
    if enabled:
        assert not {"ToolHistoryMiddleware", "SearchEpisodeMiddleware"} & set(names)
        assert ws.load_state().p2.enabled
        if "SOCMContextMiddleware" in names:
            assert names.index("SOCMContextMiddleware") < names.index("CompactionContextMiddleware")


@pytest.mark.asyncio
async def test_unbound_worker_cannot_publish_assignment_as_user_goal(monkeypatch, ws):
    monkeypatch.setattr(settings, "enable_p2_context_memory", True)
    monkeypatch.setattr(settings, "p2_context_window", 12000)
    component = pipelines._p2_compactor(SimpleNamespace(), ws, TaskContext(agent_id="search-7"))
    await component.abefore_model({"messages": [HumanMessage("Generated worker assignment")]}, None)
    assert component.task_id == "__worker__:search-7"
    assert ws.load_state().p2.goal.user_messages == []
    main = pipelines._p2_compactor(SimpleNamespace(), ws, TaskContext(agent_id="orchestrator"))
    await main.abefore_model({"messages": [HumanMessage("Correction: 2026 only.")]}, None)
    assert ws.load_state().p2.goal.user_messages == ["Correction: 2026 only."]


def test_p2_alone_uses_common_version_tickets_without_enabling_annotation(monkeypatch, ws):
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", False)
    monkeypatch.setattr(settings, "enable_p2_context_memory", True)
    assert begin_run(ws, "task", "p2-run") == "p2-run"
    assert ws.load_state().p1.executions["p2-run"].task_id == "task"
    assert settings.enable_p1_evidence_reasoning is False


def test_missing_window_rejects_setup_before_enabling_memory(monkeypatch, ws):
    monkeypatch.setattr(settings, "enable_p2_context_memory", True)
    monkeypatch.setattr(settings, "p2_context_window", 0)
    with pytest.raises(ValueError, match="context_window"):
        pipelines._p2_compactor(SimpleNamespace(), ws, TaskContext(agent_id="orchestrator"))
    assert not ws.load_state().p2.enabled
