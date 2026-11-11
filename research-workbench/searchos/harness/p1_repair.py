"""Confirmed, scoped P1 repair on the production SOCM transaction boundary.

The caller supplies an explicit confirmation and the reviewed evidence versions.
This service does not judge truth or invoke a model. It queues affected work on
the normal Frontier; scheduler/runtime own execution and guarded publication.
"""

from __future__ import annotations

import json
import time
from typing import Any

from searchos.harness.repair_planner import (
    RepairTarget,
    deterministic_repair_plan,
    render_repair_task_prompt,
    validate_repair_plan,
)
from searchos.socm import FrontierTask, FrontierTaskStatus
from searchos.socm.coverage import refresh_cells
from searchos.socm.revision_state import ConfirmedEvidenceRevision, RevisionImpact
from searchos.socm.revisions import apply_revision


def _require_tracked_outputs(workspace: Any, state: Any) -> None:
    """Do not invent historical read sets when enabling P1 on an old workspace."""
    untracked = ["section:" + section.id for section in state.outline.sections
                 if section.content.strip() and "section:" + section.id not in state.p1.artifacts]
    report_path = workspace.path / "output" / "report.md"
    if ("report:answer" not in state.p1.artifacts and report_path.exists()
            and report_path.read_text(encoding="utf-8").strip()):
        untracked.append("report:answer")
    if untracked:
        raise ValueError("Cannot revise untracked outputs; rebuild them with P1 enabled first: "
                         + ", ".join(untracked))


def _queue_repairs(state: Any, event: ConfirmedEvidenceRevision,
                   impact: RevisionImpact) -> list[str]:
    by_id = {task.id: task for task in state.frontier.questions}
    affected_tasks = set(impact.affected_task_ids) - {"__answer__"}
    missing_producers = [aid for aid in impact.affected_artifact_ids
                         if not state.p1.artifacts[aid].producer_task_id
                         or state.p1.artifacts[aid].producer_task_id == "__answer__"]
    if missing_producers or "__answer__" in impact.affected_task_ids:
        # Stable ID makes confirmed event delivery idempotent. Insert directly:
        # normal writer dedup must not merge this with an unrelated active task.
        import hashlib

        task_id = "p1_write_" + hashlib.sha256(event.id.encode()).hexdigest()[:16]
        if task_id not in by_id:
            task = FrontierTask(
                id=task_id, question="Rebuild the answer affected by confirmed evidence revision.",
                kind="write", agent_type="writer_agent", created_by="p1_revision",
                priority=1.0, p1_scope_key=event.scope_key,
                task_prompt="Rebuild only the affected answer/sections: "
                + ", ".join(missing_producers or ["report:answer"]),
            )
            state.frontier.questions.append(task)
            by_id[task_id] = task
        affected_tasks.add(task_id)
    now = time.time()
    for task_id in sorted(affected_tasks):
        task = by_id.get(task_id)
        if task is None:
            raise ValueError(f"Affected producer task is missing: {task_id}")
        own_artifacts = [artifact for artifact in state.p1.artifacts.values()
                         if artifact.id in impact.affected_artifact_ids
                         and (artifact.producer_task_id == task_id
                              or artifact.id == "task:" + task_id)]
        parents = {aid for artifact in own_artifacts for aid in artifact.read_artifacts
                   if aid in impact.affected_artifact_ids
                   and state.p1.artifacts[aid].producer_task_id != task_id}
        if task_id.startswith("p1_write_"):
            parents.update(aid for aid in impact.affected_artifact_ids
                           if state.p1.artifacts[aid].kind == "task"
                           and state.p1.artifacts[aid].producer_task_id in affected_tasks)
        task.p1_requires_artifacts = sorted(parents)
        parent_tasks = {state.p1.artifacts[aid].producer_task_id for aid in parents}
        original_parents = set(task.blocked_by) - set(task.p1_blocked_by)
        task.p1_blocked_by = sorted(parent_tasks - original_parents - {"", "__answer__", task_id})
        task.blocked_by = sorted((original_parents | parent_tasks)
                                 - {"", "__answer__", task_id})
        task.status = FrontierTaskStatus.BLOCKED if parents else FrontierTaskStatus.PENDING
        task.assigned_agent_id = ""
        task.attempts = 0
        task.not_before = 0.0
        task.resolution = ""
        task.last_agent_report_excerpt = ""
        task.p1_repair_revision = event.id
        task.priority = max(task.priority, 0.9)
        task.updated_at = now
        targets = []
        for key in task.target_cells:
            if "/" not in key and task.table_id:
                key = task.table_id + "/" + key
            target = state.coverage_map.cell_binding(key)
            if target and key in state.coverage_map.cells:
                targets.append(RepairTarget(
                    table_id=target[0], entity=target[1], attribute=target[2]))
        scope_prompt = ""
        if targets:
            plans = validate_repair_plan(deterministic_repair_plan(targets), targets)
            scope_prompt = "\n".join(render_repair_task_prompt(plan) for plan in plans)
        task.task_prompt = (
            f"[Confirmed evidence revision {event.id}]\n"
            f"Revalidate this task in use scope {task.p1_scope_key!r}: {task.question}\n"
            f"Evidence {event.old_evidence_id} is retired only for scope {event.scope_key!r}; "
            f"review replacement {event.new_evidence_id}. Reason: {event.reason}\n"
            "Use current evidence and rebuilt dependencies. Rebuild only affected outputs. "
            "Existing independent support may be reused; "
            "collect new material only for actual gaps.\n"
            "Affected outputs: " + ", ".join(sorted(
                artifact.id for artifact in own_artifacts
            ) or (missing_producers if task_id.startswith("p1_write_") else [])) + "\n"
            + scope_prompt
        )
    # An artifact DAG can collapse into a producer-task cycle when one task
    # publishes several stages. This version reuses task IDs, so reject such a
    # plan before committing rather than silently dropping required repairs.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("Repair task dependency cycle; split the producer stages first")
        if task_id in visited or task_id not in by_id:
            return
        task = by_id[task_id]
        if task.status not in {FrontierTaskStatus.PENDING, FrontierTaskStatus.BLOCKED,
                               FrontierTaskStatus.RUNNING}:
            return
        visiting.add(task_id)
        for parent in task.blocked_by:
            visit(parent)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in affected_tasks:
        visit(task_id)
    return sorted(affected_tasks)


def _apply(state: Any, event: ConfirmedEvidenceRevision) -> tuple[RevisionImpact, list[str]]:
    impact = apply_revision(state, event)
    if impact.already_applied:
        return impact, sorted(task.id for task in state.frontier.questions
                              if task.p1_repair_revision == event.id)
    for section in state.outline.sections:
        artifact = state.p1.artifacts.get("section:" + section.id)
        if artifact and artifact.id in impact.affected_artifact_ids:
            section.p1_status = "stale"
            section.p1_epoch = artifact.epoch
    if event.scope_key == "answer":
        keys = {key for key, cell in state.coverage_map.cells.items()
                if event.old_evidence_id in cell.supporting_evidence_ids}
        refresh_cells(state, keys)
    if state.p2.enabled:
        from searchos.harness.p2_memory import refresh_experiences

        refresh_experiences(state)
    return impact, _queue_repairs(state, event, impact)


def preview_revision(workspace: Any, event: ConfirmedEvidenceRevision) -> dict[str, Any]:
    """Validate the exact confirmed event against a detached state, without writes."""
    state = workspace.load_state().model_copy(deep=True)
    _require_tracked_outputs(workspace, state)
    impact, queued = _apply(state, event)
    return {"impact": impact.model_dump(mode="json"), "queued_task_ids": queued}


def confirm_revision(workspace: Any, event: ConfirmedEvidenceRevision) -> dict[str, Any]:
    """Retire a use, invalidate descendants and queue repairs atomically."""
    result = {}

    def commit(state):
        _require_tracked_outputs(workspace, state)
        impact, queued = _apply(state, event)
        result.update(impact=impact.model_dump(mode="json"), queued_task_ids=queued)
        if not impact.already_applied and hasattr(workspace, "write_output"):
            # Rendered current views omit stale content; full bodies remain in
            # SOCM and artifact_history for audit. No background process starts.
            if any(aid.startswith("section:") for aid in impact.affected_artifact_ids):
                workspace.write_output("draft.md", state.outline.rendered())
            if "report:answer" in impact.affected_artifact_ids:
                workspace.write_output(
                    "report.md", "Evidence revision confirmed; answer rebuild pending.\n")
                workspace.write_output("result.json", json.dumps({
                    "mode": "p1_repair_pending", "answer": "", "revision_id": event.id,
                }, ensure_ascii=False))
        return state

    workspace.atomic_update_state(commit)
    return result
