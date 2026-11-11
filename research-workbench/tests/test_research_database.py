"""Behavioral checks for the new, independent database. All facts are fictional."""

import hashlib
import json
import sqlite3

import pytest

from searchos.knowledge_store.database import ResearchDatabase, now

TOPIC = "topic:anduril"
QUOTE = "Fictional product A v1 uses technique T. Team X developed route R."


@pytest.fixture
def store(tmp_path):
    database = ResearchDatabase(tmp_path / "research.sqlite")
    database.initialize()
    with database.connect(write=True) as db:
        db.execute(
            "INSERT INTO source_documents VALUES(?,?,?,?,?)",
            ("doc:fiction", "https://example.invalid/fiction", "FICTIONAL TEST", "test", None),
        )
        db.execute(
            "INSERT INTO source_revisions VALUES(?,?,?,?,?,?)",
            (
                "revision:fiction",
                "doc:fiction",
                hashlib.sha256(QUOTE.encode()).hexdigest(),
                "fiction.txt",
                None,
                now(),
            ),
        )
        db.execute(
            "INSERT INTO source_segments VALUES(?,?,?,?)",
            ("segment:fiction", "revision:fiction", "paragraph:1", QUOTE),
        )
        db.execute(
            "INSERT INTO test_contexts VALUES(?,?,?,?)",
            ("context:unknown", "测试条件未披露", "{}", "partial"),
        )
    return database


def entity(store, kind, key, topic=TOPIC, namespace="anduril", **details):
    return store.create_entity(
        kind=kind, name=key, namespace=namespace, identity_key=key, topic_id=topic, details=details
    )


def evidence(quote=QUOTE):
    return [{"segment_id": "segment:fiction", "quote": quote}]


def field_claim(store, subject, value="fictional statement", **kwargs):
    return store.append_claim(
        topic_id=TOPIC,
        subject_id=subject,
        kind="field",
        detail={"field_id": "technology_brief", "value_json": json.dumps(value)},
        evidence=evidence(),
        **kwargs,
    )


def test_image_coverage_fields_and_repeatable_bootstrap(store):
    report = store.verify()
    assert report["valid"]
    assert [d["elements"] for d in report["dimensions"]] == [4, 8, 8, 7, 8, 6, 7]
    assert report["legacy_fields"] == 408
    assert report["counts"]["field_storage_mappings"] == 425
    assert report["counts"]["entities"] == 0
    assert report["topics"] == [{"id": TOPIC, "slug": "anduril", "title": "Anduril"}]
    tech = entity(store, "technology", "T")
    claim = field_claim(store, tech, status="accepted")
    store.initialize()
    with store.connect() as db:
        assert (
            db.execute("SELECT status FROM claims WHERE id=?", (claim,)).fetchone()[0] == "accepted"
        )
        assert db.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_existing_database_is_not_replaced(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE original(value TEXT)")
        db.execute("INSERT INTO original VALUES('retain')")
    with pytest.raises(ValueError, match="其他数据库"):
        ResearchDatabase(path).initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM original").fetchone()[0] == "retain"


def test_false_quotes_and_unsupported_acceptance_roll_back(store):
    tech = entity(store, "technology", "T")
    with pytest.raises(sqlite3.IntegrityError, match="continuous"):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=tech,
            kind="field",
            detail={"field_id": "technology_brief", "value_json": '"x"'},
            evidence=evidence("a quote that does not occur"),
            status="accepted",
        )
    with pytest.raises(sqlite3.IntegrityError, match="supporting"):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=tech,
            kind="field",
            detail={"field_id": "technology_brief", "value_json": '"x"'},
            evidence=[],
            status="accepted",
        )
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM claims").fetchone()[0] == 0


def test_topic_isolation_and_explicit_shared_concepts(store):
    huawei = store.create_topic("huawei", "华为")
    product_a = entity(store, "product", "Same name")
    product_h = entity(store, "product", "Same name", topic=huawei, namespace="huawei")
    tech = entity(store, "technology", "T", namespace="shared")
    assert product_a != product_h
    with pytest.raises(sqlite3.IntegrityError, match="topic"):
        store.append_claim(
            topic_id=huawei,
            subject_id=product_a,
            kind="relation",
            detail={"relation_type_id": "uses_technology", "object_id": tech},
            evidence=evidence(),
        )
    with pytest.raises(sqlite3.IntegrityError, match="topic"):
        store.append_claim(
            topic_id=huawei,
            subject_id=product_h,
            kind="relation",
            detail={"relation_type_id": "uses_technology", "object_id": tech},
            evidence=evidence(),
        )
    shared = entity(store, "technology", "T", namespace="shared", topic=huawei)
    assert shared == tech
    for topic, subject in [(TOPIC, product_a), (huawei, product_h)]:
        store.append_claim(
            topic_id=topic,
            subject_id=subject,
            kind="relation",
            detail={"relation_type_id": "uses_technology", "object_id": tech},
            evidence=evidence(),
            status="accepted",
        )
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM accepted_relations WHERE topic_id=?", (huawei,)
            ).fetchone()[0]
            == 1
        )


def test_entity_and_relation_type_constraints(store):
    product = entity(store, "product", "A")
    team = entity(store, "team", "X")
    with pytest.raises(sqlite3.IntegrityError, match="endpoints"):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=product,
            kind="relation",
            detail={"relation_type_id": "uses_technology", "object_id": team},
            evidence=evidence(),
        )
    with pytest.raises(sqlite3.IntegrityError, match="subtype"):
        with store.connect(write=True) as db:
            db.execute("INSERT INTO technologies(entity_id) VALUES(?)", (team,))


def test_metric_context_and_multiple_observations_preserved(store):
    product = entity(store, "product", "A v1", granularity="version")
    metric = entity(store, "metric", "latency", namespace="shared")
    detail = dict(
        metric_id=metric,
        raw_value="20 ms",
        numeric_value=20,
        unit_id="unit:ms",
        statistic="P95",
        value_nature="vendor_claim",
    )
    with pytest.raises(sqlite3.IntegrityError, match="context"):
        store.append_claim(
            topic_id=TOPIC, subject_id=product, kind="metric", detail=detail, evidence=evidence()
        )
    first = store.append_claim(
        topic_id=TOPIC,
        subject_id=product,
        kind="metric",
        detail=detail,
        evidence=evidence(),
        context_id="context:unknown",
        status="accepted",
    )
    second = store.append_claim(
        topic_id=TOPIC,
        subject_id=product,
        kind="metric",
        detail={**detail, "raw_value": "30 ms", "numeric_value": 30},
        evidence=evidence(),
        context_id="context:unknown",
        status="accepted",
    )
    assert first != second
    with store.connect() as db:
        assert [
            r[0]
            for r in db.execute("SELECT numeric_value FROM accepted_metrics ORDER BY numeric_value")
        ] == [20, 30]


def test_review_decision_survives_retry_and_details_cannot_be_overwritten(store):
    tech = entity(store, "technology", "T")
    claim = field_claim(store, tech, status="accepted")
    store.review_claim(claim, decision="withdraw", reviewer="test reviewer", reason="wrong subject")
    assert field_claim(store, tech, status="accepted") == claim
    with store.connect() as db:
        row = db.execute("SELECT status,review_locked FROM claims WHERE id=?", (claim,)).fetchone()
        assert tuple(row) == ("withdrawn", 1)
        assert db.execute("SELECT count(*) FROM review_decisions").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connect(write=True) as db:
            db.execute(
                "UPDATE field_values SET value_json='\"replacement\"' WHERE claim_id=?", (claim,)
            )


def test_accepted_fact_cannot_lose_evidence_or_change_source(store):
    claim = field_claim(store, entity(store, "technology", "T"), status="accepted")
    with pytest.raises(sqlite3.IntegrityError, match="retain"):
        with store.connect(write=True) as db:
            db.execute("DELETE FROM claim_evidence WHERE claim_id=?", (claim,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connect(write=True) as db:
            db.execute("UPDATE source_segments SET text_content='changed'")


def test_fields_are_typed_and_structured_metrics_cannot_go_into_generic_fields(store):
    tech = entity(store, "technology", "T")
    for value in ["未知", None]:
        with pytest.raises(sqlite3.IntegrityError):
            field_claim(store, tech, value)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=tech,
            kind="field",
            detail={"field_id": "technology_requires_annotation", "value_json": '"true"'},
            evidence=evidence(),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=tech,
            kind="field",
            detail={"field_id": "technology_input_data_types", "value_json": "[1,2]"},
            evidence=evidence(),
        )
    product = entity(store, "product", "A")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=product,
            kind="field",
            detail={"field_id": "product_length", "value_json": '{"value":1,"unit":"m"}'},
            evidence=evidence(),
        )


def test_cross_dimension_chain_is_queryable_with_real_joins(store):
    tech = entity(store, "technology", "T")
    route = entity(store, "route", "R")
    team = entity(store, "team", "X")
    product = entity(store, "product", "A v1", granularity="version")
    event = entity(store, "event", "M1", event_type="milestone")
    material = entity(store, "material", "M")
    application = entity(store, "application", "Case")
    scenario = entity(store, "scenario", "S")
    metric = entity(store, "metric", "throughput", namespace="shared")
    for subject, predicate, target in [
        (tech, "has_route", route),
        (route, "uses_material", material),
        (product, "uses_technology", tech),
        (application, "applies_to", scenario),
    ]:
        store.append_claim(
            topic_id=TOPIC,
            subject_id=subject,
            kind="relation",
            detail={"relation_type_id": predicate, "object_id": target},
            evidence=evidence(),
            status="accepted",
        )
    for subject, role, target in [
        (team, "researcher", route),
        (product, "applied_product", application),
        (route, "event_subject", event),
    ]:
        store.append_claim(
            topic_id=TOPIC,
            subject_id=subject,
            kind="participation",
            detail={"role_id": role, "object_id": target},
            evidence=evidence(),
            status="accepted",
        )
    store.append_claim(
        topic_id=TOPIC,
        subject_id=event,
        kind="event",
        detail={
            "occurred_at": "2025",
            "date_precision": "year",
            "description": "fictional milestone",
        },
        evidence=evidence(),
        status="accepted",
    )
    store.append_claim(
        topic_id=TOPIC,
        subject_id=product,
        kind="metric",
        detail={
            "metric_id": metric,
            "raw_value": "10",
            "numeric_value": 10,
            "value_nature": "vendor_claim",
        },
        evidence=evidence(),
        context_id="context:unknown",
        status="accepted",
    )
    with store.connect() as db:
        row = db.execute(
            """SELECT team.canonical_name,product.canonical_name FROM accepted_relations adopted
            JOIN accepted_relations routed ON routed.subject_id=adopted.object_id
                AND routed.relation_type_id='has_route' AND routed.topic_id=adopted.topic_id
            JOIN participations p ON p.object_id=routed.object_id AND p.role_id='researcher'
            JOIN claims c ON c.id=p.claim_id AND c.status='accepted' AND c.topic_id=adopted.topic_id
            JOIN entities team ON team.id=c.subject_id
            JOIN entities product ON product.id=adopted.subject_id
            WHERE adopted.relation_type_id='uses_technology' AND adopted.topic_id=?""",
            (TOPIC,),
        ).fetchone()
        assert tuple(row) == ("X", "A v1")
    assert store.verify()["valid"]


def test_schema_export_is_executable_and_mappings_keep_company_agnostic_labels(store, tmp_path):
    directory = store.export(tmp_path / "export")
    with sqlite3.connect(tmp_path / "structure-copy.sqlite") as db:
        db.executescript((directory / "schema.sql").read_text())
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            == 64
        )
    mappings = json.loads((directory / "field-mapping.json").read_text())
    assert len(mappings) == 425
    association = next(m for m in mappings if m["field_id"] == "technology_anduril_association")
    assert association["label"] == "与专题企业的关联"
    assert association["legacy_label"] == "与 Anduril 的关联"


def test_schema_change_requires_explicit_migration(store):
    with store.connect(write=True) as db:
        db.execute("UPDATE schema_migrations SET checksum='different' WHERE version=1")
    with pytest.raises(ValueError, match="显式迁移"):
        store.initialize()


def test_company_scope_cannot_leak_and_context_cannot_be_rewritten(store):
    tech = entity(store, "technology", "T", namespace="shared")
    huawei = store.create_topic("huawei", "华为")
    other_product = entity(store, "product", "H", topic=huawei, namespace="huawei")
    with pytest.raises(sqlite3.IntegrityError, match="scope product"):
        field_claim(store, tech, scope_product_id=other_product)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connect(write=True) as db:
            db.execute("UPDATE test_contexts SET conditions_json='{\"nodes\":10}'")


def test_composition_rejects_people_as_components(store):
    product = entity(store, "product", "A")
    person = entity(store, "person", "Expert")
    with pytest.raises(sqlite3.IntegrityError, match="technical"):
        store.append_claim(
            topic_id=TOPIC,
            subject_id=product,
            kind="composition",
            detail={"component_id": person, "row_key": "r1", "role": "component"},
            evidence=evidence(),
        )
