"""Real dispatch and publication barriers, using fixed model-free graph events."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from searchos.agents import runtime
from searchos.agents.orchestrator import lifecycle
from searchos.config.settings import settings
from searchos.harness.p1_runtime import (
    assert_current,
    assert_current_state,
    begin_run,
    continue_run,
    current_run_id,
    execution_context,
    record_snapshot,
)
from searchos.socm import SearchState
from searchos.socm.evidence import EvidenceNode, bind_task_evidence
from searchos.socm.frontier import FrontierTask, FrontierTaskStatus
from searchos.socm.revisions import StaleExecutionError
from searchos.socm.workspace import WorkspaceManager


def workspace(tmp_path, *, enabled=True):
    ws = WorkspaceManager(tmp_path, "runtime")
    ws.create()
    state = SearchState(intent="Find the current leader", research_mode="findings")
    state.p1.enabled = enabled
    state.frontier.add(
        FrontierTask(
            id="task",
            question="Find the leader",
            status=FrontierTaskStatus.RUNNING,
            assigned_agent_id="worker",
            attempts=1,
        )
    )
    state.evidence_graph.add_node(
        EvidenceNode(
            id="fact",
            finding="Ada is leader",
            source="https://example.org",
            source_excerpt="Ada is leader",
        )
    )
    ws.save_state(state)
    return ws


def info(ws, run_id="run"):
    runtime.set_orchestrator_context(workspace=ws, model=object())
    runtime._ctx.agent_graphs["worker"] = {
        "agent_type": "search_agent",
        "assigned_task_id": "task",
        "p1_run_id": run_id,
    }
    return runtime._ctx.agent_graphs["worker"]


def report():
    return lifecycle._compute_agent_report(
        agent_id="worker",
        thread_id="worker",
        pre_snapshot={"evidence_ids": set(), "filled_keys": set()},
        started_at=0,
        status="completed",
        result="Completed",
        last_ai_text="Ada leads the organization",
    )


def test_disabled_bridge_has_no_writes_and_context_restores(tmp_path, monkeypatch):
    ws = workspace(tmp_path, enabled=False)
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", False)
    before = ws.load_state().model_dump()
    assert begin_run(ws, "task", "off") == ""
    with execution_context(""):
        record_snapshot(ws, ws.load_state(), evidence_ids=["fact"])
        assert assert_current(ws) is None
    assert ws.load_state().model_dump() == before
    with execution_context("outer"):
        with execution_context(""):
            assert current_run_id.get() == ""
        assert current_run_id.get() == "outer"
    assert current_run_id.get() == ""


def test_snapshot_read_registration_rejects_changed_input(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "run")
    snapshot = ws.load_state()
    state = ws.load_state()
    state.evidence_graph.nodes[0].finding = "A changed finding"
    ws.save_state(state)
    with execution_context("run"), pytest.raises(StaleExecutionError):
        record_snapshot(ws, snapshot, evidence_ids=["fact"])
    assert ws.load_state().p1.executions["run"].read_evidence == {}


def test_task_publication_records_worker_observations_and_closes_ticket(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "run")
    data = info(ws)
    data["extractions"] = (SimpleNamespace(committed_node_ids={"fact"}),)
    # The report callback must use its own ticket, never its caller's context.
    with execution_context("parent-unrelated"):
        result = report()
    current = ws.load_state()
    assert result.status == "completed"
    assert current.frontier.questions[0].resolution == "Ada leads the organization"
    assert set(current.p1.artifacts["task:task"].read_evidence) == {"fact"}
    assert current.p1.executions["run"].closed


@pytest.mark.parametrize("callback", ["complete", "fail"])
def test_late_callbacks_cannot_overwrite_reassigned_task(tmp_path, callback):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "old")
    info(ws, "old")
    state = ws.load_state()
    state.frontier.questions[0].assigned_agent_id = "new-worker"
    state.frontier.questions[0].resolution = "new worker result"
    ws.save_state(state)
    begin_run(ws, "task", "new")
    before = ws.load_state().model_dump()
    with pytest.raises(StaleExecutionError):
        if callback == "complete":
            report()
        else:
            lifecycle._recycle_failed_task("task", "worker", "error", "late error")
    assert ws.load_state().model_dump() == before


def test_continuation_keeps_valid_history_but_rejects_changed_source(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "first")
    with execution_context("first"):
        record_snapshot(ws, ws.load_state(), evidence_ids=["fact"])
    info(ws, "first")
    report()
    assert continue_run(ws, "first", "second") == "second"
    assert set(ws.load_state().p1.executions["second"].read_evidence) == {"fact"}
    state = ws.load_state()
    state.evidence_graph.nodes[0].finding = "Revised source"
    ws.save_state(state)
    with pytest.raises(StaleExecutionError):
        continue_run(ws, "second", "third")
    assert "third" not in ws.load_state().p1.executions


@pytest.mark.asyncio
async def test_real_spawn_binds_context_and_tracks_injected_parent_evidence(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    state = ws.load_state()
    state.frontier.questions[0].status = FrontierTaskStatus.PENDING
    state.frontier.questions[0].assigned_agent_id = ""
    state.frontier.questions[0].blocked_by = ["parent"]
    state.frontier.add(
        FrontierTask(
            id="parent", question="Collect leader source", status=FrontierTaskStatus.COMPLETED
        )
    )
    bind_task_evidence(state, "parent", ["fact"])
    ws.save_state(state)
    runtime.set_orchestrator_context(workspace=ws, model=object())
    observed = {}

    class Graph:
        async def astream(self, _input, **kwargs):
            observed["run"] = current_run_id.get()
            observed["thread"] = kwargs["config"]["configurable"]["thread_id"]
            assert_current(ws)
            yield {"messages": [AIMessage(content="Ada leads the organization")]}

    def graph(**kwargs):
        observed["prompt"] = kwargs["system_prompt"]
        return Graph()

    monkeypatch.setattr(runtime, "create_search_agent_graph", graph)
    with execution_context("orchestrator-ticket"):
        agent = await lifecycle._spawn_sub_agent(
            "search_agent", "Find the leader", [], bind_task_id="task", middleware=[]
        )
        result = await runtime._ctx.task_pool[agent]
        assert current_run_id.get() == "orchestrator-ticket"
    state = ws.load_state()
    assert observed["run"] == runtime._ctx.agent_graphs[agent]["p1_run_id"]
    assert observed["thread"] == agent
    assert "Ada is leader" in observed["prompt"]
    assert set(state.p1.artifacts["task:task"].read_evidence) == {"fact"}
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_retired_graph_result_is_not_a_completed_summary(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "run")
    data = info(ws)

    class LateGraph:
        async def astream(self, _input, **_kwargs):
            state = ws.load_state()
            state.p1.executions["run"].invalidated = True
            ws.save_state(state)
            yield {"messages": [AIMessage(content="obsolete summary")]}

    data.update(
        graph=LateGraph(),
        thread_id="worker",
        wrapped_task="Find leader",
        task="Find leader",
        pre_snapshot={"evidence_ids": set(), "filled_keys": set()},
        missing_skills=[],
        started_at=0,
    )
    with pytest.raises(StaleExecutionError):
        await lifecycle._collect_sub_agent_result("worker")
    assert "worker" not in runtime._ctx.completed
    assert ws.load_state().frontier.questions[0].status == FrontierTaskStatus.RUNNING
    assert current_run_id.get() == ""


def test_atomic_guard_checks_task_owner(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "run")
    state = ws.load_state()
    state.frontier.questions[0].assigned_agent_id = "other"
    with execution_context("run"), pytest.raises(StaleExecutionError):
        assert_current_state(state)


@pytest.mark.asyncio
async def test_continuation_has_new_ticket_and_records_delivered_followup(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "first")
    data = info(ws, "first")
    report()
    state = ws.load_state()
    state.evidence_graph.add_node(
        EvidenceNode(
            id="new",
            finding="Ada's term begins in October",
            source="https://example.org/term",
        )
    )
    ws.save_state(state)
    observed = []

    class Graph:
        async def astream(self, _input, **_kwargs):
            observed.append(current_run_id.get())
            assert "new" in assert_current(ws).read_evidence
            yield {"messages": [AIMessage(content="Ada began in October")]}

    data.update(
        graph=Graph(), thread_id="worker", task="Find leader", p1_pending_evidence_ids=["new"]
    )
    result = await lifecycle._continue_sub_agent_run("worker", "New evidence: term in October")
    assert observed[0] != "first"
    assert data["p1_run_id"] == observed[0]
    assert result.last_message == "Ada began in October"
    assert ws.load_state().p1.artifacts["task:task"].version == 2
    assert set(ws.load_state().p1.artifacts["task:task"].read_evidence) == {"new"}
    assert "p1_pending_evidence_ids" not in data
    assert current_run_id.get() == ""


@pytest.mark.asyncio
async def test_stale_continuation_does_not_reenter_old_graph(tmp_path):
    ws = workspace(tmp_path)
    begin_run(ws, "task", "first")
    data = info(ws, "first")
    runtime._ctx.completed["worker"] = report()
    state = ws.load_state()
    state.p1.executions["first"].invalidated = True
    ws.save_state(state)
    # No graph is supplied: the stale ticket must fail before graph access.
    with pytest.raises(StaleExecutionError):
        await lifecycle._continue_sub_agent_run("worker", "Continue the old history")
    assert data["p1_run_id"] == "first"
    assert "worker" not in runtime._ctx.completed
