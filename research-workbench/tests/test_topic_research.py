"""Offline acceptance for local reuse and bounded autonomous topic collection."""

import asyncio
import copy

from api.knowledge_extraction import scope_namespace
from api.topic_research import (
    TopicResearchManager,
    investigation_entity_ids,
    investigation_prompt,
    local_lookup,
    plan_tasks,
)


def test_branch_scope_keeps_separate_requested_branches_and_filters_other_gaps():
    data = snapshot()
    data["clusters"] = [
        {"id": "lattice", "label": "Lattice", "entity_ids": ["e1"]},
        {"id": "underwater", "label": "水下系统", "entity_ids": ["e2"], "keywords": ["水下"]},
        {"id": "aviation", "label": "无人航空", "entity_ids": ["e3"]},
    ]
    data["gaps"] += [{"entity_id": "e2", "kind": "technology", "field": "inputs", "label": "输入"}]
    assert investigation_entity_ids(data, "Lattice 和水下技术") == {"e1", "e2"}
    plan = plan_tasks(data, "Lattice 技术")
    assert plan[0]["missing"] == 1
    assert plan[0]["gaps"][0]["entity_id"] == "e1"
    assert investigation_entity_ids(data, "Anduril") is None


def test_detailed_fields_are_distributed_across_entities_and_conflicts_researched():
    data = snapshot()
    data["gaps"] = [
        {"entity_id": entity, "kind": "technology", "field": f"f{i}", "label": f"字段{i}"}
        for entity in ("e1", "e2")
        for i in range(30)
    ]
    data["entity_fields"] = {"e1": [{"key": "mechanism", "label": "原理", "status": "conflicting"}]}
    plan = plan_tasks(data, "技术")[0]
    assert plan["missing"] == 61
    assert len(plan["gaps"]) == 16
    assert {g["entity_id"] for g in plan["gaps"]} == {"e1", "e2"}
    assert plan["gaps"][0]["status"] == "conflicting"


def snapshot():
    return {
        "topic": {"id": "topic_a", "title": "Anduril", "namespace": "anduril"},
        "knowledge": {
            "entities": [
                {
                    "id": "e1",
                    "name": "Example",
                    "kind": "technology",
                    "attributes": {"mechanism": "A documented mechanism"},
                    "field_evidence": {"mechanism": ["q1"]},
                }
            ],
            "relations": [],
        },
        "sources": [{"id": "d1"}],
        "gaps": [{"entity_id": "e1", "kind": "technology", "field": "outputs", "label": "输出"}],
    }


def test_bare_topic_includes_lineage_technology_maturity_and_external_references():
    assert [item["id"] for item in plan_tasks(snapshot(), "Anduril")] == [
        "products",
        "technology",
        "maturity",
        "references",
    ]
    assert [item["id"] for item in plan_tasks(snapshot(), "Anduril 开源项目")] == ["references"]
    plan = plan_tasks(snapshot(), "Anduril", {"products"})
    assert plan[0]["id"] == "technology" and plan[0]["missing"] == 1


def test_local_field_reuse_requires_field_evidence_and_no_freshness_request():
    data = snapshot()
    assert local_lookup(data, "Example的原理")["sufficient"]
    assert not local_lookup(data, "Example最新原理")["sufficient"]
    assert not local_lookup(data, "Anduril")["sufficient"]
    assert not local_lookup(data, "比较 Example 与 Unknown 的原理")["sufficient"]
    assert not local_lookup(data, "Example的原理和局限")["sufficient"]
    data["knowledge"]["entities"][0]["field_evidence"] = {}
    assert not local_lookup(data, "Example的原理")["sufficient"]


def test_detailed_projected_fields_and_merged_aliases_work_locally_only_when_sourced():
    data = snapshot()
    data["knowledge"]["entities"][0]["attributes"]["aliases"] = ["Example™"]
    data["entity_fields"] = {
        "e1": [
            {
                "key": "payload_capacity",
                "label": "载荷能力",
                "aliases": ["载荷容量"],
                "status": "known",
                "value": "10 kg",
                "evidence_ids": ["q1"],
                "observations": [{"value": "10 kg", "manual": False}],
            }
        ]
    }
    result = local_lookup(data, "Example™的载荷能力")
    assert result["sufficient"] and "10 kg" in result["text"]
    data["entity_fields"]["e1"][0]["evidence_ids"] = []
    assert not local_lookup(data, "Example的载荷能力")["sufficient"]


def test_manual_override_is_not_treated_as_verified_local_answer():
    data = snapshot()
    data["knowledge"]["entities"][0]["field_overrides"] = {"mechanism": {"value": "manual"}}
    assert not local_lookup(data, "Example的原理")["sufficient"]


def test_conflicting_observation_requires_further_research():
    data = snapshot()
    data["knowledge"]["entities"][0]["attributes"]["document_observations"] = [
        {"attributes": {"mechanism": "A different mechanism"}, "source_ids": ["q2"]}
    ]
    result = local_lookup(data, "Example的原理")
    assert not result["sufficient"]
    assert "多个来源值" in result["text"]


def test_stable_namespace_beats_incidental_anduril_comparison():
    assert scope_namespace("namespace: palantir\n对比 Anduril") == "palantir"
    data = snapshot()
    prompt = investigation_prompt(data["topic"], "Anduril", plan_tasks(data, "Anduril")[0], data)
    assert "namespace: anduril" in prompt
    assert "用户无需逐步追问" in prompt
    assert "搜索摘要不能冒充全文" in prompt


class FakeStore:
    def __init__(self, query="Anduril", local_only=False):
        self.run = {
            "id": "r1",
            "topic_id": "topic_a",
            "query": query,
            "engine": "openai",
            "model": "test-model",
            "max_tasks": 4,
            "max_seconds": 1200,
            "local_only": local_only,
            "status": "queued",
        }
        self.tasks = {}
        self.changes = 0

    def update_run(self, identity, **values):
        self.run.update(values)
        return copy.deepcopy(self.run)

    def get_run(self, identity):
        return copy.deepcopy(self.run)

    def snapshot(self, identity):
        return {**snapshot(), "tasks": list(self.tasks.values())}

    def add_task(self, run_id, **values):
        identity = f"task_{len(self.tasks)}"
        self.tasks[identity] = {"id": identity, **values}
        return copy.deepcopy(self.tasks[identity])

    def update_task(self, identity, **values):
        self.tasks[identity].update(values)

    def sync_topic_changes(self, topic, run):
        self.changes += 1
        return {
            "entities_added": 0,
            "entities_updated": 0,
            "relations_added": 0,
            "summary": "没有新增事实",
        }

    def list_pending_runs(self):
        return [self.get_run("r1")]


def test_local_lookup_does_not_launch_model_or_network():
    store = FakeStore(local_only=True)
    manager = TopicResearchManager(store)
    asyncio.run(manager._execute(store.get_run("r1")))
    assert store.run["status"] == "completed"
    assert len(store.tasks) == 1 and store.changes == 0
    assert "未联网" in store.run["message"]


def test_sufficient_local_fact_skips_research_even_when_network_allowed():
    store = FakeStore(query="Example的原理")
    asyncio.run(TopicResearchManager(store)._execute(store.get_run("r1")))
    assert store.run["status"] == "completed"
    assert len(store.tasks) == 1


def test_topic_research_reuses_published_field_and_its_source(tmp_path):
    from api.knowledge_handoff.review import build_plan, commit_plan
    from api.research_mapping import Source
    from searchos.knowledge_store.database import ResearchDatabase

    db = ResearchDatabase(tmp_path / "research.sqlite")
    db.initialize()
    quote = "Demo Mesh supports publish-subscribe."
    source = Source(
        "https://example.invalid/demo-mesh", "Demo Mesh documentation", quote,
        "demo.md", "doc-demo",
    )
    outputs = [{"entities": [{
        "entity_key": "mesh", "kind": "product", "name": "Demo Mesh", "identity_quote": quote,
        "fields": [{"field_id": "product_supports_publish_subscribe", "value": True, "quote": quote}],
    }]}]
    plan = build_plan(db, source, outputs, slug="anduril", artifact_root=tmp_path)
    commit_plan(db, source, plan["plan_id"], artifact_root=tmp_path)
    question = "Demo Mesh 是否" + next(
        r["label"] for r in plan["records"] if r["kind"] == "capability"
    ) + "？"
    store = FakeStore(query=question)
    manager = TopicResearchManager(store, research_store=db)

    async def unexpected_collection(*args):
        raise AssertionError("a sourced local field should not start network collection")

    manager._collect = unexpected_collection
    asyncio.run(manager._execute(store.get_run("r1")))
    assert store.run["status"] == "completed"
    assert "未联网" in store.run["message"]
    assert quote in store.run["local_result"]
    assert source.url in store.run["local_result"]
    assert len(store.tasks) == 1


def test_two_rounds_without_knowledge_progress_stop_automatically():
    store = FakeStore()
    manager = TopicResearchManager(store)

    async def collect(*args):
        return "sid", True

    async def ingest(*args):
        return True, 2

    manager._collect, manager._ingest = collect, ingest
    asyncio.run(manager._execute(store.get_run("r1")))
    assert store.run["status"] == "partial"
    assert store.run["stop_reason"] == "no_progress"
    assert len(store.tasks) == 3  # local check + two autonomous investigations
    assert store.run["remaining"] == ["成熟度与进展", "独立开源参考"]


def test_provider_error_is_redacted_and_stops_further_tasks():
    store = FakeStore()
    manager = TopicResearchManager(store)

    async def fail(*args):
        raise RuntimeError("sk-test-should-never-be-saved")

    manager._collect = fail
    asyncio.run(manager._execute(store.get_run("r1")))
    assert store.run["status"] == "failed"
    assert "sk-test" not in str(store.run) + str(store.tasks)
    assert len(store.tasks) == 2


def test_recover_marks_interrupted_without_starting_work():
    store = FakeStore()
    store.tasks["t1"] = {"id": "t1", "run_id": "r1", "status": "running"}
    manager = TopicResearchManager(store)
    manager.recover()
    assert store.run["status"] == "interrupted"
    assert store.tasks["t1"]["status"] == "interrupted"
    assert not manager.workers


def test_source_selection_prioritizes_relevance_and_ignores_nonlocal_excerpt():
    data = snapshot()
    data["sources"] = [
        {"available": True, "title": "repo excerpt", "library_id": "", "document_id": ""},
        *[
            {
                "available": True,
                "title": "Other material",
                "library_id": "lib1",
                "document_id": f"d{i:03}",
            }
            for i in range(110)
        ],
        {
            "available": True,
            "title": "Example mechanism",
            "library_id": "lib1",
            "document_id": "zlast",
        },
    ]
    selected = asyncio.run(TopicResearchManager(FakeStore())._sources(data, "Example 原理"))
    assert selected.mode == "hybrid"
    assert len(selected.local.document_ids) == 100
    assert selected.local.document_ids[0] == "zlast"
