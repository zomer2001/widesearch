"""Relationship repair is offline, explicit, independently rechecked and reversible by deletion."""

import asyncio
import copy
import json
import sqlite3

import pytest
from api.knowledge_database import KnowledgeDatabase
from api.knowledge_repair import apply_repair, propose_repairs

from searchos.local.library import LocalLibraryService
from searchos.local.reader.references import build_ref_id

FAMILY = (
    "Lattice is organized into four distinct product lines: Lattice for Command & Control, "
    "Lattice Mesh, Lattice for Mission Autonomy, and the Lattice SDK."
)
DEPLOY = (
    "Dive-XL stows and autonomously\ndeploys Seabed Sentries precisely to\nthe ocean floor. "
    "Once placed, Seabed\nSentry self-calibrates and connects."
)
CARRY = (
    "A Dive-XL can carry dozens of the smaller Copperhead-M "
    "and multiple of the larger size missile."
)
PASSIVE = (
    "Copperhead-M munition variants are described as torpedo-like payloads "
    "designed to be carried by autonomous systems including Dive-LD and Dive-XL."
)
NAMES = [
    "Lattice",
    "Lattice for Command & Control",
    "Lattice Mesh",
    "Lattice for Mission Autonomy",
    "Lattice SDK",
    "Dive-XL",
    "Seabed Sentry",
    "Copperhead-M",
    "Dive-LD",
]


def snapshot(quote, names=NAMES):
    return {
        "entities": [
            {
                "id": name,
                "name": name,
                "kind": "product",
                "origin": "archive",
                "status": "source_backed",
                "attributes": {},
                "evidence_ids": ["e"],
            }
            for name in names
        ],
        "relations": [],
        "evidence": [
            {
                "id": "e",
                "quote": quote,
                "available": True,
                "library_id": "lib_a",
                "document_id": "doc_a",
                "ref_id": "local://test",
            }
        ],
    }


def edges(value):
    return {
        (item["source_id"], item["predicate"], item["target_id"]) for item in propose_repairs(value)
    }


def test_explicit_product_line_sentence_points_child_to_parent():
    assert edges(snapshot(FAMILY)) == {(name, "part_of", "Lattice") for name in NAMES[1:5]}


def test_deployment_inflection_requires_explicit_singular_in_same_quote():
    assert edges(snapshot(DEPLOY)) == {("Dive-XL", "deploys", "Seabed Sentry")}
    assert not edges(snapshot("Dive-XL deploys Seabed Sentries precisely to the ocean floor."))
    assert not edges(
        snapshot("Dive-XL deploys the nodes to the ocean floor. Seabed Sentry is a sensor.")
    )


def test_active_passive_carrying_preserve_direction_and_do_not_infer_variant():
    assert edges(snapshot(CARRY)) == {("Dive-XL", "carries", "Copperhead-M")}
    assert edges(snapshot(PASSIVE)) == {
        ("Dive-XL", "carries", "Copperhead-M"),
        ("Dive-LD", "carries", "Copperhead-M"),
    }
    assert not edges(snapshot("Dive-XL can carry Copperhead-M.", ["Dive-XL", "Copperhead"]))


@pytest.mark.parametrize(
    "quote",
    [
        "Lattice Mesh | Lattice SDK | Lattice for Command & Control | Lattice",
        "Dive-XL and Copperhead-M are two products listed on this website.",
        "Dive-XL cannot carry Copperhead-M.",
        "Dive-XL may carry Copperhead-M.",
        "Dive-XL carries sensors made by Copperhead-M.",
        "Dive-XL carries no Copperhead-M.",
    ],
)
def test_lists_comention_negation_speculation_and_wrong_object_do_not_create_edges(quote):
    assert not propose_repairs(snapshot(quote))


def test_exact_known_aliases_only_ambiguous_canonical_identity_rejected():
    value = snapshot("Carrier can carry Payload.", ["Real Carrier", "Payload"])
    value["entities"][0]["attributes"]["aliases"] = ["Carrier"]
    assert edges(value) == {("Real Carrier", "carries", "Payload")}
    duplicate = copy.deepcopy(value["entities"][1])
    duplicate["id"] = "other-Payload"
    value["entities"].append(duplicate)
    assert not edges(value)


@pytest.mark.parametrize("change", ["missing_quote", "stale", "web_only", "manual", "wrong_kind"])
def test_only_available_archived_quotes_and_source_backed_typed_endpoints(change):
    value = snapshot(CARRY)
    if change == "missing_quote":
        value["evidence"][0]["quote"] = ""
    if change == "stale":
        value["evidence"][0]["available"] = False
    if change == "web_only":
        value["evidence"][0]["ref_id"] = ""
    if change in {"manual", "wrong_kind"}:
        for entity in value["entities"]:
            if change == "manual":
                entity["origin"] = entity["status"] = "manual"
            else:
                entity["kind"] = "technology"
    assert not propose_repairs(value)


@pytest.fixture
def stored(tmp_path):
    service = LocalLibraryService(tmp_path / "local")
    library = service.create_library("原始资料")
    original = tmp_path / "original.md"
    original.write_text("# Platforms\n\n" + CARRY + "\n\n" + PASSIVE + "\n", encoding="utf-8")
    record = service.save_original_file(library.library_id, original, "original.md")
    asyncio.run(service.parse_saved_document(library.library_id, record["document_id"]))
    evidence = []
    for index, quote in enumerate([CARRY, PASSIVE]):
        source, block = next(
            (source, block)
            for source, block in service._verified_blocks(
                library.library_id, [record["document_id"]]
            )
            if quote in block.text
        )
        ref_id = build_ref_id(
            library_id=source.library_id,
            document_id=source.document_id,
            source_revision=source.source_revision,
            locators=list(block.source_locators),
        )
        evidence.append(
            {
                "id": f"e{index}",
                "quote": quote,
                "ref_id": ref_id,
                "library_id": source.library_id,
                "document_id": source.document_id,
                "source_revision": source.source_revision,
                "url": "https://example.com/platforms",
            }
        )
    database = KnowledgeDatabase(service.data_root, tmp_path / "knowledge.sqlite")
    entities = [
        {
            "key": name,
            "name": name,
            "kind": "product",
            "namespace": "example",
            "summary": "公开平台",
            "category": "平台",
            "attributes": {},
            "evidence_ids": [item["id"] for item in evidence],
        }
        for name in ["Dive-XL", "Dive-LD", "Copperhead-M"]
    ]
    database.ingest_extraction("fixture", entities, [], evidence, {}, [])
    return database, service, library.library_id, record["document_id"]


def test_apply_rechecks_real_local_original_and_is_idempotent(stored):
    database, *_ = stored
    before = database.snapshot()
    candidates = propose_repairs(before)
    assert len(candidates) == 2
    assert sum(apply_repair(database, candidate) for candidate in candidates) == 2
    after = database.snapshot()
    assert after["entities"] == before["entities"]
    assert len(after["relations"]) == 2
    assert all(edge["status"] == "source_backed" for edge in after["relations"])
    assert sum(apply_repair(database, candidate) for candidate in candidates) == 0
    assert not propose_repairs(after)
    with sqlite3.connect(database.database) as connection:
        assert connection.execute("SELECT count(*) FROM relation_evidence").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM relation_observations").fetchone()[0] == 3


def test_apply_never_resurrects_tombstone_or_overwrites_manual_edge(stored):
    database, *_ = stored
    candidates = propose_repairs(database.snapshot())
    assert apply_repair(database, candidates[0]) == 1
    edge = database.snapshot()["relations"][0]
    database.delete_relation(edge["id"])
    assert apply_repair(database, candidates[0]) == 0
    database.create_relation(
        {key: candidates[1][key] for key in ("source_id", "target_id", "predicate")}
    )
    before = database.snapshot()
    assert apply_repair(database, candidates[1]) == 0
    assert database.snapshot()["relations"] == before["relations"]


def test_stale_source_forged_relation_and_unrelated_evidence_rejected(stored):
    database, service, library, document = stored
    candidate = propose_repairs(database.snapshot())[0]
    assert apply_repair(database, {**candidate, "predicate": "part_of"}) == 0
    assert (
        apply_repair(
            database,
            {**candidate, "source_id": candidate["target_id"], "target_id": candidate["source_id"]},
        )
        == 0
    )
    assert apply_repair(database, {**candidate, "evidence_ids": ["missing"]}) == 0
    service.delete_document(library, document)
    assert apply_repair(database, candidate) == 0
    with sqlite3.connect(database.database) as connection:
        assert connection.execute("SELECT count(*) FROM relations").fetchone()[0] == 0


def test_changed_quote_is_rejected_even_if_candidate_was_once_valid(stored):
    database, *_ = stored
    candidate = propose_repairs(database.snapshot())[0]
    with database._connection(write=True) as connection:
        for key in candidate["evidence_ids"]:
            payload = json.loads(
                connection.execute("SELECT payload FROM evidence WHERE id=?", (key,)).fetchone()[0]
            )
            payload["quote"] = "The page lists products, without any relationship claim."
            connection.execute(
                "UPDATE evidence SET payload=? WHERE id=?", (json.dumps(payload), key)
            )
    assert apply_repair(database, candidate) == 0


def test_explicit_original_can_repair_edge_even_if_quote_attached_to_another_entity():
    value = snapshot(FAMILY)
    for entity in value["entities"]:
        entity["evidence_ids"] = ["different-original"]
    assert len(propose_repairs(value)) == 4


def test_source_disappearing_after_snapshot_is_rechecked_before_commit(stored, monkeypatch):
    database, service, library, document = stored
    real_snapshot = database.snapshot
    candidate = propose_repairs(real_snapshot())[0]

    def delayed_snapshot():
        result = real_snapshot()
        service.delete_document(library, document)
        return result

    monkeypatch.setattr(database, "snapshot", delayed_snapshot)
    assert apply_repair(database, candidate) == 0
    with sqlite3.connect(database.database) as connection:
        assert connection.execute("SELECT count(*) FROM relations").fetchone()[0] == 0
