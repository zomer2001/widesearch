"""Pure transactional P1 state operations; callers own workspace locking/save.

Read sets describe inputs actually delivered to an execution, not citations an
LLM happens to mention. A confirmed revision retires one use of one assertion;
it neither deletes the source nor declares a new source universally true.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal

from searchos.socm.claim_annotations import evidence_fingerprint
from searchos.socm.conflicts import _source_grounding, normalize_text
from searchos.socm.evidence import EvidenceStatus
from searchos.socm.revision_state import (
    AppliedRevision,
    Artifact,
    ConfirmedEvidenceRevision,
    Execution,
    RevisionImpact,
)

if TYPE_CHECKING:
    from searchos.socm.state import SearchState


class StaleExecutionError(ValueError):
    """The captured inputs or execution generation no longer permit a write."""


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def source_scope_fingerprint(state: SearchState) -> str:
    return _digest(state.source_scope)


def evidence_version(state: SearchState, evidence_id: str) -> str:
    """Fingerprint saved fact, original reading window, annotation and uses.

    Hash actual observation text as well as its declared hash. Binding values
    are part of an evidence read even when the raw assertion is unchanged.
    The global allowed-source policy is checked separately on every ticket.
    """
    node = state.evidence_graph.get_node(evidence_id)
    if node is None:
        raise ValueError(f"Unknown evidence: {evidence_id}")
    annotation = state.evidence_graph.claim_annotations.get(evidence_id)
    bindings = []
    for key, records in sorted(state.coverage_map.evidence_bindings.items()):
        for record in records:
            if record.evidence_id == evidence_id:
                bindings.append({"cell": key, "binding": record.model_dump(mode="json"),
                                 "target": state.coverage_map.cell_binding(key),
                                 "visible": key in state.coverage_map.cells})
    return _digest({
        "node": node.model_dump(mode="json"),
        "grounding": _source_grounding(state, node),
        "bindings": bindings,
        "annotation": annotation.model_dump(mode="json") if annotation else None,
    })


def evidence_usable(state: SearchState, evidence_id: str, scope: str = "answer") -> bool:
    from searchos.socm.coverage import evidence_source_allowed

    node = state.evidence_graph.get_node(evidence_id)
    return bool(node is not None and node.status == EvidenceStatus.ACTIVE
                and evidence_source_allowed(state, node)
                and evidence_id not in state.p1.retired_uses.get(scope, []))


def artifact_usable(state: SearchState, artifact_id: str) -> bool:
    """Validate published lineage, including when a source changed outside P1."""
    visited: dict[str, bool] = {}
    visiting: set[str] = set()

    def current(aid: str) -> bool:
        if aid in visiting:
            return False
        if aid in visited:
            return visited[aid]
        artifact = state.p1.artifacts.get(aid)
        if artifact is None or artifact.status != "active" or artifact.version <= 0:
            return False
        visiting.add(aid)
        valid = all(
            evidence_usable(state, eid, artifact.scope_key)
            and evidence_version(state, eid) == version
            for eid, version in artifact.read_evidence.items()
        )
        valid = valid and all(
            current(parent)
            and state.p1.artifacts[parent].version == version
            for parent, version in artifact.read_artifacts.items()
        )
        if artifact.support_sets:
            valid = valid and any(
                bool(group) and all(evidence_usable(state, eid, artifact.scope_key)
                                    for eid in group)
                for group in artifact.support_sets
            )
        visiting.remove(aid)
        visited[aid] = valid
        return valid

    return current(artifact_id)


def snapshot_evidence(
    state: SearchState, evidence_ids: Iterable[str], scope_key: str = "answer",
) -> dict[str, str]:
    result = {}
    for eid in dict.fromkeys(evidence_ids):
        if not evidence_usable(state, eid, scope_key):
            raise StaleExecutionError(f"Evidence is unavailable in {scope_key}: {eid}")
        result[eid] = evidence_version(state, eid)
    return result


def snapshot_artifacts(state: SearchState, artifact_ids: Iterable[str]) -> dict[str, int]:
    result = {}
    for aid in dict.fromkeys(artifact_ids):
        if not artifact_usable(state, aid):
            raise StaleExecutionError(f"Artifact is unavailable: {aid}")
        result[aid] = state.p1.artifacts[aid].version
    return result


def begin_execution(
    state: SearchState, *, task_id: str, run_id: str, scope_key: str = "answer",
) -> Execution:
    if not run_id.strip() or not scope_key.strip():
        raise ValueError("Execution ID and use scope must be nonempty")
    task = next((task for task in state.frontier.questions if task.id == task_id), None)
    if task is None and task_id != "__answer__":
        raise ValueError(f"Unknown task: {task_id}")
    previous = state.p1.executions.get(run_id)
    if previous is not None:
        if previous.task_id != task_id or previous.scope_key != scope_key:
            raise ValueError("Execution ID reused with different task or scope")
        return assert_execution_current(state, run_id)
    task_artifact_id = f"task:{task_id}"
    task_artifact = state.p1.artifacts.get(task_artifact_id)
    if task_artifact is not None and (
        task_artifact.kind != "task" or task_artifact.scope_key != scope_key
    ):
        raise ValueError("Task output cannot change kind or use scope")
    if task_artifact is None:
        task_artifact = Artifact(id=task_artifact_id, kind="task", scope_key=scope_key,
                                 producer_task_id=task_id)
        state.p1.artifacts[task_artifact_id] = task_artifact
    execution = Execution(
        id=run_id, task_id=task_id, scope_key=scope_key, task_epoch=task_artifact.epoch,
        assigned_agent_id=task.assigned_agent_id if task is not None else "",
        source_scope_fingerprint=source_scope_fingerprint(state),
        output_epochs={aid: item.epoch for aid, item in state.p1.artifacts.items()},
        output_versions={aid: item.version for aid, item in state.p1.artifacts.items()},
    )
    # A restarted task owns a fresh ticket; the superseded worker may still
    # return, but cannot overwrite the restart's outputs.
    for old in state.p1.executions.values():
        if old.task_id == task_id and not old.closed:
            old.invalidated = True
    state.p1.enabled = True
    state.p1.executions[run_id] = execution
    return execution


def assert_execution_current(state: SearchState, run_id: str) -> Execution:
    execution = state.p1.executions.get(run_id)
    if execution is None:
        raise StaleExecutionError(f"Unknown execution: {run_id}")
    task_artifact = state.p1.artifacts.get(f"task:{execution.task_id}")
    if execution.closed or execution.invalidated:
        raise StaleExecutionError(f"Execution is closed or invalidated: {run_id}")
    if task_artifact is None or task_artifact.epoch != execution.task_epoch:
        raise StaleExecutionError("Task generation changed during execution")
    if execution.task_id != "__answer__":
        task = next((task for task in state.frontier.questions
                     if task.id == execution.task_id), None)
        if task is None or task.assigned_agent_id != execution.assigned_agent_id:
            raise StaleExecutionError("Task owner changed during execution")
    if execution.source_scope_fingerprint != source_scope_fingerprint(state):
        raise StaleExecutionError("Allowed source scope changed during execution")
    for eid, version in execution.read_evidence.items():
        if (not evidence_usable(state, eid, execution.scope_key)
                or evidence_version(state, eid) != version):
            raise StaleExecutionError(f"Consumed evidence changed: {eid}")
    for aid, version in execution.read_artifacts.items():
        if not artifact_usable(state, aid) or state.p1.artifacts[aid].version != version:
            raise StaleExecutionError(f"Consumed artifact changed: {aid}")
    return execution


def record_reads(
    state: SearchState, run_id: str, *,
    evidence: dict[str, str], artifacts: dict[str, int],
) -> None:
    """Accumulate exact delivered snapshots; never silently replace an old read."""
    execution = assert_execution_current(state, run_id)
    for eid, version in evidence.items():
        if (not evidence_usable(state, eid, execution.scope_key)
                or evidence_version(state, eid) != version
                or execution.read_evidence.get(eid, version) != version):
            raise StaleExecutionError(f"Evidence changed before read registration: {eid}")
    for aid, version in artifacts.items():
        if (not artifact_usable(state, aid) or state.p1.artifacts[aid].version != version
                or execution.read_artifacts.get(aid, version) != version):
            raise StaleExecutionError(f"Artifact changed before read registration: {aid}")
    execution.read_evidence.update(evidence)
    execution.read_artifacts.update(artifacts)


def _validate_acyclic(state: SearchState, replacement: Artifact) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(aid: str) -> None:
        if aid in visiting:
            raise ValueError("Artifact dependencies must be acyclic")
        if aid in visited:
            return
        artifact = replacement if aid == replacement.id else state.p1.artifacts.get(aid)
        if artifact is None:
            raise ValueError(f"Unknown artifact dependency: {aid}")
        visiting.add(aid)
        for parent in artifact.read_artifacts:
            visit(parent)
        visiting.remove(aid)
        visited.add(aid)

    visit(replacement.id)


def publish_artifact(
    state: SearchState, *, run_id: str, artifact_id: str,
    kind: Literal["task", "section", "report"], content: str,
    support_sets: list[list[str]] | None = None,
) -> Artifact:
    execution = assert_execution_current(state, run_id)
    if not artifact_id.strip():
        raise ValueError("Artifact ID must be nonempty")
    if kind == "task" and artifact_id != f"task:{execution.task_id}":
        raise ValueError("An execution may publish only its own task result")
    if kind == "report" and (artifact_id != "report:answer" or execution.task_id != "__answer__"):
        raise ValueError("The answer execution owns the final report")
    current = state.p1.artifacts.get(artifact_id)
    if current is not None and (
        current.kind != kind or current.scope_key != execution.scope_key
    ):
        raise ValueError("Artifact kind and use scope are immutable")
    if ((current.epoch if current else 0) != execution.output_epochs.get(artifact_id, 0)
            or (current.version if current else 0)
            != execution.output_versions.get(artifact_id, 0)):
        raise StaleExecutionError("Output changed after execution began")
    reads = dict(execution.read_evidence)
    parents = {aid: version for aid, version in execution.read_artifacts.items()
               if aid != artifact_id}
    if current is not None and artifact_id in execution.read_artifacts:
        # An edit consumed the previous version of this same output. Unfold
        # that version's lineage before removing the self edge, or a plain
        # rewording would accidentally sever all original source dependencies.
        reads = {**current.read_evidence, **reads}
        parents = {**current.read_artifacts, **parents}
    previous_support = (current.support_sets if current is not None
                        and artifact_id in execution.read_artifacts else [])
    supports = [list(dict.fromkeys(group)) for group in (
        support_sets if support_sets is not None else previous_support
    )]
    if any(not group or not set(group) <= reads.keys() for group in supports):
        raise ValueError("Support alternatives must be nonempty subsets of actual evidence reads")
    artifact = Artifact(
        id=artifact_id, kind=kind, scope_key=execution.scope_key,
        epoch=current.epoch if current else 0, version=(current.version if current else 0) + 1,
        content=content, producer_task_id=execution.task_id,
        read_evidence=reads,
        read_artifacts=parents,
        support_sets=supports,
    )
    _validate_acyclic(state, artifact)
    state.p1.artifacts[artifact_id] = artifact
    state.p1.artifact_history.setdefault(artifact_id, []).append(artifact.model_copy(deep=True))
    execution.output_epochs[artifact_id] = artifact.epoch
    execution.output_versions[artifact_id] = artifact.version
    if artifact_id in execution.read_artifacts:
        execution.read_artifacts[artifact_id] = artifact.version
    return artifact


def _checked_fact_scope(state: SearchState, eid: str) -> tuple[str, str, str]:
    node = state.evidence_graph.get_node(eid)
    annotation = state.evidence_graph.claim_annotations.get(eid)
    if (node is None or annotation is None or not annotation.verified
            or annotation.evidence_id != eid
            or annotation.evidence_fingerprint != evidence_fingerprint(node)
            or not _source_grounding(state, node)["valid"]):
        raise ValueError("Revision requires fresh, source-grounded checked claim annotations")
    expected = (annotation.entity, annotation.attribute, annotation.value)
    # Independent schema facts retain the entity/attribute/value projection on
    # cell bindings. Their fresh source-reviewed annotation supplies these
    # fields; an empty raw field is not evidence of an incompatible assertion.
    actual = (node.entity, node.attribute, node.value)
    if any(not normalize_text(a) or (normalize_text(b) and normalize_text(a) != normalize_text(b))
           for a, b in zip(expected, actual)) or not annotation.scope_key.strip():
        raise ValueError("Claim annotation does not describe the original assertion")
    return tuple(normalize_text(value).casefold() for value in
                 (annotation.entity, annotation.attribute, annotation.scope_key))


def apply_revision(state: SearchState, event: ConfirmedEvidenceRevision) -> RevisionImpact:
    """Apply one externally confirmed revision atomically to dependency state.

    Validation precedes mutation. Call in a workspace transaction to combine
    this result with task rescheduling and hiding affected outline sections.
    """
    payload_fingerprint = _digest(event.model_dump(mode="json"))
    previous = state.p1.events.get(event.id)
    if previous is not None:
        if previous.payload_fingerprint != payload_fingerprint:
            raise ValueError("Revision ID reused with different contents")
        return previous.impact.model_copy(deep=True, update={"already_applied": True})
    if not event.confirmed or not all(item.strip() for item in (
        event.id, event.scope_key, event.reason, event.expected_old_version,
        event.expected_new_version,
    )):
        raise ValueError("Explicit confirmation, reason, scope and expected versions are required")
    old, new = event.old_evidence_id, event.new_evidence_id
    if old == new or not all(evidence_usable(state, eid, event.scope_key) for eid in (old, new)):
        raise ValueError("Revision requires two distinct, currently usable assertions")
    if (evidence_version(state, old) != event.expected_old_version
            or evidence_version(state, new) != event.expected_new_version):
        raise ValueError("Evidence changed since the revision was confirmed")
    if _checked_fact_scope(state, old) != _checked_fact_scope(state, new):
        raise ValueError("Replacement assertions must share entity, attribute and fact scope")

    affected = {
        aid for aid, artifact in state.p1.artifacts.items()
        if artifact.scope_key == event.scope_key
        and (old in artifact.read_evidence or any(old in group for group in artifact.support_sets))
    }
    while True:
        consumers = {aid for aid, item in state.p1.artifacts.items()
                     if affected.intersection(item.read_artifacts)}
        if consumers <= affected:
            break
        affected.update(consumers)
    invalidated = {
        rid for rid, execution in state.p1.executions.items()
        if not execution.closed and (
            (execution.scope_key == event.scope_key and old in execution.read_evidence)
            or affected.intersection(execution.read_artifacts)
            or f"task:{execution.task_id}" in affected
        )
    }
    task_ids = {state.p1.executions[rid].task_id for rid in invalidated}
    for aid in affected:
        item = state.p1.artifacts[aid]
        if item.producer_task_id:
            task_ids.add(item.producer_task_id)
        if item.kind == "task" and aid.startswith("task:"):
            task_ids.add(aid.removeprefix("task:"))

    state.p1.enabled = True
    state.p1.retired_uses.setdefault(event.scope_key, []).append(old)
    surviving = {}
    for aid in sorted(affected):
        artifact = state.p1.artifacts[aid]
        artifact.status = "stale"
        artifact.epoch += 1
        surviving[aid] = [group[:] for group in artifact.support_sets
                          if group and all(evidence_usable(state, eid, artifact.scope_key)
                                           and artifact.read_evidence.get(eid)
                                           == evidence_version(state, eid)
                                           for eid in group)]
    for rid in invalidated:
        state.p1.executions[rid].invalidated = True
    impact = RevisionImpact(
        revision_id=event.id, affected_artifact_ids=sorted(affected),
        affected_task_ids=sorted(task_ids), surviving_support_sets=surviving,
        unchanged_ids=sorted(state.p1.artifacts.keys() - affected),
        invalidated_execution_ids=sorted(invalidated),
    )
    state.p1.events[event.id] = AppliedRevision(
        payload_fingerprint=payload_fingerprint, event=event.model_copy(deep=True),
        impact=impact.model_copy(deep=True),
    )
    return impact
