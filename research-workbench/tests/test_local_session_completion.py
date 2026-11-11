"""Local enhancement sessions must wait for their actual work and report failure."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from searchos.config.run_config import EffectiveRunConfig
from searchos.config.settings import settings
from searchos.harness.session import SearchSession, _wait_for_agent_tasks
from searchos.socm import FrontierTask, FrontierTaskStatus, SearchState


@pytest.mark.asyncio
async def test_wait_uses_remaining_budget_and_does_not_cancel_workers(monkeypatch):
    from searchos.harness import session

    worker = asyncio.create_task(asyncio.sleep(10))
    observed = []

    async def timeout(tasks, *, timeout, return_when):
        observed.append(timeout)
        return set(), set(tasks)

    monkeypatch.setattr(session.time, "monotonic", lambda: 289.0)
    monkeypatch.setattr(session.asyncio, "wait", timeout)
    try:
        assert not await _wait_for_agent_tasks({"reader": worker}, 480.0)
        assert observed == [191.0]
        assert not worker.cancelled() and not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_unlimited_wait_finishes_only_after_workers_commit():
    committed = []

    async def worker():
        await asyncio.sleep(0)
        committed.append("evidence committed")

    assert await _wait_for_agent_tasks({"reader": asyncio.create_task(worker())}, None)
    assert committed == ["evidence committed"]


def session_fixture(monkeypatch, tmp_path, graph_factory, *, max_time=5):
    from searchos.config import models
    from searchos.harness.middleware import pipelines

    monkeypatch.setattr(models, "get_model_for", lambda role: object())
    monkeypatch.setattr(settings, "enable_skills", False)
    monkeypatch.setattr(settings, "enable_access_skill_generation", False)
    monkeypatch.setattr(pipelines, "orchestrator_pipeline", graph_factory)
    config = EffectiveRunConfig.from_settings().with_modules({"enabled": ["context_memory"]})
    session = SearchSession(
        workspace_root=str(tmp_path), run_config=config, max_time_s=max_time,
        skill_library_path=str(tmp_path / "skills"),
    )
    state = SearchState(source_scope={"mode": "local", "documents": []}, research_mode="findings")
    state.frontier.add(FrontierTask(
        id="read-notices", question="Read both dated notices", status=FrontierTaskStatus.RUNNING,
        agent_type="local_search_agent", assigned_agent_id="reader",
    ))
    return session, state


@pytest.mark.asyncio
async def test_plaintext_end_waits_for_worker_then_collects_fresh_answer(monkeypatch, tmp_path):
    calls = []
    committed = asyncio.Event()

    def graph_factory(**kwargs):
        workspace = kwargs["workspace"]

        async def worker():
            await asyncio.sleep(0.02)

            def update(state):
                state.frontier.questions[0].status = FrontierTaskStatus.COMPLETED
                return state

            workspace.atomic_update_state(update)
            committed.set()

        class Graph:
            middleware = []

            async def astream(self, payload, **unused):
                from searchos.agents.runtime import _ctx

                calls.append(payload)
                if len(calls) == 1:
                    _ctx.task_pool["reader"] = asyncio.create_task(worker())
                    yield "values", {"messages": [AIMessage(content="I will wait for the reader.")]}
                else:
                    assert committed.is_set(), "No extra model turn while the worker is reading"
                    assert payload["messages"][0]["additional_kwargs"]["sf_synthetic"]
                    yield "values", {"messages": [
                        AIMessage(content="I will wait for the reader."),
                        AIMessage(content="The updated notice names the new director."),
                    ]}

        return Graph()

    session, state = session_fixture(monkeypatch, tmp_path, graph_factory)
    result = await session.run("Compare notices", initial_state=state, session_id="wait-real-work")
    assert len(calls) == 2
    assert result.execution_error == ""
    assert result.search_state.frontier.questions[0].status == FrontierTaskStatus.COMPLETED
    report = json.loads((tmp_path / "wait-real-work/output/result.json").read_text())
    assert report["answer"] == "The updated notice names the new director."


@pytest.mark.asyncio
async def test_deadline_persists_unfinished_task_and_execution_error(monkeypatch, tmp_path):
    from searchos.harness.report import synthesis

    cancelled = asyncio.Event()

    async def finalizer(**kwargs):
        assert kwargs["state"].frontier.questions[0].status == FrontierTaskStatus.CANCELLED
        return "The reading task did not finish within the research budget."

    monkeypatch.setattr(synthesis, "finalize_research", finalizer)

    def graph_factory(**kwargs):
        async def worker():
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        class Graph:
            middleware = []

            async def astream(self, payload, **unused):
                from searchos.agents.runtime import _ctx

                _ctx.task_pool["reader"] = asyncio.create_task(worker())
                yield "values", {"messages": [AIMessage(content="I will wait.")]}

        return Graph()

    session, state = session_fixture(monkeypatch, tmp_path, graph_factory, max_time=1)
    result = await session.run("Compare notices", initial_state=state, session_id="unfinished")
    assert cancelled.is_set()
    assert result.execution_error == "IncompleteResearch"
    task = result.search_state.frontier.questions[0]
    assert task.status == FrontierTaskStatus.CANCELLED
    assert "time_budget_exhausted" in task.resolution
    report = json.loads((tmp_path / "unfinished/output/result.json").read_text())
    assert report["execution_error"] == "IncompleteResearch"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "empty"])
async def test_final_generation_failure_reaches_session_result(monkeypatch, tmp_path, failure):
    from searchos.harness.report import synthesis

    attempts = []

    async def finalizer(**kwargs):
        attempts.append(kwargs)
        if failure == "timeout":
            raise TimeoutError("Synthetic finalization timeout")
        return ""

    monkeypatch.setattr(synthesis, "finalize_research", finalizer)

    def graph_factory(**kwargs):
        class Graph:
            middleware = []

            async def astream(self, payload, **unused):
                kwargs["control"].stop("test_budget")
                yield "values", {"messages": [AIMessage(content="")]}

        return Graph()

    session, state = session_fixture(monkeypatch, tmp_path, graph_factory)
    state.frontier.questions[0].status = FrontierTaskStatus.COMPLETED
    result = await session.run("Compare notices", initial_state=state, session_id=failure)
    expected = "TimeoutError" if failure == "timeout" else "EmptyFinalAnswer"
    assert result.execution_error == expected
    assert len(attempts) == (2 if failure == "timeout" else 1)
    report = json.loads((tmp_path / f"{failure}/output/result.json").read_text())
    assert report["execution_error"] == expected
    assert report["answer"] == "研究执行中断，尚未生成回答。"
