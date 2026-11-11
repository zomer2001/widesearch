"""Version-checked final report publication and fresh finalization contexts."""

from __future__ import annotations

import json
import uuid
from functools import wraps

from searchos.config.run_config import current_run_config
from searchos.harness.p1_runtime import (
    begin_run,
    current_run_id,
    execution_context,
    record_snapshot,
)


def report_generation(function):
    """A tool-free finalization gets fresh inputs, not stale main-agent history."""

    @wraps(function)
    async def wrapped(*args, **kwargs):
        workspace = kwargs["workspace"]

        if not (current_run_config().enable_p1_evidence_reasoning or current_run_config().enable_p2_context_memory
                or kwargs["state"].p1.enabled):
            return await function(*args, **kwargs)
        run_id = begin_run(workspace, "__answer__", "report_" + uuid.uuid4().hex)
        with execution_context(run_id):
            return await function(*args, **{**kwargs, "state": workspace.load_state()})

    return wrapped


def publish_report(workspace, snapshot, report, result, *, evidence_ids=(), artifact_ids=()):
    """Validate the actual generation ticket inside the same SOCM write lock.

    SOCM is the authoritative versioned report. The two output files are views;
    this does not promise a multi-file crash-atomic filesystem transaction.
    """
    run_id = current_run_id.get()
    if not run_id:

        def legacy_commit(state):
            if (current_run_config().enable_p1_evidence_reasoning or current_run_config().enable_p2_context_memory
                    or snapshot.p1.enabled or state.p1.enabled):
                from searchos.socm.revisions import StaleExecutionError

                raise StaleExecutionError("Tracked report publication needs an execution ticket")
            workspace.write_output("report.md", report)
            workspace.write_output("result.json", json.dumps(result, ensure_ascii=False, indent=2))
            return state

        # A concurrent enablement cannot slip between the check and file write.
        if callable(getattr(workspace, "atomic_update_state", None)):
            workspace.atomic_update_state(legacy_commit)
        else:
            legacy_commit(snapshot)
        return
    record_snapshot(workspace, snapshot, evidence_ids=evidence_ids, artifact_ids=artifact_ids)
    from searchos.socm.revisions import publish_artifact

    def commit(state):
        artifact = publish_artifact(
            state,
            run_id=run_id,
            artifact_id="report:answer",
            kind="report",
            content=report,
        )
        result["p1_report_version"] = artifact.version
        workspace.write_output("report.md", report)
        workspace.write_output("result.json", json.dumps(result, ensure_ascii=False, indent=2))
        return state

    workspace.atomic_update_state(commit)
