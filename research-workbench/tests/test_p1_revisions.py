"""Version barriers and scoped revision propagation, without model calls."""

import hashlib

import pytest

from searchos.socm.claim_annotations import EvidenceClaimAnnotation, evidence_fingerprint
from searchos.socm.coverage import CellEvidenceBinding
from searchos.socm.evidence import EvidenceNode, EvidenceStatus, SourceObservation
from searchos.socm.frontier import FrontierTask
from searchos.socm.revision_state import ConfirmedEvidenceRevision
from searchos.socm.revisions import (
    StaleExecutionError,
    apply_revision,
    artifact_usable,
    assert_execution_current,
    begin_execution,
    evidence_usable,
    evidence_version,
    publish_artifact,
    record_reads,
    snapshot_artifacts,
    snapshot_evidence,
)
from searchos.socm.state import SearchState


def fixture():
    state = SearchState()
    for task_id in ("collect", "writer", "other", "history"):
        state.frontier.questions.append(FrontierTask(id=task_id, question=task_id))
    for eid, value in (("old", "Alice"), ("new", "Bob"), ("backup", "Carol")):
        excerpt = f"Acme head office CEO is {value}."
        source = f"https://example.invalid/{eid}"
        obs = SourceObservation.capture(source, excerpt)
        state.evidence_graph.observations.append(obs)
        node = EvidenceNode(
            id=eid, entity="Acme", attribute="CEO", value=value,
            source=source, source_excerpt=excerpt, finding=excerpt,
            observation_id=obs.id, span=(0, len(excerpt)),
            text_hash="sha256:" + hashlib.sha256(excerpt.encode()).hexdigest(),
        )
        state.evidence_graph.add_node(node)
        state.evidence_graph.claim_annotations[eid] = EvidenceClaimAnnotation(
            evidence_id=eid, evidence_fingerprint=evidence_fingerprint(node),
            entity="Acme", attribute="CEO", value=value,
            scope_key="head office CEO", verified=True,
        )
    return state


def revision(state, **changes):
    values = dict(
        id="revision-1", old_evidence_id="old", new_evidence_id="new",
        reason="Externally confirmed replacement for this answer.", confirmed=True,
        expected_old_version=evidence_version(state, "old"),
        expected_new_version=evidence_version(state, "new"),
    )
    values.update(changes)
    return ConfirmedEvidenceRevision(**values)


def publish(state, task, run, artifact, *, evidence=(), parents=(),
            scope="answer", supports=None, content="draft"):
    begin_execution(state, task_id=task, run_id=run, scope_key=scope)
    record_reads(state, run, evidence=snapshot_evidence(state, evidence, scope),
                 artifacts=snapshot_artifacts(state, parents))
    result = publish_artifact(
        state, run_id=run, artifact_id=artifact,
        kind="task" if artifact.startswith("task:") else "section", content=content,
        support_sets=supports,
    )
    state.p1.executions[run].closed = True
    return result


def test_revision_propagates_real_dependencies_across_scopes_and_retains_sources():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old", "backup"],
            supports=[["old"], ["backup"]])
    publish(state, "writer", "r2", "section:summary", parents=["task:collect"])
    publish(state, "history", "r3", "section:history", scope="archive",
            parents=["section:summary"])
    publish(state, "other", "r4", "task:other", evidence=["new"])
    before_graph = state.evidence_graph.model_dump(mode="json")
    before_frontier = state.frontier.model_dump(mode="json")

    impact = apply_revision(state, revision(state))
    assert impact.affected_artifact_ids == ["section:history", "section:summary", "task:collect"]
    assert impact.affected_task_ids == ["collect", "history", "writer"]
    assert impact.surviving_support_sets["task:collect"] == [["backup"]]
    assert "task:other" in impact.unchanged_ids
    assert state.evidence_graph.model_dump(mode="json") == before_graph
    assert state.frontier.model_dump(mode="json") == before_frontier
    assert not evidence_usable(state, "old")
    assert evidence_usable(state, "old", "archive")
    assert not artifact_usable(state, "section:summary")
    assert artifact_usable(state, "task:other")
    # Original content is preserved at its published epoch, before invalidation.
    original = state.p1.artifact_history["task:collect"][0]
    assert original.epoch == 0 and original.status == "active"
    assert state.p1.artifacts["task:collect"].epoch == 1


def test_revision_is_idempotent_and_persisted_in_round_trip():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old"])
    event = revision(state)
    first = apply_revision(state, event)
    reloaded = SearchState.model_validate_json(state.model_dump_json())
    before = reloaded.model_dump(mode="json")
    repeated = apply_revision(reloaded, event)
    assert repeated.already_applied
    assert repeated.affected_artifact_ids == first.affected_artifact_ids
    assert reloaded.model_dump(mode="json") == before
    with pytest.raises(ValueError, match="reused"):
        apply_revision(reloaded, event.model_copy(update={"reason": "Different payload"}))


@pytest.mark.parametrize("change", ["unconfirmed", "version", "same_id", "scope", "unverified",
                                    "stale_annotation", "unanchored", "empty_reason"])
def test_invalid_revision_makes_no_partial_changes(change):
    state = fixture()
    event = revision(state)
    if change == "unconfirmed":
        event.confirmed = False
    elif change == "version":
        event.expected_old_version = "outdated"
    elif change == "same_id":
        event.new_evidence_id = "old"
    elif change == "scope":
        state.evidence_graph.claim_annotations["new"].scope_key = "regional office CEO"
        event = revision(state)
    elif change == "unverified":
        state.evidence_graph.claim_annotations["new"].verified = False
        event = revision(state)
    elif change == "stale_annotation":
        state.evidence_graph.get_node("new").finding += " changed"
        event = revision(state)
    elif change == "unanchored":
        state.evidence_graph.observations[-2].text += " corrupt"
        event = revision(state)
    else:
        event.reason = " "
    before = state.model_dump(mode="json")
    with pytest.raises(ValueError):
        apply_revision(state, event)
    assert state.model_dump(mode="json") == before


def test_inflight_read_without_published_output_is_invalidated():
    state = fixture()
    begin_execution(state, task_id="writer", run_id="late")
    record_reads(state, "late", evidence=snapshot_evidence(state, ["old"]), artifacts={})
    impact = apply_revision(state, revision(state))
    assert impact.affected_artifact_ids == []
    assert impact.affected_task_ids == ["writer"]
    assert impact.invalidated_execution_ids == ["late"]
    with pytest.raises(StaleExecutionError):
        publish_artifact(state, run_id="late", artifact_id="section:new",
                         kind="section", content="obsolete result")


def test_revision_invalidates_inflight_parent_reader_and_rebuild_rejects_late_result():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old"])
    begin_execution(state, task_id="writer", run_id="late")
    record_reads(state, "late", evidence={}, artifacts=snapshot_artifacts(state, ["task:collect"]))
    apply_revision(state, revision(state))
    rebuilt = publish(state, "collect", "r2", "task:collect", evidence=["new"], content="Bob")
    assert rebuilt.epoch == 1 and rebuilt.version == 2
    assert artifact_usable(state, "task:collect")
    with pytest.raises(StaleExecutionError):
        assert_execution_current(state, "late")
    assert [item.content for item in state.p1.artifact_history["task:collect"]] == ["draft", "Bob"]


@pytest.mark.parametrize("change", ["node", "observation", "binding", "annotation", "source_scope",
                                    "owner", "removed_task", "closed"])
def test_execution_blocks_changed_inputs_and_owner(change):
    state = fixture()
    begin_execution(state, task_id="writer", run_id="run")
    record_reads(state, "run", evidence=snapshot_evidence(state, ["old"]), artifacts={})
    if change == "node":
        state.evidence_graph.get_node("old").source_excerpt += " edited"
    elif change == "observation":
        state.evidence_graph.observations[0].text += " changed original window"
    elif change == "binding":
        state.coverage_map.evidence_bindings["missing/cell"] = [
            CellEvidenceBinding(evidence_id="old", value="changed projected value")]
    elif change == "annotation":
        state.evidence_graph.claim_annotations["old"].scope_key += " changed"
    elif change == "source_scope":
        state.source_scope = {"mode": "web"}
    elif change == "owner":
        state.frontier.questions[1].assigned_agent_id = "new worker"
    elif change == "removed_task":
        state.frontier.questions = [t for t in state.frontier.questions if t.id != "writer"]
    else:
        state.p1.executions["run"].closed = True
    with pytest.raises(StaleExecutionError):
        assert_execution_current(state, "run")


def test_read_registration_is_atomic_and_cannot_replace_delivered_snapshot():
    state = fixture()
    begin_execution(state, task_id="writer", run_id="run")
    fresh = snapshot_evidence(state, ["old", "new"])
    with pytest.raises(StaleExecutionError):
        record_reads(state, "run", evidence={**fresh, "new": "stale"}, artifacts={})
    assert not state.p1.executions["run"].read_evidence
    record_reads(state, "run", evidence=fresh, artifacts={})
    assert state.p1.executions["run"].read_evidence == fresh


def test_restarted_task_revokes_previous_worker_and_run_ids_are_not_reusable():
    state = fixture()
    first = begin_execution(state, task_id="writer", run_id="one")
    assert begin_execution(state, task_id="writer", run_id="one") == first
    with pytest.raises(ValueError, match="reused"):
        begin_execution(state, task_id="other", run_id="one")
    begin_execution(state, task_id="writer", run_id="two")
    assert first.invalidated
    with pytest.raises(StaleExecutionError):
        assert_execution_current(state, "one")
    assert_execution_current(state, "two")


def test_same_ticket_can_write_then_edit_without_losing_original_dependencies():
    state = fixture()
    publish(state, "writer", "first", "section:summary", evidence=["old"], supports=[["old"]])
    begin_execution(state, task_id="writer", run_id="edit")
    record_reads(state, "edit", evidence={},
                 artifacts=snapshot_artifacts(state, ["section:summary"]))
    edited = publish_artifact(state, run_id="edit", artifact_id="section:summary",
                              kind="section", content="reworded")
    assert set(edited.read_evidence) == {"old"}
    assert edited.read_artifacts == {}
    assert edited.support_sets == [["old"]]
    again = publish_artifact(state, run_id="edit", artifact_id="section:summary",
                             kind="section", content="another edit")
    assert again.version == 3 and "old" in again.read_evidence
    impact = apply_revision(state, revision(state))
    assert "section:summary" in impact.affected_artifact_ids


def test_same_epoch_competing_producer_cannot_overwrite_new_output():
    state = fixture()
    begin_execution(state, task_id="writer", run_id="one")
    begin_execution(state, task_id="other", run_id="two")
    publish_artifact(state, run_id="one", artifact_id="section:same",
                     kind="section", content="first")
    with pytest.raises(StaleExecutionError, match="Output changed"):
        publish_artifact(state, run_id="two", artifact_id="section:same",
                         kind="section", content="late")


def test_support_sets_cannot_invent_unread_evidence_and_publication_preserves_all_reads():
    state = fixture()
    begin_execution(state, task_id="writer", run_id="run")
    record_reads(state, "run", evidence=snapshot_evidence(state, ["old", "backup"]), artifacts={})
    with pytest.raises(ValueError, match="actual evidence reads"):
        publish_artifact(state, run_id="run", artifact_id="section:one", kind="section",
                         content="draft", support_sets=[["new"]])
    with pytest.raises(ValueError, match="nonempty"):
        publish_artifact(state, run_id="run", artifact_id="section:one", kind="section",
                         content="draft", support_sets=[[]])
    artifact = publish_artifact(state, run_id="run", artifact_id="section:one", kind="section",
                                content="draft", support_sets=[["backup"]])
    assert set(artifact.read_evidence) == {"old", "backup"}
    assert "section:one" in apply_revision(state, revision(state)).affected_artifact_ids


def test_parent_edit_invalidates_published_descendant_even_without_revision_event():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old"])
    publish(state, "writer", "r2", "section:summary", parents=["task:collect"])
    publish(state, "collect", "r3", "task:collect", evidence=["new"])
    assert not artifact_usable(state, "section:summary")
    with pytest.raises(StaleExecutionError):
        snapshot_artifacts(state, ["section:summary"])


def test_dependency_cycle_is_rejected_without_mutating_output():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old"])
    publish(state, "writer", "r2", "section:summary", parents=["task:collect"])
    begin_execution(state, task_id="collect", run_id="r3")
    record_reads(state, "r3", evidence={}, artifacts=snapshot_artifacts(state, ["section:summary"]))
    before = state.p1.artifacts["task:collect"].model_copy(deep=True)
    with pytest.raises(ValueError, match="acyclic"):
        publish_artifact(state, run_id="r3", artifact_id="task:collect",
                         kind="task", content="cycle")
    assert state.p1.artifacts["task:collect"] == before


def test_source_scope_and_evidence_status_gate_new_reads():
    state = fixture()
    state.source_scope = {"mode": "local", "documents": []}
    assert not evidence_usable(state, "old")
    with pytest.raises(StaleExecutionError):
        snapshot_evidence(state, ["old"])
    state.source_scope = None
    state.evidence_graph.get_node("old").status = EvidenceStatus.REJECTED
    assert not evidence_usable(state, "old")


def test_answer_execution_is_the_only_taskless_exception():
    state = fixture()
    with pytest.raises(ValueError, match="Unknown task"):
        begin_execution(state, task_id="fabricated", run_id="bad")
    begin_execution(state, task_id="__answer__", run_id="answer")
    record_reads(state, "answer", evidence=snapshot_evidence(state, ["old"]), artifacts={})
    report = publish_artifact(state, run_id="answer", artifact_id="report:answer",
                              kind="report", content="final")
    assert report.kind == "report"
    impact = apply_revision(state, revision(state))
    assert impact.affected_task_ids == ["__answer__"]
    assert impact.affected_artifact_ids == ["report:answer"]


def test_schema_fact_annotation_supplies_projection_missing_from_raw_node():
    state = fixture()
    for eid in ("old", "new"):
        node = state.evidence_graph.get_node(eid)
        node.entity = node.attribute = node.value = ""
        state.evidence_graph.claim_annotations[eid].evidence_fingerprint = (
            evidence_fingerprint(node)
        )
    publish(state, "collect", "r1", "task:collect", evidence=["old"])
    assert apply_revision(state, revision(state)).affected_task_ids == ["collect"]


def test_alternative_support_keeps_only_complete_unchanged_and_groups():
    state = fixture()
    publish(state, "collect", "r1", "task:collect", evidence=["old", "new", "backup"],
            supports=[["old", "backup"], ["new", "backup"], ["new"]])
    # A separate unchanged assertion cannot rescue the conjunctive old+backup
    # group. The new+backup group also fails if backup changed after consumption.
    state.evidence_graph.get_node("backup").finding += " Correction after publication."
    result = apply_revision(state, revision(state))
    assert result.surviving_support_sets["task:collect"] == [["new"]]
    assert state.p1.artifacts["task:collect"].status == "stale"
