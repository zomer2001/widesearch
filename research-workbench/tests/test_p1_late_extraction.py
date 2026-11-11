"""A real extraction commit cannot admit a response from a retired execution."""

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.try_p1_pipeline import ENTITY, FIXTURES, TABLE, FixedFixtureModel
from searchos.agents import runtime
from searchos.agents.orchestrator.scheduler import Scheduler, retire_stale_worker
from searchos.harness.middleware.extraction import (
    DeliveryMode,
    EvidenceObservation,
    ExtractionConfig,
    ExtractionCoordinator,
    WorkspaceEvidenceStore,
)
from searchos.harness.middleware.extraction.core.findings import extract_and_commit
from searchos.harness.middleware.models import TaskContext
from searchos.harness.p1_runtime import begin_run, execution_context
from searchos.socm import FrontierTask, FrontierTaskStatus, SearchState
from searchos.socm.revisions import StaleExecutionError
from searchos.socm.workspace import WorkspaceManager
from searchos.tools import tasks


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Fixed response tests must not call network or model services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def setup_workspace(tmp_path, *, table):
    ws = WorkspaceManager(tmp_path, "late-response")
    ws.create()
    state = SearchState(intent="Collect supported leadership evidence", research_mode="findings")
    state.p1.enabled = True
    if table:
        state.coverage_map.initialize(
            entities=[ENTITY],
            attributes=["Organization", "Leader"],
            table_id=TABLE,
            primary_key=["Organization"],
        )
    state.frontier.add(
        FrontierTask(
            id="task",
            question=state.intent,
            assigned_agent_id="worker",
            status=FrontierTaskStatus.RUNNING,
            result_mode="table" if table else "findings",
            table_id=TABLE if table else "",
        )
    )
    ws.save_state(state)
    begin_run(ws, "task", "old")
    return ws


def invalidate(ws, *, reassign=False):
    def update(state):
        state.p1.executions["old"].invalidated = True
        if reassign:
            state.frontier.questions[0].assigned_agent_id = "new-worker"
        return state

    ws.atomic_update_state(update)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["task_rows", "schema_facts"])
@pytest.mark.parametrize("late", [False, True])
async def test_table_coordinator_atomic_guard_preserves_socm(tmp_path, policy, late):
    ws = setup_workspace(tmp_path, table=True)
    store = WorkspaceEvidenceStore(ws)
    base = FixedFixtureModel(store)
    baseline = ws.load_state()

    class Model:
        async def ainvoke(self, prompt):
            response = await base.ainvoke(prompt)
            if late:
                invalidate(ws)
            return response

    coordinator = ExtractionCoordinator(
        Model(),
        store=store,
        policy=policy,
        task=TaskContext("worker", "task", TABLE, baseline.intent),
        config=ExtractionConfig(dual_mode=False, cross_table_backfill=False),
    )
    fixture = FIXTURES[0]
    with execution_context("old"):
        receipt = await coordinator.submit(
            EvidenceObservation(
                content=fixture.excerpt,
                source_url=fixture.source,
                target_table=TABLE,
                task_id="task",
            ),
            delivery=DeliveryMode.SYNC,
        )
    current = ws.load_state()
    assert base.calls == ["row_extraction"]
    if late:
        assert receipt.reason == "extraction_failed"
        assert not receipt.committed_node_ids
        assert not coordinator.committed_node_ids
        assert current.evidence_graph == baseline.evidence_graph
        assert current.coverage_map == baseline.coverage_map
        assert current.frontier == baseline.frontier
    else:
        assert len(receipt.committed_node_ids) == 1
        assert len(current.evidence_graph.nodes) == 1
        assert current.evidence_graph.task_bindings[0].task_id == "task"


@pytest.mark.asyncio
async def test_background_extraction_inherits_old_ticket_after_caller_exits(tmp_path):
    ws = setup_workspace(tmp_path, table=True)
    store = WorkspaceEvidenceStore(ws)
    base = FixedFixtureModel(store)
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedModel:
        async def ainvoke(self, prompt):
            response = await base.ainvoke(prompt)
            started.set()
            await release.wait()
            return response

    coordinator = ExtractionCoordinator(
        DelayedModel(),
        store=store,
        batch_n=1,
        task=TaskContext("worker", "task", TABLE, "Collect evidence"),
        config=ExtractionConfig(dual_mode=False, cross_table_backfill=False),
    )
    fixture = FIXTURES[0]
    with execution_context("old"):
        await coordinator.submit(
            EvidenceObservation(
                content=fixture.excerpt,
                source_url=fixture.source,
                target_table=TABLE,
                task_id="task",
            ),
            delivery=DeliveryMode.BUFFERED,
        )
        await asyncio.wait_for(started.wait(), timeout=2)
    invalidate(ws)
    release.set()
    await coordinator.await_idle(timeout=2)
    assert not coordinator.committed_node_ids
    assert not ws.load_state().evidence_graph.nodes
    assert not ws.load_state().evidence_graph.task_bindings


@pytest.mark.asyncio
async def test_stale_summary_cannot_enter_pre_schema_stash(tmp_path):
    ws = setup_workspace(tmp_path, table=False)
    state = ws.load_state()
    state.frontier.questions[0].result_mode = "table"
    ws.save_state(state)
    coordinator = ExtractionCoordinator(
        None,
        workspace=ws,
        task=TaskContext("worker", "task", "", "Collect evidence"),
    )
    invalidate(ws)
    with execution_context("old"):
        receipt = await coordinator.submit(
            EvidenceObservation(
                content="Obsolete agent summary with enough text for table extraction admission.",
                source_url="agent://worker/final_summary",
                task_id="task",
                result_mode="table",
            ),
            delivery=DeliveryMode.SYNC,
        )
    assert receipt.reason == "extraction_failed"
    assert ws.load_state().pending_agent_summaries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("via_coordinator", [False, True])
async def test_findings_late_support_verdict_cannot_publish(tmp_path, via_coordinator):
    ws = setup_workspace(tmp_path, table=False)
    store = WorkspaceEvidenceStore(ws)
    baseline = ws.load_state()
    fixture = FIXTURES[0]
    calls = []

    async def invoke(prompt):
        if prompt.startswith("从给定原文抽取"):
            calls.append("extract")
            return json.dumps(
                [
                    {
                        "finding": fixture.excerpt,
                        "source_excerpt": fixture.excerpt,
                        "relation": "supports",
                    }
                ]
            )
        assert prompt.startswith("核验每项事实")
        calls.append("verify")
        invalidate(ws)
        return '[{"id":"0","supported":true}]'

    with execution_context("old"):
        if via_coordinator:

            class Model:
                async def ainvoke(self, prompt):
                    return SimpleNamespace(content=await invoke(prompt))

            coordinator = ExtractionCoordinator(
                Model(), store=store, task=TaskContext("worker", "task", "", baseline.intent)
            )
            receipt = await coordinator.submit(
                EvidenceObservation(
                    content=fixture.excerpt,
                    source_url=fixture.source,
                    task_id="task",
                    result_mode="findings",
                ),
                delivery=DeliveryMode.SYNC,
            )
            assert receipt.reason == "extraction_failed"
            assert not receipt.committed_node_ids
        else:
            with pytest.raises(StaleExecutionError):
                await extract_and_commit(
                    [
                        {
                            "source_url": fixture.source,
                            "content": fixture.excerpt,
                            "task_id": "task",
                        }
                    ],
                    store=store,
                    invoke=invoke,
                )
    current = ws.load_state()
    assert calls == ["extract", "verify"]
    assert current.evidence_graph == baseline.evidence_graph
    assert current.coverage_map == baseline.coverage_map
    assert current.frontier == baseline.frontier


@pytest.mark.asyncio
async def test_stale_zombie_is_released_without_breaking_scheduler(tmp_path):
    ws = setup_workspace(tmp_path, table=False)
    invalidate(ws)
    state = ws.load_state()
    state.frontier.questions[0].updated_at = 1
    ws.save_state(state)
    runtime.set_orchestrator_context(workspace=ws, model=object())
    runtime._ctx.agent_graphs["worker"] = {
        "assigned_task_id": "task",
        "p1_run_id": "old",
        "agent_type": "search_agent",
    }
    result = await Scheduler().reap_zombie_tasks()
    task = ws.load_state().frontier.questions[0]
    assert result["reaped"] == 1
    assert task.status == FrontierTaskStatus.CANCELLED
    assert task.assigned_agent_id == ""
    assert task.resolution.startswith("stale_execution_review_required:")
    assert (await Scheduler().reap_zombie_tasks())["reaped"] == 0


def test_stale_reaper_cannot_modify_revision_requeued_or_new_worker(tmp_path):
    ws = setup_workspace(tmp_path, table=False)
    invalidate(ws)
    for status, owner in [
        (FrontierTaskStatus.PENDING, ""),
        (FrontierTaskStatus.BLOCKED, ""),
        (FrontierTaskStatus.RUNNING, "new-worker"),
    ]:
        state = ws.load_state()
        state.frontier.questions[0].status = status
        state.frontier.questions[0].assigned_agent_id = owner
        ws.save_state(state)
        before = ws.load_state().model_dump()
        assert not retire_stale_worker(ws, "task", "worker", "old")
        assert ws.load_state().model_dump() == before


@pytest.mark.asyncio
async def test_check_agents_consumes_stale_exception_and_releases_slot(tmp_path, monkeypatch):
    ws = setup_workspace(tmp_path, table=False)
    invalidate(ws)
    runtime.set_orchestrator_context(workspace=ws, model=object())
    runtime._ctx.agent_graphs["worker"] = {
        "assigned_task_id": "task",
        "p1_run_id": "old",
        "agent_type": "search_agent",
    }

    async def stale():
        raise StaleExecutionError("response arrived after revision")

    done = asyncio.create_task(stale())
    await asyncio.sleep(0)
    runtime._ctx.task_pool["worker"] = done
    monkeypatch.setattr(
        tasks, "_scheduler", lambda: SimpleNamespace(tick=AsyncMock(return_value={}))
    )
    result = json.loads(await tasks.check_agents.ainvoke({}))
    assert result["reports"][0]["status"] == "superseded"
    assert result["remaining"] == 0
    assert "worker" not in runtime._ctx.task_pool
    assert ws.load_state().frontier.questions[0].status == FrontierTaskStatus.CANCELLED
