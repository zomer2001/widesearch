"""Durable topic membership and research ledger, without model or network calls.

Facts and immutable originals remain in their existing stores. A topic owns
references, not copies, so removing membership never deletes shared knowledge.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from api.knowledge_database import DEFAULT_DATABASE, KnowledgeDatabase, normalize_entity_name
from api.research_materials import research_materials
from api.topic_schema import DEFAULT_TOPICS, TAXONOMIES, build_topic_projection, profile_for
from searchos.local.index.source_registry import SourceRegistry
from searchos.local.library import LocalLibraryService
from searchos.local.reader.references import build_ref_id, parse_ref_id, resolve_reference

DEFAULT_TOPICS_DATABASE = DEFAULT_DATABASE.with_name("topics.sqlite")
ACTIVE_STATUSES = ("queued", "planning", "researching", "ingesting", "stopping", "running")
ACTIVE_SQL = "(" + ",".join("'" + value + "'" for value in ACTIVE_STATUSES) + ")"

TEMPLATES = {
    "product": [
        {"key": "manufacturer", "label": "厂商", "type": "text", "aliases": ["厂商", "制造商"]},
        {"key": "family", "label": "产品系列", "type": "text", "aliases": ["产品系列", "产品家族"]},
        {"key": "parameters", "label": "技术参数", "type": "text", "aliases": ["技术参数", "参数"]},
        {
            "key": "maturity",
            "label": "成熟度依据",
            "type": "text",
            "aliases": ["成熟度", "成熟度依据"],
        },
    ],
    "technology": [
        {
            "key": "mechanism",
            "label": "技术原理",
            "type": "text",
            "aliases": ["技术原理", "原理", "实现机制"],
        },
        {"key": "inputs", "label": "输入", "type": "text", "aliases": ["输入"]},
        {"key": "outputs", "label": "输出", "type": "text", "aliases": ["输出"]},
        {"key": "conditions", "label": "适用条件", "type": "text", "aliases": ["适用条件", "约束"]},
    ],
    "route": [
        {"key": "mechanism", "label": "实现机制", "type": "text", "aliases": ["实现机制", "机制"]},
        {"key": "steps", "label": "关键步骤", "type": "text", "aliases": ["关键步骤", "实现步骤"]},
        {
            "key": "tradeoffs",
            "label": "取舍与约束",
            "type": "text",
            "aliases": ["取舍与约束", "约束"],
        },
        {
            "key": "maturity",
            "label": "成熟度依据",
            "type": "text",
            "aliases": ["成熟度", "成熟度依据"],
        },
    ],
    "reference": [
        {"key": "repo_url", "label": "项目地址", "type": "text", "aliases": ["仓库地址"]},
        {"key": "license", "label": "许可证", "type": "text", "aliases": ["许可证"]},
        {"key": "commit", "label": "固定版本", "type": "text", "aliases": ["固定版本", "版本"]},
        {
            "key": "execution_status",
            "label": "复现状态",
            "type": "text",
            "aliases": ["复现状态", "执行状态"],
        },
    ],
}


def _now():
    return datetime.now(UTC).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(prefix):
    return prefix + "_" + uuid.uuid4().hex


def _nonempty(value):
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "未知",
            "未记录",
            "待采集",
            "未公开",
            "unknown",
            "n/a",
        }
    if isinstance(value, dict):
        return any(_nonempty(item) for item in value.values())
    if isinstance(value, list):
        return any(_nonempty(item) for item in value)
    return True


def _field_value(attributes, field):
    for key in [field["key"], *field.get("aliases", [])]:
        if _nonempty(attributes.get(key)):
            return attributes[key]
    return None


class TopicStore:
    def __init__(self, data_root, database=None, knowledge_database=None):
        self.data_root = Path(data_root)
        self.knowledge_database = Path(knowledge_database or DEFAULT_DATABASE)
        self.database = Path(database or self.knowledge_database.with_name("topics.sqlite"))

    @contextmanager
    def _connection(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, normalized_title TEXT NOT NULL UNIQUE,
                namespace TEXT NOT NULL, description TEXT NOT NULL, aliases TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_entities (
                topic_id TEXT NOT NULL REFERENCES topics(id), entity_id TEXT NOT NULL,
                excluded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(topic_id,entity_id)
            );
            CREATE TABLE IF NOT EXISTS topic_documents (
                topic_id TEXT NOT NULL REFERENCES topics(id), library_id TEXT NOT NULL,
                document_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(topic_id,library_id,document_id)
            );
            CREATE TABLE IF NOT EXISTS topic_sessions (
                topic_id TEXT NOT NULL REFERENCES topics(id), session_id TEXT NOT NULL,
                PRIMARY KEY(topic_id,session_id)
            );
            CREATE TABLE IF NOT EXISTS topic_runs (
                id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
                payload TEXT NOT NULL, baseline TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_tasks (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES topic_runs(id),
                topic_id TEXT NOT NULL REFERENCES topics(id), payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_changes (
                id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id),
                run_id TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS topic_classifications (
                topic_id TEXT NOT NULL REFERENCES topics(id), entity_id TEXT NOT NULL,
                cluster_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(topic_id,entity_id)
            );
        """)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _topic(row):
        value = dict(row)
        value.pop("normalized_title", None)
        value["aliases"] = json.loads(value["aliases"])
        return value

    def create_topic(self, title, description="", aliases=None):
        title = title.strip()
        if not title or len(title) > 300:
            raise ValueError("专题名称应为 1 至 300 个字符")
        normalized = normalize_entity_name(title)
        if normalized in {"anduril", "anduril industries", "安杜里尔"}:
            title, normalized = "Anduril", "anduril"
            aliases = list(dict.fromkeys([*(aliases or []), "Anduril Industries", "安杜里尔"]))
        elif normalized in {"palantir", "palantir technologies", "帕兰提尔"}:
            title, normalized = "Palantir", "palantir"
            aliases = list(dict.fromkeys([*(aliases or []), "Palantir Technologies", "帕兰提尔"]))
        elif normalized in {"huawei", "huawei technologies", "华为"}:
            title, normalized = "华为", "华为"
            aliases = list(dict.fromkeys([*(aliases or []), "Huawei", "Huawei Technologies"]))
        namespace = {"anduril": "anduril", "palantir": "palantir", "华为": "huawei"}.get(
            normalized, "topic:" + hashlib.sha256(normalized.encode()).hexdigest()[:24]
        )
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM topics WHERE normalized_title=?", (normalized,)
            ).fetchone()
            if existing:
                return self._topic(existing)
            now, identity = _now(), _identity("topic")
            connection.execute(
                "INSERT OR IGNORE INTO topics VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    title,
                    normalized,
                    namespace,
                    description,
                    _json(aliases or []),
                    now,
                    now,
                ),
            )
            return self._topic(
                connection.execute(
                    "SELECT * FROM topics WHERE normalized_title=?", (normalized,)
                ).fetchone()
            )

    def get_topic(self, topic_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if row is None:
            raise KeyError("专题不存在")
        return self._topic(row)

    def list_topics(self):
        with self._connection() as connection:
            result = [
                self._topic(row)
                for row in connection.execute("SELECT * FROM topics ORDER BY created_at,id")
            ]
        for topic in result:
            snapshot = self.snapshot(topic["id"])
            topic["profile"] = snapshot["profile"]
            topic["clusters"] = snapshot["clusters"]
            topic["quality"] = snapshot["quality"]
            topic["coverage"] = snapshot["coverage"]
            topic["recent_run"] = next(iter(snapshot["runs"]), None)
            topic["counts"] = {
                "entities": len(snapshot["knowledge"]["entities"]),
                "sources": len(snapshot["sources"]),
                "runs": len(snapshot["runs"]),
            }
        return result

    def resolve_topic(self, query, topic_id=None):
        if topic_id:
            return self.get_topic(topic_id)
        normalized = normalize_entity_name(query)
        if not normalized or len(query) > 12000:
            raise ValueError("请输入主题词或任务要求")
        candidates = []
        for topic in self.list_topics():
            names = [topic["title"], *topic["aliases"]]
            if any(self._matches_name(normalized, name) for name in names):
                candidates.append(topic)
        if len(candidates) > 1:
            raise ValueError("任务涉及多个已有专题，请先选择要维护的专题")
        if candidates:
            return self.get_topic(candidates[0]["id"])
        if any(self._matches_name(normalized, name) for name in ["Anduril", "安杜里尔"]):
            return self.create_topic("Anduril", "产品谱系、技术机制、技术路线与独立开源参考")
        # Do not silently chop a complex intent into a potentially wrong company.
        if len(query.strip()) > 300:
            raise ValueError("请先创建专题，再输入详细任务要求")
        return self.create_topic(query.strip())

    @staticmethod
    def _matches_name(query, name):
        name = normalize_entity_name(name)
        if re.fullmatch(r"[\w .-]+", name, re.ASCII):
            return bool(re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", query))
        return name in query

    def ensure_default_topics(self):
        """Create actual empty topic records; never fabricate collected knowledge."""
        for title, description, aliases in DEFAULT_TOPICS:
            self.create_topic(title, description, aliases)

    def ensure_legacy_topic(self):
        """Seed topic entrances and associate legacy Anduril once, with no extraction."""
        self.ensure_default_topics()
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM topic_metadata WHERE key='legacy_anduril_v1'"
            ).fetchone():
                return
        if not self.knowledge_database.exists():
            return
        knowledge = KnowledgeDatabase(self.data_root, self.knowledge_database)
        snapshot = knowledge.snapshot()
        with knowledge._connection() as connection:
            identities = {
                row[0]
                for row in connection.execute(
                    "SELECT entity_id FROM entity_identities WHERE namespace='anduril' "
                    "UNION SELECT identity FROM seed_records WHERE record_kind='entity'"
                )
            }
        entities = [item for item in snapshot["entities"] if item["id"] in identities]
        if not entities:
            return
        topic = self.create_topic("Anduril", "产品谱系、技术机制、技术路线与独立开源参考")
        used = {key for item in entities for key in item["evidence_ids"]}
        known_documents = {
            item.get("document_id")
            for item in snapshot["evidence"]
            if item["id"] in used and item.get("document_id")
        }
        materials = research_materials(self.data_root)
        session_ids = {
            session
            for item in materials["documents"]
            if item["document_id"] in known_documents
            for session in item.get("session_ids", [])
        }
        with self._connection() as connection:
            for entity in entities:
                connection.execute(
                    "INSERT OR IGNORE INTO topic_entities VALUES(?,?,0)",
                    (topic["id"], entity["id"]),
                )
            for evidence in snapshot["evidence"]:
                if evidence["id"] in used:
                    self._attach_evidence(connection, topic["id"], evidence)
            for session_id in session_ids:
                connection.execute(
                    "INSERT OR IGNORE INTO topic_sessions VALUES(?,?)", (topic["id"], session_id)
                )
            for document in materials["documents"]:
                if session_ids.intersection(document.get("session_ids", [])):
                    payload = self._source_payload(materials["library_id"], document)
                    connection.execute(
                        "INSERT OR IGNORE INTO topic_documents VALUES(?,?,?,?)",
                        (
                            topic["id"],
                            materials["library_id"],
                            document["document_id"],
                            _json(payload),
                        ),
                    )
            connection.execute(
                "INSERT OR IGNORE INTO topic_metadata VALUES(?,?)", ("legacy_anduril_v1", _now())
            )
        self._link_document_entities(topic["id"])

    @staticmethod
    def _attach_evidence(connection, topic_id, evidence):
        library_id, document_id = evidence.get("library_id"), evidence.get("document_id")
        if not library_id or not document_id:
            match = re.match(r"local://([^/]+)/([^/?]+)", evidence.get("ref_id", ""))
            if not match:
                return
            library_id, document_id = match.groups()
        payload = {
            "id": document_id,
            "document_id": document_id,
            "library_id": library_id,
            "title": evidence.get("title", document_id),
            "url": evidence.get("url", ""),
            "ref_id": evidence.get("ref_id", ""),
            "source_revision": evidence.get("source_revision", ""),
            "session_ids": [],
        }
        connection.execute(
            "INSERT OR IGNORE INTO topic_documents VALUES(?,?,?,?)",
            (topic_id, library_id, document_id, _json(payload)),
        )

    def attach_session(self, topic_id, session_id):
        self.get_topic(topic_id)
        materials = research_materials(self.data_root, session_id)
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO topic_sessions VALUES(?,?)", (topic_id, session_id)
            )
            for document in materials["documents"]:
                payload = self._source_payload(materials["library_id"], document)
                connection.execute(
                    "INSERT INTO topic_documents VALUES(?,?,?,?) ON CONFLICT "
                    "(topic_id,library_id,document_id) DO UPDATE SET payload=excluded.payload",
                    (topic_id, materials["library_id"], document["document_id"], _json(payload)),
                )
            connection.execute("UPDATE topics SET updated_at=? WHERE id=?", (_now(), topic_id))
        self._link_document_entities(topic_id)
        return {"session_id": session_id, "documents": len(materials["documents"])}

    def register_session(self, topic_id, session_id):
        """Register ownership before research starts producing originals."""
        self.get_topic(topic_id)
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO topic_sessions VALUES(?,?)", (topic_id, session_id)
            )

    @staticmethod
    def _source_payload(library_id, document):
        metadata = document.get("metadata") or {}
        origin = document.get("origin") or {}
        ref_id = document.get("ref_id", "")
        return {
            "id": document["document_id"],
            "document_id": document["document_id"],
            "library_id": library_id,
            "title": document.get("title")
            or origin.get("title")
            or document.get("name")
            or document.get("original_filename")
            or document["document_id"],
            "url": document.get("source_url")
            or origin.get("final_url")
            or metadata.get("source_url", ""),
            "ref_id": ref_id,
            "source_revision": document.get("source_revision", ""),
            "saved_form": origin.get("saved_form", ""),
            "fetched_at": origin.get("fetched_at", ""),
            "session_ids": document.get("session_ids", []),
        }

    def _link_document_entities(self, topic_id):
        knowledge = KnowledgeDatabase(self.data_root, self.knowledge_database).snapshot()
        with self._connection() as connection:
            documents = {
                (row["library_id"], row["document_id"])
                for row in connection.execute(
                    "SELECT library_id,document_id FROM topic_documents WHERE topic_id=?",
                    (topic_id,),
                )
            }
            source_ids = {
                item["id"]
                for item in knowledge["evidence"]
                if (item.get("library_id"), item.get("document_id")) in documents
            }
            for entity in knowledge["entities"]:
                if source_ids.intersection(entity["evidence_ids"]):
                    connection.execute(
                        "INSERT OR IGNORE INTO topic_entities VALUES(?,?,0)",
                        (topic_id, entity["id"]),
                    )

    def remove_entity(self, topic_id, entity_id):
        self.get_topic(topic_id)
        if entity_id not in {
            item["id"] for item in self.snapshot(topic_id)["knowledge"]["entities"]
        }:
            raise KeyError("该专题没有这个条目")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO topic_entities VALUES(?,?,1) ON CONFLICT "
                "(topic_id,entity_id) DO UPDATE SET excluded=1",
                (topic_id, entity_id),
            )

    def update_classification(self, topic_id, entity_id, cluster_ids):
        snapshot = self.snapshot(topic_id)
        if entity_id not in {entity["id"] for entity in snapshot["knowledge"]["entities"]}:
            raise KeyError("该专题没有这个可见条目")
        if cluster_ids is not None:
            if not isinstance(cluster_ids, list) or not all(
                isinstance(key, str) for key in cluster_ids
            ):
                raise ValueError("分类应为分类标识列表，或 null 以恢复自动分类")
            parents = {
                key: parent
                for key, _, parent, _ in TAXONOMIES[profile_for(snapshot["topic"])["id"]]
            }
            if any(key not in {*parents, "unclassified"} for key in cluster_ids):
                raise ValueError("包含不属于本专题的分类")
            ids = set(cluster_ids) - {"unclassified"}
            for key in list(ids):
                parent = parents.get(key)
                while parent:
                    ids.add(parent)
                    parent = parents.get(parent)
            cluster_ids = sorted(ids)
        with self._connection() as connection:
            if cluster_ids is None:
                connection.execute(
                    "DELETE FROM topic_classifications WHERE topic_id=? AND entity_id=?",
                    (topic_id, entity_id),
                )
            else:
                connection.execute(
                    "INSERT INTO topic_classifications VALUES(?,?,?,?) "
                    "ON CONFLICT(topic_id,entity_id) DO UPDATE SET "
                    "cluster_ids=excluded.cluster_ids,updated_at=excluded.updated_at",
                    (topic_id, entity_id, _json(cluster_ids), _now()),
                )
        return self.snapshot(topic_id)

    def snapshot(self, topic_id):
        topic = self.get_topic(topic_id)
        global_knowledge = KnowledgeDatabase(self.data_root, self.knowledge_database).snapshot()
        with self._connection() as connection:
            classification_overrides = {
                row["entity_id"]: json.loads(row["cluster_ids"])
                for row in connection.execute(
                    "SELECT entity_id,cluster_ids FROM topic_classifications WHERE topic_id=?",
                    (topic_id,),
                )
            }
            entity_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT entity_id FROM topic_entities WHERE topic_id=? AND excluded=0",
                    (topic_id,),
                )
            }
            excluded_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT entity_id FROM topic_entities WHERE topic_id=? AND excluded=1",
                    (topic_id,),
                )
            }
            source_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM topic_documents WHERE topic_id=? ORDER BY document_id",
                    (topic_id,),
                )
            ]
            session_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT session_id FROM topic_sessions WHERE topic_id=?", (topic_id,)
                )
            }
            runs = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM topic_runs WHERE topic_id=? ORDER BY rowid DESC",
                    (topic_id,),
                )
            ]
            tasks = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM topic_tasks WHERE topic_id=? ORDER BY rowid", (topic_id,)
                )
            ]
            changes = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM topic_changes WHERE topic_id=? "
                    "ORDER BY rowid DESC LIMIT 100",
                    (topic_id,),
                )
            ]
        # Archival can finish in a cancelled research worker's finally block.
        # Read its durable association without resurrecting deleted originals.
        if session_ids:
            materials = research_materials(self.data_root)
            known = {(row["library_id"], row["document_id"]) for row in source_rows}
            for document in materials["documents"]:
                key = (materials["library_id"], document["document_id"])
                if key not in known and session_ids.intersection(document.get("session_ids", [])):
                    source_rows.append(
                        {
                            "library_id": key[0],
                            "document_id": key[1],
                            "payload": _json(self._source_payload(key[0], document)),
                        }
                    )
                    known.add(key)
        document_keys = {(row["library_id"], row["document_id"]) for row in source_rows}
        document_evidence = {
            item["id"]
            for item in global_knowledge["evidence"]
            if (item.get("library_id"), item.get("document_id")) in document_keys
        }
        # A queued extraction may finish after its research run was stopped.
        # Project only explicitly attached originals, without new scheduling.
        entity_ids.update(
            item["id"]
            for item in global_knowledge["entities"]
            if document_evidence.intersection(item["evidence_ids"])
        )
        entity_ids.difference_update(excluded_ids)
        entities = [item for item in global_knowledge["entities"] if item["id"] in entity_ids]
        live_ids = {item["id"] for item in entities}
        relations = [
            item
            for item in global_knowledge["relations"]
            if item["source_id"] in live_ids and item["target_id"] in live_ids
        ]
        from api.topic_resolution import apply_topic_merges, duplicate_candidates

        entities, relations, merges = apply_topic_merges(self, topic_id, entities, relations)
        live_ids = {item["id"] for item in entities}
        used_evidence = {key for item in [*entities, *relations] for key in item["evidence_ids"]}
        evidence = [item for item in global_knowledge["evidence"] if item["id"] in used_evidence]
        sources = self._live_sources(source_rows, evidence)
        classification_overrides = {
            identity: ids
            for identity, ids in classification_overrides.items()
            if identity in live_ids
        }
        projection = build_topic_projection(
            topic, entities, relations, evidence, classification_overrides
        )
        knowledge = {
            **global_knowledge,
            "title": topic["title"] + " 知识库",
            "entities": entities,
            "relations": relations,
            "evidence": evidence,
            "gaps": [],
            "counts": {
                "entities": len(entities),
                "relations": len(relations),
                **{
                    plural: sum(item["kind"] == kind for item in entities)
                    for kind, plural in [
                        ("product", "products"),
                        ("technology", "technologies"),
                        ("route", "routes"),
                        ("reference", "references"),
                    ]
                },
            },
        }
        # Optional additive fields (e.g. field evidence) are scoped by entity id.
        if isinstance(knowledge.get("field_evidence"), list):
            knowledge["field_evidence"] = [
                item for item in knowledge["field_evidence"] if item.get("entity_id") in live_ids
            ]
        return {
            "topic": topic,
            "knowledge": knowledge,
            "sources": sources,
            "runs": runs,
            "tasks": tasks,
            "changes": changes,
            "templates": copy.deepcopy(TEMPLATES),
            **projection,
            "duplicate_candidates": duplicate_candidates(entities, relations),
            "merges": merges,
        }

    def _live_sources(self, rows, evidence):
        service = LocalLibraryService(self.data_root)
        libraries, registries, results = {}, {}, []
        for row in rows:
            library_id = row["library_id"]
            if library_id not in libraries:
                try:
                    registries[library_id] = SourceRegistry(
                        Path(service.get_library(library_id).index_root)
                    )
                    libraries[library_id] = {
                        item["document_id"]: item for item in service.list_documents(library_id)
                    }
                except KeyError:
                    libraries[library_id] = {}
            document = libraries[library_id].get(row["document_id"])
            if document is None:
                continue
            payload = json.loads(row["payload"])
            payload["available"] = True
            current = self._source_payload(library_id, document)
            for key in ("title", "url", "saved_form", "fetched_at", "ref_id"):
                if current.get(key):
                    payload[key] = current[key]
            match = next(
                (
                    item
                    for item in evidence
                    if item.get("document_id") == row["document_id"]
                    and item.get("library_id") == library_id
                ),
                None,
            )
            if match:
                for key in ("ref_id", "title", "url", "source_revision"):
                    if match.get(key):
                        payload[key] = match[key]
            if document.get("searchable"):
                try:
                    record = registries[library_id].read(document["document_id"])
                    short = build_ref_id(
                        library_id=library_id,
                        document_id=record.document_id,
                        source_revision=record.source_revision,
                        reference_key="all",
                    )
                    resolved = resolve_reference(
                        parse_ref_id(short),
                        record.references,
                        library_id=library_id,
                        source_revision=record.source_revision,
                    )
                    payload["ref_id"] = build_ref_id(
                        library_id=library_id,
                        document_id=record.document_id,
                        source_revision=record.source_revision,
                        locators=list(resolved.locators),
                    )
                except (KeyError, ValueError):
                    # A valid quoted excerpt remains useful when the full index
                    # exceeds the preview locator limit.
                    pass
            payload["source_revision"] = document.get(
                "source_revision", payload.get("source_revision", "")
            )
            results.append(payload)
        # Repository excerpts have their own immutable revision and may live
        # outside LocalLibraryService. They remain visibly distinct from pages.
        existing_external = set()
        for item in evidence:
            if item.get("document_id") or not item.get("available") or not item.get("url"):
                continue
            key = (item["url"], item.get("commit", ""))
            if key in existing_external:
                continue
            existing_external.add(key)
            results.append(
                {
                    "id": item["id"],
                    "document_id": "",
                    "library_id": "",
                    "title": item.get("title", item["url"]),
                    "url": item["url"],
                    "ref_id": item.get("ref_id", ""),
                    "source_revision": item.get("commit", ""),
                    "available": True,
                    "session_ids": [],
                    "saved_form": "repository_excerpt",
                }
            )
        return results

    @staticmethod
    def _fingerprint(snapshot):
        return {
            "entities": {
                item["id"]: hashlib.sha256(_json(item).encode()).hexdigest()
                for item in snapshot["knowledge"]["entities"]
            },
            "relations": sorted(item["id"] for item in snapshot["knowledge"]["relations"]),
            "sources": sorted(item["id"] for item in snapshot["sources"]),
        }

    def create_run(self, topic_id, query, engine, model, max_tasks=4, max_seconds=1200, **extra):
        baseline = self._fingerprint(self.snapshot(topic_id))
        now = _now()
        payload = {
            **extra,
            "id": _identity("run"),
            "topic_id": topic_id,
            "query": query,
            "engine": engine,
            "model": model,
            "max_tasks": max_tasks,
            "max_seconds": max_seconds,
            "status": "queued",
            "message": "等待规划",
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM topic_runs WHERE topic_id=? AND "
                "json_extract(payload,'$.status') IN " + ACTIVE_SQL,
                (topic_id,),
            ).fetchone():
                raise ValueError("这个专题已有任务正在执行")
            connection.execute(
                "INSERT INTO topic_runs VALUES(?,?,?,?)",
                (payload["id"], topic_id, _json(payload), _json(baseline)),
            )
        return payload

    def get_run(self, run_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM topic_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("研究任务不存在")
        return json.loads(row[0])

    def update_run(self, run_id, **patch):
        return self._update_payload("topic_runs", run_id, patch)

    def _update_payload(self, table, identity, patch):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError("研究任务不存在")
            payload = json.loads(row[0])
            protected = {"id", "topic_id", "run_id", "created_at"}
            payload.update({key: value for key, value in patch.items() if key not in protected})
            payload["updated_at"] = _now()
            connection.execute(
                f"UPDATE {table} SET payload=? WHERE id=?", (_json(payload), identity)
            )
            connection.execute(
                "UPDATE topics SET updated_at=? WHERE id=?",
                (payload["updated_at"], payload["topic_id"]),
            )
        return payload

    def list_pending_runs(self):
        with self._connection() as connection:
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM topic_runs WHERE "
                    "json_extract(payload,'$.status') IN " + ACTIVE_SQL + " ORDER BY rowid"
                )
            ]

    def add_task(self, run_id, **values):
        run = self.get_run(run_id)
        now = _now()
        task = {
            "status": "queued",
            "title": "补充资料",
            "query": "",
            "session_id": "",
            **values,
            "id": _identity("task"),
            "run_id": run_id,
            "topic_id": run["topic_id"],
            "created_at": now,
            "updated_at": now,
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO topic_tasks VALUES(?,?,?,?)",
                (task["id"], run_id, run["topic_id"], _json(task)),
            )
        return task

    def update_task(self, task_id, **patch):
        return self._update_payload("topic_tasks", task_id, patch)

    def sync_topic_changes(self, topic_id, run_id=None):
        self._link_document_entities(topic_id)
        current = self._fingerprint(self.snapshot(topic_id))
        with self._connection() as connection:
            baseline = {"entities": {}, "relations": [], "sources": []}
            if run_id:
                row = connection.execute(
                    "SELECT baseline,topic_id FROM topic_runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None or row["topic_id"] != topic_id:
                    raise KeyError("研究任务不属于该专题")
                baseline = json.loads(row["baseline"])
            before, after = baseline["entities"], current["entities"]
            change = {
                "id": _identity("change"),
                "topic_id": topic_id,
                "run_id": run_id or "",
                "created_at": _now(),
                "entities_added": len(after.keys() - before.keys()),
                "entities_updated": sum(
                    before[key] != after[key] for key in before.keys() & after.keys()
                ),
                "entities_removed": len(before.keys() - after.keys()),
                "relations_added": len(set(current["relations"]) - set(baseline["relations"])),
                "relations_removed": len(set(baseline["relations"]) - set(current["relations"])),
                "sources_added": len(set(current["sources"]) - set(baseline["sources"])),
            }
            change["summary"] = (
                f"新增 {change['entities_added']} 个条目，"
                f"更新 {change['entities_updated']} 个条目，"
                f"新增 {change['relations_added']} 条关系、{change['sources_added']} 份资料"
            )
            has_change = any(
                change[key]
                for key in [
                    "entities_added",
                    "entities_updated",
                    "entities_removed",
                    "relations_added",
                    "relations_removed",
                    "sources_added",
                ]
            )
            if has_change:
                connection.execute(
                    "INSERT INTO topic_changes VALUES(?,?,?,?)",
                    (change["id"], topic_id, run_id or "", _json(change)),
                )
            if run_id:
                connection.execute(
                    "UPDATE topic_runs SET baseline=? WHERE id=?", (_json(current), run_id)
                )
            connection.execute("UPDATE topics SET updated_at=? WHERE id=?", (_now(), topic_id))
        change["changed"] = has_change
        return change
