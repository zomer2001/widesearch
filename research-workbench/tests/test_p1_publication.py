"""Real finalization/publication boundaries with in-process scripted responses."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from searchos.config.settings import settings
from searchos.harness.p1_repair import confirm_revision
from searchos.harness.p1_reports import publish_report
from searchos.harness.p1_runtime import current_run_id, execution_context
from searchos.harness.session import _finalize_stopped_research, _harness_synthesize
from searchos.socm import EvidenceNode, FrontierTask, OutlineSection, SearchState
from searchos.socm.claim_annotations import EvidenceClaimAnnotation, evidence_fingerprint
from searchos.socm.evidence import SourceObservation
from searchos.socm.revision_state import ConfirmedEvidenceRevision
from searchos.socm.revisions import (
    StaleExecutionError,
    begin_execution,
    evidence_version,
    publish_artifact,
    record_reads,
    snapshot_evidence,
)
from searchos.socm.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", False)
    ws = WorkspaceManager(tmp_path, "publication")
    ws.create()
    state = SearchState(intent="Who leads Acme?")
    state.coverage_map.add_table("t", ["CEO"], entities=["Acme"])
    state.p1.enabled = True
    state.frontier.questions.append(
        FrontierTask(id="writer", question="Write answer", kind="write")
    )
    for eid, value in (("old", "Alice"), ("new", "Bob")):
        excerpt = f"Acme head office CEO is {value}."
        source = f"https://example.invalid/{eid}"
        observation = SourceObservation.capture(source, excerpt)
        state.evidence_graph.observations.append(observation)
        node = EvidenceNode(
            id=eid,
            table_id="t",
            entity="Acme",
            attribute="CEO",
            value=value,
            finding=excerpt,
            source=source,
            source_excerpt=excerpt,
            observation_id=observation.id,
            span=(0, len(excerpt)),
            text_hash="sha256:" + hashlib.sha256(excerpt.encode()).hexdigest(),
        )
        state.evidence_graph.add_node(node)
        state.coverage_map.fill_from_evidence([node])
        state.evidence_graph.claim_annotations[eid] = EvidenceClaimAnnotation(
            evidence_id=eid,
            evidence_fingerprint=evidence_fingerprint(node),
            entity="Acme",
            attribute="CEO",
            value=value,
            scope_key="head office CEO",
            verified=True,
        )
    ws.save_state(state)
    ws.write_output("report.md", "previous report")
    ws.write_output("result.json", '{"answer":"previous report"}')
    return ws


def _outputs(ws):
    return (ws.path / "output" / "report.md").read_text(), (
        ws.path / "output" / "result.json"
    ).read_text()


def _revision(state):
    return ConfirmedEvidenceRevision(
        id="revision",
        old_evidence_id="old",
        new_evidence_id="new",
        confirmed=True,
        reason="Reviewed replacement for this answer",
        expected_old_version=evidence_version(state, "old"),
        expected_new_version=evidence_version(state, "new"),
    )


class FixedResponse:
    """Only records the real prompt and runs a synchronous local mutation."""

    def __init__(self, during=None):
        self.during = during
        self.payloads = []

    async def ainvoke(self, messages):
        self.payloads.append(json.loads(messages[-1].content))
        if self.during is not None:
            self.during()
        return AIMessage(content="Alice [source](evidence:old).")


@pytest.mark.parametrize("enablement", ["persisted", "setting", "stale_snapshot"])
def test_enabled_publication_requires_a_generation_ticket(workspace, monkeypatch, enablement):
    snapshot = workspace.load_state()
    if enablement == "setting":
        snapshot.p1.enabled = False
        workspace.save_state(snapshot)
        monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", True)
    elif enablement == "stale_snapshot":
        snapshot.p1.enabled = False
    before = _outputs(workspace)
    with pytest.raises(StaleExecutionError):
        publish_report(workspace, snapshot, "untracked report", {"answer": "untracked report"})
    assert _outputs(workspace) == before
    assert "report:answer" not in workspace.load_state().p1.artifacts


def test_default_off_publication_retains_write_only_workspace_compatibility(monkeypatch):
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", False)
    outputs = {}
    ws = SimpleNamespace(write_output=outputs.__setitem__)
    state = SearchState()
    publish_report(ws, state, "plain report", {"answer": "plain report"})
    assert outputs["report.md"] == "plain report"
    assert json.loads(outputs["result.json"]) == {"answer": "plain report"}
    assert not state.p1.artifacts


@pytest.mark.parametrize("change", ["evidence", "observation", "scope", "revision"])
@pytest.mark.asyncio
async def test_changes_while_finalizer_generates_cannot_publish(workspace, change):
    if change == "revision":
        state = workspace.load_state()
        begin_execution(state, task_id="__answer__", run_id="baseline")
        publish_artifact(
            state,
            run_id="baseline",
            artifact_id="report:answer",
            kind="report",
            content="previous report",
        )
        state.p1.executions["baseline"].closed = True
        workspace.save_state(state)
    previous_artifact = workspace.load_state().p1.artifacts.get("report:answer")
    before = _outputs(workspace)

    def during():
        if change == "revision":
            confirm_revision(workspace, _revision(workspace.load_state()))
            return
        state = workspace.load_state()
        if change == "evidence":
            state.evidence_graph.get_node("old").finding = "changed assertion"
        elif change == "observation":
            state.evidence_graph.observations[0].text += " changed reading window"
        else:
            state.source_scope = {"mode": "web"}
        workspace.save_state(state)

    response = FixedResponse(during)
    with pytest.raises(StaleExecutionError):
        await _finalize_stopped_research(
            workspace=workspace,
            state=workspace.load_state(),
            query="Who leads Acme?",
            orchestrator_model=response,
            stop_reason="test stop",
        )
    assert len(response.payloads) == 1
    assert _outputs(workspace) == before
    assert workspace.load_state().p1.artifacts.get("report:answer") == previous_artifact
    assert current_run_id.get() == ""


@pytest.mark.asyncio
async def test_finalization_records_only_admitted_prompt_records(workspace, monkeypatch):
    monkeypatch.setattr(settings, "research_finalize_max_chars", 2000)
    state = workspace.load_state()
    state.evidence_graph.add_node(
        EvidenceNode(
            id="omitted",
            finding="large " * 1000,
            source_excerpt="large " * 1000,
            source="https://example.invalid/omitted",
        )
    )
    workspace.save_state(state)
    expected = {eid: evidence_version(state, eid) for eid in ("old", "new")}

    def change_omitted_record():
        current = workspace.load_state()
        current.evidence_graph.get_node("omitted").finding = "Updated unread material"
        workspace.save_state(current)

    response = FixedResponse(change_omitted_record)
    await _finalize_stopped_research(
        workspace=workspace,
        state=state,
        query="Who leads Acme?",
        orchestrator_model=response,
        stop_reason="test stop",
    )
    assert response.payloads[0]["omitted_evidence"] == 1
    assert {row["id"] for row in response.payloads[0]["evidence"]} == set(expected)
    artifact = workspace.load_state().p1.artifacts["report:answer"]
    assert artifact.read_evidence == expected
    report, result = _outputs(workspace)
    assert report == artifact.content and "https://example.invalid/old" in report
    assert json.loads(result)["p1_report_version"] == artifact.version


def test_publication_rejects_snapshot_that_changed_before_registration(workspace):
    state = workspace.load_state()
    begin_execution(state, task_id="__answer__", run_id="publish")
    workspace.save_state(state)
    delivered_snapshot = workspace.load_state()
    state.evidence_graph.observations[0].text += " changed"
    workspace.save_state(state)
    before = _outputs(workspace)
    with execution_context("publish"), pytest.raises(StaleExecutionError):
        publish_report(
            workspace,
            delivered_snapshot,
            "old result",
            {"answer": "old result"},
            evidence_ids=["old"],
        )
    assert _outputs(workspace) == before


def _section(ws, content="versioned section"):
    state = ws.load_state()
    begin_execution(state, task_id="writer", run_id="writer-run")
    record_reads(state, "writer-run", evidence=snapshot_evidence(state, ["old"]), artifacts={})
    artifact = publish_artifact(
        state,
        run_id="writer-run",
        artifact_id="section:summary",
        kind="section",
        content=content,
        support_sets=[["old"]],
    )
    state.outline.sections.append(
        OutlineSection(
            id="summary",
            content=content,
            cited_evidence_ids=["old"],
            p1_epoch=artifact.epoch,
        )
    )
    state.p1.executions["writer-run"].closed = True
    ws.save_state(state)
    return artifact


@pytest.mark.asyncio
async def test_fallback_report_records_the_exact_section_publication(workspace, monkeypatch):
    from searchos.harness import session

    section = _section(workspace)

    async def no_writer(**kwargs):
        return None

    monkeypatch.setattr(session, "_run_writer_finalize", no_writer)
    await _harness_synthesize(workspace=workspace, state=workspace.load_state(), query="Who?")
    artifact = workspace.load_state().p1.artifacts["report:answer"]
    assert artifact.read_artifacts == {"section:summary": section.version}
    assert "versioned section" in artifact.content
    assert artifact.content == _outputs(workspace)[0]


@pytest.mark.asyncio
async def test_fallback_does_not_publish_outline_body_different_from_its_version(
    workspace, monkeypatch
):
    from searchos.harness import session

    _section(workspace)
    state = workspace.load_state()
    state.outline.find("summary").content = "UNVERSIONED_UNSUPPORTED_BODY"
    workspace.save_state(state)

    async def no_writer(**kwargs):
        return None

    monkeypatch.setattr(session, "_run_writer_finalize", no_writer)
    try:
        await _harness_synthesize(workspace=workspace, state=state, query="Who?")
    except StaleExecutionError:
        pass
    assert "UNVERSIONED_UNSUPPORTED_BODY" not in _outputs(workspace)[0]
    report = workspace.load_state().p1.artifacts.get("report:answer")
    assert report is None or "UNVERSIONED_UNSUPPORTED_BODY" not in report.content


class FixedWriterGraph:
    def __init__(self, during=None):
        self.during = during
        self.calls = []

    async def astream(self, inputs, **kwargs):
        self.calls.append({"run_id": current_run_id.get(), "input": inputs})
        if self.during:
            self.during()
        yield "values", {"messages": [AIMessage(content="Fresh writer body")]}


def _writer_context(ws, graph, *, tracked=True):
    from searchos.agents.runtime import _ctx, set_orchestrator_context
    from searchos.socm import FrontierTaskStatus

    state = ws.load_state()
    task = next(task for task in state.frontier.questions if task.id == "writer")
    task.assigned_agent_id = "writer_agent_test"
    task.status = FrontierTaskStatus.COMPLETED
    if tracked:
        begin_execution(state, task_id="writer", run_id="writer-previous")
        record_reads(
            state, "writer-previous", evidence=snapshot_evidence(state, ["old"]), artifacts={}
        )
        publish_artifact(
            state,
            run_id="writer-previous",
            artifact_id="task:writer",
            kind="task",
            content="Previous writer body",
        )
        state.p1.executions["writer-previous"].closed = True
    ws.save_state(state)
    set_orchestrator_context(workspace=ws, model=object())
    _ctx.agent_graphs["writer_agent_test"] = {
        "agent_type": "writer_agent",
        "thread_id": "writer_agent_test",
        "assigned_task_id": "writer",
        "graph": graph,
        "started_at": 0,
        "p1_run_id": "writer-previous" if tracked else "",
    }


@pytest.mark.asyncio
async def test_writer_finalizer_uses_its_own_ticket_then_publishes_versioned_task(workspace):
    graph = FixedWriterGraph()
    _writer_context(workspace, graph)
    await _harness_synthesize(workspace=workspace, state=workspace.load_state(), query="Who?")
    state = workspace.load_state()
    task = state.p1.artifacts["task:writer"]
    report = state.p1.artifacts["report:answer"]
    assert len(graph.calls) == 1
    writer_run = state.p1.executions[graph.calls[0]["run_id"]]
    assert writer_run.task_id == "writer" and writer_run.closed
    assert graph.calls[0]["run_id"] != "writer-previous"
    assert set(task.read_evidence) == {"old", "new"}
    assert task.content == "Fresh writer body" and task.version == 2
    assert report.read_artifacts == {"task:writer": task.version}
    assert "Fresh writer body" in report.content
    assert current_run_id.get() == ""


@pytest.mark.asyncio
async def test_change_during_writer_finalization_cannot_publish_task_or_report(workspace):
    def mutate():
        state = workspace.load_state()
        state.evidence_graph.observations[0].text += " changed while writing"
        workspace.save_state(state)

    graph = FixedWriterGraph(mutate)
    _writer_context(workspace, graph)
    before = _outputs(workspace)
    with pytest.raises(StaleExecutionError):
        await _harness_synthesize(workspace=workspace, state=workspace.load_state(), query="Who?")
    state = workspace.load_state()
    assert state.p1.artifacts["task:writer"].version == 1
    assert "report:answer" not in state.p1.artifacts
    assert _outputs(workspace) == before


@pytest.mark.parametrize("old_history", ["changed", "untracked"])
@pytest.mark.asyncio
async def test_unusable_writer_history_falls_back_without_reentering_old_graph(
    workspace, old_history
):
    graph = FixedWriterGraph()
    _writer_context(workspace, graph, tracked=old_history != "untracked")
    if old_history == "changed":
        state = workspace.load_state()
        state.evidence_graph.observations[0].text += " changed before finalization"
        workspace.save_state(state)
    await _harness_synthesize(workspace=workspace, state=workspace.load_state(), query="Who?")
    assert not graph.calls
    assert json.loads(_outputs(workspace)[1])["mode"] == "structured_only"
    assert "Fresh writer body" not in _outputs(workspace)[0]


@pytest.mark.asyncio
async def test_default_off_writer_finalization_keeps_original_graph_flow(workspace):
    from searchos.socm.revision_state import RevisionMemory

    state = workspace.load_state()
    state.p1 = RevisionMemory()
    workspace.save_state(state)
    graph = FixedWriterGraph()
    _writer_context(workspace, graph, tracked=False)
    await _harness_synthesize(workspace=workspace, state=workspace.load_state(), query="Who?")
    assert len(graph.calls) == 1 and graph.calls[0]["run_id"] == ""
    assert json.loads(_outputs(workspace)[1])["mode"] == "writer_finalize"
    assert not workspace.load_state().p1.artifacts
