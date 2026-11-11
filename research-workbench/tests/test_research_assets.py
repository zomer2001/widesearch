"""Asset classifications, seven-category details and scoped SQL-derived graphs."""

import pytest
from api.research_assets import asset_detail, asset_graph, asset_index, material_inventory
from api.research_mapping import Mapper, Source
from api.routes import research_database as routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from searchos.knowledge_store.database import ResearchDatabase

TEXT = (
    "Product-A and Product-B are products. Product-B is a variant of Product-A. "
    "Product-A uses Technology-T and carries Component-C. "
    "Technology-T and Technology-S depend on each other. Product-A endurance is two hours. "
    "Organization-O is associated with Product-A."
    " Product-A is also called Alpha-A."
)


@pytest.fixture
def assets(tmp_path):
    store = ResearchDatabase(tmp_path / "research.sqlite")
    store.initialize()
    source = Source(
        "https://example.invalid/assets", "Fictional source", TEXT, "fixture.md", "fixture"
    )
    entities = [
        {"entity_key": key, "kind": kind, "name": key, "identity_quote": TEXT, "fields": []}
        for key, kind in [
            ("Product-A", "product"),
            ("Product-B", "product"),
            ("Technology-T", "technology"),
            ("Technology-S", "technology"),
            ("Component-C", "component"),
            ("Organization-O", "organization"),
        ]
    ]
    entities[0]["fields"] = [
        {
            "field_id": "product_endurance",
            "value": {"value": 2, "unit": "h", "raw": "two hours"},
            "quote": TEXT,
        }
    ]
    relations = [
        {"source_key": s, "target_key": t, "predicate": p, "quote": TEXT}
        for s, t, p in [
            ("Product-B", "Product-A", "variant_of"),
            ("Product-A", "Technology-T", "uses_technology"),
            ("Product-A", "Component-C", "carries"),
            ("Technology-T", "Technology-S", "depends_on"),
            ("Technology-S", "Technology-T", "depends_on"),
        ]
    ]
    result = Mapper(store, source).apply(
        {
            "entities": entities,
            "relations": relations,
            "records": [
                {
                    "subject_key": "Organization-O",
                    "kind": "participation",
                    "label": "associated_organization",
                    "target_key": "Product-A",
                    "quote": TEXT,
                }
            ],
        }
    )
    assert not result["unmapped"]
    with store.connect() as db:
        ids = {
            r["canonical_name"]: r["id"]
            for r in db.execute("SELECT id,canonical_name FROM entities")
        }
    return store, source, ids


def test_classification_and_detail_keep_templates_separate_from_facts(assets):
    store, _, ids = assets
    products = asset_index(store)
    assert products["counts"] == {"product": 2, "technology": 2}
    assert {r["name"] for r in products["items"]} == {"Product-A", "Product-B"}
    assert asset_index(store, kind="technology")["total"] == 2
    assert asset_index(store, query="two hours")["items"][0]["name"] == "Product-A"
    assert len(asset_index(store, limit=1)["items"]) == 1
    assert asset_index(store, slug="huawei")["total"] == 0
    detail = asset_detail(store, "anduril", ids["Product-A"])
    card = next(a for a in products["items"] if a["id"] == ids["Product-A"])
    assert card["field_count"] == sum(r["kind"] != "identity" for r in detail["records"])
    assert card["source_count"] == len(detail["sources"])
    assert len(detail["dimensions"]) == 7
    assert sum(len(d["elements"]) for d in detail["dimensions"]) == 48
    element_ids = {d["id"]: {e["id"] for e in d["elements"]} for d in detail["dimensions"]}
    assert all(r["element_id"] in element_ids[r["dimension_id"]] for r in detail["records"])
    assert any(f["id"] == "product_endurance" for f in detail["fields"])
    assert any(r["field_id"] == "product_endurance" for r in detail["records"])
    assert all(r["evidence"] for r in detail["records"])
    assert any(r["id"] == ids["Product-B"] for r in detail["related"])
    assert any(r["label"] == "包含型号/版本" for r in detail["records"])
    assert any(
        r["label"] == "关联机构" and r["value"] == "Organization-O" for r in detail["records"]
    )
    tech = asset_detail(store, "anduril", ids["Technology-T"])
    assert any(r["label"] == "采用该技术的产品" for r in tech["records"])


def test_graph_reads_foreign_keys_incoming_links_and_evidence_without_inventing_edges(assets):
    store, _, ids = assets
    graph = asset_graph(store, "anduril", ids["Product-A"])
    assert {n["name"] for n in graph["nodes"]} == {
        "Product-A",
        "Product-B",
        "Technology-T",
        "Component-C",
        "Organization-O",
    }
    assert all(c["evidence"] for e in graph["edges"] for c in e["claims"])
    lineage = asset_graph(store, "anduril", ids["Product-A"], mode="lineage", depth=3)
    assert {e["predicate"] for e in lineage["edges"]} == {"variant_of"}
    assert next(n["layer"] for n in lineage["nodes"] if n["name"] == "Product-B") == 1
    parent = asset_graph(store, "anduril", ids["Product-B"], mode="lineage")
    assert next(n["layer"] for n in parent["nodes"] if n["name"] == "Product-A") == -1
    cyclic = asset_graph(store, "anduril", ids["Technology-T"], depth=3)
    assert len({n["id"] for n in cyclic["nodes"]}) == len(cyclic["nodes"])
    applications = asset_graph(store, "anduril", ids["Product-A"], mode="applications", depth=3)
    assert {n["name"] for n in applications["nodes"]} == {"Product-A", "Technology-T"}
    bounded = asset_graph(store, "anduril", ids["Product-A"], limit=2)
    assert len(bounded["nodes"]) == 2 and bounded["truncated"]
    with pytest.raises(KeyError):
        asset_graph(store, "huawei", ids["Product-A"])


def test_topic_network_keeps_provenance_scope_and_full_degree_before_truncation(assets):
    store, _, ids = assets
    isolated = store.create_entity(
        kind="product", name="Isolated-product", namespace="anduril",
        identity_key="isolated", topic_id="topic:anduril",
    )
    store.create_topic("huawei", "华为")
    other_topic = store.create_entity(
        kind="product", name="Other-topic-product", namespace="huawei",
        identity_key="other", topic_id="topic:huawei",
    )
    graph = asset_graph(store, "anduril", ids["Product-B"], scope="topic", limit=500)
    assert isolated in {n["id"] for n in graph["nodes"]}
    assert other_topic not in {n["id"] for n in graph["nodes"]}
    assert next(n for n in graph["nodes"] if n["id"] == ids["Technology-S"])["distance"] is None
    assert all(c["evidence"] for e in graph["edges"] for c in e["claims"])
    small = asset_graph(store, "anduril", ids["Product-A"], limit=2)
    root = next(n for n in small["nodes"] if n["id"] == ids["Product-A"])
    assert root["neighbor_count"] == 4
    assert root["source_count"] == 1
    assert root["record_count"] > 0
    lineage = asset_graph(store, "anduril", ids["Product-A"], mode="lineage", scope="topic")
    assert isolated not in {n["id"] for n in lineage["nodes"]}
    bounded = asset_graph(store, "anduril", ids["Product-A"], scope="topic", limit=2)
    assert len(bounded["nodes"]) == 2 and bounded["truncated"]


def test_metric_and_composition_tables_join_graph_and_withdrawal_removes_edge(assets):
    store, source, ids = assets
    claim = store.append_claim(
        topic_id="topic:anduril",
        subject_id=ids["Product-B"],
        kind="composition",
        detail={"component_id": ids["Component-C"], "row_key": "payload-1", "role": "payload"},
        evidence=[{"segment_id": source.segment_key, "quote": TEXT}],
        status="accepted",
    )
    graph = asset_graph(store, "anduril", ids["Product-B"])
    assert any("composition_items" in e["source_tables"] for e in graph["edges"])
    metric = asset_graph(store, "anduril", ids["Product-A"], mode="capabilities")
    assert any("metric_observations" in e["source_tables"] for e in metric["edges"])
    store.review_claim(claim, decision="withdraw", reviewer="test", reason="fixture correction")
    graph = asset_graph(store, "anduril", ids["Product-B"])
    assert not any("composition_items" in e["source_tables"] for e in graph["edges"])


def test_materials_link_back_to_products_and_technology_from_published_evidence(assets):
    store, source, _ = assets
    view = material_inventory(
        store, "anduril", [{"document_id": "doc", "name": "Source", "url": source.url}]
    )
    assert view["mapped"] == 1 and view["documents"][0]["field_count"] > 0
    assert {a["kind"] for a in view["documents"][0]["assets"]} == {"product", "technology"}
    assert material_inventory(store, "huawei", [{"url": source.url}])["mapped"] == 0


def test_incomplete_candidates_do_not_break_or_enter_published_asset_views(assets):
    store, _, ids = assets
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO claims(id,topic_id,subject_id,claim_kind,recorded_at) "
            "VALUES('pending','topic:anduril',?,'metric','2026-11-05')",
            (ids["Product-A"],),
        )
    detail = asset_detail(store, "anduril", ids["Product-A"])
    assert all(r["id"] != "pending" for r in detail["records"])
    assert all(
        c["id"] != "pending"
        for e in asset_graph(store, "anduril", ids["Product-A"])["edges"]
        for c in e["claims"]
    )


def test_capability_graph_does_not_render_unsupported_as_supported(assets):
    store, source, ids = assets
    capability = store.create_entity(
        kind="capability",
        name="Feature-Z",
        namespace="shared",
        identity_key="feature-z",
        topic_id="topic:anduril",
    )
    store.append_claim(
        topic_id="topic:anduril",
        subject_id=ids["Product-A"],
        kind="capability",
        detail={"capability_id": capability, "support_status": "unsupported"},
        evidence=[{"segment_id": source.segment_key, "quote": TEXT}],
        status="accepted",
    )
    graph = asset_graph(store, "anduril", ids["Product-A"], mode="capabilities")
    edge = next(e for e in graph["edges"] if e["predicate"] == "has_capability")
    assert edge["label"] == "不支持"


def test_alias_records_display_the_saved_name_instead_of_repeating_canonical_name(assets):
    store, source, ids = assets
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO entity_names(id,entity_id,name,name_type) "
            "VALUES('name:alias-fixture',?,'Alpha-A','alias')",
            (ids["Product-A"],),
        )
        db.execute(
            "INSERT INTO entity_names(id,entity_id,name,name_type) "
            "VALUES('name:canonical-alias',?,'Product-A','alias')",
            (ids["Product-A"],),
        )
    store.append_claim(
        topic_id="topic:anduril",
        subject_id=ids["Product-A"],
        kind="identity",
        detail={"name_id": "name:alias-fixture"},
        status="accepted",
        evidence=[{"segment_id": source.segment_key, "quote": TEXT}],
    )
    detail = asset_detail(store, "anduril", ids["Product-A"])
    assert any(r["label"] == "别名" and r["value"] == "Alpha-A" for r in detail["records"])
    assert asset_index(store, query="Alpha-A")["items"][0]["name"] == "Product-A"
    same = store.append_claim(
        topic_id="topic:anduril",
        subject_id=ids["Product-A"],
        kind="identity",
        detail={"name_id": "name:canonical-alias"},
        status="accepted",
        evidence=[{"segment_id": source.segment_key, "quote": TEXT}],
    )
    assert (
        next(
            r
            for r in asset_detail(store, "anduril", ids["Product-A"])["records"]
            if r["id"] == same
        )["label"]
        == "名称与身份"
    )


def test_read_apis_are_scoped_bounded_and_do_not_load_models(assets, monkeypatch):
    from api import knowledge_extraction_model

    store, _, ids = assets
    monkeypatch.setattr(routes, "DATABASE_PATH", store.path)
    monkeypatch.setattr(
        knowledge_extraction_model,
        "make_extractor",
        lambda *a, **k: pytest.fail("read API cannot invoke a model"),
    )
    app = FastAPI()
    app.include_router(routes.router)
    before = store.verify()["counts"]
    with TestClient(app) as client:
        summary = client.get("/api/research-database/asset-summary").json()
        assert summary["anduril"] == {"product": 2, "technology": 2, "sources": 1}
        assert client.get("/api/research-database/assets").json()["total"] == 2
        url = "/api/research-database/assets/" + ids["Product-A"]
        assert client.get(url).status_code == 200
        assert client.get(url + "/graph?depth=2").json()["edges"]
        assert client.get(url + "/graph?depth=20").status_code == 422
        assert client.get(url + "?slug=huawei").status_code == 404
    assert store.verify()["counts"] == before


def test_original_api_reads_archived_text_and_rejects_another_topic(assets, tmp_path, monkeypatch):
    from api.research_materials import archive_session_pages, research_materials

    from searchos.socm.workspace import WorkspaceManager

    store, _, _ = assets
    workspaces, data = tmp_path / "runs", tmp_path / "library"
    ws = WorkspaceManager(workspaces, "original")
    ws.write_page("page", "https://example.invalid/original", TEXT, "Fictional source")
    archive_session_pages("original", workspaces, data)
    materials = research_materials(data, "original")
    doc = materials["documents"][0]
    monkeypatch.setattr(routes, "DATABASE_PATH", store.path)
    monkeypatch.setattr(routes, "LOCAL_DATA_ROOT", data)
    monkeypatch.setattr(
        routes,
        "available_documents",
        lambda slug: {
            "library_id": materials["library_id"],
            "documents": [
                {
                    "document_id": doc["document_id"],
                    "name": doc["name"],
                    "url": "https://example.invalid/original",
                    "source_revision": doc["source_revision"],
                }
            ]
            if slug == "anduril"
            else [],
        },
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get("/api/research-database/materials").json()["total"] == 1
        url = "/api/research-database/materials/" + doc["document_id"]
        assert TEXT in client.get(url).json()["text_content"]
        assert client.get(url + "?slug=huawei").status_code == 404
