import copy

import pytest
from api.knowledge_database import KnowledgeDatabase
from api.routes import topics as routes
from api.topic_resolution import (
    apply_topic_merges,
    duplicate_candidates,
    merge_entities,
    undo_merge,
)
from api.topic_store import TopicStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def node(identity, name, kind="product", **attributes):
    return {
        "id": identity,
        "name": name,
        "kind": kind,
        "summary": name,
        "category": "",
        "attributes": attributes,
        "evidence_ids": [],
        "field_evidence": {},
        "field_overrides": {},
        "origin": "manual",
        "status": "manual",
    }


def test_formatting_aliases_do_not_merge_versions_types_or_family_members():
    nodes = [
        node("a", "Dive-LD"),
        node("b", "Dive LD"),
        node("c", "Dive-XL"),
        node("d", "Lattice SDK™"),
        node("e", "Lattice SDK"),
        node("f", "Altius-600"),
        node("g", "Altius-600M"),
        node("h", "Lattice"),
        node("i", "Lattice Mesh"),
        node("j", "Lattice", "technology"),
    ]
    matches = duplicate_candidates(nodes, [])
    assert {frozenset((c["canonical_id"], c["alias_id"])) for c in matches} == {
        frozenset(("a", "b")),
        frozenset(("d", "e")),
    }
    assert all(c["confidence"] == "high" for c in matches)


def test_same_name_with_different_explicit_version_or_manufacturer_is_not_mergeable():
    assert not duplicate_candidates(
        [
            node("a", "Standard Range Sentry Autonomous Awareness"),
            node("b", "Extended Range Sentry Autonomous Awareness"),
        ],
        [],
    )
    for attributes in [
        ({"version": "1"}, {"version": "2"}),
        ({"manufacturer": "A"}, {"厂商": "B"}),
        ({"model": "600"}, {"产品型号": "600M"}),
    ]:
        assert (
            duplicate_candidates(
                [node("a", "Lattice", **attributes[0]), node("b", "Lattice™", **attributes[1])], []
            )
            == []
        )


@pytest.fixture
def fixture(tmp_path):
    store = TopicStore(
        tmp_path / "local", tmp_path / "topics.sqlite", tmp_path / "knowledge.sqlite"
    )
    db = KnowledgeDatabase(store.data_root, store.knowledge_database)
    a, b = store.create_topic("A"), store.create_topic("B")
    first = db.create_entity(
        {"name": "Dive-LD", "kind": "product", "attributes": {"length": "5.8 m"}}
    )
    second = db.create_entity(
        {"name": "Dive LD", "kind": "product", "attributes": {"length": "6 m", "weight": "sample"}}
    )
    child = db.create_entity({"name": "Sensor", "kind": "product"})
    db.create_relation(
        {"source_id": second["id"], "target_id": child["id"], "predicate": "deploys"}
    )
    with store._connection() as connection:
        for topic in (a, b):
            for entity in (first, second, child):
                connection.execute(
                    "INSERT INTO topic_entities VALUES(?,?,0)", (topic["id"], entity["id"])
                )
    return store, db, a, b, first, second, child


def test_merge_is_scoped_reversible_and_preserves_conflicting_source_values(fixture):
    store, db, a, b, first, second, child = fixture
    original = copy.deepcopy(db.snapshot())
    merged = merge_entities(store, a["id"], first["id"], second["id"])
    assert len(merged["knowledge"]["entities"]) == 2
    one = next(e for e in merged["knowledge"]["entities"] if e["id"] == first["id"])
    assert one["attributes"]["length"] == "5.8 m"
    assert one["attributes"]["weight"] == "sample"
    assert one["merged_entities"][0]["attributes"]["length"] == "6 m"
    assert merged["knowledge"]["relations"][0]["source_id"] == first["id"]
    assert merged["knowledge"]["relations"][0]["status"] == "manual"
    assert (
        next(f for f in merged["entity_fields"][first["id"]] if f["key"] == "length")["status"]
        == "conflicting"
    )
    assert len(store.snapshot(b["id"])["knowledge"]["entities"]) == 3
    assert db.snapshot() == original
    restored = undo_merge(store, a["id"], second["id"])
    assert len(restored["knowledge"]["entities"]) == 3
    assert restored["knowledge"]["relations"][0]["source_id"] == second["id"]


def test_removed_membership_is_not_resurrected_and_merge_is_not_chained(fixture):
    store, _, a, _, first, second, child = fixture
    merge_entities(store, a["id"], first["id"], second["id"])
    with pytest.raises(ValueError):
        merge_entities(store, a["id"], child["id"], first["id"])
    # Simulate an independently removed original while its alias overlay exists.
    with store._connection() as connection:
        connection.execute(
            "UPDATE topic_entities SET excluded=1 WHERE topic_id=? AND entity_id=?",
            (a["id"], second["id"]),
        )
    snapshot = store.snapshot(a["id"])
    assert all("merged_entities" not in e for e in snapshot["knowledge"]["entities"])
    assert len(undo_merge(store, a["id"], second["id"])["knowledge"]["entities"]) == 2


def test_inherited_manual_override_is_not_offered_as_canonical_restorable_override(fixture):
    store, db, a, _, first, second, _ = fixture
    db.update_field(second["id"], "speed", "manual speed")
    snap = merge_entities(store, a["id"], first["id"], second["id"])
    entity = next(e for e in snap["knowledge"]["entities"] if e["id"] == first["id"])
    assert "speed" not in entity["field_overrides"]
    inherited = next(
        o
        for o in entity["attributes"]["document_observations"]
        if o.get("merged_from") == second["id"]
    )
    assert inherited["field_overrides"]["speed"]["value"] == "manual speed"


def test_duplicate_edges_keep_all_provenance_and_remove_self_edges(fixture):
    store, _, a, _, first, second, child = fixture
    merge_entities(store, a["id"], first["id"], second["id"])
    edges = [
        dict(
            id=key,
            source_id=source,
            target_id=target,
            predicate="deploys",
            evidence_ids=[evidence],
            status="source_backed",
            origin="archive",
        )
        for key, source, target, evidence in [
            ("r1", first["id"], child["id"], "q1"),
            ("r2", second["id"], child["id"], "q2"),
            ("r3", second["id"], first["id"], "q3"),
        ]
    ]
    _, result, _ = apply_topic_merges(store, a["id"], [first, second, child], edges)
    assert len(result) == 1 and result[0]["evidence_ids"] == ["q1", "q2"]


def test_merge_api_rejects_cross_topic_or_different_objects(fixture, monkeypatch):
    store, _, a, _, first, second, child = fixture
    monkeypatch.setattr(routes, "_store", lambda: store)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    path = f"/api/topics/{a['id']}/merges"
    assert (
        client.post(path, json={"canonical_id": first["id"], "alias_id": child["id"]}).status_code
        == 422
    )
    assert (
        client.post(path, json={"canonical_id": first["id"], "alias_id": second["id"]}).status_code
        == 200
    )
    assert client.delete(path + "/" + second["id"]).status_code == 200
