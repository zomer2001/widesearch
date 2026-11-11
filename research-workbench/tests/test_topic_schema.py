"""Topic-specific applicability and source-preserving projection, entirely offline."""

from __future__ import annotations

import copy

from api.topic_schema import build_topic_projection, extraction_field_guide
from api.topic_store import TopicStore


def entity(name="Lattice Mesh", kind="product", attrs=None, **extra):
    return {
        "id": "e1",
        "name": name,
        "kind": kind,
        "category": "",
        "origin": "archive",
        "attributes": attrs or {},
        "evidence_ids": [],
        **extra,
    }


def projection(records, title="Anduril", relations=None, evidence=None):
    return build_topic_projection({"title": title}, records, relations or [], evidence or [])


def fields(result, identity="e1"):
    return {field["key"]: field for field in result["entity_fields"][identity]}


def test_domain_fields_only_apply_to_relevant_objects():
    lattice = fields(projection([entity()]))
    underwater = fields(projection([entity("Dive-XL")]))
    palantir = fields(projection([entity("Foundry")], "Palantir"))
    chip = fields(projection([entity("昇腾 910")], "华为"))
    radio = fields(projection([entity("5G 基站")], "Huawei"))
    assert {"mesh_routing", "sdk_version", "data_model", "bandwidth_policy"} <= lattice.keys()
    assert "operating_depth" not in lattice
    assert {"operating_depth", "payload", "endurance", "launch_recovery"} <= underwater.keys()
    assert "sdk_version" not in underwater
    assert {"ontology_objects", "workflow", "data_lineage"} <= palantir.keys()
    assert "payload" not in palantir
    assert {"compute_precision", "memory_bandwidth", "process_node"} <= chip.keys()
    assert "frequency_bands" not in chip
    assert "frequency_bands" in radio
    assert "process_node" not in radio
    mechanism = fields(projection([entity("水下导航", "technology")]))
    assert "length" not in mechanism
    assert "navigation" in mechanism


def test_alias_projection_keeps_original_attributes_and_field_citations():
    original = entity(
        "Dive-XL",
        attrs={"最大潜深": "6000 m", "custom_attribute": "原值"},
        field_evidence={"最大潜深": ["q1"]},
    )
    before = copy.deepcopy(original)
    result = projection([original], evidence=[{"id": "q1", "available": True}])
    assert fields(result)["operating_depth"]["value"] == "6000 m"
    assert fields(result)["operating_depth"]["evidence_ids"] == ["q1"]
    assert fields(result)["custom_attribute"]["value"] == "原值"
    assert original == before
    assert "最大潜深" not in fields(result)


def test_explicit_status_requires_source_or_manual_and_not_applicable_is_not_gap():
    record = entity(
        attrs={
            "data_model": "未公开",
            "sdk_version": "不适用",
            "latency": "未知",
            "throughput": "未公开",
        },
        field_evidence={"data_model": ["q1"], "sdk_version": ["q2"]},
    )
    result = projection([record], evidence=[{"id": "q1"}, {"id": "q2"}])
    fs = fields(result)
    assert fs["data_model"]["status"] == "not_public"
    assert fs["sdk_version"]["status"] == "not_applicable"
    assert fs["latency"]["status"] == "unknown"
    assert fs["throughput"]["status"] == "unknown"
    gaps = {gap["field"] for gap in result["gaps"]}
    assert "data_model" not in gaps and "sdk_version" not in gaps
    assert "latency" in gaps and "throughput" in gaps
    manual = projection([entity(attrs={"sdk_version": "不适用"}, origin="manual")])
    assert fields(manual)["sdk_version"]["status"] == "not_applicable"


def test_conflicts_preserve_values_and_context_without_inventing_field_evidence():
    record = entity(
        "Dive-XL",
        attrs={
            "续航时间": "100 hours",
            "document_observations": [
                {
                    "attributes": {"endurance": "120 hours", "适用版本": "v2", "测试条件": "低速"},
                    "source_ids": ["entity_quote"],
                    "field_evidence": {"endurance": ["field_quote"]},
                },
            ],
        },
        evidence_ids=["entity_quote"],
    )
    result = projection([record], evidence=[{"id": "entity_quote"}, {"id": "field_quote"}])
    value = fields(result)["endurance"]
    assert value["status"] == "conflicting"
    assert value["evidence_ids"] == ["field_quote"]
    assert {item["value"] for item in value["observations"]} == {"100 hours", "120 hours"}
    assert value["observations"][1]["context"] == {"version": "v2", "operating_conditions": "低速"}
    assert value["observations"][0]["evidence_ids"] == []


def test_manual_canonical_override_is_primary_but_does_not_delete_original_observation():
    record = entity(
        "Dive-XL",
        attrs={"endurance": "已核对值", "续航时间": "100 hours"},
        field_overrides={"endurance": {"value": "已核对值"}},
        field_evidence={"续航时间": ["q1"]},
    )
    result = projection([record], evidence=[{"id": "q1"}])
    value = fields(result)["endurance"]
    assert value["status"] == "known" and value["manual"]
    assert value["value"] == "已核对值"
    assert value["evidence_ids"] == []
    assert len(value["observations"]) == 2
    assert [item["active"] for item in value["observations"]] == [True, False]


def test_hierarchy_overlaps_without_duplicating_entities_or_creating_edges():
    records = [
        entity(),
        entity("网络路由", "technology", id="t1"),
        entity("未归类机制", "technology", id="t2"),
    ]
    edges = [
        {
            "source_id": "e1",
            "target_id": "t1",
            "predicate": "uses_technology",
            "status": "source_backed",
            "evidence_ids": ["q1"],
        },
        {
            "source_id": "t1",
            "target_id": "t2",
            "predicate": "has_route",
            "status": "source_backed",
            "evidence_ids": ["q1"],
        },
    ]
    result = projection(records, relations=edges)
    assert set(result["entity_clusters"]["e1"]) == {"lattice", "lattice.network"}
    assert "lattice" in result["entity_clusters"]["t1"]
    assert result["entity_clusters"]["t2"] == ["unclassified"]
    assert len(result["entity_fields"]) == 3
    assert "relations" not in result
    assert next(c for c in result["clusters"] if c["id"] == "lattice")["count"] == 2


def test_defaults_are_idempotent_real_empty_topics_and_aliases_resolve(tmp_path):
    store = TopicStore(
        tmp_path / "local", tmp_path / "topics.sqlite", tmp_path / "knowledge.sqlite"
    )
    store.ensure_default_topics()
    first = store.list_topics()
    store.ensure_legacy_topic()
    second = store.list_topics()
    assert [(t["id"], t["title"]) for t in first] == [(t["id"], t["title"]) for t in second]
    assert {t["title"] for t in second} == {"Anduril", "Palantir", "华为"}
    assert all(t["counts"]["entities"] == 0 and t["counts"]["sources"] == 0 for t in second)
    assert store.create_topic("Huawei")["id"] == next(
        t["id"] for t in first if t["title"] == "华为"
    )
    assert store.create_topic("Palantir Technologies")["namespace"] == "palantir"
    assert all(t["clusters"] and t["recent_run"] is None for t in second)


def test_existing_topic_namespace_is_not_rewritten(tmp_path):
    store = TopicStore(
        tmp_path / "local", tmp_path / "topics.sqlite", tmp_path / "knowledge.sqlite"
    )
    topic = store.create_topic("Palantir")
    with store._connection() as con:
        con.execute("UPDATE topics SET namespace='topic:old' WHERE id=?", (topic["id"],))
    store.ensure_default_topics()
    assert store.get_topic(topic["id"])["namespace"] == "topic:old"
    assert store.snapshot(topic["id"])["profile"]["id"] == "palantir"


def test_extraction_guide_canonical_keys_domains_and_kinds():
    lattice = extraction_field_guide("anduril", "Lattice SDK", "接口")
    water = extraction_field_guide("anduril", "Dive-XL", "水下")
    assert "sdk_version" in {f["key"] for f in lattice["fields"]}
    assert "operating_depth" not in {f["key"] for f in lattice["fields"]}
    assert "operating_depth" in {f["key"] for f in water["fields"]}
    assert next(f for f in water["fields"] if f["key"] == "payload")["kinds"] == ["product"]


def test_distinct_version_or_test_condition_values_are_not_conflicts():
    record = entity(
        "Dive-XL",
        attrs={
            "endurance": "100 hours",
            "version": "v1",
            "document_observations": [
                {
                    "attributes": {"endurance": "120 hours", "version": "v2"},
                    "field_evidence": {"endurance": ["q2"]},
                },
            ],
        },
        field_evidence={"endurance": ["q1"]},
    )
    result = projection([record], evidence=[{"id": "q1"}, {"id": "q2"}])
    value = fields(result)["endurance"]
    assert value["status"] == "known"
    assert len(value["observations"]) == 2
    assert value["observations"][0]["context"] == {"version": "v1"}
    record["attributes"]["document_observations"][0]["attributes"]["version"] = "v1"
    assert fields(projection([record]))["endurance"]["status"] == "conflicting"


def test_original_detail_paragraphs_become_readable_fields_with_existing_sources():
    original = entity(
        attrs={
            "technical_details": [
                {
                    "label": "带宽策略",
                    "text": "原文对应的完整技术段落。",
                    "source_ids": ["q1", "missing"],
                },
            ]
        }
    )
    before = copy.deepcopy(original)
    result = projection([original], evidence=[{"id": "q1", "available": True}])
    detail = next(f for f in result["entity_fields"]["e1"] if f["group"] == "已采技术细节")
    assert detail["value"] == "原文对应的完整技术段落。"
    assert detail["evidence_ids"] == ["q1"]
    assert "technical_details" not in fields(result)
    assert original == before
    assert fields(result)["bandwidth_policy"]["status"] == "unknown"


def test_primary_without_context_does_not_duplicate_same_contextual_source_value():
    record = entity(
        attrs={
            "latency": "1 ms",
            "document_observations": [
                {
                    "attributes": {"latency": "1 ms", "version": "v2"},
                    "field_evidence": {"latency": ["q1"]},
                },
            ],
        }
    )
    value = fields(projection([record], evidence=[{"id": "q1"}]))["latency"]
    assert len(value["observations"]) == 1
    assert value["observations"][0]["context"] == {"version": "v2"}


def test_alias_manual_observation_does_not_suppress_primary_value():
    record = entity(
        attrs={
            "latency": "1 ms",
            "document_observations": [
                {
                    "attributes": {"latency": "2 ms"},
                    "field_overrides": {"latency": {"value": "2 ms"}},
                    "origin": "archive",
                    "merged_from": "e2",
                },
            ],
        },
        field_evidence={"latency": ["q1"]},
    )
    value = fields(projection([record], evidence=[{"id": "q1"}]))["latency"]
    assert value["status"] == "conflicting"
    assert len(value["observations"]) == 2
    assert [item["manual"] for item in value["observations"]] == [False, True]
    assert all(item["active"] for item in value["observations"])


def test_classification_can_be_overridden_reset_and_isolated(tmp_path):
    from api.knowledge_database import KnowledgeDatabase

    store = TopicStore(
        tmp_path / "local", tmp_path / "topics.sqlite", tmp_path / "knowledge.sqlite"
    )
    a, b = store.create_topic("Anduril"), store.create_topic("Other")
    record = KnowledgeDatabase(store.data_root, store.knowledge_database).create_entity(
        {"name": "Lattice Mesh", "kind": "product", "attributes": {}}
    )
    with store._connection() as con:
        con.execute("INSERT INTO topic_entities VALUES(?,?,0)", (a["id"], record["id"]))
        con.execute("INSERT INTO topic_entities VALUES(?,?,0)", (b["id"], record["id"]))
    changed = store.update_classification(a["id"], record["id"], ["underwater.vehicles"])
    assert changed["entity_clusters"][record["id"]] == ["underwater", "underwater.vehicles"]
    assert changed["entity_classification_origin"][record["id"]] == "manual"
    assert store.snapshot(b["id"])["classification_overrides"] == {}
    empty = store.update_classification(a["id"], record["id"], [])
    assert empty["entity_clusters"][record["id"]] == ["unclassified"]
    reset = store.update_classification(a["id"], record["id"], None)
    assert "lattice.network" in reset["entity_clusters"][record["id"]]
    assert reset["entity_classification_origin"][record["id"]] == "classification_rule"
    import pytest

    with pytest.raises(ValueError):
        store.update_classification(a["id"], record["id"], ["foundry"])
    with pytest.raises(KeyError):
        store.update_classification(a["id"], "missing", [])


def test_existing_aviation_parameters_use_one_canonical_field_without_extra_missing_fields():
    record = entity(
        "Altius-600",
        attrs={"航程": "100 km", "range": "120 km"},
        field_evidence={"航程": ["q1"], "range": ["q2"]},
    )
    original = copy.deepcopy(record)
    result = projection([record], evidence=[{"id": "q1"}, {"id": "q2"}])
    fs = fields(result)
    assert "range" in fs and "航程" not in fs
    assert sum(f["label"] == "航程" for f in result["entity_fields"]["e1"]) == 1
    assert fs["range"]["status"] == "conflicting"
    assert fs["range"]["evidence_ids"] == ["q1", "q2"]
    assert "operating_depth" not in fs and "pressure_hull" not in fs
    assert record == original
    observed_only = entity(
        "Fury",
        attrs={
            "document_observations": [
                {"attributes": {"航程": "200 km"}, "field_evidence": {"航程": ["q1"]}},
            ]
        },
    )
    assert (
        fields(projection([observed_only], evidence=[{"id": "q1"}]))["range"]["value"] == "200 km"
    )


def test_unknown_maturity_stage_with_explanatory_basis_is_not_filled():
    value = {"stage": "未知", "basis": "尚无可用的原文阶段依据。"}
    record = entity(attrs={"maturity": value}, field_evidence={"maturity": ["q1"]})
    result = projection([record], evidence=[{"id": "q1"}])
    maturity = fields(result)["maturity"]
    assert maturity["status"] == "unknown"
    assert maturity["observations"][0]["value"] == value
    assert "maturity" in {gap["field"] for gap in result["gaps"]}
    assert result["coverage"]["filled"] == 0
    record["attributes"]["maturity"]["stage"] = "试验验证"
    assert fields(projection([record]))["maturity"]["status"] == "known"


def test_legacy_absence_note_is_not_a_known_product_family():
    record = entity(attrs={"family": "原文未注明家族关系", "overview": "已有补充概述"})
    result = fields(projection([record]))
    assert result["family"]["status"] == "unknown"
    assert result["overview"]["label"] == "补充概述"
