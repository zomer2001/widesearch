"""P1's optional temporal gate operates on checked, version-bound source annotations."""

import hashlib
import json

import pytest

from searchos.harness.middleware.extraction.core.conflicts import ConflictReviewer
from searchos.harness.middleware.extraction.core.store import InMemoryEvidenceStore
from searchos.socm import EvidenceNode, SearchState
from searchos.socm.claim_annotations import EvidenceClaimAnnotation, evidence_fingerprint
from searchos.socm.conflicts import cell_pairs, reconcile_conflicts
from searchos.socm.coverage import CellEvidenceBinding, bind_cell_evidence, commit_evidence
from searchos.socm.evidence import SourceObservation
from searchos.socm.temporal import TemporalScope

KEY = "firms/Acme.CEO"


def make_state(*, overlap=False, projected=False):
    state = SearchState()
    state.coverage_map.initialize(
        ["Acme"], ["Name", "CEO"], primary_key=["Name"], table_id="firms",
    )
    for index, name in enumerate(("Alice", "Bob")):
        year = 2024 if index == 0 or overlap else 2025
        dates = f"{year}-01-01 to {year}-12-31"
        excerpt = f"Acme head office CEO: {name}, effective {dates}."
        source = f"https://example.invalid/fixture/{index}"
        observation = SourceObservation.capture(source, "Heading\n" + excerpt + "\nFooter")
        state.evidence_graph.observations.append(observation)
        node = EvidenceNode(
            id=f"e{index}", value="" if projected else name,
            entity="" if projected else "Acme", attribute="" if projected else "CEO",
            table_id="" if projected else "firms", finding=excerpt,
            source=source, source_excerpt=excerpt, observation_id=observation.id,
            span=(len("Heading\n"), len("Heading\n") + len(excerpt)), alignment="full",
            text_hash="sha1:" + hashlib.sha1(excerpt.encode()).hexdigest(),
        )
        if projected:
            state.evidence_graph.add_node(node)
            bind_cell_evidence(
                state, [(KEY, CellEvidenceBinding(evidence_id=node.id, value=name))],
            )
        else:
            commit_evidence(state, [node])
        original = state.evidence_graph.get_node(node.id)
        state.evidence_graph.claim_annotations[node.id] = EvidenceClaimAnnotation(
            evidence_id=node.id, evidence_fingerprint=evidence_fingerprint(original),
            entity="Acme", attribute="CEO", value=name, scope_key="head office CEO",
            valid_time=TemporalScope(
                start=f"{year}-01-01", end=f"{year}-12-31", basis_quote=dates,
            ),
            verified=True, reason="Synthetic source-support review fixture.",
        )
    return state


class FakeJudge:
    def __init__(self, verdict="uncertain", mutate=None):
        self.verdict = verdict
        self.mutate = mutate
        self.payloads = []

    async def __call__(self, prompt):
        pairs = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
        self.payloads.append(pairs)
        if self.mutate is not None:
            self.mutate()
        return json.dumps([
            {
                "id": pair["id"], "verdict": self.verdict, "reason": "固定测试判定",
                "supported_a": True, "supported_b": True, "same_scope": True,
                "scope": "测试口径", "quote_a": pair["a"]["excerpt"],
                "quote_b": pair["b"]["excerpt"],
            }
            for pair in pairs
        ])


async def review(store, judge, *, enabled=True):
    return await ConflictReviewer(store, judge, p1_enabled=enabled).review({KEY})


def current_result(state, *, enabled=None):
    pair, = cell_pairs(state, KEY, p1_enabled=enabled)
    return state.coverage_map.evidence_comparisons[KEY][pair.fingerprint]


def test_disabled_mode_retains_original_comparison_cache_key():
    # Captured from the unmodified 0aa92ba cell_pairs on this source fixture.
    legacy = "6cb710fe00a4e620b398912fc85d79b050e05709453c54a7e92317ec9772309a"
    state = make_state()
    assert cell_pairs(state, KEY, p1_enabled=False)[0].fingerprint == legacy
    state.evidence_graph.claim_annotations.clear()
    assert cell_pairs(state, KEY, p1_enabled=False)[0].fingerprint == legacy


@pytest.mark.asyncio
async def test_verified_disjoint_periods_skip_model_without_selecting_a_winner():
    store = InMemoryEvidenceStore(make_state())
    before_nodes = store.state.evidence_graph.model_dump(mode="json")
    before_cell = store.state.coverage_map.cells[KEY].model_dump(mode="json")
    judge = FakeJudge()
    assert await review(store, judge) == {"pairs": 1, "calls": 0, "applied": 1}
    assert not judge.payloads
    result = current_result(store.state)
    assert result.method == "p1_rule" and result.rule == "P1-DISJOINT"
    assert result.verdict == "compatible" and result.verified
    assert result.temporal_relation == "before"
    assert store.state.evidence_graph.model_dump(mode="json") == before_nodes
    cell = store.state.coverage_map.cells[KEY]
    assert cell.value == before_cell["value"]
    assert cell.supporting_evidence_ids == before_cell["supporting_evidence_ids"]
    assert cell.conflict_status == "clear"
    # Normal scheduler/coverage reconciliation follows the persisted review mode.
    reloaded = SearchState.model_validate_json(store.state.model_dump_json())
    reconcile_conflicts(reloaded, {KEY})
    assert reloaded.coverage_map.p1_enabled
    assert reloaded.coverage_map.cells[KEY].conflict_status == "clear"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stale", "unverified", "scope", "unknown", "unanchored"])
async def test_ineligible_annotations_fall_back_to_existing_model_review(change):
    state = make_state()
    annotation = state.evidence_graph.claim_annotations["e0"]
    if change == "stale":
        state.evidence_graph.get_node("e0").finding += " A later edit."
    elif change == "unverified":
        annotation.verified = False
    elif change == "scope":
        annotation.scope_key = "different branch CEO"
    elif change == "unknown":
        annotation.valid_time = None
    else:
        annotation.valid_time.basis_quote = "a date quotation absent from the source"
    store, judge = InMemoryEvidenceStore(state), FakeJudge()
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"
    assert store.state.coverage_map.cells[KEY].conflict_status == "uncertain"


@pytest.mark.asyncio
async def test_overlap_keeps_two_pass_llm_conflict_verification():
    store, judge = InMemoryEvidenceStore(make_state(overlap=True)), FakeJudge("contradictory")
    result = await review(store, judge)
    assert result["calls"] == 2
    assert current_result(store.state).method == "llm"
    assert current_result(store.state).verified
    assert store.state.coverage_map.cells[KEY].conflict_status == "confirmed"
    assert judge.payloads[0][0]["a"] == judge.payloads[1][0]["b"]


@pytest.mark.asyncio
async def test_baseline_and_p1_caches_are_separate_and_mode_switch_is_persisted():
    store, judge = InMemoryEvidenceStore(make_state()), FakeJudge("contradictory")
    assert (await review(store, judge, enabled=False))["calls"] == 2
    baseline = cell_pairs(store.state, KEY, p1_enabled=False)[0].fingerprint
    assert (await review(store, judge, enabled=True))["calls"] == 0
    p1 = cell_pairs(store.state, KEY, p1_enabled=True)[0].fingerprint
    assert baseline != p1
    assert store.state.coverage_map.cells[KEY].conflict_status == "clear"
    # Switching back reuses only the baseline entry, even when nothing is pending.
    assert (await review(store, judge, enabled=False))["pairs"] == 0
    assert not store.state.coverage_map.p1_enabled
    assert store.state.coverage_map.cells[KEY].conflict_status == "confirmed"
    assert current_result(store.state).method == "llm"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remove", "metadata", "source"])
async def test_annotation_or_original_source_changes_invalidate_cached_compatibility(change):
    store, judge = InMemoryEvidenceStore(make_state()), FakeJudge()
    await review(store, judge)
    previous = cell_pairs(store.state, KEY)[0].fingerprint
    if change == "remove":
        del store.state.evidence_graph.claim_annotations["e0"]
    elif change == "metadata":
        store.state.evidence_graph.claim_annotations["e0"].valid_time = None
    else:
        # finding is hidden by the cell value but is part of the authoritative source hash.
        store.state.evidence_graph.get_node("e0").finding += " Source correction."
    assert cell_pairs(store.state, KEY)[0].fingerprint != previous
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"


@pytest.mark.asyncio
async def test_projection_checks_original_fingerprint_but_exact_projected_claim_scope():
    store, judge = InMemoryEvidenceStore(make_state(projected=True)), FakeJudge()
    assert (await review(store, judge))["calls"] == 0
    assert current_result(store.state).method == "p1_rule"
    store.state.coverage_map.evidence_bindings[KEY][0].value = "Carol"
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["entity", "attribute", "value"])
async def test_annotation_cannot_authorize_a_different_projected_claim(field):
    state = make_state(projected=True)
    setattr(state.evidence_graph.claim_annotations["e0"], field, "not the projected claim")
    store, judge = InMemoryEvidenceStore(state), FakeJudge()
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"


@pytest.mark.asyncio
async def test_annotation_change_during_model_call_cannot_commit_stale_result():
    store = InMemoryEvidenceStore(make_state(overlap=True))

    def mutate():
        store.state.evidence_graph.claim_annotations["e0"].reason += " Updated review."

    result = await review(store, FakeJudge("compatible", mutate=mutate))
    assert result["applied"] == 0
    assert not store.state.coverage_map.evidence_comparisons
    assert store.state.coverage_map.cells[KEY].conflict_status == "pending"


@pytest.mark.asyncio
async def test_rule_result_rechecks_annotations_inside_atomic_commit():
    class ChangedBeforeCommit(InMemoryEvidenceStore):
        def atomic_update_state(self, updater):
            self.state.evidence_graph.claim_annotations.pop("e0", None)
            return super().atomic_update_state(updater)

    store, judge = ChangedBeforeCommit(make_state()), FakeJudge()
    result = await review(store, judge)
    assert result == {"pairs": 1, "calls": 0, "applied": 0}
    assert not store.state.coverage_map.evidence_comparisons
    assert store.state.coverage_map.cells[KEY].conflict_status == "pending"


def break_grounding(state, kind):
    node = state.evidence_graph.get_node("e0")
    observation = next(item for item in state.evidence_graph.observations
                       if item.id == node.observation_id)
    if kind == "missing":
        state.evidence_graph.observations.remove(observation)
    elif kind == "duplicate":
        state.evidence_graph.observations.append(observation.model_copy(deep=True))
    elif kind == "full_hash":
        observation.text_hash = "sha256:incorrect"
    elif kind == "corrupt_text":
        observation.text += " Corrupted content without a matching hash."
    elif kind == "source":
        observation.source = "https://example.invalid/different-source"
    elif kind == "metadata":
        observation.local_source_metadata = {"document_id": "another-document"}
    elif kind == "span":
        node.span = (node.span[0] + 1, node.span[1])
    elif kind == "excerpt":
        node.source_excerpt += " Fabricated text."
        node.text_hash = "sha1:" + hashlib.sha1(node.source_excerpt.encode()).hexdigest()
    elif kind == "excerpt_hash":
        node.text_hash = "sha1:incorrect"
    else:
        raise AssertionError(f"Unknown fixture mutation: {kind}")
    # Isolate grounding checks from the separate annotation-freshness guard.
    state.evidence_graph.claim_annotations["e0"].evidence_fingerprint = evidence_fingerprint(node)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "missing", "duplicate", "full_hash", "corrupt_text", "source", "metadata",
    "span", "excerpt", "excerpt_hash",
])
async def test_verified_annotation_requires_current_unique_grounded_observation(kind):
    state = make_state()
    break_grounding(state, kind)
    store, judge = InMemoryEvidenceStore(state), FakeJudge()
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"
    assert judge.payloads[0][0]["p1"]["reason"] == "原始来源窗口或定位已失效"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "full_hash", "corrupt_text", "source", "metadata"])
async def test_observation_changes_invalidate_cached_rule_compatibility(kind):
    store, judge = InMemoryEvidenceStore(make_state()), FakeJudge()
    await review(store, judge)
    previous = cell_pairs(store.state, KEY)[0].fingerprint
    break_grounding(store.state, kind)
    assert cell_pairs(store.state, KEY)[0].fingerprint != previous
    assert (await review(store, judge))["calls"] == 1
    assert current_result(store.state).method == "llm"


@pytest.mark.asyncio
async def test_observation_change_during_model_call_discards_old_decision():
    store = InMemoryEvidenceStore(make_state(overlap=True))

    def mutate():
        break_grounding(store.state, "corrupt_text")

    result = await review(store, FakeJudge("compatible", mutate=mutate))
    assert result["applied"] == 0
    assert not store.state.coverage_map.evidence_comparisons
    assert store.state.coverage_map.cells[KEY].conflict_status == "pending"


@pytest.mark.asyncio
async def test_observation_change_before_rule_commit_discards_old_decision():
    class ChangedBeforeCommit(InMemoryEvidenceStore):
        def atomic_update_state(self, updater):
            break_grounding(self.state, "missing")
            return super().atomic_update_state(updater)

    store, judge = ChangedBeforeCommit(make_state()), FakeJudge()
    result = await review(store, judge)
    assert result == {"pairs": 1, "calls": 0, "applied": 0}
    assert not store.state.coverage_map.evidence_comparisons
    assert store.state.coverage_map.cells[KEY].conflict_status == "pending"
