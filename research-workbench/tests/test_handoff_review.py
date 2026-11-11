"""Offline write-plan tests; fixtures are fictional and never touch business data."""

import json
import sqlite3

import pytest
from api.knowledge_handoff import review
from api.research_mapping import Source, key

from searchos.knowledge_store.database import ResearchDatabase

TEXT = "Product-X v2 supports publish-subscribe. Product-X v2 operates for 2 hours."


@pytest.fixture
def store(tmp_path):
    store = ResearchDatabase(tmp_path / "research.sqlite")
    store.initialize()
    return store


def source(text=TEXT, url="https://example.invalid/product-x"):
    return Source(
        url,
        "Fictional product",
        text,
        "fiction.md",
        "doc-fiction",
        library_id="library-fiction",
        document_id="doc-fiction",
    )


def output():
    return [
        {
            "entities": [
                {
                    "entity_key": "x",
                    "kind": "product",
                    "name": "Product-X v2",
                    "identity_quote": TEXT,
                    "fields": [
                        {
                            "field_id": "product_supports_publish_subscribe",
                            "value": True,
                            "quote": "Product-X v2 supports publish-subscribe.",
                        },
                        {
                            "field_id": "product_endurance",
                            "value": {"value": 2, "unit": "h", "raw": "2 hours"},
                            "quote": "Product-X v2 operates for 2 hours.",
                            "qualifiers": {"version": "v2"},
                        },
                    ],
                }
            ]
        }
    ]


def plan(store, tmp_path, src=None, outputs=None, **kwargs):
    return review.build_plan(
        store,
        src or source(),
        outputs or output(),
        slug="fiction",
        artifact_root=tmp_path / "artifacts",
        **kwargs,
    )


def commit(store, tmp_path, p, src=None, **kwargs):
    return review.commit_plan(
        store, src or source(), p["plan_id"], artifact_root=tmp_path / "artifacts", **kwargs
    )


def counts(store):
    with store.connect() as db:
        return {
            name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("entities", "claims", "source_documents", "field_definitions")
        }


def test_preview_is_read_only_and_has_real_bound_sql(store, tmp_path):
    before = counts(store)
    p = plan(store, tmp_path)
    assert counts(store) == before
    assert p["can_commit"] and not p["requires_partial_confirmation"]
    assert p["counts"]["new_records"] == 3
    assert p["sql_statements"]
    assert all(TEXT not in stmt["sql"] for stmt in p["sql_statements"])
    assert any(TEXT in stmt["params"] for stmt in p["sql_statements"])
    assert {r["status"] for r in p["records"]} == {"candidate"}
    receipt = commit(store, tmp_path, p)
    assert receipt["model_calls"] == 0
    assert counts(store)["claims"] == 3
    assert store.verify()["valid"]


def test_commit_is_idempotent_even_after_database_changes(store, tmp_path):
    p = plan(store, tmp_path)
    first = commit(store, tmp_path, p)
    before = counts(store)
    second = commit(store, tmp_path, p)
    assert first["reused"] is False and second["reused"] is True
    assert counts(store) == before
    p2 = plan(store, tmp_path)
    assert p2["counts"]["duplicates"] == 3
    commit(store, tmp_path, p2)
    assert counts(store) == before


def test_stale_database_and_stale_source_are_blocked(store, tmp_path):
    p = plan(store, tmp_path)
    with pytest.raises(ValueError, match="原文版本"):
        commit(store, tmp_path, p, source(TEXT + " changed"))
    store.create_topic("new-topic", "New")
    with pytest.raises(ValueError, match="数据库在预览后"):
        commit(store, tmp_path, p)
    assert counts(store)["claims"] == 0


def test_tampered_sql_never_runs(store, tmp_path):
    p = plan(store, tmp_path)
    path = tmp_path / "artifacts" / "plans" / (p["plan_id"] + ".json")
    saved = json.loads(path.read_text())
    saved["sql_statements"].append({"sql": "DELETE FROM topics", "params": []})
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="被修改"):
        commit(store, tmp_path, p)
    assert counts(store)["claims"] == 0


def test_bad_quote_requires_explicit_partial_confirmation(store, tmp_path):
    outputs = output()
    outputs[0]["entities"][0]["fields"][1]["quote"] = "Not in the source"
    p = plan(store, tmp_path, outputs=outputs)
    assert p["requires_partial_confirmation"] and p["unmapped"]
    assert p["counts"]["new_records"] == 2
    with pytest.raises(ValueError, match="明确确认"):
        commit(store, tmp_path, p)
    commit(store, tmp_path, p, allow_partial=True)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM metric_observations").fetchone()[0] == 0


def test_same_name_new_source_requires_identity_decision(store, tmp_path):
    p = plan(store, tmp_path)
    commit(store, tmp_path, p)
    other = source(url="https://example.invalid/other")
    p2 = plan(store, tmp_path, other)
    pending = next(e for e in p2["entities"] if e["status"] == "unresolved")
    assert not p2["can_commit"]
    p3 = plan(
        store,
        tmp_path,
        other,
        entity_choices={pending["resolution_key"]: pending["candidates"][0]["id"]},
    )
    assert p3["can_commit"] and not p3["requires_partial_confirmation"]
    commit(store, tmp_path, p3, other)
    assert counts(store)["entities"] == 3  # product + capability + metric


def test_source_scoped_alias_matches_only_its_source_and_keeps_scope(store, tmp_path):
    original = plan(store, tmp_path)
    commit(store, tmp_path, original)
    product_id = next(e["id"] for e in original["entities"] if e["kind"] == "product")
    text = "The product is shortened to X2 in this source."
    source_a = source(text, "https://example.invalid/source-a")
    source_b = source(text, "https://example.invalid/source-b")
    source_a.register(store)
    name_id = "name:" + key(product_id, "X2")
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO entity_names(id,entity_id,name,name_type) VALUES(?,?,?,'alias')",
            (name_id, product_id, "X2"),
        )
        db.execute("INSERT INTO entity_name_scopes VALUES(?,?)", (name_id, source_a.document_key))
    outputs = [{"entities": [{
        "entity_key": "x", "kind": "product", "name": "X2",
        "identity_quote": text, "fields": [],
    }]}]
    matched = plan(store, tmp_path, source_a, outputs, entity_choices={"x": product_id})
    assert matched["can_commit"]
    assert matched["entities"][0]["status"] == "matched"
    commit(store, tmp_path, matched, source_a)
    with store.connect() as db:
        assert db.execute("SELECT name_type FROM entity_names WHERE id=?", (name_id,)).fetchone()[0] == "alias"
        assert db.execute("SELECT source_document_id FROM entity_name_scopes WHERE name_id=?", (name_id,)).fetchone()[0] == source_a.document_key
    other = plan(store, tmp_path, source_b, outputs)
    assert other["entities"][0]["status"] == "new"
    assert other["entities"][0]["id"] != product_id
    assert not other["entities"][0]["candidates"]


def test_conflicting_scalar_keeps_existing_fact(store, tmp_path):
    p = plan(store, tmp_path)
    commit(store, tmp_path, p)
    altered_text = TEXT + " Product-X v2 does not support publish-subscribe."
    outputs = output()
    outputs[0]["entities"][0]["fields"] = [
        {
            "field_id": "product_supports_publish_subscribe",
            "value": False,
            "quote": "Product-X v2 does not support publish-subscribe.",
        }
    ]
    product_id = next(e["id"] for e in p["entities"] if e["kind"] == "product")
    p2 = plan(store, tmp_path, source(altered_text), outputs, entity_choices={"x": product_id})
    assert p2["conflicts"] and p2["requires_partial_confirmation"]
    commit(store, tmp_path, p2, source(altered_text), allow_partial=True)
    with store.connect() as db:
        assert [r[0] for r in db.execute("SELECT support_status FROM product_capabilities")] == [
            "supported"
        ]


def test_metric_conflict_ignores_value_in_context(store, tmp_path):
    p = plan(store, tmp_path)
    commit(store, tmp_path, p)
    new_text = TEXT.replace("2 hours", "3 hours")
    outputs = output()
    outputs[0]["entities"][0]["identity_quote"] = new_text
    fields = outputs[0]["entities"][0]["fields"]
    fields[1]["quote"] = "Product-X v2 operates for 3 hours."
    fields[1]["value"] = {"value": 3, "unit": "h", "raw": "3 hours"}
    product_id = next(e["id"] for e in p["entities"] if e["kind"] == "product")
    p2 = plan(store, tmp_path, source(new_text), outputs, entity_choices={"x": product_id})
    assert p2["conflicts"]
    assert not any(r["kind"] == "metric" for r in p2["records"])


def test_source_descriptions_can_coexist_without_false_scalar_conflict(store, tmp_path):
    product_id = None
    for index, text in enumerate([
        "Product-X v2 is a publish-subscribe platform.",
        "Product-X v2 provides a messaging interface.",
    ]):
        outputs = output()
        entity = outputs[0]["entities"][0]
        entity["identity_quote"] = text
        entity["fields"] = [{"field_id": "product_summary", "value": text, "quote": text}]
        src = source(text, f"https://example.invalid/description-{index}")
        choices = {"x": product_id} if product_id else {}
        p = plan(store, tmp_path, src, outputs, entity_choices=choices)
        assert not p["conflicts"]
        commit(store, tmp_path, p, src)
        product_id = next(e["id"] for e in p["entities"] if e["kind"] == "product")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 1
        assert db.execute(
            "SELECT count(*) FROM field_values WHERE field_id='product_summary'"
        ).fetchone()[0] == 2


def test_failed_transaction_rolls_back_all_writes(store, tmp_path):
    p = plan(store, tmp_path)
    p["sql_statements"].append(
        {"sql": "INSERT INTO table_that_does_not_exist VALUES(?)", "params": [1]}
    )
    p["signature"] = review._signature(p, review._secret(tmp_path / "artifacts"))
    review._write(review._plan_path(tmp_path / "artifacts", p["plan_id"]), p)
    before = counts(store)
    with pytest.raises(sqlite3.OperationalError):
        commit(store, tmp_path, p)
    assert counts(store) == before


def test_savepoint_rollback_removes_recorded_failed_writes():
    recorder = review._Recorder()
    recorder.executed("INSERT INTO a VALUES(?)", (1,))
    recorder.executed("SAVEPOINT example", ())
    recorder.executed("INSERT INTO a VALUES(?)", (2,))
    recorder.executed("SAVEPOINT nested", ())
    recorder.executed("INSERT INTO a VALUES(?)", (3,))
    recorder.executed("ROLLBACK TO example", ())
    recorder.executed("RELEASE example", ())
    assert recorder.statements == [{"sql": "INSERT INTO a VALUES(?)", "params": [1]}]


def test_explicit_new_does_not_reuse_same_source_identity(store, tmp_path):
    p = plan(store, tmp_path)
    commit(store, tmp_path, p)
    p2 = plan(store, tmp_path, entity_choices={"x": "new"})
    old = next(e["id"] for e in p["entities"] if e["kind"] == "product")
    new = next(e["id"] for e in p2["entities"] if e["kind"] == "product")
    assert old != new
    commit(store, tmp_path, p2)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM products").fetchone()[0] == 2


def test_invalid_group_rolls_back_earlier_members(store, tmp_path):
    from api.knowledge_handoff.review import _RecordingStore, _ReviewMapper
    from api.research_mapping import ExtractedEntity, ExtractedField

    temp = _RecordingStore(store.path)
    topic = store.create_topic("fiction", "Fiction")
    store.create_entity(
        kind="technology",
        name="Ambiguous-Tech",
        namespace="shared",
        identity_key="existing-tech",
        topic_id=topic,
    )
    with temp.batch():
        mapper = _ReviewMapper(temp, source(), slug="fiction")
        entity = ExtractedEntity(
            entity_key="root", kind="technology", name="Root-Tech", identity_quote=TEXT
        )
        mapper.identity(entity)
        before = len(temp.recorder.statements)
        fields = [
            ExtractedField(
                field_id="technology_dependency_technology",
                value=["Fresh-Tech", "Ambiguous-Tech"],
                quote=TEXT,
                qualifiers={"row_key": "dependency-1"},
            )
        ]
        with pytest.raises(ValueError, match="身份待确认"):
            mapper.repeat(entity, "dependencies", fields)
        assert len(temp.recorder.statements) == before
        with temp.connect() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM entities WHERE canonical_name='Fresh-Tech'"
                ).fetchone()[0]
                == 0
            )
            assert db.execute("SELECT count(*) FROM entity_relations").fetchone()[0] == 0


def test_receipt_cache_failure_reports_success_and_retry_recovers(store, tmp_path, monkeypatch):
    p = plan(store, tmp_path)
    writer = review._write

    def failed_receipt_write(path, value):
        if path.parent.name == "receipts":
            raise OSError("simulated receipt disk failure")
        return writer(path, value)

    monkeypatch.setattr(review, "_write", failed_receipt_write)
    first = commit(store, tmp_path, p)
    assert first["status"] == "completed"
    assert "数据库已入库" in first["artifact_error"]
    assert not first["reused"]
    after = counts(store)
    assert after["claims"] == 3
    with store.connect() as db:
        marker = db.execute(
            "SELECT value FROM database_metadata WHERE key=?",
            ("handoff_commit:" + p["plan_id"],),
        ).fetchone()
    assert marker is not None
    assert "artifact_error" not in json.loads(marker[0])

    # Persistent cache failures still return the durable committed result.
    repeated = commit(store, tmp_path, p)
    assert repeated["reused"] and repeated["artifact_error"]
    assert counts(store) == after

    monkeypatch.setattr(review, "_write", writer)
    recovered = commit(store, tmp_path, p)
    assert recovered["reused"] and "artifact_error" not in recovered
    cache = tmp_path / "artifacts" / "receipts" / (p["plan_id"] + ".json")
    assert json.loads(cache.read_text())["committed_at"] == first["committed_at"]
    assert counts(store) == after
