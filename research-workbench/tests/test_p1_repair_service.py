"""Production workspace transactions and scheduled repair, with no network."""

import json
import socket
from pathlib import Path

import pytest

from scripts.try_p1_repair import build_baseline, reviewed_event, run_demo, seed_workspace
from searchos.agents.runtime import set_orchestrator_context
from searchos.harness.p1_repair import confirm_revision, preview_revision
from searchos.socm import FrontierTask, FrontierTaskStatus, OutlineSection
from searchos.socm.revisions import (
    artifact_usable,
    begin_execution,
    evidence_usable,
    evidence_version,
    publish_artifact,
    record_reads,
    snapshot_artifacts,
    snapshot_evidence,
)
from searchos.socm.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("P1 repair fixtures must not contact network or local model services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.asyncio
async def test_scheduled_repair_runs_real_reads_writer_lifecycle_and_report_guards(tmp_path):
    result = await run_demo(tmp_path)
    assert result["passed"], result["checks"]
    assert len(result["checks"]) >= 16
    assert json.loads(Path(result["result_file"]).read_text()) == result
    ws_path = Path(result["workspace"])
    reloaded = WorkspaceManager(ws_path.parent, ws_path.name).load_state()
    assert not evidence_usable(reloaded, "old")
    assert evidence_usable(reloaded, "old", "archive")
    assert artifact_usable(reloaded, "report:answer")
    assert "Bob" in Path(result["report_file"]).read_text()
    assert "Alice" not in Path(result["report_file"]).read_text()
    assert [item["task_id"] for item in result["dispatches"]][:2] == ["leader", "writer"]
    assert reloaded.p1.executions["repair-run-1"].read_evidence.keys() == {"new", "backup"}
    ticks = result["scheduler_ticks"]
    assert ticks[0]["maybe_spawn_writer"]["action"] == "deferred_repair_dependencies"
    assert ticks[1]["maybe_spawn_writer"]["p1_repair"] is True
    assert ticks[1]["maybe_spawn_writer"]["write_task_id"] == "writer"
    assert all(tick["maybe_continue_writer"]["action"] == "disabled_targeted_repair"
               for tick in ticks)


@pytest.mark.asyncio
async def test_demo_uses_new_workspace_and_keeps_previous_user_content(tmp_path):
    old = tmp_path / "user-workspace"
    old.mkdir()
    marker = old / "notes.txt"
    marker.write_text("Keep this research.")
    first = await run_demo(tmp_path)
    before = Path(first["state_file"]).read_bytes()
    second = await run_demo(tmp_path)
    assert first["passed"] and second["passed"]
    assert first["workspace"] != second["workspace"]
    assert Path(first["state_file"]).read_bytes() == before
    assert marker.read_text() == "Keep this research."


@pytest.mark.asyncio
async def test_missing_producer_rejects_confirmation_without_partial_state_or_views(tmp_path):
    ws = seed_workspace(tmp_path)
    set_orchestrator_context(ws, model=None)
    await build_baseline(ws)

    def remove_producer(state):
        state.frontier.questions = [task for task in state.frontier.questions
                                    if task.id != "leader"]
        return state

    ws.atomic_update_state(remove_producer)
    event = reviewed_event(ws)
    before = {name: (ws.path / name).read_bytes() for name in (
        "search_state.json", "output/draft.md", "output/report.md", "output/result.json",
    )}
    with pytest.raises(ValueError, match="missing"):
        confirm_revision(ws, event)
    assert all((ws.path / name).read_bytes() == content for name, content in before.items())
    assert not ws.load_state().p1.events


@pytest.mark.asyncio
async def test_confirmation_rejects_reviewed_versions_changed_after_preview(tmp_path):
    ws = seed_workspace(tmp_path)
    set_orchestrator_context(ws, model=None)
    await build_baseline(ws)
    event = reviewed_event(ws)
    preview_revision(ws, event)

    def change_source(state):
        state.evidence_graph.observations[0].text += " Newly changed source window."
        return state

    ws.atomic_update_state(change_source)
    before = ws.path.joinpath("search_state.json").read_bytes()
    with pytest.raises(ValueError, match="changed"):
        confirm_revision(ws, event)
    assert ws.path.joinpath("search_state.json").read_bytes() == before
    assert ws.load_state().p1.retired_uses == {}


@pytest.mark.asyncio
async def test_retirement_for_another_use_does_not_queue_or_hide_answer_outputs(tmp_path):
    ws = seed_workspace(tmp_path)
    set_orchestrator_context(ws, model=None)
    await build_baseline(ws)
    event = reviewed_event(ws).model_copy(update={"scope_key": "archive"})
    before = ws.load_state().p1.artifacts["report:answer"].model_dump(mode="json")
    report_bytes = (ws.path / "output" / "report.md").read_bytes()
    result = confirm_revision(ws, event)
    after = ws.load_state()
    assert result["queued_task_ids"] == []
    assert result["impact"]["affected_artifact_ids"] == []
    assert after.p1.artifacts["report:answer"].model_dump(mode="json") == before
    assert artifact_usable(after, "report:answer")
    assert evidence_usable(after, "old") and not evidence_usable(after, "old", "archive")
    assert (ws.path / "output" / "report.md").read_bytes() == report_bytes


def test_unconfirmed_service_input_does_not_write_state(tmp_path):
    ws = seed_workspace(tmp_path)
    event = reviewed_event(ws).model_copy(update={"confirmed": False})
    before = ws.path.joinpath("search_state.json").read_bytes()
    with pytest.raises(ValueError, match="confirmation"):
        preview_revision(ws, event)
    with pytest.raises(ValueError, match="confirmation"):
        confirm_revision(ws, event)
    assert ws.path.joinpath("search_state.json").read_bytes() == before


@pytest.mark.parametrize("legacy_output", ["section", "report"])
def test_untracked_legacy_outputs_reject_revision_without_any_writes(tmp_path, legacy_output):
    ws = seed_workspace(tmp_path)
    if legacy_output == "section":
        def legacy_section(state):
            state.outline.sections.append(
                OutlineSection(id="legacy", content="Legacy Alice answer."),
            )
            return state

        ws.atomic_update_state(legacy_section)
        ws.write_output("draft.md", "Legacy Alice answer.")
    else:
        ws.write_output("report.md", "Legacy Alice report.")
    event = reviewed_event(ws)
    before = {path.relative_to(ws.path): path.read_bytes() for path in ws.path.rglob("*")
              if path.is_file() and path.name != ".state.lock"}
    for invoke in (preview_revision, confirm_revision):
        with pytest.raises(ValueError, match="untracked"):
            invoke(ws, event)
        after = {path.relative_to(ws.path): path.read_bytes() for path in ws.path.rglob("*")
                 if path.is_file() and path.name != ".state.lock"}
        assert after == before


def _save_artifact(ws, task_id, run_id, artifact_id, *, evidence=(), parents=()):
    """Real tickets/publish for a task that has multiple output artifacts."""
    def commit(state):
        begin_execution(state, task_id=task_id, run_id=run_id)
        record_reads(state, run_id, evidence=snapshot_evidence(state, evidence),
                     artifacts=snapshot_artifacts(state, parents))
        publish_artifact(state, run_id=run_id, artifact_id=artifact_id,
                         kind="section", content="Synthetic scoped result")
        state.p1.executions[run_id].closed = True
        next(t for t in state.frontier.questions if t.id == task_id).status = (
            FrontierTaskStatus.COMPLETED
        )
        return state

    ws.atomic_update_state(commit)


def test_acyclic_artifacts_that_form_producer_task_cycle_reject_confirmation_atomically(tmp_path):
    ws = seed_workspace(tmp_path)

    def tasks(state):
        state.frontier.questions += [FrontierTask(id="A", question="A"),
                                     FrontierTask(id="B", question="B")]
        return state

    ws.atomic_update_state(tasks)
    _save_artifact(ws, "A", "a1", "section:a1", evidence=["old"])
    _save_artifact(ws, "B", "b1", "section:b1", parents=["section:a1"])
    _save_artifact(ws, "A", "a2", "section:a2", parents=["section:b1"])
    # a1 -> b1 -> a2 is a valid artifact DAG. Grouping a1 and a2 into A
    # must not silently commit A -> B -> A to the production task scheduler.
    event = reviewed_event(ws)
    before = (ws.path / "search_state.json").read_bytes()
    for invoke in (preview_revision, confirm_revision):
        with pytest.raises(ValueError, match="cycle"):
            invoke(ws, event)
        assert (ws.path / "search_state.json").read_bytes() == before


def test_later_revision_replaces_earlier_generated_dependencies_without_creating_cycle(tmp_path):
    ws = seed_workspace(tmp_path)

    def tasks(state):
        state.frontier.questions += [FrontierTask(id="A", question="A"),
                                     FrontierTask(id="B", question="B")]
        return state

    ws.atomic_update_state(tasks)
    _save_artifact(ws, "A", "a1", "section:a", evidence=["old"])
    _save_artifact(ws, "B", "b1", "section:b", parents=["section:a"])
    first = confirm_revision(ws, reviewed_event(ws))
    assert first["queued_task_ids"] == ["A", "B"]
    initial_tasks = {task.id: task for task in ws.load_state().frontier.questions}
    assert initial_tasks["B"].blocked_by == ["A"]

    # Revalidation finds direct replacement evidence for B; A then consumes B.
    # This is a new, legitimate artifact DAG with the dependency reversed.
    _save_artifact(ws, "B", "b2", "section:b", evidence=["new"])
    _save_artifact(ws, "A", "a2", "section:a", parents=["section:b"])
    snapshot = ws.load_state()
    second_event = reviewed_event(ws, event_id="second").model_copy(update={
        "old_evidence_id": "new", "new_evidence_id": "backup",
        "expected_old_version": evidence_version(snapshot, "new"),
        "expected_new_version": evidence_version(snapshot, "backup"),
    })
    second = confirm_revision(ws, second_event)
    assert second["queued_task_ids"] == ["A", "B"]
    current_tasks = {task.id: task for task in ws.load_state().frontier.questions}
    assert current_tasks["A"].blocked_by == ["B"]
    assert current_tasks["B"].blocked_by == []
    assert current_tasks["A"].p1_requires_artifacts == ["section:b"]
    assert current_tasks["B"].p1_requires_artifacts == []
