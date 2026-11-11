"""Local entity/relation database. Sync only evidence-bound archived records.

The collection's older report-derived candidate store is deliberately separate.
This module does not load model settings, fetch URLs, or invoke any model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from api.anduril_reader import load_anduril_knowledge, quote_matches
from searchos.local.library import LocalLibraryService

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / "data/knowledge/knowledge.sqlite"
ENTITY_KINDS = {"product", "technology", "route", "reference"}
PREDICATES = {
    "variant_of": "型号属于",
    "part_of": "组成部分",
    "replaces": "替代",
    "uses_technology": "涉及技术",
    "has_route": "关联路线",
    "reference_for": "独立参考",
    "carries": "可搭载",
    "deploys": "可部署",
    "integrates": "集成",
    "evolves_from": "演进自",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_entity_name(value: str) -> str:
    """Exact Unicode/case/whitespace identity, preserving model/version punctuation."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _relation_id(source: str, predicate: str, target: str) -> str:
    value = hashlib.sha256(_json([source, predicate, target]).encode()).hexdigest()[:28]
    return "relation:" + value


def _http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"https", "http"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


class KnowledgeDatabase:
    def __init__(self, data_root: str | Path, database: str | Path | None = None):
        self.data_root = Path(data_root)
        self.database = Path(database) if database is not None else DEFAULT_DATABASE

    @contextmanager
    def _connection(self, *, write: bool = False):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                summary TEXT NOT NULL, category TEXT NOT NULL, attributes TEXT NOT NULL,
                origin TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relations (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES entities(id),
                target_id TEXT NOT NULL REFERENCES entities(id),
                predicate TEXT NOT NULL, origin TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, deleted_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(source_id, predicate, target_id)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entity_evidence (
                entity_id TEXT NOT NULL REFERENCES entities(id),
                evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(entity_id, evidence_id)
            );
            CREATE TABLE IF NOT EXISTS relation_evidence (
                relation_id TEXT NOT NULL REFERENCES relations(id),
                evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(relation_id, evidence_id)
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS entities_kind ON entities(kind, active, deleted_at);
            CREATE INDEX IF NOT EXISTS relations_source ON relations(source_id);
            CREATE INDEX IF NOT EXISTS relations_target ON relations(target_id);
            CREATE TABLE IF NOT EXISTS entity_observations (
                id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
                document_key TEXT NOT NULL, payload TEXT NOT NULL,
                evidence_ids TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relation_observations (
                id TEXT PRIMARY KEY, relation_id TEXT NOT NULL REFERENCES relations(id),
                document_key TEXT NOT NULL, evidence_ids TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entity_identities (
                namespace TEXT NOT NULL, kind TEXT NOT NULL, normalized_name TEXT NOT NULL,
                entity_id TEXT NOT NULL REFERENCES entities(id),
                PRIMARY KEY(namespace,kind,normalized_name)
            );
            CREATE TABLE IF NOT EXISTS seed_records (
                record_kind TEXT NOT NULL, identity TEXT NOT NULL,
                PRIMARY KEY(record_kind,identity)
            );
            CREATE TABLE IF NOT EXISTS extraction_runs (
                run_key TEXT PRIMARY KEY, result TEXT NOT NULL, raw_outputs TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entity_field_overrides (
                entity_id TEXT NOT NULL REFERENCES entities(id), field TEXT NOT NULL,
                value TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(entity_id,field)
            );
            CREATE TABLE IF NOT EXISTS entity_field_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
                field TEXT NOT NULL, value TEXT, previous_value TEXT, updated_at TEXT NOT NULL
            );
        """)
        if (
            connection.execute(
                "SELECT 1 FROM metadata WHERE key='observations_schema_initialized'"
            ).fetchone()
            is None
        ):
            connection.execute(
                "INSERT OR IGNORE INTO seed_records SELECT 'entity',id FROM entities "
                "WHERE origin='archive' AND id NOT IN (SELECT entity_id FROM entity_observations)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO seed_records SELECT 'relation',id FROM relations "
                "WHERE origin='archive' AND id NOT IN "
                "(SELECT relation_id FROM relation_observations)"
            )
            self._metadata(connection, "observations_schema_initialized", True)
        # The bundled seed dataset has an explicit Anduril scope. Bind it once,
        # including tombstones, so a later import cannot recreate a deleted node
        # under a fresh generated ID. Other namespaces never use these aliases.
        for row in connection.execute(
            "SELECT id,kind,name FROM entities WHERE id IN "
            "(SELECT identity FROM seed_records WHERE record_kind='entity')"
        ):
            connection.execute(
                "INSERT OR IGNORE INTO entity_identities VALUES('anduril',?,?,?)",
                (row["kind"], normalize_entity_name(row["name"]), row["id"]),
            )
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _metadata(connection, key, value):
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )

    def sync(self, reader_root: str | Path | None = None) -> dict:
        """Explicit deterministic sync; soft deletions and manual edits survive."""
        reader = load_anduril_knowledge(self.data_root, reader_root)
        sources = {item["id"]: item for item in reader["sources"] if item.get("available")}
        entities: dict[str, dict] = {}
        for collection, kind in (
            ("products", "product"),
            ("technologies", "technology"),
            ("routes", "route"),
            ("reproduction_units", "reference"),
        ):
            for item in reader[collection]:
                evidence_ids = [key for key in item.get("source_ids", []) if key in sources]
                if not evidence_ids:
                    continue
                attributes = copy.deepcopy(item)
                for key in ("id", "name", "source_ids"):
                    attributes.pop(key, None)
                entities[item["id"]] = {
                    "id": item["id"],
                    "kind": kind,
                    "name": item["name"],
                    "summary": item.get("overview")
                    or item.get("mechanism")
                    or item.get("description", ""),
                    "category": item.get("category", ""),
                    "attributes": attributes,
                    "evidence_ids": list(dict.fromkeys(evidence_ids)),
                }

        relations: dict[str, dict] = {}

        def connect(source_id, predicate, target_id):
            source, target = entities.get(source_id), entities.get(target_id)
            if not source or not target:
                return

            # A shared document/version alone is not sufficient: callers also
            # require an explicit typed association in the reader artifact.
            # Product and mechanism quotes may come from different paragraphs.
            def document_key(identity):
                item = sources[identity]
                if not item.get("ref_id") or not item.get("document_id"):
                    return None
                return (item.get("library_id"), item["document_id"], item.get("source_revision"))

            common_documents = {document_key(key) for key in source["evidence_ids"]} & {
                document_key(key) for key in target["evidence_ids"]
            }
            common_documents.discard(None)
            evidence_ids = sorted(
                {
                    key
                    # Explain this specific mechanism/route association with its
                    # own quote, not every unrelated detail on the product page.
                    for key in target["evidence_ids"]
                    if document_key(key) in common_documents
                }
            )
            if not evidence_ids:
                return
            identity = _relation_id(source_id, predicate, target_id)
            relations[identity] = {
                "id": identity,
                "source_id": source_id,
                "target_id": target_id,
                "predicate": predicate,
                "evidence_ids": evidence_ids,
            }

        for item in reader["products"]:
            for technology in item.get("technology_ids", []):
                connect(item["id"], "uses_technology", technology)
        for item in reader["technologies"]:
            for product in item.get("product_ids", []):
                connect(product, "uses_technology", item["id"])
            for route in item.get("route_ids", []):
                connect(item["id"], "has_route", route)
        for item in reader["routes"]:
            for product in item.get("product_ids", []):
                connect(product, "has_route", item["id"])
        # No name-based family, algorithm, or repository adoption inference.
        # Repository entries stay independent unless an explicit reference link
        # and shared evidence exist. A reference link is never an adoption claim.
        for item in reader["reproduction_units"]:
            for technology in item.get("related_technology_ids", []):
                connect(item["id"], "reference_for", technology)

        timestamp = _now()
        skipped = 0
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE entities SET active=0 WHERE origin='archive' "
                "AND id IN (SELECT identity FROM seed_records WHERE record_kind='entity') "
            )
            connection.execute(
                "UPDATE relations SET active=0 WHERE origin='archive' "
                "AND id IN (SELECT identity FROM seed_records WHERE record_kind='relation') "
            )
            # Retire removed seed prose while independently imported document
            # observations remain eligible to supply their own checked fields.
            for row in connection.execute("SELECT * FROM seed_records"):
                collection = entities if row["record_kind"] == "entity" else relations
                if row["identity"] not in collection:
                    table = (
                        "entity_evidence" if row["record_kind"] == "entity" else "relation_evidence"
                    )
                    column = "entity_id" if row["record_kind"] == "entity" else "relation_id"
                    connection.execute(f"DELETE FROM {table} WHERE {column}=?", (row["identity"],))
            connection.execute(
                "UPDATE entities SET active=1 WHERE origin='archive' "
                "AND id IN (SELECT entity_id FROM entity_observations)"
            )
            connection.execute(
                "UPDATE relations SET active=1 WHERE origin='archive' "
                "AND id IN (SELECT relation_id FROM relation_observations)"
            )
            for source in sources.values():
                connection.execute(
                    "INSERT INTO evidence(id,payload,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                    "updated_at=excluded.updated_at",
                    (source["id"], _json(source), timestamp),
                )
            for entity in entities.values():
                connection.execute(
                    "INSERT OR IGNORE INTO seed_records VALUES('entity',?)", (entity["id"],)
                )
                existing = connection.execute(
                    "SELECT deleted_at,origin FROM entities WHERE id=?", (entity["id"],)
                ).fetchone()
                if existing and (existing["deleted_at"] or existing["origin"] == "manual"):
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO entities(id,kind,name,summary,category,attributes,origin,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,'archive',?,?) "
                    "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,name=excluded.name,"
                    "summary=excluded.summary,category=excluded.category,"
                    "attributes=excluded.attributes,active=1,updated_at=excluded.updated_at",
                    (
                        entity["id"],
                        entity["kind"],
                        entity["name"],
                        entity["summary"],
                        entity["category"],
                        _json(entity["attributes"]),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute("DELETE FROM entity_evidence WHERE entity_id=?", (entity["id"],))
                connection.executemany(
                    "INSERT INTO entity_evidence(entity_id,evidence_id) VALUES(?,?)",
                    [(entity["id"], source) for source in entity["evidence_ids"]],
                )
            for relation in relations.values():
                connection.execute(
                    "INSERT OR IGNORE INTO seed_records VALUES('relation',?)", (relation["id"],)
                )
                endpoints = connection.execute(
                    "SELECT id FROM entities WHERE id IN (?,?) AND active=1 AND deleted_at IS NULL",
                    (relation["source_id"], relation["target_id"]),
                ).fetchall()
                if len(endpoints) != 2:
                    continue
                existing = connection.execute(
                    "SELECT deleted_at,origin FROM relations WHERE id=?", (relation["id"],)
                ).fetchone()
                if existing and (existing["deleted_at"] or existing["origin"] == "manual"):
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO relations(id,source_id,target_id,predicate,origin,created_at,"
                    "updated_at) VALUES(?,?,?,?,'archive',?,?) ON CONFLICT(id) DO UPDATE SET "
                    "active=1,updated_at=excluded.updated_at",
                    (
                        relation["id"],
                        relation["source_id"],
                        relation["target_id"],
                        relation["predicate"],
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "DELETE FROM relation_evidence WHERE relation_id=?", (relation["id"],)
                )
                connection.executemany(
                    "INSERT INTO relation_evidence(relation_id,evidence_id) VALUES(?,?)",
                    [(relation["id"], source) for source in relation["evidence_ids"]],
                )
            self._metadata(connection, "updated_at", timestamp)
            self._metadata(connection, "gaps", reader["gaps"])
        result = self.snapshot()
        result["sync"] = {
            "entities": result["counts"]["entities"],
            "relations": result["counts"]["relations"],
            "evidence": len(result["evidence"]),
            "skipped_deleted_or_manual": skipped,
        }
        return result

    def extraction_result(self, run_key: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result FROM extraction_runs WHERE run_key=?", (run_key,)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def ingest_extraction(
        self,
        run_key: str,
        entities: list[dict],
        relations: list[dict],
        evidence: list[dict],
        result: dict,
        raw_outputs: list[dict],
        *,
        force: bool = False,
    ) -> dict:
        """Atomically publish previously validated observations, never reports.

        Canonical identity is (namespace, kind, exact normalized name). Each
        document extraction keeps its own summary/field/evidence observation.
        This internal entry point is called only after original quote validation.
        """
        timestamp = _now()
        with self._connection(write=True) as connection:
            existing_run = connection.execute(
                "SELECT result FROM extraction_runs WHERE run_key=?", (run_key,)
            ).fetchone()
            if existing_run and not force:
                return {**json.loads(existing_run["result"]), "model_calls": 0, "cached": True}
            connection.execute("DELETE FROM entity_observations WHERE document_key=?", (run_key,))
            connection.execute("DELETE FROM relation_observations WHERE document_key=?", (run_key,))
            for source in evidence:
                connection.execute(
                    "INSERT INTO evidence VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=excluded.updated_at",
                    (source["id"], _json(source), timestamp),
                )
            key_to_id = {}
            accepted_entities, accepted_relations = set(), set()
            skipped = 0
            for entity in entities:
                normalized = normalize_entity_name(entity["name"])
                namespace = normalize_entity_name(entity["namespace"])
                row = connection.execute(
                    "SELECT entity_id FROM entity_identities "
                    "WHERE namespace=? AND kind=? AND normalized_name=?",
                    (namespace, entity["kind"], normalized),
                ).fetchone()
                identity = (
                    row["entity_id"]
                    if row
                    else entity["kind"]
                    + ":auto:"
                    + hashlib.sha256(
                        _json([namespace, entity["kind"], normalized]).encode()
                    ).hexdigest()[:28]
                )
                existing = connection.execute(
                    "SELECT origin,deleted_at FROM entities WHERE id=?", (identity,)
                ).fetchone()
                if existing and (existing["deleted_at"] or existing["origin"] == "manual"):
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO entities(id,kind,name,summary,category,attributes,origin,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'archive',?,?) ON CONFLICT(id) DO UPDATE SET "
                    "active=1,updated_at=excluded.updated_at",
                    (
                        identity,
                        entity["kind"],
                        entity["name"],
                        entity["summary"],
                        entity["category"],
                        _json(entity["attributes"]),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO entity_identities VALUES(?,?,?,?)",
                    (namespace, entity["kind"], normalized, identity),
                )
                observation_id = hashlib.sha256(
                    _json([run_key, entity["key"]]).encode()
                ).hexdigest()
                connection.execute(
                    "INSERT INTO entity_observations VALUES(?,?,?,?,?,?)",
                    (
                        observation_id,
                        identity,
                        run_key,
                        _json(
                            {
                                key: entity[key]
                                for key in (
                                    "name",
                                    "namespace",
                                    "summary",
                                    "category",
                                    "attributes",
                                    "original_category",
                                    "evidence_status",
                                    "field_evidence",
                                )
                                if key in entity
                            }
                        ),
                        _json(entity["evidence_ids"]),
                        timestamp,
                    ),
                )
                key_to_id[entity["key"]] = identity
                accepted_entities.add(identity)
            for relation in relations:
                source_id = key_to_id.get(relation["source_key"])
                target_id = key_to_id.get(relation["target_key"])
                if not source_id or not target_id or source_id == target_id:
                    skipped += 1
                    continue
                identity = _relation_id(source_id, relation["predicate"], target_id)
                existing = connection.execute(
                    "SELECT origin,deleted_at FROM relations WHERE id=?", (identity,)
                ).fetchone()
                if existing and (existing["deleted_at"] or existing["origin"] == "manual"):
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO relations(id,source_id,target_id,predicate,origin,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,'archive',?,?) ON CONFLICT(id) DO UPDATE SET "
                    "active=1,updated_at=excluded.updated_at",
                    (identity, source_id, target_id, relation["predicate"], timestamp, timestamp),
                )
                observation_id = hashlib.sha256(
                    _json([run_key, identity, relation["evidence_ids"]]).encode()
                ).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO relation_observations VALUES(?,?,?,?,?)",
                    (observation_id, identity, run_key, _json(relation["evidence_ids"]), timestamp),
                )
                accepted_relations.add(identity)
            used_evidence = {
                key
                for entity in entities
                if entity["key"] in key_to_id
                for key in entity["evidence_ids"]
            }
            used_evidence.update(
                source_id
                for row in connection.execute(
                    "SELECT evidence_ids FROM relation_observations WHERE document_key=?",
                    (run_key,),
                )
                for source_id in json.loads(row["evidence_ids"])
            )
            published = {
                **result,
                "counts": {
                    "entities": len(accepted_entities),
                    "relations": len(accepted_relations),
                    "evidence": len(used_evidence),
                },
                "skipped_manual_or_deleted": skipped,
            }
            connection.execute(
                "INSERT INTO extraction_runs VALUES(?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET "
                "result=excluded.result,raw_outputs=excluded.raw_outputs,updated_at=excluded.updated_at",
                (run_key, _json(published), _json(raw_outputs), timestamp),
            )
            self._metadata(connection, "updated_at", timestamp)
        return published

    def _verify_evidence(self, payload: dict, library: LocalLibraryService, cache: dict) -> dict:
        source = copy.deepcopy(payload)
        reference = source.get("ref_id", "")
        source["available"] = False
        if reference:
            try:
                if reference not in cache:
                    try:
                        cache[reference] = library.resolve_citation(reference)
                    except Exception:
                        cache[reference] = None
                resolved = cache[reference]
                if resolved is None:
                    return source
                if source.get("library_id") and resolved["library_id"] != source["library_id"]:
                    return source
                if source.get("document_id") and resolved["document_id"] != source["document_id"]:
                    return source
                source["available"] = quote_matches(source.get("quote", ""), resolved["content"])
            except Exception:
                pass
        else:
            source["available"] = _http_url(source.get("url", ""))
        return source

    def update_field(self, identity: str, field: str, value: str | None) -> dict:
        """Keep a manual field overlay without discarding other source-backed facts."""
        field = field.strip()
        reserved = {
            "id",
            "name",
            "kind",
            "summary",
            "category",
            "origin",
            "status",
            "namespace",
            "source_ids",
            "evidence_ids",
            "document_observations",
            "field_evidence",
            "field_overrides",
        }
        if not field or len(field) > 160 or field in reserved:
            raise ValueError("不能修改该属性字段")
        if value is not None and (not isinstance(value, str) or len(value) > 12000):
            raise ValueError("字段内容过长或格式无效")
        timestamp = _now()
        with self._connection(write=True) as connection:
            entity = connection.execute(
                "SELECT id FROM entities WHERE id=? AND active=1 AND deleted_at IS NULL",
                (identity,),
            ).fetchone()
            if entity is None:
                raise KeyError("实体不存在")
            previous = connection.execute(
                "SELECT value FROM entity_field_overrides WHERE entity_id=? AND field=?",
                (identity, field),
            ).fetchone()
            encoded = _json(value) if value is not None else None
            connection.execute(
                "INSERT INTO entity_field_history(entity_id,field,value,previous_value,updated_at) "
                "VALUES(?,?,?,?,?)",
                (identity, field, encoded, previous[0] if previous else None, timestamp),
            )
            if value is None:
                connection.execute(
                    "DELETE FROM entity_field_overrides WHERE entity_id=? AND field=?",
                    (identity, field),
                )
            else:
                connection.execute(
                    "INSERT INTO entity_field_overrides VALUES(?,?,?,?) "
                    "ON CONFLICT(entity_id,field) DO UPDATE SET value=excluded.value,"
                    "updated_at=excluded.updated_at",
                    (identity, field, encoded, timestamp),
                )
            connection.execute("UPDATE entities SET updated_at=? WHERE id=?", (timestamp, identity))
        return {"entity_id": identity, "field": field, "value": value, "manual": value is not None}

    def snapshot(self, query: str = "", kind: str = "") -> dict:
        if kind and kind not in ENTITY_KINDS:
            raise ValueError("未知实体类型")
        with self._connection() as connection:
            nodes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM entities WHERE active=1 AND deleted_at IS NULL "
                    "ORDER BY kind,name,id"
                )
            ]
            edges = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM relations WHERE active=1 AND deleted_at IS NULL ORDER BY id"
                )
            ]
            source_rows = list(connection.execute("SELECT id,payload FROM evidence"))
            node_evidence: dict[str, list[str]] = {}
            edge_evidence: dict[str, list[str]] = {}
            for row in connection.execute("SELECT entity_id,evidence_id FROM entity_evidence"):
                node_evidence.setdefault(row["entity_id"], []).append(row["evidence_id"])
            for row in connection.execute("SELECT relation_id,evidence_id FROM relation_evidence"):
                edge_evidence.setdefault(row["relation_id"], []).append(row["evidence_id"])
            observations: dict[str, list[dict]] = {}
            for row in connection.execute(
                "SELECT * FROM entity_observations ORDER BY updated_at,id"
            ):
                observations.setdefault(row["entity_id"], []).append(dict(row))
            overrides: dict[str, dict] = {}
            for row in connection.execute("SELECT * FROM entity_field_overrides"):
                overrides.setdefault(row["entity_id"], {})[row["field"]] = {
                    "value": json.loads(row["value"]),
                    "updated_at": row["updated_at"],
                }
            relation_observations: dict[str, list[list[str]]] = {}
            for row in connection.execute(
                "SELECT relation_id,evidence_ids FROM relation_observations"
            ):
                relation_observations.setdefault(row["relation_id"], []).append(
                    json.loads(row["evidence_ids"])
                )
            metadata = {
                row["key"]: json.loads(row["value"])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
        library = LocalLibraryService(self.data_root)
        resolution_cache: dict = {}
        evidence = {
            row["id"]: self._verify_evidence(json.loads(row["payload"]), library, resolution_cache)
            for row in source_rows
        }

        # A node/edge with any stale evidence is withheld as a whole, rather
        # than keeping prose whose specific supporting source was removed.
        def backed(ids):
            return bool(ids) and all(evidence.get(key, {}).get("available") for key in ids)

        result_nodes = []
        hidden = 0
        for node in nodes:
            ids = sorted(node_evidence.get(node["id"], []))
            attributes = json.loads(node["attributes"])
            field_evidence: dict[str, list[str]] = {}
            if node["origin"] == "archive":
                available_observations = [
                    (json.loads(item["payload"]), json.loads(item["evidence_ids"]))
                    for item in observations.get(node["id"], [])
                    if backed(json.loads(item["evidence_ids"]))
                ]
                base_available = backed(ids)
                if not base_available and not available_observations:
                    hidden += 1
                    continue
                if not base_available:
                    primary, ids = available_observations[-1]
                    node.update({key: primary[key] for key in ("name", "summary", "category")})
                    attributes = copy.deepcopy(primary["attributes"])
                else:
                    ids = list(ids)
                # Observations retain their own fields and provenance. Never
                # concatenate summaries across documents and then let an unrelated
                # surviving citation appear to support removed claims.
                ids = sorted(set(ids) | {key for _, keys in available_observations for key in keys})
                if available_observations:
                    attributes["document_observations"] = [
                        {
                            "summary": item["summary"],
                            "attributes": item["attributes"],
                            "source_ids": keys,
                            "field_evidence": item.get("field_evidence", {}),
                        }
                        for item, keys in available_observations
                    ]
                    for item, _ in available_observations:
                        for field, keys in item.get("field_evidence", {}).items():
                            if item["attributes"].get(field) == attributes.get(field):
                                field_evidence[field] = sorted(
                                    set(field_evidence.get(field, [])) | set(keys)
                                )
            for field, override in overrides.get(node["id"], {}).items():
                attributes[field] = override["value"]
                field_evidence.pop(field, None)
            if (kind and node["kind"] != kind) or (
                query.strip()
                and query.strip().casefold()
                not in (node["name"] + " " + node["summary"]).casefold()
            ):
                continue
            result_nodes.append(
                {key: node[key] for key in ("id", "kind", "name", "summary", "category", "origin")}
                | {
                    "attributes": attributes,
                    "field_evidence": field_evidence,
                    "field_overrides": overrides.get(node["id"], {}),
                    "evidence_ids": ids,
                    "status": "source_backed" if node["origin"] == "archive" else "manual",
                }
            )
        node_ids = {node["id"] for node in result_nodes}
        result_edges = []
        for edge in edges:
            ids = sorted(edge_evidence.get(edge["id"], []))
            if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids:
                continue
            if edge["origin"] == "archive":
                available_groups = [
                    keys for keys in relation_observations.get(edge["id"], []) if backed(keys)
                ]
                ids = sorted(
                    set(ids if backed(ids) else [])
                    | {key for keys in available_groups for key in keys}
                )
                if not ids:
                    continue
            result_edges.append(
                {key: edge[key] for key in ("id", "source_id", "target_id", "predicate", "origin")}
                | {
                    "label": PREDICATES[edge["predicate"]],
                    "evidence_ids": ids,
                    "status": "source_backed" if edge["origin"] == "archive" else "manual",
                }
            )
        used = {
            source for record in [*result_nodes, *result_edges] for source in record["evidence_ids"]
        }
        gaps = list(metadata.get("gaps", []))
        if hidden:
            gaps.append(f"{hidden} 个归档实体的原文依据不可用，相关实体与关系已隐藏。")
        if not nodes:
            gaps.append("尚未导入实体。可同步已有归档知识，或新建人工条目。")
        return {
            "title": "Anduril 知识数据库",
            "updated_at": metadata.get("updated_at", ""),
            "counts": {
                "entities": len(result_nodes),
                "relations": len(result_edges),
                **{
                    plural: sum(node["kind"] == singular for node in result_nodes)
                    for singular, plural in (
                        ("product", "products"),
                        ("technology", "technologies"),
                        ("route", "routes"),
                        ("reference", "references"),
                    )
                },
            },
            "entities": result_nodes,
            "relations": result_edges,
            "evidence": [evidence[key] for key in sorted(used)],
            "gaps": gaps,
        }

    @staticmethod
    def _validate_entity(values: dict, *, creating: bool):
        if creating and values.get("kind") not in ENTITY_KINDS:
            raise ValueError("未知实体类型")
        if "name" in values and (not isinstance(values["name"], str) or not values["name"].strip()):
            raise ValueError("实体名称不能为空")
        attributes = values.get("attributes", {})
        if not isinstance(attributes, dict) or len(_json(attributes)) > 100_000:
            raise ValueError("实体属性格式无效或过长")
        # Evidence is managed by the archive importer, not accepted from forms.
        if any(key in values for key in ("evidence_ids", "origin", "status")):
            raise ValueError("来源依据不能由人工条目伪造")

    def create_entity(self, values: dict) -> dict:
        self._validate_entity(values, creating=True)
        identity = values["kind"] + ":manual:" + uuid.uuid4().hex
        timestamp = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO entities(id,kind,name,summary,category,attributes,origin,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,'manual',?,?)",
                (
                    identity,
                    values["kind"],
                    values["name"].strip(),
                    values.get("summary", ""),
                    values.get("category", ""),
                    _json(values.get("attributes", {})),
                    timestamp,
                    timestamp,
                ),
            )
            self._metadata(connection, "updated_at", timestamp)
        return next(entity for entity in self.snapshot()["entities"] if entity["id"] == identity)

    def update_entity(self, identity: str, values: dict) -> dict:
        self._validate_entity(values, creating=False)
        timestamp = _now()
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM entities WHERE id=? AND active=1 AND deleted_at IS NULL", (identity,)
            ).fetchone()
            if not existing:
                raise KeyError("实体不存在")
            record = dict(existing)
            for key in ("name", "summary", "category", "attributes"):
                if key in values:
                    record[key] = values[key].strip() if key == "name" else values[key]
            attributes = record["attributes"]
            if not isinstance(attributes, str):
                attributes = _json(attributes)
            connection.execute(
                "UPDATE entities SET name=?,summary=?,category=?,attributes=?,origin='manual',"
                "updated_at=? WHERE id=?",
                (
                    record["name"],
                    record["summary"],
                    record["category"],
                    attributes,
                    timestamp,
                    identity,
                ),
            )
            connection.execute("DELETE FROM entity_evidence WHERE entity_id=?", (identity,))
            # Editing imported prose is a user assertion. Existing archive edges
            # cannot retain their verification badge through that assertion.
            connection.execute(
                "UPDATE relations SET origin='manual',updated_at=? "
                "WHERE source_id=? OR target_id=?",
                (timestamp, identity, identity),
            )
            connection.execute(
                "DELETE FROM relation_evidence WHERE relation_id IN "
                "(SELECT id FROM relations WHERE source_id=? OR target_id=?)",
                (identity, identity),
            )
            self._metadata(connection, "updated_at", timestamp)
        return next(entity for entity in self.snapshot()["entities"] if entity["id"] == identity)

    def delete_entity(self, identity: str):
        timestamp = _now()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE entities SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (timestamp, timestamp, identity),
            )
            if not cursor.rowcount:
                raise KeyError("实体不存在")
            connection.execute(
                "UPDATE relations SET deleted_at=?,updated_at=? WHERE source_id=? OR target_id=?",
                (timestamp, timestamp, identity, identity),
            )
            self._metadata(connection, "updated_at", timestamp)

    def create_relation(self, values: dict) -> dict:
        source_id, target_id = values["source_id"], values["target_id"]
        predicate = values["predicate"]
        if predicate not in PREDICATES:
            raise ValueError("未知关系类型")
        if source_id == target_id:
            raise ValueError("关系两端必须为不同实体")
        visible = {item["id"]: item for item in self.snapshot()["entities"]}
        if source_id not in visible or target_id not in visible:
            raise KeyError("关系端点不存在或原文不可用")
        # Validate typed links so graph direction retains meaning.
        source_kind, target_kind = visible[source_id]["kind"], visible[target_id]["kind"]
        if predicate == "uses_technology" and (
            source_kind != "product" or target_kind != "technology"
        ):
            raise ValueError("涉及技术关系须从产品指向技术")
        if predicate == "has_route" and (
            source_kind not in {"product", "technology"} or target_kind != "route"
        ):
            raise ValueError("关联路线关系须从产品或技术指向技术路线")
        if predicate in {
            "variant_of",
            "part_of",
            "replaces",
            "carries",
            "deploys",
            "integrates",
            "evolves_from",
        } and (source_kind != "product" or target_kind != "product"):
            raise ValueError("产品谱系关系须连接两个产品")
        if predicate == "reference_for" and (
            source_kind != "reference" or target_kind not in {"technology", "route"}
        ):
            raise ValueError("独立参考关系须从开源参考指向技术或路线")
        identity = _relation_id(source_id, predicate, target_id)
        timestamp = _now()
        with self._connection(write=True) as connection:
            # Recheck endpoints under the same write transaction as INSERT.
            count = connection.execute(
                "SELECT count(*) FROM entities WHERE id IN (?,?) "
                "AND active=1 AND deleted_at IS NULL",
                (source_id, target_id),
            ).fetchone()[0]
            if count != 2:
                raise KeyError("关系端点不存在")
            existing = connection.execute(
                "SELECT id FROM relations WHERE id=?", (identity,)
            ).fetchone()
            if existing:
                raise ValueError("该关系已存在或已删除；请勿重复建立")
            connection.execute(
                "INSERT INTO relations(id,source_id,target_id,predicate,origin,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,'manual',?,?)",
                (identity, source_id, target_id, predicate, timestamp, timestamp),
            )
            self._metadata(connection, "updated_at", timestamp)
        return next(edge for edge in self.snapshot()["relations"] if edge["id"] == identity)

    def delete_relation(self, identity: str):
        timestamp = _now()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE relations SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (timestamp, timestamp, identity),
            )
            if not cursor.rowcount:
                raise KeyError("关系不存在")
            self._metadata(connection, "updated_at", timestamp)
