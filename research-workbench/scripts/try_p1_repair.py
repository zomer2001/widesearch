#!/usr/bin/env python3
"""Offline confirmed-revision repair across real SearchOS state/tool boundaries.

Evidence and worker text are explicit synthetic fixtures. The scheduler calls a
fixed executor in place of an LLM graph; it still runs production read tools,
execution tickets, writer tools and lifecycle completion. No model or network
service is used. This verifies integration, not model quality or source truth.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from searchos.agents.orchestrator import lifecycle  # noqa: E402
from searchos.agents.orchestrator.scheduler import Scheduler  # noqa: E402
from searchos.agents.runtime import set_orchestrator_context  # noqa: E402
from searchos.config.settings import settings  # noqa: E402
from searchos.harness.p1_repair import confirm_revision, preview_revision  # noqa: E402
from searchos.harness.p1_reports import publish_report  # noqa: E402
from searchos.harness.p1_runtime import (  # noqa: E402
    begin_run,
    execution_context,
    record_snapshot,
)
from searchos.socm import FrontierTask, FrontierTaskStatus, SearchState  # noqa: E402
from searchos.socm.claim_annotations import (  # noqa: E402
    EvidenceClaimAnnotation,
    evidence_fingerprint,
)
from searchos.socm.coverage import commit_evidence  # noqa: E402
from searchos.socm.evidence import EvidenceNode, SourceObservation  # noqa: E402
from searchos.socm.revision_state import ConfirmedEvidenceRevision  # noqa: E402
from searchos.socm.revisions import (  # noqa: E402
    StaleExecutionError,
    artifact_usable,
    evidence_usable,
    evidence_version,
    publish_artifact,
)
from searchos.socm.workspace import WorkspaceManager  # noqa: E402
from searchos.tools import search_state  # noqa: E402
from searchos.tools.socm_read import read_evidence  # noqa: E402
from searchos.tools.writer import read_outline, write_section  # noqa: E402

CELL = "institutes/Acme.Leader"


def seed_workspace(root: Path) -> WorkspaceManager:
    """Create a unique saved workspace, using actual evidence admission."""
    ws = WorkspaceManager(root.resolve())
    ws.create()
    state = SearchState(intent="Identify Acme's current leader and founding year.",
                        source_scope={"mode": "web", "documents": []})
    state.p1.enabled = True
    state.coverage_map.initialize(
        ["Acme"], ["Name", "Leader", "Founded"], primary_key=["Name"], table_id="institutes",
    )
    for eid, attribute, value in (
        ("old", "Leader", "Alice"), ("new", "Leader", "Bob"),
        ("backup", "Leader", "Bob"), ("independent", "Founded", "1999"),
    ):
        quote = f"[Synthetic source] Acme {attribute}: {value}."
        source = f"https://example.invalid/p1-repair/{eid}"
        observation = SourceObservation.capture(source, quote)
        state.evidence_graph.observations.append(observation)
        node = EvidenceNode(
            id=eid, finding=quote, entity="Acme", attribute=attribute, value=value,
            table_id="institutes", source=source, source_excerpt=quote, alignment="full",
            observation_id=observation.id, span=(0, len(quote)),
            text_hash="sha256:" + hashlib.sha256(quote.encode()).hexdigest(),
        )
        commit_evidence(state, [node])
        state.evidence_graph.claim_annotations[eid] = EvidenceClaimAnnotation(
            evidence_id=eid, evidence_fingerprint=evidence_fingerprint(node),
            entity="Acme", attribute=attribute, value=value,
            scope_key="institute-wide " + attribute, verified=True,
            reason="Explicit synthetic source-support fixture; no model validation claimed.",
        )
    state.frontier.questions = [
        FrontierTask(id="leader", question="Check current leader", target_cells=[CELL],
                     table_id="institutes"),
        FrontierTask(id="writer", question="Write leadership section", kind="write",
                     agent_type="writer_agent"),
        FrontierTask(id="independent", question="Check founding year"),
    ]
    ws.save_state(state)
    return ws


@contextmanager
def tool_context(ws, agent_id, run_id):
    workspace_token = search_state._workspace_var.set(ws)
    agent_token = search_state._current_agent_var.set(agent_id)
    try:
        with execution_context(run_id):
            yield
    finally:
        search_state._current_agent_var.reset(agent_token)
        search_state._workspace_var.reset(workspace_token)


def start_worker(ws, task_id, agent_id, run_id):
    def assign(state):
        task = next(t for t in state.frontier.questions if t.id == task_id)
        task.assigned_agent_id = agent_id
        task.status = FrontierTaskStatus.RUNNING
        return state

    ws.atomic_update_state(assign)
    begin_run(ws, task_id, run_id)
    lifecycle._ctx.agent_graphs[agent_id] = {
        "agent_type": "writer_agent" if agent_id.startswith("writer_agent") else "search_agent",
        "assigned_task_id": task_id, "p1_run_id": run_id,
    }


def reviewed_event(ws, *, event_id="synthetic-leader-correction"):
    state = ws.load_state()
    return ConfirmedEvidenceRevision(
        id=event_id, old_evidence_id="old", new_evidence_id="new",
        reason="Synthetic external confirmation: Alice's assertion is inapplicable to this answer; "
               "recheck current leadership against Bob's source.",
        confirmed=True, expected_old_version=evidence_version(state, "old"),
        expected_new_version=evidence_version(state, "new"),
    )


async def build_baseline(ws):
    """Save source-derived baseline outputs before the confirmation arrives."""
    for task_id, ids, text in (
        ("leader", ["old", "backup"], "Leadership evidence collected; draft uses Alice."),
        ("independent", ["independent"], "Acme was founded in 1999."),
    ):
        agent_id, run_id = f"search_agent:initial-{task_id}", f"initial-{task_id}"
        start_worker(ws, task_id, agent_id, run_id)
        with tool_context(ws, agent_id, run_id):
            await read_evidence.ainvoke({"evidence_ids": ",".join(ids)})

            def complete(state):
                publish_artifact(
                    state, run_id=run_id, artifact_id="task:" + task_id,
                    kind="task", content=text, support_sets=[[eid] for eid in ids],
                )
                task = next(t for t in state.frontier.questions if t.id == task_id)
                task.status = FrontierTaskStatus.COMPLETED
                task.resolution = text
                state.p1.executions[run_id].closed = True
                return state

            ws.atomic_update_state(complete)

    start_worker(ws, "writer", "writer_agent:initial", "initial-writer")
    before = lifecycle._snapshot_state_for_diff(ws.load_state())
    with tool_context(ws, "writer_agent:initial", "initial-writer"):
        await read_evidence.ainvoke({"evidence_ids": "old"})
        record_snapshot(ws, ws.load_state(), artifact_ids=["task:leader"])
        written = json.loads(await write_section.ainvoke({
            "section_id": "leadership", "content": "Acme's current leader is Alice.",
            "cited_evidence_ids": "old",
        }))
        assert written["ok"]
        lifecycle._compute_agent_report(
            agent_id="writer_agent:initial", thread_id="initial-writer", pre_snapshot=before,
            started_at=time.time(), status="completed", result="Baseline draft written.",
            last_ai_text="Leadership section prepared from collected evidence.",
        )
    begin_run(ws, "__answer__", "initial-answer")
    with tool_context(ws, "main", "initial-answer"):
        await read_outline.ainvoke({"section_id": "leadership"})
        record_snapshot(ws, ws.load_state(), artifact_ids=["task:independent"])
        publish_report(ws, ws.load_state(), "Acme's current leader is Alice; founded in 1999.",
                       {"answer": "Alice; 1999", "mode": "synthetic"})


class FixedRepairExecutor:
    """Replace only worker spawning/model execution; keep real lifecycle writes."""

    def __init__(self, ws):
        self.ws = ws
        self.dispatches = []

    async def spawn(self, agent_type, prompt, skills, *, bind_task_id, **kwargs):
        index = len(self.dispatches) + 1
        agent_id = f"{agent_type}:fixed-repair-{index}"
        run_id = f"repair-run-{index}"
        self.dispatches.append({"task_id": bind_task_id, "agent_type": agent_type,
                                "run_id": run_id})
        start_worker(self.ws, bind_task_id, agent_id, run_id)
        before = lifecycle._snapshot_state_for_diff(self.ws.load_state())
        with tool_context(self.ws, agent_id, run_id):
            task = next(t for t in self.ws.load_state().frontier.questions if t.id == bind_task_id)
            record_snapshot(self.ws, self.ws.load_state(), artifact_ids=task.p1_requires_artifacts)
            await read_evidence.ainvoke({"evidence_ids": "old,new,backup"})
            if bind_task_id == "writer":
                written = json.loads(await write_section.ainvoke({
                    "section_id": "leadership", "content": "Acme's current leader is Bob.",
                    "cited_evidence_ids": "new,backup",
                }))
                assert written["ok"]
            elif agent_type == "writer_agent":
                # The replacement model would synthesize the final response.
                # Actual report publication occurs through p1_reports below.
                await read_outline.ainvoke({"section_id": "leadership"})
            report = lifecycle._compute_agent_report(
                agent_id=agent_id, thread_id=run_id, pre_snapshot=before,
                started_at=time.time(), status="completed", result="Fixed repair completed.",
                last_ai_text="Current evidence identifies Bob; unrelated 1999 finding is retained.",
            )
            lifecycle._ctx.completed[agent_id] = report
        return agent_id


async def run_demo(output_root: Path) -> dict:
    ws = seed_workspace(output_root)
    set_orchestrator_context(ws, model=None)
    await build_baseline(ws)
    before = ws.load_state()
    before_sources = before.evidence_graph.model_dump(mode="json")
    independent = before.p1.artifacts["task:independent"].model_dump(mode="json")

    # A dispatched worker has consumed old evidence but has not returned yet.
    start_worker(ws, "leader", "search_agent:late", "late-worker")
    with tool_context(ws, "search_agent:late", "late-worker"):
        await read_evidence.ainvoke({"evidence_ids": "old"})

    event = reviewed_event(ws)
    before_preview = ws.path.joinpath("search_state.json").read_bytes()
    preview = preview_revision(ws, event)
    preview_read_only = before_preview == ws.path.joinpath("search_state.json").read_bytes()
    confirmation = confirm_revision(ws, event)
    invalidated = ws.load_state()
    draft_after_revision = (ws.path / "output" / "draft.md").read_text()
    report_after_revision = (ws.path / "output" / "report.md").read_text()
    repeated_before = ws.path.joinpath("search_state.json").read_bytes()
    repeated = confirm_revision(ws, event)
    repeated_unchanged = repeated_before == ws.path.joinpath("search_state.json").read_bytes()

    late_rejected = False
    try:
        lifecycle._compute_agent_report(
            agent_id="search_agent:late", thread_id="late-worker",
            pre_snapshot=lifecycle._snapshot_state_for_diff(before), started_at=time.time(),
            status="completed", result="Late Alice result", last_ai_text="Alice",
        )
    except StaleExecutionError:
        late_rejected = True

    # Reopen the actual saved workspace before scheduling any repair.
    ws = WorkspaceManager(ws.path.parent, ws.path.name)
    set_orchestrator_context(ws, model=None)
    executor = FixedRepairExecutor(ws)
    scheduler = Scheduler(task_allowlist=set(confirmation["queued_task_ids"]))
    ticks = []
    with (patch.object(settings, "enable_scheduler", True),
          patch.object(settings, "enable_writer_agent", True),
          patch.object(settings, "spawn_stagger_s", 0),
          patch.object(lifecycle, "_spawn_sub_agent", executor.spawn)):
        for _ in range(6):
            ticks.append(await scheduler.tick())
            tasks = {t.id: t for t in ws.load_state().frontier.questions}
            if all(tasks[tid].status == FrontierTaskStatus.COMPLETED
                   for tid in confirmation["queued_task_ids"]):
                break

    begin_run(ws, "__answer__", "repaired-answer")
    with tool_context(ws, "main", "repaired-answer"):
        section = json.loads(await read_outline.ainvoke({"section_id": "leadership"}))
        record_snapshot(ws, ws.load_state(), artifact_ids=["task:independent"])
        report = section["content"] + " Acme was founded in 1999."
        publish_report(ws, ws.load_state(), report, {"answer": report, "mode": "synthetic"})

    final = WorkspaceManager(ws.path.parent, ws.path.name).load_state()
    tasks = {task.id: task for task in final.frontier.questions}
    impact = confirmation["impact"]
    checks = {
        "preview_is_read_only": preview_read_only,
        "preview_matches_confirmed_impact": preview["impact"] == impact,
        "sources_and_original_observations_preserved": (
            final.evidence_graph.model_dump(mode="json") == before_sources),
        "retirement_is_scoped": not evidence_usable(final, "old")
        and evidence_usable(final, "old", "archive"),
        "affected_summary_section_and_report_invalidated": {
            "task:leader", "task:writer", "section:leadership", "report:answer",
        } <= set(impact["affected_artifact_ids"]),
        "alternative_support_survives_without_automatic_truth_claim": (
            impact["surviving_support_sets"]["task:leader"] == [["backup"]]
            and invalidated.p1.artifacts["task:leader"].status == "stale"),
        "stale_output_views_hidden": "Alice" not in draft_after_revision
        and "Alice" not in report_after_revision,
        "late_worker_rejected": late_rejected,
        "repeat_confirmation_is_idempotent": repeated["impact"]["already_applied"]
        and repeated_unchanged,
        "independent_branch_unchanged": (
            final.p1.artifacts["task:independent"].model_dump(mode="json") == independent),
        "native_scheduler_dispatched_affected_tasks_only": (
            {item["task_id"] for item in executor.dispatches}
            == set(confirmation["queued_task_ids"])),
        "all_queued_repairs_completed_through_lifecycle": all(
            tasks[tid].status == FrontierTaskStatus.COMPLETED
            for tid in confirmation["queued_task_ids"]),
        "repaired_outputs_are_current": all(artifact_usable(final, aid) for aid in
            ("task:leader", "task:writer", "section:leadership", "report:answer")),
        "old_and_new_publications_retained": (
            "Alice" in final.p1.artifact_history["section:leadership"][0].content
            and "Bob" in final.p1.artifacts["section:leadership"].content),
        "final_report_rebuilt_from_current_section": (
            "Bob" in final.p1.artifacts["report:answer"].content
            and "Alice" not in final.p1.artifacts["report:answer"].content),
        "final_report_persisted": (ws.path / "output" / "report.md").read_text() == report,
    }
    result_file = ws.path / "repair-demo-result.json"
    result = {
        "passed": all(checks.values()), "checks": checks,
        "boundary": "Synthetic sources and fixed worker executor; real WorkspaceManager, "
                    "SOCM reads, writer tools, scheduler, lifecycle completion and report guards. "
                    "No model, model download, network access or real source-quality evaluation.",
        "workspace": str(ws.path), "state_file": str(ws.path / "search_state.json"),
        "report_file": str(ws.path / "output" / "report.md"), "result_file": str(result_file),
        "impact": impact, "dispatches": executor.dispatches, "scheduler_ticks": ticks,
    }
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=REPO / "artifacts" / "p1-repair-demo")
    args = parser.parse_args()
    result = asyncio.run(run_demo(args.output_root))
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "result_file": result["result_file"]}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
