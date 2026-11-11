"""Bind durable P1 execution tickets to real agent and tool calls."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from searchos.config.run_config import current_run_config

current_run_id: ContextVar[str] = ContextVar("searchos_p1_run_id", default="")


@contextmanager
def execution_context(run_id: str):
    """A child task inherits this ticket; always restore the caller's context."""
    token = current_run_id.set(run_id)
    try:
        yield
    finally:
        current_run_id.reset(token)


def begin_run(ws: Any, task_id: str, run_id: str, scope_key: str = "answer") -> str:
    """Create a run only for an enabled, persisted Frontier task."""
    if ws is None or not task_id:
        return ""
    from searchos.socm.revisions import begin_execution

    if not callable(getattr(ws, "load_state", None)):
        if current_run_config().enable_p1_evidence_reasoning or current_run_config().enable_p2_context_memory:
            raise TypeError("P1 execution requires a stateful workspace")
        return ""
    snapshot = ws.load_state()
    if not (
        current_run_config().enable_p1_evidence_reasoning
        or current_run_config().enable_p2_context_memory
        or getattr(getattr(snapshot, "p1", None), "enabled", False)
    ):
        return ""

    def update(state):
        begin_execution(state, task_id=task_id, run_id=run_id, scope_key=scope_key)
        return state

    ws.atomic_update_state(update)
    return run_id


def assert_current(ws: Any):
    """Raise StaleExecutionError instead of letting a retired run publish."""
    run_id = current_run_id.get()
    if not run_id or ws is None:
        return None
    return assert_current_state(ws.load_state())


def assert_current_state(state: Any):
    """The same publication barrier for an already locked state transaction."""
    run_id = current_run_id.get()
    if not run_id:
        return None
    from searchos.socm.revisions import assert_execution_current

    return assert_execution_current(state, run_id)


def current_scope(state: Any) -> str:
    execution = assert_current_state(state)
    return execution.scope_key if execution is not None else "answer"


def continue_run(ws: Any, previous_run_id: str, run_id: str) -> str:
    """Continue valid retained history with a fresh ticket; stale history must restart."""
    if not previous_run_id:
        return ""
    from searchos.socm.revisions import (
        StaleExecutionError,
        begin_execution,
        record_reads,
        snapshot_artifacts,
        snapshot_evidence,
        source_scope_fingerprint,
    )

    def update(state):
        previous = state.p1.executions.get(previous_run_id)
        if previous is None:
            raise StaleExecutionError("Continuation has no original execution ticket")
        task_artifact = state.p1.artifacts.get(f"task:{previous.task_id}")
        task = next((q for q in state.frontier.questions if q.id == previous.task_id), None)
        if (
            previous.invalidated
            or task_artifact is None
            or task_artifact.epoch != previous.task_epoch
            or task is None
            or task.assigned_agent_id != previous.assigned_agent_id
            or previous.source_scope_fingerprint != source_scope_fingerprint(state)
            or snapshot_evidence(state, previous.read_evidence, scope_key=previous.scope_key)
            != previous.read_evidence
            or snapshot_artifacts(state, previous.read_artifacts) != previous.read_artifacts
        ):
            raise StaleExecutionError("Retained history is stale; start a fresh task graph")
        begin_execution(
            state, task_id=previous.task_id, run_id=run_id, scope_key=previous.scope_key
        )
        record_reads(
            state, run_id, evidence=previous.read_evidence, artifacts=previous.read_artifacts
        )
        return state

    ws.atomic_update_state(update)
    return run_id


def record_snapshot(
    ws: Any,
    snapshot: Any,
    *,
    evidence_ids: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
) -> None:
    """Record exactly the versions rendered from the supplied state snapshot."""
    run_id = current_run_id.get()
    if not run_id or ws is None:
        return
    from searchos.socm.revisions import (
        assert_execution_current,
        record_reads,
        snapshot_artifacts,
        snapshot_evidence,
    )

    execution = assert_execution_current(snapshot, run_id)
    evidence = snapshot_evidence(snapshot, list(evidence_ids), scope_key=execution.scope_key)
    artifacts = snapshot_artifacts(snapshot, list(artifact_ids))

    def update(state):
        record_reads(state, run_id, evidence=evidence, artifacts=artifacts)
        return state

    ws.atomic_update_state(update)
