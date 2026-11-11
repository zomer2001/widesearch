#!/usr/bin/env python3
"""Offline P2 integration: real extraction/lifecycle/compactor with fixed models.

The source documents, search outcomes and model responses are synthetic fixtures.
No model service is called. This demonstrates the code boundaries and guards,
not measured model quality, search effectiveness or source truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from scripts.try_p1_pipeline import (  # noqa: E402
    CELL_KEY,
    ENTITY,
    FIXTURES,
    TABLE,
    FixedFixtureModel,
)
from scripts.try_p1_repair import tool_context  # noqa: E402
from searchos.agents.orchestrator import lifecycle  # noqa: E402
from searchos.agents.runtime import set_orchestrator_context  # noqa: E402
from searchos.harness.middleware.context.compaction_context_middleware import (  # noqa: E402
    CompactionContextMiddleware,
)
from searchos.harness.middleware.extraction import (  # noqa: E402
    DeliveryMode,
    EvidenceObservation,
    ExtractionConfig,
    ExtractionCoordinator,
    WorkspaceEvidenceStore,
)
from searchos.harness.p1_repair import confirm_revision  # noqa: E402
from searchos.harness.p1_runtime import begin_run  # noqa: E402
from searchos.harness.p2_memory import build_context_packet, verify_packet  # noqa: E402
from searchos.socm import FrontierTask, FrontierTaskStatus, SearchState  # noqa: E402
from searchos.socm.retry_routes import (  # noqa: E402
    check_and_record_route,
    record_route_outcome,
    route_signature,
)
from searchos.socm.revision_state import ConfirmedEvidenceRevision  # noqa: E402
from searchos.socm.revisions import StaleExecutionError, evidence_version  # noqa: E402
from searchos.socm.workspace import WorkspaceManager  # noqa: E402
from searchos.tools.socm_read import read_evidence  # noqa: E402

CORRECTION = (
    "Use the 2025 leadership period only. Preserve effective dates; do not invent missing dates."
)
SUMMARY = "Historical note: the previous source identifies Alice as the institute leader."


class OmitGoalSummary:
    """Intentionally omit the goal to test the deterministic protected block."""

    profile = {"max_input_tokens": 4000}

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content=SUMMARY)


async def collect_fixture(ws, *, index: int, task_id: str) -> tuple[str, dict]:
    """Execute real admission and completion around a fixed tool/model boundary."""
    fixture = FIXTURES[index]
    agent_id, run_id = "search_agent:" + task_id, "p2-" + task_id

    def assign(state):
        task = next(task for task in state.frontier.questions if task.id == task_id)
        task.assigned_agent_id = agent_id
        task.status = FrontierTaskStatus.RUNNING
        return state

    ws.atomic_update_state(assign)
    begin_run(ws, task_id, run_id)
    baseline = lifecycle._snapshot_state_for_diff(ws.load_state())
    store = WorkspaceEvidenceStore(ws)
    fixed_model = FixedFixtureModel(store)
    coordinator = ExtractionCoordinator(
        fixed_model,
        store=store,
        policy="schema_facts",
        config=ExtractionConfig(
            dual_mode=False, cross_table_backfill=False, p1_evidence_reasoning=True
        ),
    )
    lifecycle._ctx.agent_graphs[agent_id] = {
        "agent_type": "search_agent",
        "assigned_task_id": task_id,
        "p1_run_id": run_id,
        "extractions": [coordinator],
    }
    started_at = time.time()
    with tool_context(ws, agent_id, run_id):
        args = {"query": f"{ENTITY} leader {2024 + index} site:example.invalid"}
        assert check_and_record_route(ws, agent_id, "search", args) == ""
        # The search executor is a fixture. Real production route telemetry is
        # used, but success here is explicitly synthetic, not a web search.
        record_route_outcome(ws, agent_id, route_signature("search", args), "success")
        receipt = await coordinator.submit(
            EvidenceObservation(
                content=fixture.excerpt,
                source_url=fixture.source,
                page_id=fixture.key,
                target_table=TABLE,
                task_id=task_id,
            ),
            delivery=DeliveryMode.SYNC,
        )
        await coordinator.finalize()
        assert len(receipt.committed_node_ids) == 1
        lifecycle._compute_agent_report(
            agent_id=agent_id,
            thread_id=run_id,
            pre_snapshot=baseline,
            started_at=started_at,
            status="completed",
            result="Synthetic collection completed.",
            last_ai_text=f"Synthetic source records {fixture.leader}; "
            "effective period is explicit.",
        )
    return receipt.committed_node_ids[0], {
        "task_id": task_id,
        "run_id": run_id,
        "before_evidence_ids": sorted(baseline["evidence_ids"]),
        "before_filled_cells": sorted(baseline["filled_keys"]),
        "own_committed_ids": sorted(coordinator.committed_node_ids),
        "fixed_extraction_model_calls": list(fixed_model.calls),
    }


def compactor(ws, model):
    return CompactionContextMiddleware(
        model,
        context_window=4000,
        summary_fraction=0.08,
        workspace=ws,
        task_id="__answer__",
        p2_enabled=True,
        max_experiences=3,
        experience_max_chars=2600,
    )


async def run_demo(output_root: Path) -> dict:
    ws = WorkspaceManager(output_root.resolve())
    ws.create()
    state = SearchState(
        intent=f"Identify the leadership of {ENTITY} and cite the effective period.",
        source_scope={"mode": "web", "documents": []},
    )
    state.p1.enabled = state.p2.enabled = True
    state.coverage_map.initialize(
        [ENTITY], ["Organization", "Leader"], table_id=TABLE, primary_key=["Organization"]
    )
    state.frontier.questions = [
        FrontierTask(
            id="archive",
            question=f"Find {ENTITY} leadership evidence for 2024.",
            table_id=TABLE,
            target_cells=[CELL_KEY],
        ),
        FrontierTask(
            id="current",
            question=f"Find {ENTITY} leadership evidence for 2025.",
            table_id=TABLE,
            target_cells=[CELL_KEY],
        ),
    ]
    ws.save_state(state)
    set_orchestrator_context(ws, model=None)
    old_id, initial_collection = await collect_fixture(ws, index=0, task_id="archive")
    first_card = ws.load_state().p2.experiences["experience:p2-archive"]
    model = OmitGoalSummary()
    before_compaction = compactor(ws, model)
    begin_run(ws, "__answer__", "p2-main-before")
    with tool_context(ws, "main", "p2-main-before"):
        source_read = await read_evidence.ainvoke({"evidence_ids": old_id})
        history = [
            HumanMessage(content=state.intent),
            HumanMessage(content=CORRECTION),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_evidence",
                        "args": {"evidence_ids": old_id},
                        "id": "fixture-read",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=source_read, tool_call_id="fixture-read"),
            *[
                AIMessage(
                    content=("Synthetic completed search trace; no extra source evidence. " * 110)
                )
                for _ in range(8)
            ],
        ]
        first_update = await before_compaction.abefore_model({"messages": history}, None)
    compacted = [
        message for message in first_update["messages"] if not isinstance(message, RemoveMessage)
    ]
    first_text = "\n".join(message.text for message in compacted)
    first_packet = build_context_packet(ws.load_state(), max_chars=2600)
    first_meta = first_update["p2_context"]
    # Persist actual compactor checkpoint data then reload it, so the test does
    # not rely on an in-memory cached summary or message list.
    checkpoint_file = ws.path / "p2-checkpoint-before.json"
    checkpoint_file.write_text(
        json.dumps(
            {
                "messages": messages_to_dict(compacted),
                "p2_context": first_meta,
                "context_compaction": first_update.get("context_compaction", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    initial_calls = model.calls

    new_id, replacement_collection = await collect_fixture(ws, index=1, task_id="current")
    before_revision = ws.load_state()
    original_sources = before_revision.evidence_graph.model_dump(mode="json")
    event = ConfirmedEvidenceRevision(
        id="p2-fixture-answer-period-correction",
        old_evidence_id=old_id,
        new_evidence_id=new_id,
        scope_key="answer",
        confirmed=True,
        reason="Synthetic external confirmation: answer is restricted to 2025; "
        "the 2024 assertion remains historical evidence but cannot support this answer.",
        expected_old_version=evidence_version(before_revision, old_id),
        expected_new_version=evidence_version(before_revision, new_id),
    )
    confirmation = confirm_revision(ws, event)
    stale_packet_rejected = False
    try:
        verify_packet(ws.load_state(), first_packet)
    except StaleExecutionError:
        stale_packet_rejected = True

    ws = WorkspaceManager(ws.path.parent, ws.path.name)
    set_orchestrator_context(ws, model=None)
    checkpoint = json.loads(checkpoint_file.read_text())
    checkpoint["messages"] = messages_from_dict(checkpoint["messages"])
    begin_run(ws, "__answer__", "p2-main-after")
    with tool_context(ws, "main", "p2-main-after"):
        rebuilt = await compactor(ws, model).abefore_model(checkpoint, None)
    final_messages = [
        message for message in rebuilt["messages"] if not isinstance(message, RemoveMessage)
    ]
    final_text = "\n".join(message.text for message in final_messages)
    final = ws.load_state()
    final_packet = build_context_packet(final, max_chars=2600)
    verify_packet(final, final_packet)
    metrics = final.p2.compaction_metrics
    checks = {
        "real_extraction_records_source_and_checked_annotation": bool(
            final.evidence_graph.claim_annotations[old_id].verified
        )
        and len(final.evidence_graph.observations) == 2,
        "dispatch_baseline_precedes_own_commit": old_id
        not in initial_collection["before_evidence_ids"]
        and initial_collection["own_committed_ids"] == [old_id],
        "lifecycle_records_one_grounded_new_evidence_credit": (
            first_card.feedback.credited_evidence_ids == [old_id]
        ),
        "unique_gap_fill_is_measured_against_dispatch_baseline": (
            first_card.feedback.newly_filled_cells == [CELL_KEY]
        ),
        "actual_attempt_route_is_retained": bool(first_card.routes)
        and first_card.routes[0]["outcome"] == "success",
        "unknown_cost_remains_unknown": first_card.feedback.token_count is None,
        "existing_compactor_actually_called_summary_boundary": initial_calls > 0,
        "goal_omitting_summary_cannot_erase_original_goal": state.intent not in SUMMARY
        and state.intent in first_text,
        "ordered_user_correction_survives_compression": CORRECTION not in SUMMARY
        and CORRECTION in first_text
        and CORRECTION in final_text,
        "relevant_experience_and_versions_enter_compacted_context": "experience:p2-archive"
        in first_text
        and old_id in first_meta["read_evidence"]
        and "task:archive" in first_meta["read_artifacts"],
        "compression_cost_and_token_estimates_are_recorded": any(
            item["summary_model_calls"] > 0 and item["after_tokens"] < item["before_tokens"]
            for item in metrics
        ),
        "confirmed_p1_revision_rejects_prior_packet": stale_packet_rejected,
        "old_experience_retained_for_revalidation": final.p2.experiences[
            "experience:p2-archive"
        ].status
        == "revalidation_required"
        and final.p2.experiences["experience:p2-archive"].feedback.credited_evidence_ids
        == [old_id],
        "old_experience_leaves_current_strategy_context": "experience:p2-archive"
        not in final_packet.experience_ids
        and "experience:p2-current" in final_packet.experience_ids,
        "persisted_stale_history_is_withdrawn_after_reopen": "P2 history rebuilt" in final_text
        and SUMMARY not in final_text,
        "rebuild_requires_no_additional_model_call": model.calls == initial_calls,
        "source_history_and_use_scoped_retirement_preserved": final.evidence_graph.model_dump(
            mode="json"
        )
        == original_sources
        and old_id in final.p1.retired_uses["answer"],
    }
    result_file = ws.path / "p2-demo-result.json"
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "boundary": "Synthetic sources/search outcomes and fixed extraction/summary models; "
        "real WorkspaceManager, extraction coordinator, grounding, route telemetry, "
        "task lifecycle, compaction checkpoints and P1 confirmation. "
        "No model service, network or quality benchmark.",
        "workspace": str(ws.path),
        "state_file": str(ws.path / "search_state.json"),
        "result_file": str(result_file),
        "checkpoint_file": str(checkpoint_file),
        "collections": [initial_collection, replacement_collection],
        "experience_before": first_card.model_dump(mode="json"),
        "selected_after": final_packet.experience_ids,
        "confirmation": confirmation,
        "compaction_metrics": metrics,
        "token_measurement": "SearchOS message token estimates, not model billing counters.",
        "summary_model_calls": model.calls,
    }
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    ws.write_output("p2-context-before.txt", first_text)
    ws.write_output("p2-context-after.txt", final_text)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=REPO / "artifacts" / "p2-demo")
    result = asyncio.run(run_demo(parser.parse_args().output_root))
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "checks": len(result["checks"]),
                "result_file": result["result_file"],
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
