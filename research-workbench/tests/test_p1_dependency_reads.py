"""Real tool reads and writer transactions keep bounded runtime dependencies."""

import json

import pytest

from searchos.harness.middleware.context.socm_middleware import SOCMContextMiddleware
from searchos.harness.middleware.models import TaskContext
from searchos.harness.p1_runtime import execution_context
from searchos.socm import EvidenceNode, FrontierTask, OutlineSection, SearchState
from searchos.socm.revisions import (
    StaleExecutionError,
    begin_execution,
    publish_artifact,
    record_reads,
    snapshot_evidence,
)
from searchos.socm.workspace import WorkspaceManager
from searchos.tools import socm_read, writer
from searchos.tools.search_state import _current_agent_var, _workspace_var


@pytest.fixture
def workspace(tmp_path):
    ws = WorkspaceManager(tmp_path, "dependency-reads")
    ws.create()
    state = SearchState()
    state.coverage_map.add_table("t", ["Value"], entities=["A", "B", "C"])
    for index, entity in enumerate(["A", "B", "C"]):
        node = EvidenceNode(
            id=f"e{index}",
            table_id="t",
            entity=entity,
            attribute="Value",
            value=f"value-{index}",
            finding=f"fact-{index}",
            source=f"https://example.org/{index}",
            source_excerpt=f"quote-{index}",
            alignment="full",
            created_at=float(index + 1),
        )
        state.evidence_graph.add_node(node)
        state.coverage_map.fill_from_evidence([node])
    state.frontier.add(
        FrontierTask(
            id="write", question="write", kind="write", assigned_agent_id="writer_agent_test"
        )
    )
    state.frontier.add(
        FrontierTask(id="upstream", question="upstream", resolution="upstream report")
    )
    begin_execution(state, task_id="write", run_id="run")
    ws.save_state(state)
    ws_token = _workspace_var.set(ws)
    agent_token = _current_agent_var.set("writer_agent_test")
    try:
        yield ws
    finally:
        _workspace_var.reset(ws_token)
        _current_agent_var.reset(agent_token)


def _reads(ws):
    return ws.load_state().p1.executions["run"]


@pytest.mark.asyncio
async def test_evidence_tracks_only_the_delivered_window_and_raw_version(workspace):
    with execution_context("run"):
        result = json.loads(await socm_read.read_evidence.ainvoke({"max_nodes": 1}))
    assert [row["id"] for row in result["nodes"]] == ["e2"]
    assert set(_reads(workspace).read_evidence) == {"e2"}


@pytest.mark.asyncio
async def test_provenance_records_only_returned_fact(workspace):
    with execution_context("run"):
        result = json.loads(await socm_read.resolve_cell_provenance.ainvoke({"evidence_id": "e0"}))
    assert result["records"][0]["snippet"] == "quote-0"
    assert set(_reads(workspace).read_evidence) == {"e0"}


@pytest.mark.asyncio
async def test_coverage_value_preview_records_only_delivered_cells(workspace):
    with execution_context("run"):
        result = json.loads(
            await socm_read.read_coverage.ainvoke({"mode": "cells", "entity": "B", "limit": 1})
        )
    assert result["cells"][0]["display_hint"] == "value-1"
    assert set(_reads(workspace).read_evidence) == {"e1"}


@pytest.mark.asyncio
async def test_untracked_reports_and_sections_do_not_claim_current_provenance(workspace):
    state = workspace.load_state()
    state.outline.sections.append(
        OutlineSection(id="legacy", content="old prose", cited_evidence_ids=["e0"])
    )
    workspace.save_state(state)
    with execution_context("run"):
        task = json.loads(await socm_read.read_task_report.ainvoke({"task_id": "upstream"}))
        section = json.loads(await writer.read_outline.ainvoke({"section_id": "legacy"}))
        listed = json.loads(await socm_read.list_frontier.ainvoke({}))
    assert task["dependency_status"] == section["dependency_status"] == "untracked"
    assert task["resolution"] == section["content"] == ""
    assert (
        next(item for item in listed["tasks"] if item["id"] == "upstream")["resolution_excerpt"]
        == ""
    )
    assert not _reads(workspace).read_artifacts
    assert workspace.load_state().outline.find("legacy").content == "old prose"


@pytest.mark.asyncio
async def test_task_report_reads_exact_published_artifact_version(workspace):
    state = workspace.load_state()
    begin_execution(state, task_id="upstream", run_id="producer")
    record_reads(state, "producer", evidence=snapshot_evidence(state, ["e1"]), artifacts={})
    artifact = publish_artifact(
        state,
        run_id="producer",
        artifact_id="task:upstream",
        kind="task",
        content="upstream report",
    )
    workspace.save_state(state)
    with execution_context("run"):
        result = json.loads(await socm_read.read_task_report.ainvoke({"task_id": "upstream"}))
    assert result["resolution"] == "upstream report"
    assert _reads(workspace).read_artifacts == {"task:upstream": artifact.version}


@pytest.mark.asyncio
async def test_writer_keeps_actual_reads_beyond_declared_citations(workspace):
    with execution_context("run"):
        await socm_read.read_evidence.ainvoke({"evidence_ids": "e0,e1"})
        result = json.loads(
            await writer.write_section.ainvoke(
                {"section_id": "s", "content": "Derived prose", "cited_evidence_ids": "e1"}
            )
        )
    assert result["ok"]
    state = workspace.load_state()
    artifact = state.p1.artifacts["section:s"]
    assert set(artifact.read_evidence) == {"e0", "e1"}
    assert artifact.support_sets == [["e1"]]
    assert state.outline.find("s").p1_status == "active"


@pytest.mark.asyncio
async def test_late_writer_cannot_publish_after_consumed_source_changes(workspace):
    with execution_context("run"):
        await socm_read.read_evidence.ainvoke({"evidence_ids": "e0"})
        state = workspace.load_state()
        state.evidence_graph.get_node("e0").source_excerpt = "changed quote"
        workspace.save_state(state)
        with pytest.raises(StaleExecutionError):
            await writer.write_section.ainvoke(
                {"section_id": "s", "content": "late prose", "cited_evidence_ids": "e1"}
            )
    assert workspace.load_state().outline.find("s") is None


@pytest.mark.asyncio
async def test_edit_consumes_prior_section_version_without_self_cycle(workspace):
    with execution_context("run"):
        await writer.write_section.ainvoke(
            {"section_id": "s", "content": "before text", "cited_evidence_ids": "e0"}
        )
        await writer.read_outline.ainvoke({"section_id": "s"})
        result = json.loads(
            await writer.edit_section.ainvoke(
                {"section_id": "s", "old_string": "before", "new_string": "after"}
            )
        )
    artifact = workspace.load_state().p1.artifacts["section:s"]
    assert result["ok"] and artifact.content == "after text"
    assert artifact.version == 2 and "section:s" not in artifact.read_artifacts
    assert set(artifact.read_evidence) == {"e0"}


def test_socm_tracks_only_assigned_cell_candidates(workspace):
    state = workspace.load_state()
    task = next(task for task in state.frontier.questions if task.id == "write")
    task.target_cells = ["t/A.Value"]
    workspace.save_state(state)
    middleware = SOCMContextMiddleware(workspace, task=TaskContext(agent_id="writer_agent_test"))
    with execution_context("run"):
        text = middleware.render()
    assert "value-0" in text and "value-1" not in text
    assert set(_reads(workspace).read_evidence) == {"e0"}


@pytest.mark.asyncio
async def test_scoped_retirement_hides_read_and_preview_without_changing_other_scope(workspace):
    state = workspace.load_state()
    state.p1.executions.clear()
    state.p1.artifacts.clear()
    state.p1.retired_uses["historical"] = ["e0"]
    task = next(task for task in state.frontier.questions if task.id == "write")
    task.target_cells = ["t/A.Value"]
    begin_execution(state, task_id="write", run_id="run", scope_key="historical")
    workspace.save_state(state)
    middleware = SOCMContextMiddleware(workspace, task=TaskContext(agent_id="writer_agent_test"))
    with execution_context("run"):
        text = middleware.render()
        result = json.loads(
            await socm_read.read_evidence.ainvoke({"evidence_ids": "e0", "include_inactive": True})
        )
    assert "value-0" not in text and not result["nodes"]
    assert not _reads(workspace).read_evidence
    assert workspace.load_state().coverage_map.cells["t/A.Value"].display_hint == "value-0"


@pytest.mark.asyncio
async def test_without_execution_ticket_preserves_legacy_tool_behavior(workspace):
    result = json.loads(await socm_read.read_task_report.ainvoke({"task_id": "upstream"}))
    assert result["resolution"] == "upstream report" and "dependency_status" not in result
    await writer.write_section.ainvoke(
        {"section_id": "legacy", "content": "legacy body", "cited_evidence_ids": "e0"}
    )
    assert "section:legacy" not in workspace.load_state().p1.artifacts
    assert not _reads(workspace).read_evidence
