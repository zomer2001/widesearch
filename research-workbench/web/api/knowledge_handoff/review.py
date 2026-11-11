"""Offline extraction review: simulate parameterized writes, then explicitly commit.

Preview uses an SQLite backup, never the live database. SQL comes only from the
existing typed mapper and is authenticated locally; no model-written SQL runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from api.research_mapping import DocumentExtraction, Mapper, key, normalized
from searchos.knowledge_store.database import DETAIL_TABLES, ResearchDatabase, encode, now

from . import SCHEMA_VERSION


class PlanError(ValueError):
    pass


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fingerprint(connection):
    digest = hashlib.sha256()
    for line in connection.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _secret(root, *, create=False):
    path = Path(root) / ".plan-signing-key"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(os.urandom(32))
    if not path.is_file():
        raise PlanError("写入计划签名文件不存在，请重新生成预览")
    return path.read_bytes()


def _signature(plan, secret):
    payload = {k: v for k, v in plan.items() if k != "signature"}
    return hmac.new(secret, encode(payload).encode(), hashlib.sha256).hexdigest()


def _plan_path(root, plan_id):
    if not re.fullmatch(r"plan_[a-f0-9]{64}", plan_id):
        raise PlanError("写入计划编号无效")
    return Path(root) / "plans" / (plan_id + ".json")


def load_plan(artifact_root, plan_id):
    path = _plan_path(artifact_root, plan_id)
    if not path.is_file():
        raise PlanError("找不到写入计划")
    plan = json.loads(path.read_text())
    if plan.get("plan_id") != plan_id or not hmac.compare_digest(
        plan.get("signature", ""), _signature(plan, _secret(artifact_root))
    ):
        raise PlanError("写入计划被修改，请重新生成预览")
    return plan


class _Recorder:
    def __init__(self):
        self.statements = []
        self.savepoints = []

    def executed(self, sql, params):
        command = " ".join(sql.strip().split())
        upper = command.upper()
        if upper.startswith("SAVEPOINT "):
            self.savepoints.append((command.split()[-1], len(self.statements)))
        elif upper.startswith("ROLLBACK TO "):
            name = command.split()[-1]
            index = next(
                i for i in range(len(self.savepoints) - 1, -1, -1) if self.savepoints[i][0] == name
            )
            self.statements[self.savepoints[index][1] :] = []
            self.savepoints[index + 1 :] = []
        elif upper.startswith("RELEASE "):
            name = command.split()[-1]
            index = next(
                i for i in range(len(self.savepoints) - 1, -1, -1) if self.savepoints[i][0] == name
            )
            self.savepoints[index:] = []
        elif re.match(r"^(INSERT|UPDATE|DELETE|REPLACE)\b", upper):
            self.statements.append({"sql": sql, "params": list(params)})


class _Connection:
    def __init__(self, connection, recorder):
        self.connection, self.recorder = connection, recorder

    def execute(self, sql, params=()):
        result = self.connection.execute(sql, params)
        self.recorder.executed(sql, params)
        return result

    def executemany(self, sql, rows):
        result = None
        for params in rows:
            result = self.execute(sql, params)
        return result

    def executescript(self, *_):
        raise PlanError("写入计划只允许逐条参数化 SQL")

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _RecordingStore(ResearchDatabase):
    def __init__(self, path):
        super().__init__(path)
        self.recorder = _Recorder()

    @contextmanager
    def connect(self, *, write=False):
        with super().connect(write=write) as connection:
            yield (
                connection
                if isinstance(connection, _Connection)
                else _Connection(connection, self.recorder)
            )


class _ReviewMapper(Mapper):
    """Conservative identity alignment; uncertainty never silently merges names."""

    def __init__(self, *args, choices=None, **kwargs):
        self.choices = choices or {}
        self.resolutions, self.conflicts, self.duplicates = {}, [], set()
        self.local_entities = {}
        self.entity_input = None
        super().__init__(*args, **kwargs)

    @contextmanager
    def atomic_item(self):
        claims, cache = self.claim_ids.copy(), self.local_entities.copy()
        try:
            with self.store.batch():
                yield
        except BaseException:
            self.claim_ids, self.local_entities = claims, cache
            raise

    def identity(self, entity):
        self.entity_input = entity.entity_key
        try:
            with self.atomic_item():
                return super().identity(entity)
        finally:
            self.entity_input = None

    def field(self, entity, field):
        with self.atomic_item():
            return super().field(entity, field)

    def repeat(self, entity, group, fields):
        with self.atomic_item():
            return super().repeat(entity, group, fields)

    def record(self, record):
        with self.atomic_item():
            return super().record(record)

    def entity(self, kind, name, namespace=None, **details):
        namespace = namespace or self.namespace
        name = str(name)
        lookup = (namespace, kind, normalized(name))
        resolution_key = self.entity_input or f"{kind}:{namespace}:{normalized(name)}"
        choice = self.choices.get(resolution_key)
        cache_key = (*lookup, choice)
        if cache_key in self.local_entities:
            return self.local_entities[cache_key]
        with self.store.connect() as db:
            scoped_names = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_name_scopes'"
            ).fetchone()
            alias_sql = (
                "SELECT 1 FROM entity_names n WHERE n.entity_id=? AND lower(n.name)=lower(?)"
            )
            alias_params = (name,)
            if scoped_names:
                alias_sql += (
                    " AND (NOT EXISTS(SELECT 1 FROM entity_name_scopes s WHERE s.name_id=n.id)"
                    " OR EXISTS(SELECT 1 FROM entity_name_scopes s"
                    " WHERE s.name_id=n.id AND s.source_document_id=?))"
                )
                alias_params += (self.source.document_key,)
            candidates = [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT e.id,e.kind,e.canonical_name AS name,e.namespace "
                    "FROM entities e LEFT JOIN entity_names n ON n.entity_id=e.id "
                    "WHERE e.kind=? AND e.namespace=? AND e.state='active'",
                    (kind, namespace),
                )
                if normalized(row["name"]) == normalized(name)
                or db.execute(
                    alias_sql,
                    (row["id"], *alias_params),
                ).fetchone()
            ]
            chosen = None
            if choice and choice != "new":
                chosen = next((row for row in candidates if row["id"] == choice), None)
                if chosen is None:
                    raise ValueError("实体选择不在当前类型、命名空间及名称候选中")
            elif choice != "new" and len(candidates) == 1:
                # Scalar vocabularies are closed labels. Named entities require a
                # decision, except an exact source revision already identifying it.
                same_source = db.execute(
                    "SELECT 1 FROM claims c JOIN claim_evidence e ON e.claim_id=c.id "
                    "JOIN source_segments s ON s.id=e.segment_id "
                    "WHERE c.subject_id=? AND c.claim_kind='identity' AND s.revision_id=?",
                    (candidates[0]["id"], self.source.revision_key),
                ).fetchone()
                if kind in {"metric", "capability"} or same_source:
                    chosen = candidates[0]
            if not choice and candidates and chosen is None:
                self.resolutions[resolution_key] = {
                    "resolution_key": resolution_key,
                    "entity_key": self.entity_input,
                    "name": name,
                    "kind": kind,
                    "namespace": namespace,
                    "status": "unresolved",
                    "candidates": candidates,
                    "reason": "名称或别名相同不等于同一对象，请选择已有对象或独立建档",
                }
                raise ValueError("实体身份待确认：" + name)
        if chosen:
            identity = chosen["id"]
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT OR IGNORE INTO topic_entities(topic_id,entity_id,membership_role) "
                    "VALUES(?,?,?)",
                    (
                        self.topic,
                        identity,
                        "shared_concept" if namespace == "shared" else "subject",
                    ),
                )
        else:
            # Named objects start as document-scoped identities; later matching
            # is explicit rather than a silent cross-document name collision.
            identity_key = (
                normalized(name)
                if kind in {"metric", "capability"}
                else (normalized(name) + "|source:" + key(self.source.document_key, resolution_key))
            )
            if choice == "new" and candidates:
                identity_key += "|separate:" + key(sorted(row["id"] for row in candidates))
            identity = self.store.create_entity(
                kind=kind,
                name=name,
                namespace=namespace,
                identity_key=identity_key,
                topic_id=self.topic,
                details=details,
            )
        self.local_entities[cache_key] = identity
        self.resolutions[resolution_key] = {
            "resolution_key": resolution_key,
            "name": name,
            "kind": kind,
            "namespace": namespace,
            "status": "matched" if chosen else "new",
            "id": identity,
            "candidates": candidates,
        }
        return identity

    def commit(self, subject, kind, detail, quotes, qualifiers=None):
        qualifiers = qualifiers or {}
        if isinstance(quotes, str):
            quotes = [quotes]
        quotes = [self.source.quote(quote) for quote in quotes]
        table = DETAIL_TABLES[kind]
        with self.store.connect() as db:
            rows = list(
                db.execute(
                    "SELECT c.id,c.status,c.context_id,c.scope_note,d.* FROM claims c "
                    f'JOIN "{table}" d '
                    "ON d.claim_id=c.id WHERE c.subject_id=? AND c.claim_kind=?",
                    (subject, kind),
                )
            )
            for row in rows:
                same_detail = all(row[k] == v for k, v in detail.items())
                same_scope = {
                    k: v
                    for k, v in json.loads(row["scope_note"]).items()
                    if k not in {"original_value", "row_fields"}
                } == {
                    k: v for k, v in qualifiers.items() if k not in {"original_value", "row_fields"}
                }
                if same_detail and same_scope:
                    existing_quotes = {
                        r[0]
                        for r in db.execute(
                            "SELECT quote FROM claim_evidence WHERE claim_id=? AND segment_id=?",
                            (row["id"], self.source.segment_key),
                        )
                    }
                    if set(quotes) <= existing_quotes:
                        if row["status"] != "accepted":
                            raise ValueError("已有事实仍待核或已撤回，不能通过重复导入改变审核决定")
                        self.claim_ids.add(row["id"])
                        self.duplicates.add(row["id"])
                        return row["id"]
                if row["status"] != "accepted" or not same_scope or same_detail:
                    continue
                conflict = (
                    kind == "field"
                    and row["field_id"] == detail.get("field_id")
                    and self.fields.get(detail.get("field_id"), {}).get("value_type") != "text"
                    and not isinstance(json.loads(detail["value_json"]), list)
                    or kind == "capability"
                    and row["capability_id"] == detail.get("capability_id")
                    or kind == "metric"
                    and row["metric_id"] == detail.get("metric_id")
                    and row["raw_unit"] == detail.get("raw_unit")
                    and row["statistic"] == detail.get("statistic", "unspecified")
                    and any(
                        row[k] != detail.get(k)
                        for k in ("numeric_value", "min_value", "max_value", "comparator")
                    )
                )
                if conflict:
                    item = {
                        "subject_id": subject,
                        "kind": kind,
                        "existing_claim_id": row["id"],
                        "incoming": detail,
                        "reason": "同一对象和条件下存在不同取值，保留原记录并待核",
                    }
                    self.conflicts.append(item)
                    raise ValueError(item["reason"])
        return super().commit(subject, kind, detail, quotes, qualifiers)


def _record_display(db, item):
    detail, kind = item["detail"], item["kind"]

    def scalar(sql, params, fallback=""):
        row = db.execute(sql, params).fetchone()
        return row[0] if row else fallback

    if kind == "field":
        return scalar(
            "SELECT label FROM field_definitions WHERE id=?",
            (detail["field_id"],),
            detail["field_id"],
        ), json.loads(detail["value_json"])
    if kind == "identity":
        return "名称", scalar(
            "SELECT name FROM entity_names WHERE id=?", (detail.get("name_id"),), item["subject"]
        )
    if kind == "metric":
        label = scalar(
            "SELECT canonical_name FROM entities WHERE id=?", (detail["metric_id"],), "指标"
        )
        return label, {
            "raw": detail["raw_value"],
            "value": detail["numeric_value"],
            "unit": detail["raw_unit"],
            "min": detail["min_value"],
            "max": detail["max_value"],
            "operator": detail["comparator"],
        }
    if kind == "relation":
        return (
            scalar(
                "SELECT label FROM relation_types WHERE id=?", (detail["relation_type_id"],), "关系"
            ),
            scalar("SELECT canonical_name FROM entities WHERE id=?", (detail["object_id"],)),
        )
    if kind == "capability":
        statuses = {
            "supported": "支持",
            "unsupported": "不支持",
            "planned": "计划支持",
            "partial": "部分支持",
        }
        return (
            scalar(
                "SELECT canonical_name FROM entities WHERE id=?", (detail["capability_id"],), "功能"
            ),
            statuses.get(detail["support_status"], detail["support_status"]),
        )
    if kind == "participation":
        return (
            scalar(
                "SELECT label FROM participation_roles WHERE id=?", (detail["role_id"],), "参与"
            ),
            scalar("SELECT canonical_name FROM entities WHERE id=?", (detail["object_id"],)),
        )
    if kind == "event":
        return detail["description"], detail["occurred_at"] or "日期未给出"
    if kind == "maturity":
        return "成熟度", {"scale": detail["scale_id"], "level": detail["level"]}
    if kind == "assessment":
        return detail["assessment_type"], detail["conclusion"]
    if kind == "classification":
        return "分类", scalar("SELECT label FROM terms WHERE id=?", (detail["term_id"],))
    return kind, {
        key: value for key, value in detail.items() if key != "claim_id" and value is not None
    }


def _source_state(source):
    return {
        "document_id": source.document_id or source.source_id,
        "source_id": source.source_id,
        "library_id": source.library_id,
        "source_revision": source.source_revision or source.digest,
        "text_digest": source.digest,
        "revision_key": source.revision_key,
        "source_url": source.url,
        "title": source.title,
    }


def build_plan(store, source, outputs, *, slug, artifact_root, entity_choices=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9:_-]{0,99}", slug):
        raise PlanError("专题编号无效")
    if not source.text.strip():
        raise PlanError("原文正文为空")
    raw_outputs = [DocumentExtraction.model_validate(value).model_dump() for value in outputs]
    choices = entity_choices or {}
    if not isinstance(choices, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in choices.items()
    ):
        raise PlanError("实体选择必须是编号到目标编号的映射")
    with tempfile.TemporaryDirectory(prefix="knowledge-plan-") as temporary:
        path = Path(temporary) / "preview.sqlite"
        with store.connect() as original, sqlite3.connect(path) as shadow:
            original.backup(shadow)
            fingerprint = _fingerprint(shadow)
        shadow_store = _RecordingStore(path)
        from .contracts import install_contract, normalize_outputs

        with shadow_store.batch():
            install_contract(shadow_store)
            values, issues = normalize_outputs(shadow_store, raw_outputs, source)
            # Stable run identity: a retry of the same source and extraction does
            # not manufacture a new provenance run or duplicate its facts.
            from api.research_extraction import start_run

            run_id = start_run(
                shadow_store, source, slug, "handoff:" + key(SCHEMA_VERSION, raw_outputs)
            )
            mapper = _ReviewMapper(shadow_store, source, slug=slug, run_id=run_id, choices=choices)
            for value in values:
                mapper.apply(value)
                mapper.entities.clear()
                mapper.entity_info.clear()
            receipt = mapper.receipt()
            unmapped = list(issues) + receipt["unmapped"]
            status = "partial" if unmapped or mapper.conflicts else "completed"
            with shadow_store.connect(write=True) as db:
                db.execute(
                    "UPDATE extraction_runs SET status=?,finished_at=? WHERE id=?",
                    (status, now(), run_id),
                )
                records = []
                for claim_id in sorted(mapper.claim_ids):
                    row = db.execute(
                        "SELECT c.id,c.subject_id,e.canonical_name AS subject,"
                        "c.claim_kind AS kind,c.status "
                        "FROM claims c JOIN entities e ON e.id=c.subject_id WHERE c.id=?",
                        (claim_id,),
                    ).fetchone()
                    item = dict(row)
                    item["status"] = "duplicate" if claim_id in mapper.duplicates else "candidate"
                    item["detail"] = dict(
                        db.execute(
                            f'SELECT * FROM "{DETAIL_TABLES[item["kind"]]}" WHERE claim_id=?',
                            (claim_id,),
                        ).fetchone()
                    )
                    item["label"], item["value"] = _record_display(db, item)
                    item["quotes"] = [
                        r[0]
                        for r in db.execute(
                            "SELECT quote FROM claim_evidence WHERE claim_id=?", (claim_id,)
                        )
                    ]
                    records.append(item)
        counts = receipt["counts"]
        counts.update(
            records=len(records),
            entities=len({r["subject_id"] for r in records}),
            new_records=len(records) - len(mapper.duplicates),
            duplicates=len(mapper.duplicates),
            unresolved_entities=sum(
                r["status"] == "unresolved" for r in mapper.resolutions.values()
            ),
            conflicts=len(mapper.conflicts),
            unmapped=len(unmapped),
        )
        plan = {
            "schema_version": SCHEMA_VERSION,
            "created_at": now(),
            "slug": slug,
            **_source_state(source),
            "database_path": str(store.path),
            "db_fingerprint": fingerprint,
            "status": "needs_review" if unmapped else "ready",
            "counts": counts,
            "unmapped": unmapped,
            "conflicts": mapper.conflicts,
            "entities": list(mapper.resolutions.values()),
            "records": records,
            "sql_statements": shadow_store.recorder.statements,
            "outputs": raw_outputs,
            "entity_choices": choices,
            "run_id": run_id,
            "can_commit": bool(records),
            "requires_partial_confirmation": bool(unmapped or mapper.conflicts),
            "model_calls": 0,
            "validation_note": (
                "结构、引句和约束已检查；引句存在不等于语义已人工核实。确认后仅发布列出的候选。"
            ),
        }
    plan["plan_id"] = "plan_" + key(plan)
    plan["signature"] = _signature(plan, _secret(artifact_root, create=True))
    _write(_plan_path(artifact_root, plan["plan_id"]), plan)
    return plan


def commit_plan(store, source, plan_id, *, artifact_root, allow_partial=False):
    plan = load_plan(artifact_root, plan_id)
    if plan["database_path"] != str(store.path):
        raise PlanError("写入计划不属于当前数据库")
    state = _source_state(source)
    for field in (
        "document_id",
        "library_id",
        "source_revision",
        "text_digest",
        "revision_key",
        "source_url",
    ):
        if plan[field] != state[field]:
            raise PlanError("原文版本或身份已变化，请重新生成预览")
    if plan["requires_partial_confirmation"] and not allow_partial:
        raise PlanError("仍有未通过或待定项目；请明确确认仅提交可用部分")
    if not plan["can_commit"]:
        raise PlanError("本次没有可入库记录")
    receipt_path = Path(artifact_root) / "receipts" / (plan_id + ".json")
    marker = "handoff_commit:" + plan_id
    with store.connect(write=True) as db:
        done = db.execute("SELECT value FROM database_metadata WHERE key=?", (marker,)).fetchone()
        if done:
            receipt = json.loads(done[0])
            receipt["reused"] = True
        else:
            if _fingerprint(db) != plan["db_fingerprint"]:
                raise PlanError("数据库在预览后发生变化，请重新生成预览")
            for statement in plan["sql_statements"]:
                # Authenticated, server-generated DML only. The identifier allowlist
                # is enforced by the typed mapper; all content is bound as values.
                if not re.match(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement["sql"], re.I):
                    raise PlanError("写入计划包含不允许的语句")
                db.execute(statement["sql"], statement["params"])
            receipt = {
                "plan_id": plan_id,
                "run_id": plan["run_id"],
                "document_id": plan["document_id"],
                "source_revision": plan["source_revision"],
                "status": "partial" if plan["requires_partial_confirmation"] else "completed",
                "counts": plan["counts"],
                "unmapped": plan["unmapped"],
                "conflicts": plan["conflicts"],
                "committed_at": now(),
                "model_calls": 0,
                "reused": False,
            }
            db.execute(
                "INSERT INTO database_metadata(key,value) VALUES(?,?)", (marker, encode(receipt))
            )
    # The transactional marker is authoritative. A cache failure after COMMIT
    # cannot turn a successful database import into a reported transaction failure.
    # Repeated commits take the marker branch above and retry this cache write.
    try:
        _write(receipt_path, receipt)
    except OSError:
        receipt = {
            **receipt,
            "artifact_error": (
                "数据库已入库，回执文件保存失败；重复提交可恢复回执，不会重复写入事实"
            ),
        }
    return receipt
