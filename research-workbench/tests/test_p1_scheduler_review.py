"""Downstream reviews preserve the workspace's persisted comparison policy."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from searchos.harness.middleware.extraction.core.conflicts import ConflictReviewer
from searchos.harness.middleware.extraction.core.store import InMemoryEvidenceStore
from searchos.socm import EvidenceNode, FrontierTask, SearchState
from searchos.socm.claim_annotations import EvidenceClaimAnnotation, evidence_fingerprint
from searchos.socm.conflicts import cell_pairs
from searchos.socm.coverage import CellEvidenceBinding, bind_cell_evidence
from searchos.socm.evidence import SourceObservation
from searchos.socm.temporal import TemporalScope

KEY = "firms/Acme.CEO"


def source_state(*, enabled, bound=True):
    state = SearchState()
    state.coverage_map.initialize(
        ["Acme"], ["Name", "CEO"], primary_key=["Name"], table_id="firms",
    )
    state.coverage_map.p1_enabled = enabled
    state.frontier.add(FrontierTask(id="task", question="Collect CEO evidence", table_id="firms"))
    for index, name in enumerate(("Alice", "Bob")):
        year = 2024 + index
        dates = f"{year}-01-01 to {year}-12-31"
        excerpt = f"Acme CEO: {name}, effective {dates}."
        observation = SourceObservation.capture(f"https://example.invalid/{index}", excerpt)
        node = EvidenceNode(
            id=f"e{index}", finding=excerpt, source=observation.source,
            source_excerpt=excerpt, observation_id=observation.id,
            span=(0, len(excerpt)), text_hash="sha1:" + hashlib.sha1(excerpt.encode()).hexdigest(),
            alignment="full",
        )
        state.evidence_graph.add_node(node)
        state.evidence_graph.observations.append(observation)
        state.evidence_graph.claim_annotations[node.id] = EvidenceClaimAnnotation(
            evidence_id=node.id, evidence_fingerprint=evidence_fingerprint(node),
            entity="Acme", attribute="CEO", value=name, scope_key="company CEO",
            valid_time=TemporalScope(
                start=f"{year}-01-01", end=f"{year}-12-31", basis_quote=dates,
            ), verified=True, reason="Offline source-support fixture",
        )
        if bound:
            bind_cell_evidence(
                state, [(KEY, CellEvidenceBinding(evidence_id=node.id, value=name))],
            )
    return state


class FixedModel:
    def __init__(self, *, on_projection=None):
        self.comparison_calls = 0
        self.projection_calls = 0
        self.on_projection = on_projection

    async def ainvoke(self, prompt):
        if prompt.startswith("[CELL_PROJECTION_REVIEW]"):
            self.projection_calls += 1
            rows = json.loads(prompt.rsplit("\n", 1)[1])
            result = [{"id": row["id"], "supported": True,
                       "quote": row["source_excerpt"], "calculation": None} for row in rows]
            if self.on_projection:
                self.on_projection()
        else:
            self.comparison_calls += 1
            rows = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
            result = [{"id": row["id"], "verdict": "compatible",
                       "reason": "Offline semantic comparison", "supported_a": True,
                       "supported_b": True, "same_scope": True} for row in rows]
        return SimpleNamespace(content=json.dumps(result))

    async def invoke(self, prompt):
        return (await self.ainvoke(prompt)).content


def assert_policy_result(store, enabled):
    state = store.load_state()
    assert state.coverage_map.p1_enabled is enabled
    pair, = cell_pairs(state, KEY)
    result = state.coverage_map.evidence_comparisons[KEY][pair.fingerprint]
    assert result.method == ("p1_rule" if enabled else "llm")
    assert result.verdict == "compatible"
    assert state.coverage_map.cells[KEY].conflict_status == "clear"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("cached", [False, True])
async def test_scheduler_preserves_current_policy_and_reuses_its_cache(
    monkeypatch, enabled, cached,
):
    from searchos.agents.orchestrator import lifecycle
    from searchos.agents.orchestrator.scheduler import Scheduler

    store, model = InMemoryEvidenceStore(source_state(enabled=enabled)), FixedModel()
    if cached:
        await ConflictReviewer(store, model.invoke, p1_enabled=enabled).review({KEY})
        model.comparison_calls = 0
    monkeypatch.setattr(
        lifecycle, "_ctx", SimpleNamespace(workspace=store, extraction_model=model),
    )
    assert await Scheduler().detect_evidence_conflicts() == {"detected": 0, "enqueued": 0}
    assert_policy_result(store, enabled)
    assert model.comparison_calls == int(not enabled and not cached)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_schema_projection_preserves_current_policy(monkeypatch, enabled):
    from searchos.tools import schema

    store, model = InMemoryEvidenceStore(source_state(enabled=enabled, bound=False)), FixedModel()
    monkeypatch.setattr(
        schema, "_orch_ctx", lambda: SimpleNamespace(workspace=store, judge_model=model),
    )
    response = await schema.project_evidence.ainvoke({
        "task_id": "task", "bindings_json": json.dumps([
            {"cell": KEY, "evidence_id": f"e{index}", "value": name}
            for index, name in enumerate(("Alice", "Bob"))
        ]),
    })
    assert not response.startswith("Error:"), response
    assert_policy_result(store, enabled)
    assert model.projection_calls == 1
    assert model.comparison_calls == int(not enabled)


@pytest.mark.asyncio
async def test_schema_review_uses_policy_current_after_projection_wait(monkeypatch):
    from searchos.tools import schema

    store = InMemoryEvidenceStore(source_state(enabled=False, bound=False))

    def enable_policy():
        store.state.coverage_map.p1_enabled = True

    model = FixedModel(on_projection=enable_policy)
    monkeypatch.setattr(
        schema, "_orch_ctx", lambda: SimpleNamespace(workspace=store, judge_model=model),
    )
    response = await schema.project_evidence.ainvoke({
        "task_id": "task", "bindings_json": json.dumps([
            {"cell": KEY, "evidence_id": f"e{index}", "value": name}
            for index, name in enumerate(("Alice", "Bob"))
        ]),
    })
    assert not response.startswith("Error:"), response
    assert_policy_result(store, True)
    assert model.comparison_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_policy_switch_refreshes_unreviewed_cells_without_model_calls(enabled):
    store, model = InMemoryEvidenceStore(source_state(enabled=enabled)), FixedModel()
    await ConflictReviewer(store, model.invoke, p1_enabled=enabled).review({KEY})
    assert store.state.coverage_map.cells[KEY].conflict_status == "clear"
    saved_comparisons = store.state.coverage_map.evidence_comparisons.copy()
    model.comparison_calls = 0

    # Reviewing another cell changes the workspace-wide policy. Its existing
    # CEO verdict cannot remain current under the other policy's fingerprint.
    report = await ConflictReviewer(store, model.invoke, p1_enabled=not enabled).review(
        {"firms/Acme.Name"},
    )
    assert report == {"pairs": 0, "calls": 0, "applied": 0}
    assert store.state.coverage_map.p1_enabled is not enabled
    assert store.state.coverage_map.cells[KEY].conflict_status == "pending"
    assert store.state.coverage_map.evidence_comparisons == saved_comparisons
    assert model.comparison_calls == 0
