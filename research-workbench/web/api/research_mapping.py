"""Original text -> sparse extraction -> typed research records. No model calls here."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from searchos.knowledge_store.catalog import ENTITY_KINDS
from searchos.knowledge_store.database import ResearchDatabase, encode, now

VERSION = "document-fields-v1"
ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "design/knowledge-extraction-handoff-20261104"


def key(*parts):
    return hashlib.sha256(encode(parts).encode()).hexdigest()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedField(Strict):
    field_id: str
    value: JsonValue
    quote: str
    qualifiers: dict[str, JsonValue] = Field(default_factory=dict)


class ExtractedEntity(Strict):
    entity_key: str
    kind: str
    name: str
    namespace: str = ""
    identity_quote: str
    fields: list[ExtractedField] = Field(default_factory=list)


class ExtractedRelation(Strict):
    source_key: str
    target_key: str
    predicate: str
    quote: str
    field_id: str | None = None


class ExtractedRecord(Strict):
    subject_key: str
    kind: str
    label: str = ""
    target_key: str | None = None
    value: JsonValue = None
    quote: str
    qualifiers: dict[str, JsonValue] = Field(default_factory=dict)


class DocumentExtraction(Strict):
    schema_version: str = VERSION
    source_id: str = ""
    status: str = "extracted"
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
    records: list[ExtractedRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unmapped: list[dict[str, JsonValue]] = Field(default_factory=list)


@dataclass
class Source:
    url: str
    title: str
    text: str
    artifact_path: str
    source_id: str
    library_id: str = ""
    document_id: str = ""
    source_revision: str = ""
    fetched_at: str | None = None
    segments: list[dict] | None = None

    @property
    def document_key(self):
        return "document:" + key(self.url)

    @property
    def digest(self):
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def revision_key(self):
        return "revision:" + key(self.document_key, self.digest)

    @property
    def segment_key(self):
        return "segment:" + key(self.revision_key, "full_text")

    def quote(self, quote):
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("缺少原文引句")
        if quote in self.text:
            return quote
        pattern = r"\s+".join(re.escape(part) for part in re.split(r"\s+", quote.strip()))
        match = re.search(pattern, self.text)
        if not match:
            raise ValueError("引句不在当前原文中")
        return match.group()

    def register(self, store):
        with store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO source_documents VALUES(?,?,?,?,NULL)",
                (self.document_key, self.url, self.title, "archived_document"),
            )
            db.execute(
                "INSERT OR IGNORE INTO source_revisions VALUES(?,?,?,?,NULL,?)",
                (
                    self.revision_key,
                    self.document_key,
                    self.digest,
                    self.artifact_path,
                    self.fetched_at or now(),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO source_segments VALUES(?,?,?,?)",
                (self.segment_key, self.revision_key, "完整正文", self.text),
            )


def library_source(data_root, library_id, document_id, revision):
    from api.anduril_reader import is_failed_source
    from searchos.local.library import LocalLibraryService

    service = LocalLibraryService(data_root)
    pairs = service._verified_blocks(library_id, [document_id])
    if not pairs or any(record.source_revision != revision for record, _ in pairs):
        raise ValueError("原文已删除、版本已变化或解析文件不可用")
    primary = [(record, block) for record, block in pairs if block.is_primary]
    if not primary:
        raise ValueError("原文没有可抽取正文")
    record = primary[0][0]
    text = "\n\n".join(block.text for _, block in primary)
    if is_failed_source(text):
        raise ValueError("抓取失败页或搜索摘要不能作为抽取正文")
    origin = record.origin or {}
    return Source(
        url=origin.get("final_url")
        or origin.get("request_url")
        or f"local://{library_id}/{document_id}",
        title=origin.get("title") or Path(record.relative_path).name,
        text=text,
        artifact_path=record.source_path,
        source_id=document_id,
        library_id=library_id,
        document_id=document_id,
        source_revision=revision,
        fetched_at=origin.get("fetched_at"),
        segments=[
            {
                "id": block.block_id,
                "text": block.text,
                "location": ", ".join(loc.label for loc in block.source_locators),
            }
            for _, block in primary
        ],
    )


def handoff_source(source_id, directory=HANDOFF):
    directory = Path(directory)
    if not re.fullmatch(r"src_[a-f0-9]{16}", source_id):
        raise ValueError("来源编号无效")
    manifest = json.loads((directory / "manifest.json").read_text())
    item = next((s for s in manifest["sources"] if s["source_id"] == source_id), None)
    if item is None or item["status"] != "ready":
        raise ValueError("该来源不在已确认可处理清单中")
    path = (directory / item["path"]).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("来源文件越界")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != item["sha256"]:
        raise ValueError("来源原文校验值发生变化")
    return Source(
        url=item["url"],
        title=item["title"],
        text=body.decode("utf-8"),
        artifact_path=str(path),
        source_id=source_id,
        library_id=item.get("library_id", ""),
        document_id=item.get("document_id", ""),
        source_revision=item.get("source_revision", ""),
        fetched_at=item.get("fetched_at"),
    )


def chunks(source, size=10000):
    """Complete coverage with stable offsets; no document/window count cap."""
    result = []
    start = 0
    while start < len(source.text):
        stop = min(start + size, len(source.text))
        if stop < len(source.text):
            boundary = source.text.rfind("\n\n", start + size // 2, stop)
            if boundary > start:
                stop = boundary + 2
        result.append(
            {
                "segment_id": source.segment_key,
                "start": start,
                "end": stop,
                "text": source.text[start:stop],
            }
        )
        start = stop
    return result


class Mapper:
    def __init__(self, store: ResearchDatabase, source: Source, *, slug="anduril", run_id=None):
        self.store, self.source = store, source
        self.topic = store.create_topic(
            slug, {"anduril": "Anduril", "huawei": "华为"}.get(slug, slug)
        )
        self.namespace = slug
        self.run_id = run_id
        self.unmapped, self.errors, self.claim_ids = [], [], set()
        self.entities, self.entity_info = {}, {}
        with store.connect() as db:
            self.fields = {
                r["id"]: dict(r)
                for r in db.execute(
                    "SELECT f.*,m.target_table,m.mode FROM field_definitions f "
                    "JOIN field_storage_mappings m ON m.field_id=f.id"
                )
            }
        source.register(store)

    def pending(self, item, reason):
        if isinstance(item, BaseModel):
            item = item.model_dump()
        self.unmapped.append({"item": item, "reason": reason})

    def context(self, qualifiers):
        identity = "context:" + key(qualifiers)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO test_contexts VALUES(?,?,?,?)",
                (
                    identity,
                    "原文给出的适用条件",
                    encode(qualifiers),
                    "specified"
                    if qualifiers.get("condition") or qualifiers.get("conditions")
                    else "partial",
                ),
            )
        return identity

    def entity(self, kind, name, namespace=None, **details):
        return self.store.create_entity(
            kind=kind,
            name=str(name),
            namespace=namespace or self.namespace,
            identity_key=normalized(name),
            topic_id=self.topic,
            details=details,
        )

    def commit(self, subject, kind, detail, quotes, qualifiers=None):
        qualifiers = qualifiers or {}
        if isinstance(quotes, str):
            quotes = [quotes]
        evidence = [
            {"segment_id": self.source.segment_key, "quote": self.source.quote(q)} for q in quotes
        ]
        if not evidence:
            raise ValueError("事实没有原文依据")
        identity = self.store.append_claim(
            topic_id=self.topic,
            subject_id=subject,
            kind=kind,
            detail=detail,
            evidence=evidence,
            context_id=self.context(qualifiers),
            scope_note=encode(qualifiers),
            extraction_run_id=self.run_id,
            assertion_type="source_statement",
            status="accepted",
        )
        self.claim_ids.add(identity)
        return identity

    def identity(self, entity):
        q = self.source.quote(entity.identity_quote)
        if entity.kind not in ENTITY_KINDS:
            raise ValueError("当前结构尚不支持该对象类型")
        if not entity.name.strip() or len(entity.name) > 300:
            raise ValueError("对象名称为空或过长")
        # Preserve original proper names for named products; avoid navigation-only associations.
        if entity.kind in {"product", "reference"} and re.search(r"[A-Za-z]", entity.name):
            name = entity.name.replace("™", "").replace("®", "")
            if normalized(name) not in normalized(q.replace("™", "").replace("®", "")):
                raise ValueError("身份引句没有明确包含产品或仓库名称")
        namespace = entity.namespace or self.namespace
        if namespace in {"shared_technology", "shared"}:
            namespace = "shared"
        if namespace == "shared" and entity.kind in {"product", "organization", "team", "person"}:
            namespace = self.namespace
        details = {"event_type": "source_reported"} if entity.kind == "event" else {}
        identity = self.entity(entity.kind, entity.name, namespace, **details)
        name_id = "name:" + key(identity, entity.name)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO entity_names(id,entity_id,name,name_type) "
                "VALUES(?,?,?,'canonical')",
                (name_id, identity, entity.name),
            )
        self.commit(identity, "identity", {"name_id": name_id}, q)
        self.entities[entity.entity_key] = identity
        self.entity_info[entity.entity_key] = entity
        return identity

    def metric(self, subject, label, value, quote, qualifiers=None):
        qualifiers = dict(qualifiers or {})
        spec = value if isinstance(value, dict) else {"value": value}

        def number(v):
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
                return None
            return v

        raw = spec.get("raw") or str(spec.get("value", ""))
        if not raw and spec.get("min") is None and spec.get("max") is None:
            raise ValueError("指标没有数值或原始规格")
        unit = spec.get("unit") or qualifiers.get("unit")
        metric = self.entity("metric", label, "shared")
        detail = dict(
            metric_id=metric,
            raw_value=str(raw or encode(spec)),
            numeric_value=number(spec.get("value")),
            min_value=number(spec.get("min")),
            max_value=number(spec.get("max")),
            raw_unit=str(unit) if unit else None,
            statistic=str(
                qualifiers.get("measurement_type") or qualifiers.get("statistic") or "unspecified"
            ),
            value_nature="vendor_claim" if "anduril.com" in self.source.url else "unspecified",
        )
        if spec.get("operator") in {"=", ">", ">=", "<", "<=", "approx"}:
            detail["comparator"] = spec["operator"]
        self.commit(subject, "metric", detail, quote, {**qualifiers, "original_value": value})

    def relation(self, subject, predicate, target, quote, qualifiers=None, field_id=None):
        detail = {"relation_type_id": predicate, "object_id": target}
        if field_id:
            detail["function_field_id"] = field_id
        self.commit(subject, "relation", detail, quote, qualifiers)

    def field(self, entity, field):
        subject = self.entities[entity.entity_key]
        spec = self.fields.get(field.field_id)
        if not spec or spec["subject_kind"] != entity.kind:
            raise ValueError("字段不存在或不适用于该对象")
        self.source.quote(field.quote)
        if spec["is_derived"]:
            raise ValueError("反向显示字段由已有关系生成，不重复填写")
        target = spec["target_table"]
        value, qualifiers = field.value, field.qualifiers
        if value is None or normalized(value) in {
            "未知",
            "暂无",
            "未公开",
            "不适用",
            "unknown",
            "—",
        }:
            raise ValueError("无证据的空值不能作为事实入库")
        if target == "field_values":
            self.commit(
                subject,
                "field",
                {
                    "field_id": field.field_id,
                    "value_json": encode(value),
                    "qualifiers_json": encode(qualifiers),
                },
                field.quote,
                qualifiers,
            )
        elif target == "identity_assertions":
            for name in value if isinstance(value, list) else [value]:
                name_id = "name:" + key(subject, name)
                with self.store.connect(write=True) as db:
                    db.execute(
                        "INSERT OR IGNORE INTO entity_names(id,entity_id,name,name_type) "
                        "VALUES(?,?,?,'alias')",
                        (name_id, subject, str(name)),
                    )
                self.commit(subject, "identity", {"name_id": name_id}, field.quote, qualifiers)
        elif target == "topic_entities":
            # Membership is already bound by the selected topic, not inferred as vendor adoption.
            return
        elif target == "metric_observations":
            self.metric(subject, spec["label"], value, field.quote, qualifiers)
        elif target == "product_capabilities":
            if not isinstance(value, bool):
                raise ValueError("能力字段需要明确真假值")
            if not value and not re.search(
                r"\b(not|no|without|cannot|doesn't)\b|不|无|未|无法", field.quote, re.I
            ):
                raise ValueError("否定能力缺少明确否定引句，保留待核")
            capability = self.entity("capability", spec["label"], "shared")
            self.commit(
                subject,
                "capability",
                {
                    "capability_id": capability,
                    "support_status": "supported" if value else "unsupported",
                    "function_field_id": field.field_id,
                },
                field.quote,
                qualifiers,
            )
        elif target == "participations":
            roles = {
                "product_company": "associated_organization",
                "product_partners": "associated_organization",
                "product_suppliers": "supplier",
                "product_users": "user",
            }
            for name in value if isinstance(value, list) else [value]:
                if len(str(name)) > 160:
                    raise ValueError("机构名称含有过多说明，需要拆分后核对")
                actor = self.entity("organization", name, "organization")
                self.commit(
                    actor,
                    "participation",
                    {"role_id": roles[field.field_id], "object_id": subject},
                    field.quote,
                    qualifiers,
                )
        elif target == "event_details":
            if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", str(value)):
                raise ValueError("日期精度或格式无效")
            event = self.entity(
                "event", f"{entity.name} · {spec['label']} · {value}", event_type=field.field_id
            )
            self.commit(
                event,
                "event",
                {
                    "occurred_at": value,
                    "date_precision": {4: "year", 7: "month", 10: "day"}[len(value)],
                    "description": spec["label"],
                },
                field.quote,
                qualifiers,
            )
            self.commit(
                subject,
                "participation",
                {"role_id": "event_subject", "object_id": event},
                field.quote,
                qualifiers,
            )
        elif target == "classifications":
            for label in value if isinstance(value, list) else [value]:
                term = "term:" + key(field.field_id, label)
                with self.store.connect(write=True) as db:
                    db.execute(
                        "INSERT OR IGNORE INTO terms(id,scheme,label) VALUES(?,?,?)",
                        (term, field.field_id, str(label)),
                    )
                self.commit(subject, "classification", {"term_id": term}, field.quote, qualifiers)
        elif target == "assessments":
            typ = (
                "advantage"
                if any(s in field.field_id for s in ["advantage", "differentiator"])
                else "limitation"
            )
            if "innovation" in field.field_id:
                typ = "innovation"
            self.commit(
                subject,
                "assessment",
                {
                    "assessment_type": typ,
                    "conclusion": "；".join(map(str, value))
                    if isinstance(value, list)
                    else str(value),
                },
                field.quote,
                qualifiers,
            )
        elif target == "maturity_assessments":
            levels = {
                "概念": "concept",
                "研发": "research",
                "研究": "research",
                "原型": "prototype",
                "试验": "testing",
                "测试": "testing",
                "量产": "production",
                "交付": "delivered",
                "实际使用": "in_use",
                "退役": "retired",
            }
            level = levels.get(str(value), value)
            self.commit(
                subject,
                "maturity",
                {"scale_id": "source_stage", "level": level, "rationale": field.quote},
                field.quote,
                qualifiers,
            )
        elif target in {"entity_relations", "technology_routes"}:
            mapping = {
                "product_parent_product_line": ("variant_of", "product", False),
                "product_predecessor": ("evolves_from", "product", False),
                "product_successor": ("evolves_from", "product", True),
                "product_derived_from": ("evolves_from", "product", False),
                "product_shared_platform": ("shares_platform_with", "product", False),
                "product_integrated_products": ("integrates", "product", False),
                "technology_parent_technology": ("subtechnology_of", "technology", False),
                "technology_upstream_technologies": ("upstream_of", "technology", True),
                "technology_downstream_technologies": ("upstream_of", "technology", False),
                "technology_predecessor": ("evolves_from", "technology", False),
                "technology_successor": ("evolves_from", "technology", True),
                "technology_alternative_routes": ("alternative_to", "route", False),
                "technology_technical_route": ("has_route", "route", False),
                "technology_route_branch": ("has_route", "route", False),
            }
            if field.field_id not in mapping:
                raise ValueError("该字段需要进一步确定关系类型，原文已保留")
            predicate, kind, reverse = mapping[field.field_id]
            for name in value if isinstance(value, list) else [value]:
                target_id = self.entity(
                    kind, name, "shared" if kind in {"technology", "route"} else entity.namespace
                )
                self.relation(
                    target_id if reverse else subject,
                    predicate,
                    subject if reverse else target_id,
                    field.quote,
                    qualifiers,
                )
        else:
            raise ValueError("该存储规则尚需专门转换，保留到未映射清单")

    def repeat(self, entity, group, fields):
        values = {f.field_id: f.value for f in fields}
        quotes = [f.quote for f in fields]
        qualifiers = {**fields[0].qualifiers, "row_fields": values}
        subject = self.entities[entity.entity_key]
        if group in {"components", "payloads"}:
            name = (
                values.get("product_module_name")
                or values.get("product_payload_name")
                or values.get("product_linked_product")
            )
            if not name:
                raise ValueError("组成行缺少模块或载荷名称")
            child = self.entity("component", name, entity.namespace)
            self.commit(
                subject,
                "composition",
                {
                    "component_id": child,
                    "row_key": str(qualifiers["row_key"]),
                    "role": "payload" if group == "payloads" else "component",
                    "quantity": values.get("product_quantity"),
                    "configuration": values.get("product_configuration_status"),
                    "replaceable": int(values["product_replaceable"])
                    if isinstance(values.get("product_replaceable"), bool)
                    else None,
                },
                quotes,
                qualifiers,
            )
            if values.get("product_payload_mass") is not None:
                self.metric(child, "载荷重量", values["product_payload_mass"], quotes, qualifiers)
        elif group == "metrics":
            if (
                not values.get("technology_metric_name")
                or values.get("technology_metric_value") is None
            ):
                raise ValueError("指标行缺少指标名或值")
            self.metric(
                subject,
                values["technology_metric_name"],
                {
                    "value": values["technology_metric_value"],
                    "unit": values.get("technology_metric_unit"),
                },
                quotes,
                qualifiers,
            )
        elif group == "dependencies":
            names = []
            for name in [
                "technology_dependency_technology",
                "technology_required_standards",
                "technology_required_protocols",
            ]:
                value = values.get(name, [])
                names.extend(value if isinstance(value, list) else [value])
            if not names:
                raise ValueError("依赖行缺少明确的技术、标准或协议对象")
            for name in names:
                self.relation(
                    subject,
                    "depends_on",
                    self.entity("technology", name, "shared"),
                    quotes,
                    qualifiers,
                )
        elif group == "implementations":
            name = values.get("technology_repository_name") or values.get(
                "technology_repository_url"
            )
            if not name:
                raise ValueError("参考实现行缺少仓库名或链接")
            reference = self.entity(
                "reference",
                name,
                "reference",
                repository_url=values.get("technology_repository_url"),
            )
            # This is a reference relationship, never a vendor adoption edge.
            self.relation(reference, "reference_for", subject, quotes, qualifiers)
        else:
            raise ValueError("未知重复行类型")

    def record(self, record):
        subject = self.entities[record.subject_key]
        q = record.qualifiers
        if record.kind == "metric":
            self.metric(subject, record.label, record.value, record.quote, q)
        elif record.kind == "participation":
            self.commit(
                subject,
                "participation",
                {"role_id": record.label, "object_id": self.entities[record.target_key]},
                record.quote,
                q,
            )
        elif record.kind == "event":
            date = str(record.value) if record.value is not None else None
            self.commit(
                subject,
                "event",
                {
                    "occurred_at": date,
                    "date_precision": {4: "year", 7: "month", 10: "day"}.get(
                        len(date or ""), "unknown"
                    ),
                    "description": record.label,
                },
                record.quote,
                q,
            )
        elif record.kind == "assessment":
            self.commit(
                subject,
                "assessment",
                {
                    "assessment_type": record.label,
                    "conclusion": str(record.value),
                    "comparator_entity_id": self.entities.get(record.target_key),
                },
                record.quote,
                q,
            )
        elif record.kind == "route_step":
            self.commit(
                subject,
                "route_step",
                {
                    "ordinal": int(q.get("ordinal", 1)),
                    "step_entity_id": self.entities.get(record.target_key),
                    "description": str(record.value),
                    "row_key": str(q.get("row_key", "step:1")),
                },
                record.quote,
                q,
            )
        elif record.kind == "maturity":
            self.commit(
                subject,
                "maturity",
                {
                    "scale_id": q.get("scale", "source_stage"),
                    "level": str(record.value),
                    "rationale": record.quote,
                },
                record.quote,
                q,
            )
        else:
            raise ValueError("该记录类型暂未支持，保留原文结果")

    def apply(self, payload):
        with self.store.batch():
            return self._apply(payload)

    def _apply(self, payload):
        try:
            parsed = DocumentExtraction.model_validate(payload)
        except ValueError:
            self.pending(payload, "抽取结果结构无效；已保留原始响应")
            return self.receipt()
        self.unmapped.extend(parsed.unmapped)
        for entity in parsed.entities:
            try:
                if entity.entity_key in self.entities:
                    raise ValueError("本篇重复对象编号")
                self.identity(entity)
            except (ValueError, sqlite3.IntegrityError) as exc:
                self.pending(entity, str(exc))
        for entity in parsed.entities:
            if entity.entity_key not in self.entities:
                continue
            rows = defaultdict(list)
            for field in entity.fields:
                definition = self.fields.get(field.field_id)
                if definition and definition["repeat_group"]:
                    if not field.qualifiers.get("row_key"):
                        self.pending(field, "重复行缺少row_key")
                    else:
                        rows[(definition["repeat_group"], str(field.qualifiers["row_key"]))].append(
                            field
                        )
                    continue
                try:
                    self.field(entity, field)
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    self.pending(field, str(exc))
            for (group, _), fields in rows.items():
                try:
                    for field in fields:
                        self.source.quote(field.quote)
                    self.repeat(entity, group, fields)
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    self.pending([f.model_dump() for f in fields], str(exc))
        for relation in parsed.relations:
            try:
                self.relation(
                    self.entities[relation.source_key],
                    relation.predicate,
                    self.entities[relation.target_key],
                    relation.quote,
                    field_id=relation.field_id,
                )
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                self.pending(relation, str(exc))
        for record in parsed.records:
            try:
                self.record(record)
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                self.pending(record, str(exc))
        return self.receipt(notes=parsed.notes)

    def receipt(self, notes=None):
        with self.store.connect() as db:
            ids = tuple(self.claim_ids)
            counts = dict(entities=len(self.entities), relations=0, fields=0, records=len(ids))
            if ids:
                for kind, count in db.execute(
                    "SELECT claim_kind,count(*) FROM claims WHERE id IN ("
                    + ",".join("?" for _ in ids)
                    + ") GROUP BY claim_kind",
                    ids,
                ):
                    if kind == "relation":
                        counts["relations"] += count
                    elif kind != "identity":
                        counts["fields"] += count
        return {
            "status": "partial" if self.unmapped else "completed",
            "counts": counts,
            "unmapped": self.unmapped,
            "notes": notes or [],
            "source_revision": self.source.digest,
            "source_id": self.source.source_id,
            "schema_version": VERSION,
        }
