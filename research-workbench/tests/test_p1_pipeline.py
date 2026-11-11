"""Offline integration checks across the production extraction/store boundaries."""

import json
import socket
from pathlib import Path

import pytest

from scripts.try_p1_pipeline import ENTITY, FIXTURES, TABLE, FixedFixtureModel, run_demo
from searchos.config.settings import settings
from searchos.harness.middleware.extraction import WorkspaceEvidenceStore
from searchos.harness.middleware.extraction.core.replay import extract_saved_observation
from searchos.socm import FrontierTask, SearchState
from searchos.socm.claim_annotations import evidence_fingerprint
from searchos.socm.evidence import SourceObservation
from searchos.socm.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("The synthetic pipeline test must not access any network service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["task_rows", "schema_facts"])
async def test_p1_pipeline_preserves_sources_and_persists_real_comparisons(tmp_path, policy):
    result = await run_demo(tmp_path, policy=policy)

    assert result["passed"], result["checks"]
    assert result["counts"] == {"facts": 3, "observations": 3, "annotations": 3, "comparisons": 3}
    assert result["fixture_call_counts"]["row_extraction"] == 3
    assert result["fixture_call_counts"]["claim_extract"] == 3
    assert result["fixture_call_counts"]["claim_verify"] == 3
    rule_pairs = [item for item in result["comparisons"] if item["method"] == "p1_rule"]
    assert len(rule_pairs) == 1
    assert rule_pairs[0]["sources"] == sorted(f.source for f in FIXTURES[:2])
    assert rule_pairs[0]["verdict"] == "compatible"
    assert rule_pairs[0]["rule"] == "P1-DISJOINT"
    assert all(
        item["verdict"] == "uncertain" for item in result["comparisons"] if item["method"] == "llm"
    )
    assert json.loads(Path(result["result_file"]).read_text()) == result
    persisted = SearchState.model_validate_json(Path(result["state_file"]).read_text())
    assert len(persisted.evidence_graph.claim_annotations) == 3
    assert {node.source for node in persisted.evidence_graph.nodes} == {f.source for f in FIXTURES}


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["task_rows", "schema_facts"])
async def test_disabled_p1_has_no_enrichment_calls_and_keeps_prior_run(tmp_path, policy):
    previous = tmp_path / "existing-workspace"
    previous.mkdir()
    sentinel = previous / "user-content.txt"
    sentinel.write_text("Keep this user workspace unchanged.")

    first = await run_demo(tmp_path, policy=policy, p1_enabled=False)
    first_state = Path(first["state_file"]).read_bytes()
    second = await run_demo(tmp_path, policy=policy, p1_enabled=False)

    assert first["passed"], first["checks"]
    assert second["passed"], second["checks"]
    assert first["workspace"] != second["workspace"]
    assert Path(first["state_file"]).read_bytes() == first_state
    assert sentinel.read_text() == "Keep this user workspace unchanged."
    assert first["counts"]["annotations"] == 0
    assert "claim_extract" not in first["fixture_call_counts"]
    assert "claim_verify" not in first["fixture_call_counts"]
    assert all(item["method"] == "llm" for item in first["comparisons"])


@pytest.fixture
def saved_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_p1_evidence_reasoning", True)
    monkeypatch.setattr(settings, "extraction_dual_mode", False)
    monkeypatch.setattr(settings, "extraction_cross_table_backfill", False)
    fixture = FIXTURES[0]
    # A web observation has no local-document identity metadata.
    source = SourceObservation.capture(fixture.source, fixture.excerpt)
    state = SearchState(source_scope={"mode": "web", "documents": []})
    state.coverage_map.initialize(
        entities=[ENTITY],
        attributes=["Organization", "Leader"],
        table_id=TABLE,
        primary_key=["Organization"],
    )
    state.frontier.add(
        FrontierTask(
            id="reextract",
            question="Extract synthetic leadership assertions and dates.",
            table_id=TABLE,
            agent_type="search_agent",
        )
    )
    state.evidence_graph.observations.append(source)
    workspace = WorkspaceManager(tmp_path)
    workspace.create()
    workspace.save_state(state)
    return workspace, source, FixedFixtureModel(WorkspaceEvidenceStore(workspace))


@pytest.mark.asyncio
async def test_saved_observation_table_path_enriches_actual_committed_fact(saved_source):
    workspace, source, model = saved_source

    async def invoke(prompt):
        return (await model.ainvoke(prompt)).content

    result = await extract_saved_observation(
        workspace,
        invoke,
        task_id="reextract",
        observation_id=source.id,
    )
    state = WorkspaceManager(workspace._path.parent, workspace._path.name).load_state()
    assert len(result["evidence_ids"]) == len(state.evidence_graph.nodes) == 1
    node = state.evidence_graph.get_node(result["evidence_ids"][0])
    assert node is not None
    annotation = state.evidence_graph.claim_annotations[node.id]
    assert annotation.verified and annotation.value == FIXTURES[0].leader
    assert annotation.evidence_fingerprint == evidence_fingerprint(node)
    assert annotation.valid_time.start.isoformat() == FIXTURES[0].start
    assert annotation.valid_time.end.isoformat() == FIXTURES[0].end
    assert node.source == source.source and node.observation_id == source.id
    assert source.text[slice(*node.span)] == node.source_excerpt
    assert state.evidence_graph.observations == [source]
    assert {
        binding.evidence_id
        for binding in state.evidence_graph.task_bindings
        if binding.task_id == "reextract"
    } == {node.id}
    assert model.calls == ["row_extraction", "claim_extract", "claim_verify"]


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_model", "before_commit"])
async def test_saved_observation_p1_does_not_bypass_source_scope_guard(saved_source, when):
    workspace, source, model = saved_source

    def restrict_scope(state):
        state.source_scope = {"mode": "local", "documents": []}
        return state

    if when == "before_model":
        workspace.atomic_update_state(restrict_scope)

    async def invoke(prompt):
        response = await model.ainvoke(prompt)
        if when == "before_commit":
            workspace.atomic_update_state(restrict_scope)
        return response.content

    with pytest.raises(ValueError, match="scope"):
        await extract_saved_observation(
            workspace,
            invoke,
            task_id="reextract",
            observation_id=source.id,
        )
    state = workspace.load_state()
    assert not state.evidence_graph.nodes
    assert not state.evidence_graph.claim_annotations
    assert not state.evidence_graph.task_bindings
    assert state.evidence_graph.observations == [source]
    assert model.calls == ([] if when == "before_model" else ["row_extraction"])
