"""P2 values actual acquisition feedback, and withdraws stale support."""

import hashlib

import pytest

from searchos.harness.p2_memory import (
    build_context_packet,
    record_task_experience,
    refresh_experiences,
    sync_goals,
    verify_packet,
)
from searchos.socm.coverage import CellStatus, CoverageCell
from searchos.socm.evidence import EvidenceNode, SourceObservation
from searchos.socm.frontier import FrontierTask, FrontierTaskStatus
from searchos.socm.revisions import (
    StaleExecutionError,
    begin_execution,
    publish_artifact,
    record_reads,
    snapshot_evidence,
)
from searchos.socm.state import SearchState


def fixture():
    state = SearchState(intent="Compare Acme battery performance. Preserve units and dates.")
    state.p2.enabled = True
    state.frontier.questions = [
        FrontierTask(
            id="collect",
            question="Acme battery capacity official datasheet",
            status=FrontierTaskStatus.COMPLETED,
            assigned_agent_id="worker-1",
            attempt_queries=["Acme battery capacity site:acme.example"],
            target_cells=["_default/Acme.capacity"],
            route_history=[
                {
                    "worker": "worker-1",
                    "signature": '["search", {"query": "Acme battery"}]',
                    "outcome": "success",
                },
                {"worker": "previous-worker", "signature": "other", "outcome": "success"},
            ],
        ),
        FrontierTask(
            id="next",
            question="Acme battery capacity and lifetime",
            target_cells=["_default/Acme.capacity"],
        ),
    ]
    add_fact(state, "battery", "Acme battery capacity is 10 kWh.")
    state.coverage_map.cells["_default/Acme.capacity"] = CoverageCell(
        status=CellStatus.FILLED, supporting_evidence_ids=["battery"], best_alignment="full"
    )
    return state


def add_fact(state, eid, quote, source=None):
    source = source or "https://acme.example/" + eid
    obs = SourceObservation.capture(source, quote)
    state.evidence_graph.observations.append(obs)
    state.evidence_graph.add_node(
        EvidenceNode(
            id=eid,
            source=source,
            finding=quote,
            source_excerpt=quote,
            observation_id=obs.id,
            span=(0, len(quote)),
            text_hash="sha256:" + hashlib.sha256(quote.encode()).hexdigest(),
        )
    )


def record(state, **kwargs):
    params = dict(execution_id="run-1", baseline_evidence_ids=[], baseline_filled_cells=[])
    params.update(kwargs)
    return record_task_experience(state, "collect", "worker-1", ["battery"], **params)


def test_real_feedback_is_persisted_and_supplied_with_actual_dependencies():
    state = fixture()
    card = record(state, duration_s=3.5, query_count=2)
    assert card.status == "active"
    assert card.feedback.credited_evidence_ids == ["battery"]
    assert card.feedback.newly_filled_cells == ["_default/Acme.capacity"]
    assert card.feedback.token_count is None
    assert len(card.routes) == 1 and card.routes[0]["worker"] == "worker-1"
    packet = build_context_packet(state, "next")
    assert packet.experience_ids == [card.id]
    assert packet.read_evidence == card.evidence_versions
    assert "10 kWh" not in packet.content  # strategies, not answer copying
    assert "causal guarantee" in packet.content
    verify_packet(state, packet)
    reloaded = SearchState.model_validate_json(state.model_dump_json())
    assert build_context_packet(reloaded, "next") == packet


def test_default_off_does_not_record_and_legacy_state_loads():
    state = fixture()
    del_data = state.model_dump()
    del del_data["p2"]
    legacy = SearchState.model_validate(del_data)
    assert not legacy.p2.enabled
    assert record(legacy) is None
    assert not legacy.p2.experiences


def test_goals_and_user_corrections_remain_verbatim_when_experience_budget_is_zero():
    state = fixture()
    constraint = "只收集2024—2025年；保留原始单位，不得推算缺失值。"
    update = "改为只比较电池容量，不比较价格。"
    sync_goals(state, user_messages=[update], hard_constraints=[constraint])
    version = state.p2.goal.version
    sync_goals(state, user_messages=[update], hard_constraints=[constraint])
    assert state.p2.goal.version == version
    record(state)
    packet = build_context_packet(
        state, "next", max_chars=0, user_messages=(text for text in ["再排除试验样机。 "])
    )
    assert (
        state.intent in packet.content and constraint in packet.content and update in packet.content
    )
    assert "再排除试验样机。 " in packet.content
    assert not packet.experience_ids and not packet.read_evidence
    verify_packet(state, packet)


def test_same_execution_is_idempotent_and_changed_feedback_cannot_overwrite():
    state = fixture()
    first = record(state)
    snapshot = state.model_dump_json()
    assert record(state) == first
    assert state.model_dump_json() == snapshot
    with pytest.raises(ValueError, match="reused"):
        record(state, query_count=10)


def test_duplicate_evidence_does_not_create_repeated_positive_credit():
    state = fixture()
    record(state)
    second = record(state, execution_id="run-2")
    assert second.status == "insufficient_feedback"
    assert second.feedback.credited_evidence_ids == []
    assert second.feedback.repeated_evidence_count == 1
    assert build_context_packet(state, "next").experience_ids == ["experience:run-1"]


def test_missing_before_snapshot_does_not_fabricate_novelty_or_cells_filled():
    state = fixture()
    card = record(state, baseline_evidence_ids=None, baseline_filled_cells=None)
    assert card.feedback.new_evidence_ids is None
    assert card.feedback.newly_filled_cells is None
    assert card.status == "insufficient_feedback"


def test_preexisting_evidence_does_not_earn_new_credit():
    state = fixture()
    card = record(state, baseline_evidence_ids=["battery"])
    assert card.feedback.new_evidence_ids == []
    assert card.feedback.repeated_evidence_count == 1
    assert card.status == "insufficient_feedback"


@pytest.mark.parametrize("mode", ["self_report", "unanchored", "corrupt_window", "no_route"])
def test_positive_model_report_and_hit_count_cannot_substitute_for_feedback(mode):
    state = fixture()
    task = state.frontier.questions[0]
    task.resolution = "Perfect strategy! 100% correct and 10000 search hits!"
    node = state.evidence_graph.get_node("battery")
    if mode == "self_report":
        node.source = "agent://worker-1"
    elif mode == "unanchored":
        node.span = None
    elif mode == "corrupt_window":
        state.evidence_graph.observations[0].text += "corrupt"
    else:
        task.attempt_queries = []
        task.route_history = []
    card = record(state)
    assert card.status == "insufficient_feedback"
    assert not build_context_packet(state, "next").experience_ids


def test_irrelevant_successful_history_is_not_injected_into_specific_task():
    state = fixture()
    state.frontier.questions[0].question = "Banana farming fertilizer dosage"
    state.frontier.questions[0].attempt_queries = ["banana potassium fertilizer"]
    state.frontier.questions[0].target_cells = ["farm/Banana.fertilizer"]
    record(state)
    assert not build_context_packet(state, "next").experience_ids


def test_chinese_relevance_is_supported_without_embeddings():
    state = fixture()
    state.frontier.questions[0].question = "卫星通信载荷频段与速率"
    state.frontier.questions[0].attempt_queries = ["卫星通信 官方 速率"]
    state.frontier.questions[0].target_cells = []
    state.frontier.questions[1].question = "核验卫星通信速率"
    record(state)
    assert build_context_packet(state, "next").experience_ids


@pytest.mark.parametrize("mutation", ["retired", "changed", "source_scope", "parent"])
def test_stale_feedback_cannot_be_reused_and_history_is_retained(mutation):
    state = fixture()
    if mutation == "parent":
        begin_execution(state, task_id="collect", run_id="p1")
        record_reads(state, "p1", evidence=snapshot_evidence(state, ["battery"]), artifacts={})
        publish_artifact(
            state, run_id="p1", artifact_id="task:collect", kind="task", content="report"
        )
        state.p1.executions["p1"].closed = True
    card = record(state)
    packet = build_context_packet(state, "next")
    if mutation == "retired":
        state.p1.retired_uses["answer"] = ["battery"]
    elif mutation == "changed":
        state.evidence_graph.get_node("battery").finding += " now corrected"
    elif mutation == "source_scope":
        state.source_scope = {"mode": "local", "documents": []}
    else:
        state.p1.artifacts["task:collect"].status = "stale"
    with pytest.raises(StaleExecutionError):
        verify_packet(state, packet)
    assert not build_context_packet(state, "next").experience_ids  # even before refresh
    assert refresh_experiences(state) == [card.id]
    assert card.status == "revalidation_required"
    assert card.feedback.credited_evidence_ids == ["battery"]
    assert card.invalidation_reasons
    assert refresh_experiences(state) == []


def test_retirement_is_use_scoped_and_does_not_condemn_the_strategy():
    state = fixture()
    state.frontier.questions[0].p1_scope_key = "archive"
    state.frontier.questions[1].p1_scope_key = "archive"
    record(state)
    state.p1.retired_uses["answer"] = ["battery"]
    assert build_context_packet(state, "next").experience_ids
    assert not build_context_packet(state, "next", scope_key="answer").experience_ids
    assert refresh_experiences(state) == []


def test_concurrent_user_goal_or_card_changes_reject_old_packet():
    state = fixture()
    card = record(state)
    packet = build_context_packet(state, "next")
    sync_goals(state, user_messages=["Only 2025 models."])
    with pytest.raises(StaleExecutionError, match="goal"):
        verify_packet(state, packet)
    packet = build_context_packet(state, "next")
    card.queries.append("unobserved edited query")
    with pytest.raises(StaleExecutionError, match="experience"):
        verify_packet(state, packet)


def test_experience_budget_never_truncates_partial_card_or_goal():
    state = fixture()
    record(state)
    full = build_context_packet(state, "next", max_chars=5000)
    exact = build_context_packet(state, "next", max_chars=full.experience_chars)
    short = build_context_packet(state, "next", max_chars=full.experience_chars - 1)
    assert exact.experience_ids == full.experience_ids
    assert not short.experience_ids
    assert short.experience_chars == 0
    assert state.intent in short.content


def test_existing_or_ambiguous_cell_support_cannot_be_claimed_as_exclusive_new_fill():
    state = fixture()
    add_fact(state, "prior", "An independent source agrees.")
    state.coverage_map.cells["_default/Acme.capacity"].supporting_evidence_ids.append("prior")
    card = record(state)
    assert card.feedback.newly_filled_cells == []
    assert card.feedback.unique_supported_cells == []


def test_unknown_cost_is_not_rendered_as_free_and_invalid_cost_is_unknown():
    state = fixture()
    card = record(state, duration_s=-1, query_count=-1, token_count=-2)
    assert card.feedback.duration_s is None
    assert card.feedback.query_count is None
    assert card.feedback.token_count is None
    assert "query_count: None" in build_context_packet(state, "next").content


def test_completed_task_owner_and_published_parent_are_required():
    state = fixture()
    state.frontier.questions[0].assigned_agent_id = "new-owner"
    with pytest.raises(ValueError, match="owner"):
        record(state)
    state.frontier.questions[0].assigned_agent_id = "worker-1"
    state.p1.enabled = True
    with pytest.raises(StaleExecutionError, match="published"):
        record(state)
    assert not state.p2.experiences


def test_gap_progress_changes_packet_snapshot_but_not_the_goal_identity():
    state = fixture()
    before = build_context_packet(state, "next")
    state.coverage_map.cells["_default/Acme.capacity"].status = CellStatus.MISSING
    after = build_context_packet(state, "next")
    assert before.goal_fingerprint == after.goal_fingerprint
    assert before.state_fingerprint != after.state_fingerprint
    with pytest.raises(StaleExecutionError, match="gaps"):
        verify_packet(state, before)


def test_repeated_user_reversal_is_preserved_in_order_and_old_prefix_cannot_undo_it():
    state = fixture()
    sync_goals(state, user_messages=["Only A", "Only B"])
    sync_goals(state, user_messages=["Only A", "Only B", "Only A"])
    assert state.p2.goal.user_messages == ["Only A", "Only B", "Only A"]
    version = state.p2.goal.version
    sync_goals(state, user_messages=["Only A", "Only B"])
    assert state.p2.goal.version == version
    packet = build_context_packet(state, user_messages=["Only A", "Only B", "Only A"])
    assert '["Only A", "Only B", "Only A"]' in packet.content
    verify_packet(state, packet)


def test_failed_routes_are_retained_for_audit_but_never_positive_examples():
    state = fixture()
    state.frontier.questions[0].route_history[0]["outcome"] = "permanent_error"
    card = record(state)
    assert card.routes[0]["outcome"] == "permanent_error"
    assert card.status == "insufficient_feedback"
    assert not build_context_packet(state, "next").experience_ids


def test_diversity_penalty_avoids_selecting_only_the_same_source_family():
    state = fixture()
    for index, host in ((1, "independent.example"), (2, "acme.example"), (3, "acme.example")):
        eid = "fact-" + str(index)
        add_fact(
            state, eid, f"Battery capacity observation {index}.", "https://" + host + "/" + eid
        )
        record_task_experience(
            state,
            "collect",
            "worker-1",
            [eid],
            execution_id=f"run-{index}",
            baseline_evidence_ids=[],
            baseline_filled_cells=[],
        )
    packet = build_context_packet(state, "next", max_experiences=2, max_chars=5000)
    assert packet.experience_ids == ["experience:run-3", "experience:run-1"]
    assert packet.experience_chars <= 5000


def test_shared_tool_instructions_do_not_make_unrelated_experience_relevant():
    state = fixture()
    other = state.frontier.questions[0]
    other.question = "Banana fertilizer official website"
    other.attempt_queries = ["Banana fertilizer official website"]
    other.target_cells = []
    state.frontier.questions[1].task_prompt = (
        "Acme battery capacity. Use sources; collect information. "
        "Banana fertilizer generic example."
    )
    record(state)
    assert not build_context_packet(state, "next").experience_ids
