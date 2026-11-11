"""Offline evidence revision tests: scoped use, support alternatives and version barriers."""

from urllib.parse import quote

import pytest
from rdflib import Literal, Namespace
from rdflib.namespace import RDF, XSD

from searchos.socm.evidence import EvidenceNode, EvidenceStatus
from searchos.socm.symbolic_revision import (
    Claim,
    ConfirmedRevision,
    DerivedArtifact,
    SymbolicRepairReplay,
    compare_claims,
)
from searchos.socm.temporal import TemporalRelation, TemporalScope

P1 = Namespace("urn:searchos:p1:")
PROV = Namespace("http://www.w3.org/ns/prov#")
TIME = Namespace("http://www.w3.org/2006/time#")


def make_claim(evidence_id, *, value="Alice", start="2024-01-01", end="2024-12-31",
               entity="Acme", scope_key="current", single_valued=True):
    basis = f"Effective period: {start} to {end}."
    return Claim(
        evidence_id=evidence_id, entity=entity, attribute="CEO", value=value,
        scope_key=scope_key, source=f"https://example.org/{evidence_id}",
        source_excerpt=f"{entity}'s CEO is {value}. {basis}",
        observation_id=f"obs-{evidence_id}", single_valued=single_valued,
        temporal=TemporalScope(start=start, end=end, basis_quote=basis),
    )


@pytest.fixture
def revision():
    return ConfirmedRevision(
        id="correction-1", old_evidence_id="old", new_evidence_id="new",
        scope_key="current", reason="The source explicitly corrected its earlier claim.",
        confirmed=True,
    )


@pytest.fixture
def replay():
    claims = [make_claim("old"), make_claim("new", value="Bob"),
              make_claim("alternative"), make_claim("context")]
    artifacts = [
        DerivedArtifact(id="summary", scope_key="current", content="Old summary",
                        read_evidence_ids=["old"], support_sets=[["old"]]),
        DerivedArtifact(id="task", kind="task", scope_key="current", content="Old task",
                        read_artifact_ids=["summary"], read_artifact_epochs={"summary": 0}),
        DerivedArtifact(id="report", kind="report", scope_key="current", content="Old report",
                        read_artifact_ids=["task"], read_artifact_epochs={"task": 0}),
        DerivedArtifact(id="audit", kind="report", scope_key="audit", content="Old audit",
                        read_artifact_ids=["report"], read_artifact_epochs={"report": 0}),
        DerivedArtifact(id="history", scope_key="historical", content="Historical summary",
                        read_evidence_ids=["old"], support_sets=[["old"]]),
        DerivedArtifact(id="independent", scope_key="current", content="Independent summary",
                        read_evidence_ids=["alternative"]),
    ]
    return SymbolicRepairReplay(claims, artifacts)


def rebuilt(replay, artifact_id, **updates):
    changes = {"content": f"Rebuilt {artifact_id}", "status": "active"}
    changes.update(updates)
    return replay.artifacts[artifact_id].model_copy(deep=True, update=changes)


def publish_summary(replay):
    return replay.publish(
        rebuilt(replay, "summary", read_evidence_ids=["new"], support_sets=[["new"]]),
        expected_epoch=1,
    )


def test_real_evidence_projection_preserves_identity_grounding_and_original():
    evidence = EvidenceNode(
        id="ev-live", entity="Acme", attribute="CEO", value="Alice",
        finding="Alice led Acme during 2024.", source="https://example.org/filing",
        source_excerpt="Alice led Acme during 2024.", observation_id="obs-live",
        span=(12, 43), text_hash="sha256:sample", confidence=0.8,
    )
    original = evidence.model_dump()
    temporal = TemporalScope(start="2024-01-01", end="2024-12-31", basis_quote="during 2024")

    claim = Claim.from_evidence(evidence, scope_key="company", temporal=temporal,
                                single_valued=True)

    assert (claim.evidence_id, claim.value, claim.observation_id) == (
        "ev-live", "Alice", "obs-live",
    )
    assert claim.source == evidence.source
    assert claim.source_excerpt == evidence.source_excerpt
    assert claim.temporal == temporal
    assert claim.single_valued is True
    assert evidence.model_dump() == original


def test_projection_preserves_inactive_status_and_uses_finding_when_value_is_absent():
    evidence = EvidenceNode(id="ev", finding="An historical assertion", status="superseded")

    claim = Claim.from_evidence(evidence, scope_key="historical")

    assert claim.value == evidence.finding
    assert claim.status == EvidenceStatus.SUPERSEDED
    assert claim.temporal is None
    assert claim.single_valued is False


def test_disjoint_effective_periods_are_evolution_without_retracting_sources():
    old = make_claim("old", end="2024-06-01")
    new = make_claim("new", value="Bob", start="2024-06-02")

    decision = compare_claims(old, new)

    assert decision.outcome == "temporal_evolution"
    assert decision.temporal_relation == TemporalRelation.BEFORE
    assert compare_claims(new, old).temporal_relation == TemporalRelation.AFTER
    assert old.status == new.status == EvidenceStatus.ACTIVE


def test_exclusive_values_in_same_effective_period_are_only_conflict_candidates():
    decision = compare_claims(make_claim("old"), make_claim("new", value="Bob"))

    assert decision.outcome == "conflict_candidate"
    assert decision.rule == "P1-OVERLAP-EXCLUSIVE"
    assert decision.evidence_ids == ["old", "new"]
    assert decision.temporal_relation == TemporalRelation.EQUAL


def test_shared_calendar_day_is_overlap_not_automatic_supersession():
    decision = compare_claims(make_claim("old", end="2024-06-01"),
                              make_claim("new", value="Bob", start="2024-06-01"))

    assert decision.outcome == "conflict_candidate"
    assert decision.temporal_relation == TemporalRelation.OVERLAPS


@pytest.mark.parametrize("left_single,right_single", [(False, True), (True, False)])
def test_conflict_requires_both_claims_to_have_explicit_exclusivity(left_single, right_single):
    decision = compare_claims(make_claim("old", single_valued=left_single),
                              make_claim("new", value="Bob", single_valued=right_single))

    assert decision.outcome == "not_exclusive"


def test_anchored_unknown_endpoints_remain_unknown():
    decision = compare_claims(make_claim("old", start=None, end=None),
                              make_claim("new", value="Bob"))

    assert decision.outcome == "unknown"
    assert decision.rule == "P1-TIME-UNKNOWN"


@pytest.mark.parametrize("temporal", [None, TemporalScope(
    start="2025-01-01", end="2025-12-31", basis_quote="Not present in the source excerpt")])
def test_missing_or_unanchored_time_cannot_establish_evolution(temporal):
    new = make_claim("new", value="Bob").model_copy(update={"temporal": temporal})

    decision = compare_claims(make_claim("old"), new)

    assert decision.outcome == "unknown"
    assert decision.rule == "P1-TIME-ANCHOR"


@pytest.mark.parametrize("field,value", [("scope_key", "subsidiary"), ("entity", "Other Co"),
                                         ("attribute", "CFO")])
def test_different_claim_dimensions_are_not_comparable(field, value):
    new = make_claim("new", value="Bob").model_copy(update={field: value})

    assert compare_claims(make_claim("old"), new).outcome == "not_comparable"


def test_agreeing_values_do_not_require_fabricated_temporal_metadata():
    left = make_claim("old", value=" Alice  Smith ").model_copy(update={"temporal": None})
    right = make_claim("new", value="alice smith").model_copy(update={"temporal": None})

    assert compare_claims(left, right).outcome == "same_value"


def test_confirmed_revision_propagates_consumption_and_preserves_unrelated_scope(replay, revision):
    original_claim = replay.claims["old"].model_dump()

    plan = replay.apply(revision)

    assert {impact.artifact_id for impact in plan.impacts} == {"summary", "task", "report", "audit"}
    assert plan.task_ids == ["task"]
    assert set(plan.unaffected_ids) == {"history", "independent"}
    assert replay.active_context() == {"history": "Historical summary",
                                       "independent": "Independent summary"}
    assert not replay.evidence_is_usable("old", "current")
    assert replay.evidence_is_usable("old", "historical")
    assert replay.claims["old"].model_dump() == original_claim
    assert all(impact.execution_epoch == 1 for impact in plan.impacts)


def test_or_of_and_supports_preserve_whole_alternatives_but_read_input_still_needs_rebuild(
    replay, revision
):
    replay.artifacts["summary"].support_sets = [
        ["old", "context"], ["alternative", "context"], ["old", "alternative"],
    ]

    impact = next(item for item in replay.apply(revision).impacts if item.artifact_id == "summary")

    assert impact.surviving_support_sets == [["alternative", "context"]]
    assert replay.artifacts["summary"].status == "stale"
    assert "summary" not in replay.active_context()


def test_logical_support_loss_is_recorded_even_without_a_direct_read(replay, revision):
    replay.artifacts["summary"].read_evidence_ids = []

    plan = replay.apply(revision)

    assert "summary" in {impact.artifact_id for impact in plan.impacts}
    assert replay.artifacts["summary"].status == "stale"


def test_same_revision_is_idempotent_and_returned_plan_is_not_internal_state(replay, revision):
    first = replay.apply(revision)
    first.impacts.clear()

    again = replay.apply(revision)

    assert again.already_applied is True
    assert len(again.impacts) == 4
    assert replay.artifacts["summary"].execution_epoch == 1
    assert len(replay.revisions) == 1


def test_same_revision_id_with_different_payload_is_rejected(replay, revision):
    replay.apply(revision)

    with pytest.raises(ValueError, match="different contents"):
        replay.apply(revision.model_copy(update={"reason": "A different decision"}))

    assert replay.artifacts["summary"].execution_epoch == 1


def test_unconfirmed_correction_cannot_retire_evidence_or_hide_context(replay, revision):
    initial_context = replay.active_context()

    with pytest.raises(ValueError, match="confirmed"):
        replay.apply(revision.model_copy(update={"confirmed": False}))

    assert replay.active_context() == initial_context
    assert replay.retired_uses == set()
    assert replay.revisions == {}


def test_replacement_cannot_cross_entity_even_when_explicitly_confirmed(replay, revision):
    replay.claims["new"].entity = "Other Co"

    with pytest.raises(ValueError):
        replay.apply(revision)

    assert replay.retired_uses == set()
    assert replay.artifacts["summary"].execution_epoch == 0


def test_late_result_from_old_epoch_cannot_reactivate_an_invalidated_summary(replay, revision):
    late = rebuilt(replay, "summary")
    replay.apply(revision)

    assert replay.publish(late, expected_epoch=0) is False
    assert replay.artifacts["summary"].status == "stale"


def test_new_epoch_does_not_authorize_using_retired_evidence(replay, revision):
    replay.apply(revision)

    assert replay.publish(rebuilt(replay, "summary"), expected_epoch=1) is False
    assert "summary" not in replay.active_context()


def test_child_cannot_publish_while_its_parent_is_still_stale(replay, revision):
    replay.apply(revision)
    task = rebuilt(replay, "task", read_artifact_epochs={"summary": 1})

    assert replay.publish(task, expected_epoch=1) is False


def test_child_cannot_publish_against_a_parent_version_it_did_not_read(replay, revision):
    replay.apply(revision)
    assert publish_summary(replay)

    assert replay.publish(rebuilt(replay, "task", read_artifact_epochs={"summary": 0}),
                          expected_epoch=1) is False


def test_rebuilt_outputs_publish_in_dependency_order_using_current_versions(replay, revision):
    replay.apply(revision)

    assert publish_summary(replay)
    for child, parent in [("task", "summary"), ("report", "task"), ("audit", "report")]:
        assert replay.publish(rebuilt(replay, child, read_artifact_epochs={parent: 1}),
                              expected_epoch=1)

    assert set(replay.active_context()) == set(replay.artifacts)
    assert replay.active_context()["report"] == "Rebuilt report"
    assert replay.artifacts["history"].execution_epoch == 0
    assert replay.artifacts["summary"].read_evidence_ids == ["new"]


def test_publish_requires_one_complete_support_alternative_not_one_surviving_member(
    replay, revision
):
    replay.apply(revision)
    partial = rebuilt(replay, "summary", read_evidence_ids=["new"],
                      support_sets=[["old", "new"]])

    assert replay.publish(partial, expected_epoch=1) is False
    with_alternative = partial.model_copy(update={"support_sets": [
        ["old", "new"], ["alternative", "context"],
    ]})
    assert replay.publish(with_alternative, expected_epoch=1) is True


def test_active_output_cannot_be_replaced_by_different_payload_at_same_epoch(replay):
    previous = replay.artifacts["independent"].model_dump()

    assert replay.publish(rebuilt(replay, "independent"), expected_epoch=0) is False
    assert replay.artifacts["independent"].model_dump() == previous


def test_identical_active_output_is_an_idempotent_publish(replay):
    same = replay.artifacts["independent"].model_copy(deep=True)

    assert replay.publish(same, expected_epoch=0) is True
    assert replay.artifacts["independent"] == same


def test_dependency_cycle_is_rejected_when_constructing_replay():
    artifacts = [DerivedArtifact(id=child, scope_key="current", read_artifact_ids=[parent],
                                 read_artifact_epochs={parent: 0})
                 for child, parent in [("a", "b"), ("b", "c"), ("c", "a")]]

    with pytest.raises(ValueError, match="[Cc]ycl"):
        SymbolicRepairReplay([], artifacts)


@pytest.mark.parametrize("parent_status,observed_epoch", [("stale", 1), ("active", 0)])
def test_replay_cannot_start_with_active_child_of_stale_or_wrong_version_parent(
    parent_status, observed_epoch
):
    parent = DerivedArtifact(id="parent", scope_key="current", status=parent_status,
                             execution_epoch=1)
    child = DerivedArtifact(id="child", scope_key="current", read_artifact_ids=["parent"],
                            read_artifact_epochs={"parent": observed_epoch})

    with pytest.raises(ValueError, match="current, active parent"):
        SymbolicRepairReplay([], [parent, child])


def test_rebuild_cannot_create_cycle_through_existing_descendants(replay, revision):
    replay.apply(revision)
    cyclic = rebuilt(replay, "summary", read_evidence_ids=["new"], support_sets=[["new"]],
                     read_artifact_ids=["report"], read_artifact_epochs={"report": 1})

    with pytest.raises(ValueError, match="[Cc]ycl"):
        replay.publish(cyclic, expected_epoch=1)

    assert replay.artifacts["summary"].read_artifact_ids == []


def test_rdf_projection_keeps_claim_observation_interval_and_execution_types(replay):
    graph = replay.to_rdf()
    claim = P1["claim/old"]
    observation = P1["observation/obs-old"]
    interval = P1["interval/old"]
    summary = P1["artifact/summary%3A0"]
    execution = P1["execution/summary%3A0"]

    assert (claim, RDF.type, P1.Claim) in graph
    assert (observation, RDF.type, P1.SourceObservation) in graph
    assert (claim, P1.source, observation) in graph
    assert (interval, RDF.type, TIME.ProperInterval) in graph
    assert graph.value(interval, P1.startDate).datatype == XSD.date
    assert (summary, RDF.type, P1.DerivedArtifact) in graph
    assert (execution, RDF.type, P1.Execution) in graph
    assert (summary, PROV.wasGeneratedBy, execution) in graph
    assert (execution, P1.usedEvidence, claim) in graph


def test_rdf_keeps_support_groups_and_scoped_retirement_without_retracting_claim(replay, revision):
    replay.artifacts["summary"].support_sets = [["old", "context"], ["alternative"]]
    replay.apply(revision)
    graph = replay.to_rdf()
    summary = P1["artifact/summary%3A1"]
    groups = list(graph.objects(summary, P1.supportAlternative))

    assert len(groups) == 2
    members = {frozenset(graph.objects(group, P1.memberEvidence)) for group in groups}
    assert members == {frozenset([P1["claim/old"], P1["claim/context"]]),
                       frozenset([P1["claim/alternative"]])}
    assert all((group, RDF.type, P1.SupportSet) in graph for group in groups)
    retired = list(graph.subjects(RDF.type, P1.EvidenceUse))
    assert len(retired) == 1
    assert graph.value(retired[0], P1.usedClaim) == P1["claim/old"]
    assert graph.value(retired[0], P1.scopeKey) == Literal("current")
    assert graph.value(retired[0], P1.status) == Literal("retired")
    assert graph.value(P1["claim/old"], P1.status) == Literal("active")
    event = P1[f"revision/{quote(revision.id, safe='')}"]
    assert (event, RDF.type, P1.RevisionEvent) in graph
    assert (summary, PROV.wasInfluencedBy, event) in graph
