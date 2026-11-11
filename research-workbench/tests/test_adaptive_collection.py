"""Behavioral checks for the optional, source-grounded collection policy."""

import json

import pytest

from searchos.collection_policy import adapter, engine
from searchos.collection_policy.algorithms import relevance
from searchos.collection_policy.models import Candidate, CollectionState, Goal
from searchos.config.run_config import EffectiveRunConfig, normalize_module_config
from searchos.harness.middleware.extraction import InMemoryEvidenceStore
from searchos.socm import FrontierTask, SearchState
from searchos.socm.evidence import EvidenceNode, bind_task_evidence
from searchos.tools.simple_browser.search.base import SearchResult


def workspace():
    state = SearchState(
        intent="Lattice 通信接口 messaging API",
        research_mode="findings",
        module_config=normalize_module_config({"enabled": [adapter.MODULE_ID]}),
    )
    state.frontier.add(FrontierTask(id="task", question=state.intent, result_mode="findings"))
    return InMemoryEvidenceStore(state)


def fact(identity="f", claim="The API supports subscriptions."):
    return {"id": identity, "claim": claim}


def test_repeat_and_corroboration_do_not_count_as_new_information():
    state = CollectionState(goals={"t": Goal(text="API")})
    engine.observe(
        state,
        task_id="t",
        source="https://a.test/1",
        content="API manual",
        facts=[fact()],
        goal_status="partial",
    )
    engine.observe(
        state,
        task_id="t",
        source="https://b.test/1",
        content="Independent manual",
        facts=[fact("other")],
    )
    # A retry with a different transient evidence ID still has the same batch identity.
    engine.observe(
        state, task_id="t", source="https://a.test/1", content="API manual", facts=[fact("retry")]
    )
    restored = CollectionState.model_validate_json(state.model_dump_json())
    engine.observe(
        restored,
        task_id="t",
        source="https://a.test/1",
        content="API manual",
        facts=[fact("restart")],
    )
    assert len(restored.batches) == 2
    assert sum(b.new for b in restored.batches.values()) == 1
    assert sum(b.corroboration for b in restored.batches.values()) == 1
    assert restored.goals["t"].gap == 0.5


def test_missing_goal_assessment_stays_unknown_and_empty_feedback_is_not_completion():
    state = CollectionState(goals={"t": Goal(text="All products")})
    engine.observe(
        state, task_id="t", source="https://a.test", content="No relevant facts", facts=[]
    )
    assert state.goals["t"].gap is None
    assert engine.recent_gain(state, "t") == 0


def test_missing_goals_and_successful_branches_get_priority_without_deleting_tasks():
    state = CollectionState(
        goals={
            "complete": Goal(text="messaging API", gap=0),
            "missing": Goal(text="messaging API", gap=1),
        }
    )
    tasks = [
        {"id": key, "text": "messaging API", "priority": 0.5, "created_at": 10}
        for key in ("complete", "missing")
    ]
    assert engine.rank_tasks(state, tasks, intent="messaging API", now=10) == [
        "missing",
        "complete",
    ]
    # Both branches have equal gaps; successful recent reading should break the tie.
    state.goals["complete"].gap = 1
    engine.observe(state, task_id="complete", source="https://a.test", content="empty", facts=[])
    engine.observe(
        state, task_id="missing", source="https://b.test", content="new facts", facts=[fact()]
    )
    assert engine.rank_tasks(state, tasks, intent="messaging API", now=10)[0] == "missing"


def test_graph_and_mmr_prefer_new_relevant_material_over_already_read_copy():
    state = CollectionState(goals={"t": Goal(text="messaging API")})
    engine.observe(
        state,
        task_id="t",
        source="https://a.test/old",
        content="messaging API overview introduction",
        facts=[fact()],
    )
    candidates = [
        Candidate(url="https://a.test/copy", title="messaging API overview introduction"),
        Candidate(
            url="https://a.test/docs", title="messaging API protocol schema developer documentation"
        ),
        Candidate(url="https://a.test/news", title="office relocation annual celebration"),
    ]
    ordered = engine.rank_pages(state, candidates, query="messaging API", task_id="t")
    assert ordered[0] == "https://a.test/docs"
    assert set(ordered) == {c.url for c in candidates}
    assert state.communities
    assert all("cluster" in decision for decision in state.decisions)
    assert state.decisions[0]["relevance"] > 0


def test_chinese_relevance_and_nonmatching_documents():
    scores = relevance("通信接口", ["通信接口开发文档", "无人机产品宣传", "市场发布消息"])
    assert scores[0] > scores[1]
    assert scores[2] == 0


def test_graph_preserves_real_links_and_reuses_similarity_work(monkeypatch):
    from searchos.collection_policy import algorithms

    pair_calls = []
    original_similarity = algorithms.similarity
    original_pagerank = algorithms.nx.pagerank

    def similarity(left, right):
        pair_calls.append(1)
        return original_similarity(left, right)

    def pagerank(graph, **kwargs):
        assert graph["https://a.test/hub"]["https://a.test/docs"]["weight"] == 1
        return original_pagerank(graph, **kwargs)

    monkeypatch.setattr(algorithms, "similarity", similarity)
    monkeypatch.setattr(algorithms.nx, "pagerank", pagerank)
    candidates = [
        Candidate(
            url="https://a.test/hub", text="messaging architecture", links=["https://a.test/docs"]
        ),
        Candidate(url="https://a.test/docs", text="messaging protocol details"),
        Candidate(url="https://a.test/old", text="messaging overview", read=True),
    ]
    algorithms.select(candidates, "messaging", {}, {c.url for c in candidates})
    assert len(pair_calls) == 3


def test_disabled_module_preserves_order_and_does_not_write(monkeypatch):
    store = InMemoryEvidenceStore(SearchState())
    store.atomic_update_state = lambda _: pytest.fail("disabled module must not write")
    monkeypatch.setattr(adapter, "_context", lambda: (store, "task"))
    results = [
        SearchResult(url="https://a.test", title="irrelevant"),
        SearchResult(url="https://b.test", title="API"),
    ]
    assert adapter.rank_search_results("API", results) is results
    assert adapter.rank_ready_tasks(store, []) == []


def test_search_reordering_persists_but_keeps_result_ids_usable(monkeypatch):
    store = workspace()
    monkeypatch.setattr(adapter, "_context", lambda: (store, "task"))
    results = [
        SearchResult(url="https://a.test", title="Holiday calendar"),
        SearchResult(url="https://b.test", title="Lattice messaging API documentation"),
    ]
    ranked = adapter.rank_search_results("Lattice messaging API", results)
    assert ranked[0] is results[1]
    assert store.state.collection_policy.decisions
    assert not store.state.collection_policy.last_error


def test_feedback_requires_own_task_binding_and_matching_reading_window():
    store = workspace()
    state = store.state
    state.frontier.add(FrontierTask(id="peer", question="Other topic"))
    source = "https://a.test"
    own = EvidenceNode(
        id="own",
        finding="supports subscriptions",
        source=source,
        source_excerpt="supports subscriptions",
        alignment="full",
    )
    peer = EvidenceNode(
        id="peer",
        finding="owns an office",
        source=source,
        source_excerpt="owns an office",
        alignment="full",
    )
    state.evidence_graph.add_node(own)
    state.evidence_graph.add_node(peer)
    bind_task_evidence(state, "task", ["own"])
    bind_task_evidence(state, "peer", ["peer"])
    adapter.observe_in_state(
        state,
        {
            "task_id": "task",
            "source_url": source,
            "content": "supports subscriptions and owns an office",
        },
        ["own", "peer"],
    )
    batch = next(iter(state.collection_policy.batches.values()))
    assert batch.evidence_ids == ["own"]
    assert batch.new == 1


@pytest.mark.asyncio
async def test_real_findings_callback_records_verified_delta_without_extra_model_call():
    from searchos.harness.middleware.extraction.core.findings import extract_and_commit

    store = workspace()
    calls = []

    async def invoke(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps(
                [
                    {
                        "finding": "The API supports subscriptions.",
                        "source_excerpt": "The API supports subscriptions.",
                        "relation": "supports",
                    }
                ]
            )
        return json.dumps(
            [{"id": "0", "supported": True, "novelty": "new", "goal_status": "partial"}]
        )

    ids = await extract_and_commit(
        [
            {
                "task_id": "task",
                "source_url": "https://a.test/manual",
                "content": "The API supports subscriptions.",
            }
        ],
        store=store,
        invoke=invoke,
    )
    assert len(calls) == 2  # The original extractor + support check, no third judge.
    assert "novelty" in calls[1]
    assert len(ids) == 1
    assert len(store.state.collection_policy.batches) == 1
    assert store.state.collection_policy.goals["task"].gap == 0.5
    assert next(iter(store.state.collection_policy.batches.values())).new == 1


def test_task_completion_alone_never_closes_goal():
    from searchos.socm import FrontierTaskStatus

    store = workspace()
    store.state.frontier.questions[0].status = FrontierTaskStatus.COMPLETED
    adapter.sync_goals(store.state)
    assert store.state.collection_policy.goals["task"].gap is None


def test_module_is_separate_from_p1_p2_and_old_states_load():
    config = EffectiveRunConfig.from_settings().with_modules({"enabled": [adapter.MODULE_ID]})
    assert config.enable_adaptive_collection
    assert not config.enable_p1_evidence_reasoning
    assert not config.enable_p2_context_memory
    assert config.module_config()["enabled"] == [adapter.MODULE_ID]
    assert SearchState.model_validate({"intent": "legacy"}).collection_policy.batches == {}


def test_page_links_are_available_without_being_counted_as_read_evidence():
    from searchos.tools.simple_browser.render import PageContents

    store = workspace()
    adapter.record_page(
        store,
        "https://a.test/product",
        PageContents(
            url="https://a.test/product", title="API", text="[Protocol](/docs/protocol)", urls={}
        ),
    )
    policy = store.state.collection_policy
    assert "https://a.test/docs/protocol" in policy.candidates
    assert not policy.candidates["https://a.test/product"].read
    assert not policy.batches


@pytest.mark.asyncio
async def test_table_reader_updates_gap_and_feedback_after_atomic_commit():
    from searchos.harness.middleware.extraction import (
        DeliveryMode,
        EvidenceObservation,
        ExtractionConfig,
        ExtractionCoordinator,
    )

    store = workspace()
    state = store.state
    state.research_mode = "table"
    state.coverage_map.initialize(
        entities=["Acme"],
        attributes=["Company", "Revenue"],
        table_id="companies",
        primary_key=["Company"],
    )
    task = state.frontier.questions[0]
    task.table_id = "companies"
    task.target_cells = ["Acme.Revenue"]
    task.result_mode = "table"

    class Judge:
        async def ainvoke(self, _prompt):
            from types import SimpleNamespace

            return SimpleNamespace(
                content=json.dumps(
                    [
                        {
                            "Company": "Acme",
                            "Revenue": "1234",
                            "_source_page": 1,
                            "_source_excerpt": "Acme reported revenue of 1234 million in 2025.",
                            "_alignment": "full",
                            "_confidence": "high",
                        }
                    ]
                )
            )

    coordinator = ExtractionCoordinator(
        Judge(), store, batch_n=10, config=ExtractionConfig(dual_mode=False)
    )
    await coordinator.submit(
        EvidenceObservation(
            task_id="task",
            target_table="companies",
            result_mode="table",
            content="Acme annual report. Acme reported revenue of 1234 million in 2025.",
            source_url="https://example.com/annual",
        ),
        delivery=DeliveryMode.SYNC,
    )
    assert store.state.collection_policy.goals["task"].gap == 0
    assert sum(batch.new for batch in store.state.collection_policy.batches.values()) == 1


@pytest.mark.asyncio
async def test_topic_entry_forwards_the_selected_module(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from api import deps, workbench
    from api.routes.search import SourcesRequest
    from api.topic_research import TopicResearchManager

    captured = []

    class Store:
        def register_session(self, *args):
            pass

        def update_run(self, *args, **kwargs):
            pass

        def update_task(self, *args, **kwargs):
            pass

    async def launch(request):
        captured.append(request)
        return SimpleNamespace(session_id="adaptive_test")

    manager = TopicResearchManager(Store(), launcher=launch)

    async def sources(*args):
        return SourcesRequest(mode="web")

    monkeypatch.setattr(manager, "_sources", sources)
    monkeypatch.setattr(
        workbench,
        "load_config",
        lambda: SimpleNamespace(openai=SimpleNamespace(model="test-model")),
    )
    monkeypatch.setattr(deps, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("api.topic_research.investigation_prompt", lambda *args: "Investigate API")
    import time

    await manager._collect(
        {
            "id": "run",
            "engine": "openai",
            "model": "test-model",
            "topic_id": "topic",
            "query": "API",
            "modules": [adapter.MODULE_ID],
        },
        {"id": "task"},
        {"topic": {}},
        time.monotonic() + 60,
    )
    assert captured[0].modules == [adapter.MODULE_ID]
