"""P2 compaction: durable goals, bounded experience and invalidation in real graphs."""

import asyncio
from copy import deepcopy

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import add_messages

from searchos.agents.runtime import create_search_agent_graph
from searchos.harness.middleware.context.compaction_context_middleware import (
    CompactionContextMiddleware,
)
from searchos.harness.middleware.context.utils.compaction import count_context_tokens
from searchos.harness.middleware.context.utils.messages import request_overhead_tokens
from searchos.harness.p1_runtime import execution_context
from searchos.socm import EvidenceNode, FrontierTask, SearchState
from searchos.socm.context_memory import ExperienceCard, ExperienceFeedback
from searchos.socm.revisions import (
    StaleExecutionError,
    begin_execution,
    evidence_version,
    source_scope_fingerprint,
)
from searchos.socm.workspace import WorkspaceManager


class Summarizer:
    def __init__(self, result="Read the annual filing; compare the remaining revenue figures."):
        self.result = result
        self.requests = []

    async def ainvoke(self, messages):
        self.requests.append(messages)
        return AIMessage(content=self.result)


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def workspace(tmp_path, name="p2", goal="Compare revenue of A and B."):
    ws = WorkspaceManager(tmp_path, name)
    ws.create()
    state = SearchState(intent=goal)
    state.p2.enabled = True
    node = EvidenceNode(
        id="e0",
        finding="Revenue is 42.",
        value="42",
        source="https://example.org/report",
        source_excerpt="Revenue is 42.",
    )
    state.evidence_graph.add_node(node)
    state.p2.experiences["experience:one"] = ExperienceCard(
        id="experience:one",
        execution_id="one",
        payload_fingerprint="payload",
        task_id="prior",
        agent_id="search-one",
        question="Compare revenue from annual reports",
        source_scope_fingerprint=source_scope_fingerprint(state),
        queries=["annual report revenue official filing"],
        sources=[node.source],
        evidence_versions={node.id: evidence_version(state, node.id)},
        feedback=ExperienceFeedback(new_evidence_ids=["e0"], credited_evidence_ids=["e0"]),
    )
    ws.save_state(state)
    return ws


def component(ws, summary=None, **kwargs):
    return CompactionContextMiddleware(
        summary or Summarizer(),
        context_window=6000,
        trigger_fraction=0.75,
        keep_recent_fraction=0.08,
        summary_fraction=0.05,
        reserve_fraction=0.05,
        workspace=ws,
        p2_enabled=True,
        **kwargs,
    )


def history():
    return [
        SystemMessage(content="Use only source-supported evidence."),
        HumanMessage(content="Compare revenue of A and B."),
        AIMessage(content="", tool_calls=[{"id": "a", "name": "read", "args": {}}]),
        ToolMessage(content="Old reading notes " * 1000, tool_call_id="a"),
        HumanMessage(content="Correction: report 2025 revenue only."),
        AIMessage(content="", tool_calls=[{"id": "b", "name": "read", "args": {}}]),
        ToolMessage(content="Latest report: revenue 42.", tool_call_id="b"),
    ]


def text(messages):
    return "\n".join(m.text for m in messages)


def graph(mw, saver=None):
    return create_search_agent_graph(
        model=Model(responses=[AIMessage(content="Continue with the current goal.")]),
        tools=[],
        middleware=[mw],
        checkpointer=saver,
    )


@pytest.mark.asyncio
async def test_graph_preserves_goal_when_summary_omits_it_and_injects_grounded_strategy(tmp_path):
    ws = workspace(tmp_path)
    summarizer = Summarizer("Completed one reading; more work remains.")
    result = await graph(component(ws, summarizer)).ainvoke(
        {"messages": history()}, {"configurable": {"thread_id": "goal-preserved"}}
    )
    packet = [m for m in result["messages"] if m.additional_kwargs.get("sf_kind") == "p2_context"]
    assert len(packet) == 1
    assert "Compare revenue of A and B." in packet[0].content
    assert "Correction: report 2025 revenue only." in packet[0].content
    assert "annual report revenue official filing" in packet[0].content
    assert result["p2_context"]["read_evidence"]["e0"] == evidence_version(ws.load_state(), "e0")
    assert result["p2_context"]["dependency_mode"] == "conservative_workspace_snapshot"
    assert result["context_compaction"]["summary_model_calls"] > 0
    assert summarizer.requests
    assert ws.load_state().p2.compaction_metrics[-1]["summary_model_calls"] > 0
    assert [m.tool_call_id for m in result["messages"] if m.type == "tool"] == ["b"]


@pytest.mark.asyncio
async def test_retirement_discards_old_summary_and_generated_history_without_resummarizing(
    tmp_path,
):
    ws = workspace(tmp_path)
    summary = Summarizer("POISONED_OLD_CONCLUSION")
    mw = component(ws, summary)
    first = await mw.abefore_model({"messages": history()}, None)
    previous = add_messages([], first["messages"])
    assert "POISONED_OLD_CONCLUSION" in text(previous)
    state = ws.load_state()
    state.p1.retired_uses["answer"] = ["e0"]
    ws.save_state(state)
    summary.requests.clear()
    update = await mw.abefore_model({"messages": previous, "p2_context": first["p2_context"]}, None)
    fresh = add_messages([], update["messages"])
    assert "POISONED_OLD_CONCLUSION" not in text(fresh)
    assert "annual report revenue official filing" not in text(fresh)
    assert not summary.requests
    assert not any(m.type in {"tool", "ai"} for m in fresh)
    assert "Correction: report 2025 revenue only." in text(fresh)
    assert ws.load_state().p2.experiences["experience:one"].status == "revalidation_required"
    assert update["context_compaction"]["rebuild_count"] == 1


@pytest.mark.asyncio
async def test_goal_change_rebuilds_but_worker_prompt_never_becomes_human_correction(tmp_path):
    ws = workspace(tmp_path)
    mw = component(ws)
    first = await mw.abefore_model({"messages": history()}, None)
    previous = add_messages([], first["messages"])
    update = await mw.abefore_model(
        {
            "messages": [*previous, HumanMessage("Only company A now.")],
            "p2_context": first["p2_context"],
        },
        None,
    )
    assert update["context_compaction"]["rebuild_count"] == 1
    state = ws.load_state()
    state.frontier.add(FrontierTask(id="worker", question="Read company A report"))
    ws.save_state(state)
    worker = component(ws, task_id="worker")
    await worker.abefore_model({"messages": [HumanMessage("AUTOMATED SEARCH ASSIGNMENT")]}, None)
    assert "AUTOMATED SEARCH ASSIGNMENT" not in ws.load_state().p2.goal.user_messages
    assert "Only company A now." in ws.load_state().p2.goal.user_messages


@pytest.mark.asyncio
async def test_revision_during_summary_rejects_handoff_without_history_mutation(tmp_path):
    ws = workspace(tmp_path)

    class Revising(Summarizer):
        async def ainvoke(self, messages):
            state = ws.load_state()
            state.p1.retired_uses["answer"] = ["e0"]
            ws.save_state(state)
            return AIMessage(content="STALE_NEW_SUMMARY")

    original = history()
    saved = deepcopy(original)
    with pytest.raises(StaleExecutionError):
        await component(ws, Revising()).abefore_model({"messages": original}, None)
    assert original == saved
    assert not any("STALE_NEW_SUMMARY" in str(e) for e in ws.load_state().p2.compaction_metrics)


@pytest.mark.asyncio
async def test_execution_read_set_records_selected_strategy_and_is_not_all_store(tmp_path):
    ws = workspace(tmp_path)
    state = ws.load_state()
    state.evidence_graph.add_node(
        EvidenceNode(id="unread", finding="Never shown", source="https://example.org/other")
    )
    begin_execution(state, task_id="__answer__", run_id="run")
    ws.save_state(state)
    with execution_context("run"):
        result = await component(ws).abefore_model({"messages": history()}, None)
    assert set(result["p2_context"]["read_evidence"]) == {"e0"}
    assert set(ws.load_state().p1.executions["run"].read_evidence) == {"e0"}
    assert result["p2_context"]["dependency_mode"] == "execution_read_set"


@pytest.mark.asyncio
async def test_graph_checkpoint_resume_and_independent_threads_do_not_mix_goals(tmp_path):
    saver = MemorySaver()
    ws = workspace(tmp_path, "first")
    config = {"configurable": {"thread_id": "first"}}
    first = await graph(component(ws), saver).ainvoke({"messages": history()}, config)
    rebuilt = graph(component(ws), saver)
    resumed = await rebuilt.ainvoke({"messages": [HumanMessage("Continue")]}, config)
    assert (
        len([m for m in resumed["messages"] if m.additional_kwargs.get("sf_kind") == "p2_context"])
        == 1
    )
    other_ws = workspace(tmp_path, "second", "Investigate battery chemistry.")
    other = await graph(component(other_ws), saver).ainvoke(
        {"messages": [HumanMessage("Investigate battery chemistry.")]},
        {"configurable": {"thread_id": "second"}},
    )
    assert "revenue" not in text(other["messages"])
    assert "battery" not in text(first["messages"])
    assert other_ws.load_state().p2.goal.user_messages == ["Investigate battery chemistry."]


@pytest.mark.asyncio
async def test_actual_request_budget_includes_system_tools_and_keeps_pairs(tmp_path):
    ws = workspace(tmp_path)
    mw = component(ws)
    # Fits the message-only threshold, but substantial tool definitions and
    # actual system instructions make request-level compression necessary.
    messages = history()
    messages[3] = ToolMessage(content="reading " * 600, tool_call_id="a")
    before = await mw.abefore_model({"messages": messages}, None)
    messages = add_messages([], before["messages"])
    request = ModelRequest(
        model=Model(responses=[AIMessage("ok")]),
        messages=messages,
        system_message=SystemMessage("System instructions. " * 170),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read sources " * 200,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        state={"messages": messages, "p2_context": before["p2_context"]},
    )
    seen = []

    async def handler(actual):
        seen.append(actual)
        return "ok"

    assert await mw.awrap_model_call(request, handler) == "ok"
    assert any(
        event["mode"] == "request" and event["summary_model_calls"] > 0
        for event in ws.load_state().p2.compaction_metrics
    )
    actual = seen[0]
    assert (
        count_context_tokens(actual.messages) + request_overhead_tokens(actual) + mw.reserve_tokens
        < mw.context_window
    )
    assert "Correction: report 2025 revenue only." in text(actual.messages)
    calls = {call["id"] for m in actual.messages for call in getattr(m, "tool_calls", [])}
    assert all(m.tool_call_id in calls for m in actual.messages if m.type == "tool")


@pytest.mark.asyncio
async def test_impossible_authoritative_goal_budget_raises_instead_of_truncating(tmp_path):
    ws = workspace(tmp_path, goal="mandatory constraint " * 2000)
    with pytest.raises(ValueError, match="Authoritative P2 goal"):
        await component(ws).abefore_model({"messages": [HumanMessage("Continue")]}, None)
    assert ws.load_state().intent == "mandatory constraint " * 2000


@pytest.mark.asyncio
async def test_timeout_never_commits_partial_handoff(tmp_path):
    ws = workspace(tmp_path)

    class Slow(Summarizer):
        async def ainvoke(self, messages):
            await asyncio.sleep(10)

    messages = history()
    original = deepcopy(messages)
    with pytest.raises(TimeoutError):
        await component(ws, Slow(), timeout=0.01).abefore_model({"messages": messages}, None)
    assert messages == original
    assert ws.load_state().p2.compaction_metrics[-1]["outcome"] == "rejected"
    assert ws.load_state().p2.compaction_metrics[-1]["summary_model_calls"] == 1


@pytest.mark.asyncio
async def test_disabled_middleware_preserves_original_small_history_noop(tmp_path):
    ws = workspace(tmp_path)
    summary = Summarizer()
    mw = CompactionContextMiddleware(summary, context_window=6000, workspace=ws, p2_enabled=False)
    assert await mw.abefore_model({"messages": [HumanMessage("Hi")]}, None) is None
    assert not summary.requests
    assert not ws.load_state().p2.goal.user_messages


@pytest.mark.asyncio
async def test_experience_feedback_change_invalidates_even_when_fact_version_is_unchanged(tmp_path):
    ws = workspace(tmp_path)
    mw = component(ws, Summarizer("OLD_STRATEGY_CONCLUSION"))
    first = await mw.abefore_model({"messages": history()}, None)
    previous = add_messages([], first["messages"])
    state = ws.load_state()
    version = evidence_version(state, "e0")
    state.p2.experiences["experience:one"].status = "insufficient_feedback"
    ws.save_state(state)
    fresh = await mw.abefore_model({"messages": previous, "p2_context": first["p2_context"]}, None)
    assert evidence_version(ws.load_state(), "e0") == version
    assert "OLD_STRATEGY_CONCLUSION" not in text(add_messages([], fresh["messages"]))
    assert fresh["context_compaction"]["rebuild_count"] == 1


@pytest.mark.asyncio
async def test_another_workspace_cannot_reuse_same_goal_summary_checkpoint(tmp_path):
    first_ws = workspace(tmp_path, "original")
    first = await component(first_ws, Summarizer("WORKSPACE_PRIVATE_HISTORY")).abefore_model(
        {"messages": history()}, None
    )
    second_ws = workspace(tmp_path, "independent")
    reused = await component(second_ws).abefore_model(
        {"messages": add_messages([], first["messages"]), "p2_context": first["p2_context"]},
        None,
    )
    assert "WORKSPACE_PRIVATE_HISTORY" not in text(add_messages([], reused["messages"]))
    assert reused["context_compaction"]["rebuild_count"] == 1


@pytest.mark.asyncio
async def test_normal_gap_progress_refreshes_packet_without_discarding_history(tmp_path):
    ws = workspace(tmp_path)
    state = ws.load_state()
    state.coverage_map.add_table("t", ["Revenue"], entities=["A"])
    ws.save_state(state)
    mw = component(ws, Summarizer("VALID_HISTORY_REMAINS"), max_experiences=0)
    first = await mw.abefore_model({"messages": history()}, None)
    state = ws.load_state()
    from searchos.socm.coverage import CellStatus

    for cell in state.coverage_map.cells.values():
        cell.status = CellStatus.FILLED
    ws.save_state(state)
    continued = await mw.abefore_model(
        {"messages": add_messages([], first["messages"]), "p2_context": first["p2_context"]},
        None,
    )
    assert "VALID_HISTORY_REMAINS" in text(add_messages([], continued["messages"]))
    assert not continued.get("context_compaction", {}).get("rebuild_count")


@pytest.mark.asyncio
async def test_graph_settles_own_extraction_and_records_reads_before_summary(tmp_path):
    from scripts.try_p1_pipeline import ENTITY, FIXTURES, TABLE, FixedFixtureModel
    from searchos.harness.middleware.extraction import (
        EvidenceObservation,
        ExtractionConfig,
        ExtractionCoordinator,
        WebEvidenceMiddleware,
        WorkspaceEvidenceStore,
    )
    from searchos.harness.middleware.models import TaskContext

    ws = workspace(tmp_path)
    state = ws.load_state()
    state.p2.experiences = {}
    state.coverage_map.initialize(
        entities=[ENTITY],
        attributes=["Organization", "Leader"],
        table_id=TABLE,
        primary_key=["Organization"],
    )
    state.frontier.add(
        FrontierTask(
            id="collector",
            question="Collect leader facts",
            assigned_agent_id="collector-agent",
            table_id=TABLE,
        )
    )
    begin_execution(state, task_id="collector", run_id="acquisition-run")
    ws.save_state(state)
    task = TaskContext(task_id="collector", agent_id="collector-agent", target_table=TABLE)
    coordinator = ExtractionCoordinator(
        FixedFixtureModel(WorkspaceEvidenceStore(ws)),
        workspace=ws,
        task=task,
        policy="schema_facts",
        batch_n=1,
        config=ExtractionConfig(
            dual_mode=False, cross_table_backfill=False, p1_evidence_reasoning=True
        ),
    )
    seen = []

    class CheckingSummary(Summarizer):
        async def ainvoke(self, messages):
            snapshot = ws.load_state()
            own_ids = set(coordinator.committed_node_ids)
            assert len(own_ids) == 1
            assert own_ids <= snapshot.evidence_graph.claim_annotations.keys()
            reads = snapshot.p1.executions["acquisition-run"].read_evidence
            assert set(reads) == own_ids
            assert "e0" not in reads  # another task's store fact was not consumed
            seen.append(own_ids)
            return AIMessage(content="Collected a source-supported leader statement.")

    mw = component(ws, CheckingSummary(), task_id="collector", max_experiences=0)
    g = create_search_agent_graph(
        model=Model(responses=[AIMessage(content="Continue collecting.")]),
        tools=[],
        middleware=[mw, WebEvidenceMiddleware(coordinator)],
        workspace=ws,
    )
    fixture = FIXTURES[0]
    with execution_context("acquisition-run"):
        await coordinator.submit(
            EvidenceObservation(
                content=fixture.excerpt,
                source_url=fixture.source,
                page_id=fixture.key,
                target_table=TABLE,
                task_id="collector",
            )
        )
        result = await g.ainvoke(
            {"messages": history()}, {"configurable": {"thread_id": "acquisition"}}
        )
    assert seen
    assert set(result["p2_context"]["read_evidence"]) == set(coordinator.committed_node_ids)
    assert "e0" not in result["p2_context"]["read_evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["evidence", "source_scope", "goal"])
async def test_real_graph_rejects_tool_call_when_request_changes_during_generation(
    tmp_path, change
):
    from langchain_core.tools import tool

    from searchos.harness.p2_memory import sync_goals

    ws = workspace(tmp_path)
    invoked = []

    @tool
    def next_search(query: str) -> str:
        """Record the next acquisition query without contacting any service."""
        invoked.append(query)
        return "No network used."

    class RevisingModel(Model):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            result = await super()._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            state = ws.load_state()
            if change == "evidence":
                state.p1.retired_uses["answer"] = ["e0"]
            elif change == "source_scope":
                state.source_scope = {"mode": "local", "documents": []}
            else:
                sync_goals(state, user_messages=["Compare revenue of A and B.", "Only A now."])
            ws.save_state(state)
            return result

    g = create_search_agent_graph(
        model=RevisingModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "stale-call",
                            "name": "next_search",
                            "args": {"query": "OLD_SCOPE_QUERY"},
                        }
                    ],
                )
            ]
        ),
        tools=[next_search],
        middleware=[component(ws)],
        workspace=ws,
    )
    with pytest.raises(StaleExecutionError):
        await g.ainvoke(
            {"messages": [HumanMessage("Compare revenue of A and B.")]},
            {"configurable": {"thread_id": "late-action"}},
        )
    assert invoked == []


@pytest.mark.asyncio
async def test_compactor_keeps_repeated_human_scope_reversal_in_exact_order(tmp_path):
    ws = workspace(tmp_path)
    mw = component(ws)
    messages = [HumanMessage("Only A"), HumanMessage("Only B"), HumanMessage("Only A")]
    first = await mw.abefore_model({"messages": messages}, None)
    second = await mw.abefore_model(
        {"messages": add_messages([], first["messages"]), "p2_context": first["p2_context"]},
        None,
    )
    assert ws.load_state().p2.goal.user_messages == ["Only A", "Only B", "Only A"]
    packet = next(
        m
        for m in add_messages([], second["messages"])
        if m.additional_kwargs.get("sf_kind") == "p2_context"
    )
    assert '["Only A", "Only B", "Only A"]' in packet.content
