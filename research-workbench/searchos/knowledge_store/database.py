"""SQLite foundation for the seven research dimensions. Standard library only."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from .catalog import (
    DIMENSIONS,
    ELEMENTS,
    ENTITY_KINDS,
    EXTRA_FIELDS,
    RELATIONS,
    ROLES,
    map_legacy_field,
)

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / "data/knowledge/research.sqlite"
DEFAULT_CATALOG = ROOT / "design/knowledge-extraction-handoff-20261104/schema/field_catalog.v1.json"
DEFAULT_EXPORT = ROOT / "design/research-database-v1"
DETAIL_TABLES = {
    "field": "field_values",
    "relation": "entity_relations",
    "metric": "metric_observations",
    "capability": "product_capabilities",
    "event": "event_details",
    "participation": "participations",
    "assessment": "assessments",
    "maturity": "maturity_assessments",
    "composition": "composition_items",
    "interface": "interface_connections",
    "route_step": "route_steps",
    "classification": "classifications",
    "identity": "identity_assertions",
}

_BATCH = ContextVar("research_database_batch", default=None)
ENTITY_NAME_SCOPES_SQL = """CREATE TABLE IF NOT EXISTS entity_name_scopes (
    name_id TEXT NOT NULL REFERENCES entity_names(id),
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    PRIMARY KEY(name_id, source_document_id)
)"""


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def generated_constraints():
    """Persist these guards in SQLite so they also apply to direct SQL clients."""
    statements = []
    for kind, table in DETAIL_TABLES.items():
        statements += [
            f"""CREATE TRIGGER IF NOT EXISTS {table}_claim_kind
        BEFORE INSERT ON {table}
            WHEN (SELECT claim_kind FROM claims WHERE id=NEW.claim_id)<>'{kind}'
            BEGIN SELECT RAISE(
            ABORT,'detail table does not match claim kind'); END;""",
            f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(
            ABORT,'fact details are immutable; append a new claim'); END;""",
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(
            ABORT,'retain history; withdraw the claim instead'); END;""",
            f"""CREATE TRIGGER IF NOT EXISTS accepted_{kind}_needs_detail
            BEFORE UPDATE OF status ON claims WHEN NEW.status='accepted'
            AND NEW.claim_kind='{kind}'
            AND NOT EXISTS(SELECT 1 FROM {table} WHERE claim_id=NEW.id)
            BEGIN SELECT RAISE(
            ABORT,'accepted claim requires its typed detail row'); END;""",
        ]
    for kind, (_, table) in ENTITY_KINDS.items():
        statements += [
            f"""CREATE TRIGGER IF NOT EXISTS {table}_entity_kind
        BEFORE INSERT ON {table}
            WHEN (SELECT kind FROM entities WHERE id=NEW.entity_id)<>'{kind}'
            BEGIN SELECT RAISE(
            ABORT,'entity subtype does not match kind'); END;""",
            f"""CREATE TRIGGER IF NOT EXISTS {table}_identity_immutable
            BEFORE UPDATE OF entity_id ON {table} WHEN NEW.entity_id<>OLD.entity_id
            BEGIN SELECT RAISE(
            ABORT,'entity subtype identity is immutable'); END;""",
        ]
    statements += [
        """CREATE TRIGGER IF NOT EXISTS entity_kind_identity_immutable
        BEFORE UPDATE ON entities WHEN NEW.id<>OLD.id OR NEW.kind<>OLD.kind
         OR NEW.namespace<>OLD.namespace OR NEW.identity_key<>OLD.identity_key
        BEGIN SELECT RAISE(
            ABORT,'entity identity and kind are immutable'); END;""",
        """CREATE TRIGGER IF NOT EXISTS relation_types_must_match
        BEFORE INSERT ON entity_relations
        WHEN NOT EXISTS(
          SELECT 1 FROM claims c JOIN entities s ON s.id=c.subject_id
          JOIN entities o ON o.id=NEW.object_id JOIN relation_types r ON r.id=NEW.relation_type_id
          WHERE c.id=NEW.claim_id
          AND s.kind IN (SELECT value FROM json_each(r.source_kinds_json))
          AND o.kind IN (SELECT value FROM json_each(r.target_kinds_json))
          AND EXISTS(SELECT 1 FROM topic_entities t
            WHERE t.topic_id=c.topic_id AND t.entity_id=o.id)
        ) OR (NEW.function_field_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM field_definitions WHERE id=NEW.function_field_id
          AND subject_kind='product' AND (is_function=1 OR value_type='boolean'
          OR id LIKE '%_other_features')
        ))
        BEGIN SELECT RAISE(
            ABORT,'relation endpoints, topic or function field are invalid'); END;""",
        """CREATE TRIGGER IF NOT EXISTS participation_types_must_match
        BEFORE INSERT ON participations
        WHEN NOT EXISTS(
          SELECT 1 FROM claims c JOIN entities s ON s.id=c.subject_id
          JOIN entities o ON o.id=NEW.object_id JOIN participation_roles r ON r.id=NEW.role_id
          WHERE c.id=NEW.claim_id
          AND s.kind IN (SELECT value FROM json_each(r.subject_kinds_json))
          AND o.kind IN (SELECT value FROM json_each(r.object_kinds_json))
          AND EXISTS(SELECT 1 FROM topic_entities t
            WHERE t.topic_id=c.topic_id AND t.entity_id=o.id)
        )
        BEGIN SELECT RAISE(
            ABORT,'participation role, endpoints or topic are invalid'); END;""",
        """CREATE TRIGGER IF NOT EXISTS field_type_and_subject
        BEFORE INSERT ON field_values
        WHEN NOT EXISTS(
          SELECT 1 FROM field_definitions f JOIN claims c ON c.id=NEW.claim_id
          JOIN entities e ON e.id=c.subject_id
          JOIN field_storage_mappings m ON m.field_id=f.id
          WHERE f.id=NEW.field_id AND f.subject_kind=e.kind AND f.is_derived=0
          AND m.target_table='field_values'
          AND (f.repeat_group IS NULL OR length(trim(NEW.row_key))>0)
          AND CASE f.value_type
           WHEN 'boolean' THEN json_type(NEW.value_json) IN ('true','false')
           WHEN 'number' THEN json_type(NEW.value_json) IN ('integer','real')
           WHEN 'measurement' THEN json_type(NEW.value_json)='object'
           WHEN 'text_list' THEN json_type(NEW.value_json)='array'
           WHEN 'url_list' THEN json_type(NEW.value_json)='array'
           WHEN 'entity_reference_list' THEN json_type(NEW.value_json)='array'
           ELSE json_type(NEW.value_json)='text' END
        ) OR lower(trim(CAST(json_extract(NEW.value_json,'$') AS TEXT)))
          IN ('unknown','未知','暂无','—','未记录')
        OR (json_type(NEW.value_json)='array' AND (
          json_array_length(NEW.value_json)=0 OR EXISTS(
            SELECT 1 FROM json_each(NEW.value_json) WHERE type<>'text' OR length(trim(value))=0)))
        BEGIN SELECT RAISE(
            ABORT,'field subject/type/repeat group is invalid or value is unknown'); END;""",
        """CREATE TRIGGER IF NOT EXISTS metric_requires_subject_context
        BEFORE INSERT ON metric_observations
        WHEN NOT EXISTS(SELECT 1 FROM claims c JOIN entities e ON e.id=c.subject_id
          WHERE c.id=NEW.claim_id AND c.context_id IS NOT NULL
          AND e.kind IN ('technology','route','product','component','process','capability'))
        BEGIN SELECT RAISE(
            ABORT,'metric needs a measurable subject and explicit test context'); END;""",
        """CREATE TRIGGER IF NOT EXISTS capability_requires_product
        BEFORE INSERT ON product_capabilities
        WHEN (SELECT e.kind FROM claims c JOIN entities e ON e.id=c.subject_id
          WHERE c.id=NEW.claim_id)<>'product'
        OR (NEW.function_field_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM field_definitions WHERE id=NEW.function_field_id
          AND subject_kind='product' AND value_type='boolean'))
        BEGIN SELECT RAISE(
            ABORT,'capability needs a product and a product capability field'); END;""",
        """CREATE TRIGGER IF NOT EXISTS event_requires_event_subject
        BEFORE INSERT ON event_details
        WHEN (SELECT e.kind FROM claims c JOIN entities e ON e.id=c.subject_id
          WHERE c.id=NEW.claim_id)<>'event'
        BEGIN SELECT RAISE(
            ABORT,'event details need an event subject'); END;""",
        """CREATE TRIGGER IF NOT EXISTS route_step_requires_route
        BEFORE INSERT ON route_steps
        WHEN (SELECT e.kind FROM claims c JOIN entities e ON e.id=c.subject_id
          WHERE c.id=NEW.claim_id)<>'route'
        BEGIN SELECT RAISE(
            ABORT,'route step needs a route subject'); END;""",
        """CREATE TRIGGER IF NOT EXISTS identity_assertion_owner
        BEFORE INSERT ON identity_assertions
        WHEN (NEW.name_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM entity_names n JOIN claims c ON c.subject_id=n.entity_id
          WHERE n.id=NEW.name_id AND c.id=NEW.claim_id))
        OR (NEW.identifier_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM entity_identifiers i JOIN claims c ON c.subject_id=i.entity_id
          WHERE i.id=NEW.identifier_id AND c.id=NEW.claim_id))
        BEGIN SELECT RAISE(
            ABORT,'identity assertion must reference its subject'); END;""",
        """CREATE TRIGGER IF NOT EXISTS maturity_needs_scale_context
        BEFORE INSERT ON maturity_assessments
        WHEN (SELECT context_id FROM claims WHERE id=NEW.claim_id) IS NULL
        OR NOT EXISTS(SELECT 1 FROM assessment_scales s,json_each(s.levels_json) j
          WHERE s.id=NEW.scale_id AND CAST(j.value AS TEXT)=NEW.level)
        BEGIN SELECT RAISE(
            ABORT,'maturity needs a valid scale level and an explicit context'); END;""",
    ]
    return "\n".join(statements)


class ResearchDatabase:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path).resolve()

    def ensure_entity_name_scopes(self):
        """Add optional source scopes without changing the immutable v1 schema."""
        with self.connect(write=True) as db:
            db.execute(ENTITY_NAME_SCOPES_SQL)

    @contextmanager
    def connect(self, *, write=False):
        batch = _BATCH.get()
        if batch and batch[0] == self.path:
            db = batch[1]
            savepoint = "fact_" + uuid.uuid4().hex
            if write:
                db.execute("SAVEPOINT " + savepoint)
            try:
                yield db
                if write:
                    db.execute("RELEASE " + savepoint)
            except BaseException:
                if write:
                    db.execute("ROLLBACK TO " + savepoint)
                    db.execute("RELEASE " + savepoint)
                raise
            return
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def batch(self):
        """Group document writes; each nested fact keeps its own rollback boundary."""
        with self.connect(write=True) as db:
            token = _BATCH.set((self.path, db))
            try:
                yield db
            finally:
                _BATCH.reset(token)

    def initialize(self, catalog_path=DEFAULT_CATALOG):
        catalog_bytes = Path(catalog_path).read_bytes()
        catalog = json.loads(catalog_bytes)
        schema = Path(__file__).with_name("schema.sql").read_text() + "\n" + generated_constraints()
        checksum = hashlib.sha256(schema.encode()).hexdigest()
        catalog_checksum = hashlib.sha256(catalog_bytes).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=20)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "schema_migrations" not in tables:
                raise ValueError("目标是其他数据库；请为研究新库选择独立文件。")
            if "schema_migrations" in tables:
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=1"
                ).fetchone()
                if row is None or row[0] != checksum:
                    raise ValueError("表结构校验值已变化；需要显式迁移，不能覆盖已有库。")
                registered = dict(connection.execute("SELECT key,value FROM database_metadata"))
                if registered.get("legacy_catalog_sha256") != catalog_checksum:
                    raise ValueError("字段目录已变化；需要显式字段迁移。")
                connection.execute(ENTITY_NAME_SCOPES_SQL)
                connection.commit()
                return self.verify()
            connection.executescript("BEGIN IMMEDIATE;\n" + schema)
            self._seed(connection, catalog, catalog_checksum)
            connection.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (1, checksum, now()))
            connection.execute(ENTITY_NAME_SCOPES_SQL)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.verify()

    @staticmethod
    def _seed(db, catalog, catalog_checksum):
        db.executemany("INSERT INTO dimensions VALUES(?,?,?)", DIMENSIONS)
        orders = {}
        for dimension, key, name, table, rule in ELEMENTS:
            orders[dimension] = orders.get(dimension, 0) + 1
            db.execute(
                "INSERT INTO dimension_elements VALUES(?,?,?,?,?,?)",
                (f"{dimension}.{key}", dimension, name, orders[dimension], table, rule),
            )
        db.executemany(
            "INSERT INTO entity_kinds VALUES(?,?)",
            [(kind, label) for kind, (label, _) in ENTITY_KINDS.items()],
        )
        db.execute(
            "INSERT INTO topics VALUES(?,?,?,?,?)",
            (
                "topic:anduril",
                "anduril",
                "Anduril",
                "首批维护专题；字段和表结构跨专题共用。",
                now(),
            ),
        )
        db.executemany(
            "INSERT INTO relation_types VALUES(?,?,?,?,?)",
            [
                (
                    key,
                    label,
                    encode(sources),
                    encode(targets),
                    "必须有原文证据；不得由名称或同页出现推导。",
                )
                for key, label, sources, targets in RELATIONS
            ],
        )
        db.executemany(
            "INSERT INTO participation_roles VALUES(?,?,?,?)",
            [
                (key, label, encode(sources), encode(targets))
                for key, label, sources, targets in ROLES
            ],
        )
        db.executemany(
            "INSERT INTO units VALUES(?,?,?)",
            [
                ("unit:ms", "ms", "time"),
                ("unit:s", "s", "time"),
                ("unit:m", "m", "length"),
                ("unit:km", "km", "length"),
                ("unit:kg", "kg", "mass"),
                ("unit:W", "W", "power"),
                ("unit:Hz", "Hz", "frequency"),
                ("unit:percent", "%", "ratio"),
                ("unit:count", "count", "count"),
            ],
        )
        db.executemany(
            "INSERT INTO assessment_scales VALUES(?,?,?)",
            [
                ("trl", "技术成熟度TRL（仅来源明确给出时填写）", encode(list(range(1, 10)))),
                (
                    "source_stage",
                    "来源明确的阶段",
                    encode(
                        [
                            "concept",
                            "research",
                            "prototype",
                            "testing",
                            "production",
                            "delivered",
                            "in_use",
                            "retired",
                        ]
                    ),
                ),
            ],
        )
        for group in catalog["groups"]:
            db.execute(
                "INSERT INTO field_groups VALUES(?,?,?,?)",
                (
                    group["kind"] + ":" + group["group_id"],
                    group["kind"],
                    group["group_id"],
                    group["label"],
                ),
            )
        for field in catalog["fields"]:
            fid = field["field_id"]
            label = (
                "与专题企业的关联" if fid == "technology_anduril_association" else field["label"]
            )
            db.execute(
                "INSERT INTO field_definitions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    field["kind"],
                    label,
                    field["label"],
                    field["value_type"],
                    field["repeat_group"],
                    int(field["is_function"]),
                    int(field["derived"]),
                    catalog["schema_version"],
                    encode(field),
                ),
            )
            for group in field["groups"]:
                db.execute(
                    "INSERT INTO field_group_members VALUES(?,?)",
                    (fid, field["kind"] + ":" + group),
                )
            element, table, mode, rule = map_legacy_field(field)
            db.execute(
                "INSERT INTO field_storage_mappings VALUES(?,?,?,?,?)",
                (fid, element, table, rule, mode),
            )
        for fid, kind, label, value_type, element in EXTRA_FIELDS:
            group_id = kind + ":framework_core"
            db.execute(
                "INSERT OR IGNORE INTO field_groups VALUES(?,?,?,?)",
                (group_id, kind, "framework_core", "七类框架补充字段"),
            )
            definition = {
                "field_id": fid,
                "kind": kind,
                "label": label,
                "value_type": value_type,
                "source_required": True,
                "extraction_unknown_policy": "omit",
            }
            db.execute(
                "INSERT INTO field_definitions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    kind,
                    label,
                    label,
                    value_type,
                    None,
                    0,
                    0,
                    "research_framework_v1",
                    encode(definition),
                ),
            )
            db.execute("INSERT INTO field_group_members VALUES(?,?)", (fid, group_id))
            db.execute(
                "INSERT INTO field_storage_mappings VALUES(?,?,?,?,?)",
                (
                    fid,
                    element,
                    "field_values",
                    "该类对象自身的带证据说明；关联事实进入关系或角色表。",
                    "attribute",
                ),
            )
        db.executemany(
            "INSERT INTO database_metadata VALUES(?,?)",
            [
                ("schema_version", "research_framework_v1"),
                ("legacy_catalog_sha256", catalog_checksum),
                ("legacy_catalog_version", catalog["schema_version"]),
                ("reference_framework", "用户提供的七栏参考图，7类48个小点"),
                ("initial_scope", "anduril"),
                ("business_data_imported", "false"),
                (
                    "mapping_contract",
                    "字段映射为目标结构规则；实体消歧、重复行组装和自动转换器尚待接入。",
                ),
            ],
        )

    def create_topic(self, slug, title):
        topic_id = "topic:" + slug
        with self.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO topics VALUES(?,?,?,?,?)", (topic_id, slug, title, "", now())
            )
        return topic_id

    def create_entity(self, *, kind, name, namespace, identity_key, topic_id, details=None):
        """Caller resolves identity; matching a name alone never merges objects."""
        if kind not in ENTITY_KINDS or not identity_key.strip() or not namespace.strip():
            raise ValueError("需要合法对象类型、命名空间和经消歧的身份标识。")
        entity_id = (
            "entity:"
            + hashlib.sha256(encode([namespace, kind, identity_key]).encode()).hexdigest()[:32]
        )
        table = ENTITY_KINDS[kind][1]
        details = dict(details or {})
        if kind == "event":
            details.setdefault("event_type", "unspecified")
        if kind == "generation":
            details.setdefault("generation_label", name)
        with self.connect(write=True) as db:
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(details) <= columns - {"entity_id"}:
                raise ValueError("对象明细列无效。")
            db.execute(
                "INSERT OR IGNORE INTO entities(id,kind,canonical_name,namespace,identity_key,"
                "created_at) "
                "VALUES(?,?,?,?,?,?)",
                (entity_id, kind, name, namespace, identity_key, now()),
            )
            values = {"entity_id": entity_id, **details}
            names = ",".join('"' + key + '"' for key in values)
            placeholders = ",".join("?" for _ in values)
            db.execute(
                f'INSERT OR IGNORE INTO "{table}"({names}) VALUES({placeholders})',
                tuple(values.values()),
            )
            db.execute(
                "INSERT OR IGNORE INTO topic_entities(topic_id,entity_id,membership_role) "
                "VALUES(?,?,?)",
                (topic_id, entity_id, "shared_concept" if namespace == "shared" else "subject"),
            )
        return entity_id

    def append_claim(
        self,
        *,
        topic_id,
        subject_id,
        kind,
        detail,
        evidence,
        status="candidate",
        assertion_type="source_statement",
        context_id=None,
        scope_product_id=None,
        valid_from=None,
        valid_to=None,
        extraction_run_id=None,
        scope_note="",
    ):
        """Append a typed, evidence-bound record; identical retries reuse its ID.

        Evidence items contain segment_id, quote and optional stance. This method
        checks structure and literal quotation, not the truth of a conclusion.
        A previously reviewed record is returned without modifying its decision.
        """
        if kind not in DETAIL_TABLES or status not in {"candidate", "accepted"}:
            raise ValueError("新增事实类型或初始状态无效。")
        evidence = sorted(
            {
                encode(
                    {
                        "segment_id": e["segment_id"],
                        "quote": e["quote"],
                        "stance": e.get("stance", "supports"),
                    }
                )
                for e in evidence
            }
        )
        fact = dict(
            topic_id=topic_id,
            subject_id=subject_id,
            kind=kind,
            detail=detail,
            evidence=evidence,
            assertion_type=assertion_type,
            context_id=context_id,
            scope_product_id=scope_product_id,
            valid_from=valid_from,
            valid_to=valid_to,
            extraction_run_id=extraction_run_id,
            scope_note=scope_note,
        )
        claim_id = "claim:" + hashlib.sha256(encode(fact).encode()).hexdigest()
        table = DETAIL_TABLES[kind]
        with self.connect(write=True) as db:
            if db.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone():
                return claim_id
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(detail) <= columns - {"claim_id"}:
                raise ValueError("事实明细列无效。")
            db.execute(
                """INSERT INTO claims(id,topic_id,subject_id,claim_kind,assertion_type,
                context_id,scope_product_id,valid_from,valid_to,extraction_run_id,scope_note,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim_id,
                    topic_id,
                    subject_id,
                    kind,
                    assertion_type,
                    context_id,
                    scope_product_id,
                    valid_from,
                    valid_to,
                    extraction_run_id,
                    scope_note,
                    now(),
                ),
            )
            values = {"claim_id": claim_id, **detail}
            names = ",".join('"' + key + '"' for key in values)
            placeholders = ",".join("?" for _ in values)
            db.execute(
                f'INSERT INTO "{table}"({names}) VALUES({placeholders})', tuple(values.values())
            )
            for item in evidence:
                e = json.loads(item)
                db.execute(
                    "INSERT INTO claim_evidence VALUES(?,?,?,?,?)",
                    (
                        "evidence:" + hashlib.sha256((claim_id + item).encode()).hexdigest(),
                        claim_id,
                        e["segment_id"],
                        e["quote"],
                        e["stance"],
                    ),
                )
            if status == "accepted":
                db.execute("UPDATE claims SET status='accepted' WHERE id=?", (claim_id,))
        return claim_id

    def review_claim(self, claim_id, *, decision, reviewer, reason):
        states = {"accept": "accepted", "dispute": "disputed", "withdraw": "withdrawn"}
        if decision not in {*states, "unlock"} or not reviewer.strip() or not reason.strip():
            raise ValueError("复核需要合法决定、复核人和理由。")
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone():
                raise ValueError("事实不存在。")
            if decision == "unlock":
                db.execute("UPDATE claims SET review_locked=0 WHERE id=?", (claim_id,))
            else:
                db.execute(
                    "UPDATE claims SET status=?,review_locked=1 WHERE id=?",
                    (states[decision], claim_id),
                )
            db.execute(
                "INSERT INTO review_decisions VALUES(?,?,?,?,?,?)",
                ("review:" + uuid.uuid4().hex, claim_id, reviewer, decision, reason, now()),
            )

    def verify(self):
        with self.connect() as db:
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {
                table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in tables
            }
            invalid_targets = []
            for (table,) in db.execute(
                "SELECT primary_table FROM dimension_elements UNION "
                "SELECT target_table FROM field_storage_mappings"
            ):
                if table not in tables:
                    invalid_targets.append(table)
            missing_mappings = db.execute(
                "SELECT count(*) FROM field_definitions f LEFT JOIN "
                "field_storage_mappings m ON m.field_id=f.id WHERE m.field_id IS NULL"
            ).fetchone()[0]
            issues = list(db.execute("PRAGMA foreign_key_check"))
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            dimensions = [
                dict(r)
                for r in db.execute(
                    "SELECT d.id,d.name,count(e.id) elements "
                    "FROM dimensions d JOIN dimension_elements e ON e.dimension_id=d.id "
                    "GROUP BY d.id"
                )
            ]
            metadata = dict(db.execute("SELECT key,value FROM database_metadata"))
            legacy = db.execute(
                "SELECT count(*) FROM field_definitions WHERE source_schema_version=?",
                (metadata["legacy_catalog_version"],),
            ).fetchone()[0]
            accepted_without_evidence = db.execute(
                "SELECT count(*) FROM claims c WHERE status='accepted' "
                "AND NOT EXISTS(SELECT 1 FROM claim_evidence e "
                "WHERE e.claim_id=c.id AND e.stance='supports')"
            ).fetchone()[0]
            report = dict(
                database=str(self.path),
                schema_version=SCHEMA_VERSION,
                tables=len(tables),
                views=db.execute("SELECT count(*) FROM sqlite_master WHERE type='view'").fetchone()[
                    0
                ],
                triggers=db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                ).fetchone()[0],
                counts=counts,
                dimensions=dimensions,
                legacy_fields=legacy,
                extra_fields=counts["field_definitions"] - legacy,
                topics=[
                    dict(r) for r in db.execute("SELECT id,slug,title FROM topics ORDER BY slug")
                ],
                foreign_key_issues=[tuple(r) for r in issues],
                integrity=integrity,
                missing_field_mappings=missing_mappings,
                invalid_target_tables=invalid_targets,
                accepted_without_evidence=accepted_without_evidence,
                business_data_imported=metadata["business_data_imported"] == "true",
            )
            report["valid"] = (
                integrity == "ok"
                and not issues
                and not invalid_targets
                and not missing_mappings
                and not accepted_without_evidence
                and len(dimensions) == 7
                and counts["dimension_elements"] == 48
                and legacy == 408
            )
            return report

    def export(self, directory=DEFAULT_EXPORT):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        report = self.verify()
        with self.connect() as db:
            mappings = [
                dict(r)
                for r in db.execute("""SELECT f.id field_id,f.label,f.legacy_label,
                f.subject_kind,f.value_type,f.repeat_group,f.source_schema_version,
                d.id dimension_id,
                d.name dimension,e.name element,m.target_table,m.mode,m.storage_rule
                FROM field_definitions f JOIN field_storage_mappings m ON m.field_id=f.id
                JOIN dimension_elements e ON e.id=m.element_id
                JOIN dimensions d ON d.id=e.dimension_id
                ORDER BY d.id,f.subject_kind,f.id""")
            ]
            elements = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM dimension_elements ORDER BY dimension_id,ordinal"
                )
            ]
            schema_items = db.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type "
                "WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
                "WHEN 'view' THEN 2 ELSE 3 END,name"
            ).fetchall()
            schema = (
                "-- Structure only. For a seeded database, run the init CLI on a new path.\n"
                "PRAGMA foreign_keys=ON;\n\n"
            )
            schema += "\n\n".join(r[2].rstrip(";") + ";" for r in schema_items) + "\n"
            schema_docs = []
            for table in report["counts"]:
                columns = [dict(r) for r in db.execute(f'PRAGMA table_info("{table}")')]
                schema_docs.append(dict(table=table, rows=report["counts"][table], columns=columns))
        (directory / "schema.sql").write_text(schema, encoding="utf-8")
        (directory / "queries.sql").write_text(
            Path(__file__).with_name("queries.sql").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (directory / "inventory.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "field-mapping.json").write_text(
            json.dumps(mappings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "framework-elements.json").write_text(
            json.dumps(elements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "table-dictionary.json").write_text(
            json.dumps(schema_docs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with (directory / "field-mapping.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(mappings[0]))
            writer.writeheader()
            writer.writerows(mappings)
        self._write_overview(directory, report, elements, schema_docs)
        return directory

    @staticmethod
    def _write_overview(directory, report, elements, schema_docs):
        esc = html.escape
        sections = []
        for dimension, name, description in DIMENSIONS:
            rows = "".join(
                "<tr><td>"
                + esc(e["name"])
                + "</td><td><code>"
                + esc(e["primary_table"])
                + "</code></td><td>"
                + esc(e["storage_rule"])
                + "</td></tr>"
                for e in elements
                if e["dimension_id"] == dimension
            )
            sections.append(
                f'<section id="dimension-{dimension}"><h2>{dimension} {esc(name)}</h2>'
                f"<p>{esc(description)}</p><table><thead><tr><th>图片小点</th><th>主要表</th>"
                f"<th>保存与关联方式</th></tr></thead><tbody>{rows}</tbody></table></section>"
            )
        tables = []
        for entry in schema_docs:
            rows = "".join(
                f"<tr><td><code>{esc(c['name'])}</code></td><td>{esc(c['type'])}</td>"
                f"<td>{'主键' if c['pk'] else ''}</td>"
                f"<td>{'必填' if c['notnull'] else '可空'}</td></tr>"
                for c in entry["columns"]
            )
            tables.append(
                f"<details><summary>{esc(entry['table'])} · {entry['rows']} 行</summary>"
                f"<table><tr><th>列</th><th>类型</th><th>主键</th><th>空值</th></tr>{rows}</table></details>"
            )
        nav = " ".join(f'<a href="#dimension-{i}">{i} {esc(n)}</a>' for i, n, _ in DIMENSIONS)
        document = Path(__file__).with_name("overview.html").read_text(encoding="utf-8")
        replacements = {
            "TABLE_COUNT": report["tables"],
            "VIEW_COUNT": report["views"],
            "EXTRA_FIELDS": report["extra_fields"],
            "CLAIM_COUNT": report["counts"]["claims"],
            "DATABASE": esc(report["database"]),
            "NAV": nav,
            "SECTIONS": "".join(sections),
            "TABLES": "".join(tables),
            "REFERENCE_IMAGE": (
                '<div class="card"><p>用户提供的建库主体框架</p>'
                '<img src="reference-framework.png" alt="七类研究要素参考图" '
                'style="width:100%;height:auto"></div>'
                if (directory / "reference-framework.png").is_file()
                else ""
            ),
        }
        for key, value in replacements.items():
            document = document.replace("@@" + key + "@@", str(value))
        (directory / "overview.html").write_text(document, encoding="utf-8")
