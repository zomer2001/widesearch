"""Versioned extraction contracts and deterministic normalization; no model calls.

The existing catalog is retained. Explicitly reviewed dimensions are distinguished
from type-level templates: a large catalog is not a claim of semantic completeness.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date
from urllib.parse import urlparse

from searchos.knowledge_store.catalog import ENTITY_KINDS
from searchos.knowledge_store.database import encode

CONTRACT_VERSION = "field-contract-v2.1"

# Independent axes; old mixed fields remain readable, never rewritten in place.
NEW_FIELDS = [
    (
        "technology_computational_paradigms",
        "计算方法范式",
        "technology",
        "text_list",
        "3.principle",
        ["规则", "传统统计学习", "深度学习"],
        "实现所采用的计算方法范式；混合方法分别列出已有证据的范式。",
    ),
    (
        "technology_learning_modes",
        "学习方式",
        "technology",
        "text_list",
        "3.principle",
        [
            "监督学习",
            "无监督学习",
            "自监督学习",
            "半监督学习",
            "弱监督学习",
            "强化学习",
            "无需训练",
        ],
        "训练监督信号或学习机制，可多选；不与网络架构或算法家族混用。",
    ),
    (
        "technology_detection_pipeline",
        "检测流程",
        "technology",
        "short_text",
        "3.composition",
        ["单阶段", "两阶段"],
        "仅描述目标检测的阶段组织；Transformer 不属于此列。",
    ),
    (
        "technology_backbone_architectures",
        "主干架构",
        "technology",
        "text_list",
        "3.composition",
        None,
        "原文明确的模型主干架构，如 CNN、Transformer；不根据模型家族自行推断。",
    ),
    (
        "product_application_settings",
        "应用场所",
        "product",
        "text_list",
        "7.scenarios",
        None,
        "产品应用的具体场所类别，如港口、机场、边境；不同于运行环境和任务目的。",
    ),
    (
        "product_mission_purposes",
        "任务目的",
        "product",
        "text_list",
        "7.scenarios",
        None,
        "原文明确的任务目的，如监测、巡检、反制；目标类型和应用场所分别填写。",
    ),
]

OVERRIDES = {
    "product_operating_domains": {
        "definition": "产品自身运行所在的物理环境；不能用其探测目标所在环境代替。",
        "allowed_values": ["空中", "地面", "水面", "水下", "太空"],
        "not_to_confuse": ["product_target_domains", "product_application_settings"],
    },
    "product_target_domains": {
        "definition": "被感知或处理的目标所在环境；不是产品自身运行环境。",
        "allowed_values": ["空中", "地面", "水面", "水下", "太空"],
        "not_to_confuse": ["product_operating_domains"],
    },
    "product_target_types": {
        "definition": "作用对象类别，每项一个类别；目标所在环境、数量及型号另存。",
        "not_to_confuse": ["product_target_domains", "product_mission_purposes"],
    },
    "product_use_cases": {
        "definition": "来源明确的完整应用场景名称；可保留场景关联，具体场所/目的/目标另填独立列。",
        "not_to_confuse": [
            "product_application_settings",
            "product_mission_purposes",
            "product_target_types",
        ],
    },
    "technology_route_branch": {
        "definition": "兼容旧记录的混合路线分支列；新结果优先使用检测流程、主干架构等独立轴。",
        "deprecated_for_new_extraction": True,
        "not_to_confuse": ["technology_detection_pipeline", "technology_backbone_architectures"],
    },
    "technology_technical_route": {
        "definition": (
            "有独立名称的具体技术路线，通过 has_route 关联路线实体；"
            "规则/深度学习等范式填计算方法范式。"
        ),
        "not_to_confuse": ["technology_computational_paradigms", "technology_learning_modes"],
    },
    "technology_training_method": {
        "definition": "兼容旧版单值训练方式列；多个学习机制改填 technology_learning_modes。",
        "not_to_confuse": ["technology_learning_modes", "technology_backbone_architectures"],
    },
    "technology_method_family": {
        "definition": (
            "明确命名的算法或模型家族，作为可复用实体引用；不把具体版本和家族混为一个对象。"
        ),
        "not_to_confuse": ["technology_specific_model"],
    },
    "technology_specific_model": {
        "definition": "原文明确的具体算法或模型版本；版本不明确时不自行补齐。",
        "not_to_confuse": ["technology_method_family"],
    },
    "technology_compute_architecture": {
        "definition": "计算节点组织方式（集中/分布/混合），不是神经网络主干架构。",
        "allowed_values": ["集中", "分布", "混合"],
        "not_to_confuse": ["technology_backbone_architectures"],
    },
    "product_open_source_status": {
        "definition": "指定产品及版本的公开源代码状态；提供 SDK/API 不等于产品开源。",
        "allowed_values": ["闭源", "部分开源", "开源"],
        "not_to_confuse": ["product_supports_plugins"],
    },
    "product_supports_publish_subscribe": {
        "definition": (
            "原文明确声明该产品/版本已支持发布订阅通信模式。计划支持应保留待核，不能填 true。"
        ),
        "not_to_confuse": ["采用 DDS 的技术关系", "支持主题过滤"],
    },
    "product_detection_range": {
        "definition": "指定目标类型和条件下的探测距离；保留上下限及版本，不与通信/识别距离混用。",
        "required_context": [
            "target_type（原文给出时）",
            "conditions（原文给出时）",
            "version（原文给出时）",
        ],
    },
    "product_payload_mass": {
        "definition": (
            "某一载荷的质量；同一载荷使用相同 row_key，不与产品整机质量或最大载荷能力混用。"
        )
    },
    "technology_advantages": {
        "definition": "来源明确的优势评价，保留比较对象与适用条件；不能把分析推断当来源事实。"
    },
    "technology_mechanism": {
        "definition": "技术工作原理的来源表述；允许完整句子，不要求压缩成单词。"
    },
    "route_principle": {
        "definition": "这条具体路线的原理；与通用技术定义区分，保留关键条件与步骤。"
    },
}

SYNONYMS = {
    "空中": ["air", "aerial", "airborne"],
    "地面": ["land", "ground", "陆地"],
    "水面": ["surface", "sea surface", "水上"],
    "水下": ["underwater", "undersea"],
    "太空": ["space"],
    "单阶段": ["one-stage", "one stage", "single-stage", "single stage"],
    "两阶段": ["two-stage", "two stage", "双阶段"],
    "监督学习": ["supervised", "supervised learning", "监督"],
    "无监督学习": ["unsupervised", "unsupervised learning", "无监督"],
    "自监督学习": ["self-supervised", "self-supervised learning", "自监督"],
    "半监督学习": ["semi-supervised", "semi-supervised learning", "半监督"],
    "弱监督学习": ["weakly supervised", "弱监督"],
    "强化学习": ["reinforcement learning", "rl"],
    "无需训练": ["training-free", "training free"],
    "规则": ["rule-based", "rule based"],
    "传统统计学习": ["statistical learning"],
    "深度学习": ["deep learning"],
    "集中": ["centralized", "centralised"],
    "分布": ["distributed", "分布式"],
    "混合": ["hybrid"],
    "开源": ["open-source", "open source"],
    "闭源": ["closed-source", "closed source", "proprietary"],
    "部分开源": ["partially open-source", "partially open source"],
}
UNIT_ALIASES = {
    "hours": "h",
    "hour": "h",
    "小时": "h",
    "hrs": "h",
    "seconds": "s",
    "second": "s",
    "秒": "s",
    "milliseconds": "ms",
    "毫秒": "ms",
    "kilometers": "km",
    "kilometres": "km",
    "千米": "km",
    "公里": "km",
    "meters": "m",
    "metres": "m",
    "米": "m",
    "kilograms": "kg",
    "kilogram": "kg",
    "千克": "kg",
    "公斤": "kg",
    "watts": "W",
    "瓦": "W",
    "percent": "%",
    "赫兹": "Hz",
}
UNKNOWN = {"", "unknown", "n/a", "null", "none", "未知", "暂无", "未公开", "未记录", "不适用", "—"}
TYPE_RULES = {
    "boolean": "JSON true/false。无信息省略；false 需要明确否定，计划/部分支持不能压成 true。",
    "measurement": (
        "对象 {value,unit,raw} 或 {min,max,unit,raw}；可加 operator。"
        "原文未给单位不得猜测，保留目标、版本与条件。"
    ),
    "number": "有限 JSON 数字，不把布尔值当数字，不凭文本模糊表述估数。",
    "partial_date": "YYYY、YYYY-MM 或 YYYY-MM-DD；保留实际精度，不填补日期。",
    "text_list": "字符串数组，每项表达一个概念；无信息不输出空数组。",
    "short_text": "单个简短概念；需要多个值时不要用逗号拼接掩盖结构问题。",
    "text": "来源支持的完整表述，允许多句，保留必要限定。",
    "entity_reference": "原文明确命名的一个对象；其类型、版本与关系必须正确。",
    "entity_reference_list": "原文明确命名的多个对象；每项一个对象，不合并型号/版本。",
    "url": "原文提供或当前来源元数据确认的 http(s) URL，不构造不存在的链接。",
    "url_list": "URL 数组，逐项可追溯，不凭常识补仓库。",
}


def _new_rows():
    for fid, label, kind, typ, element, allowed, definition in NEW_FIELDS:
        yield dict(
            id=fid,
            subject_kind=kind,
            label=label,
            legacy_label=label,
            value_type=typ,
            repeat_group=None,
            is_function=0,
            is_derived=0,
            source_schema_version=CONTRACT_VERSION,
            element_id=element,
            target_table="field_values",
            mode="attribute",
            definition_json=encode(
                dict(
                    group="atomic_axes", allowed_values=allowed, examples=[], definition=definition
                )
            ),
        )


def build_contract(store):
    """Read-only complete catalog, plus the explicit v2 additions."""
    with store.connect() as db:
        rows = {
            r["id"]: dict(r)
            for r in db.execute(
                "SELECT f.*,m.element_id,m.target_table,m.mode FROM field_definitions f "
                "JOIN field_storage_mappings m ON m.field_id=f.id ORDER BY f.id"
            )
        }
        dimensions = [dict(r) for r in db.execute("SELECT * FROM dimensions ORDER BY id")]
        relations = [dict(r) for r in db.execute("SELECT * FROM relation_types ORDER BY id")]
        roles = [dict(r) for r in db.execute("SELECT * FROM participation_roles ORDER BY id")]
    for row in _new_rows():
        rows.setdefault(row["id"], row)
    fields = []
    for row in sorted(rows.values(), key=lambda r: r["id"]):
        spec = json.loads(row.pop("definition_json"))
        override = OVERRIDES.get(row["id"], {})
        allowed = override.get("allowed_values", spec.get("allowed_values"))
        field = dict(
            row,
            field_id=row["id"],
            group=spec.get("group", ""),
            definition=override.get(
                "definition",
                spec.get("definition")
                or f"记录该{row['subject_kind']}对象的“{row['label']}”；取值须由对应原文明确支持。",
            ),
            type_rule=TYPE_RULES.get(row["value_type"], "遵循原类型约束"),
            allowed_values=allowed,
            vocabulary_policy="closed" if allowed is not None else "open",
            synonyms={k: v for k, v in SYNONYMS.items() if allowed and k in allowed},
            examples=spec.get("examples", []),
            examples_are_restrictions=False,
            unit_rule=spec.get("unit_rule")
            or (
                "保留原始单位；仅规范同义单位拼写，不改变数值。"
                if row["value_type"] == "measurement"
                else None
            ),
            required_context=override.get(
                "required_context", ["version/time/conditions：来源给出时必须保留"]
            ),
            not_to_confuse=override.get("not_to_confuse", []),
            definition_status="reviewed"
            if row["id"] in OVERRIDES or row["source_schema_version"] == CONTRACT_VERSION
            else "type_template",
            source_required=True,
            unknown_policy="omit_with_processing_status",
            deprecated_for_new_extraction=override.get("deprecated_for_new_extraction", False),
        )
        fields.append(field)
    payload = dict(
        version=CONTRACT_VERSION,
        dimensions=dimensions,
        entity_kinds=[{"id": kind, "label": detail[0]} for kind, detail in ENTITY_KINDS.items()],
        fields=fields,
        relation_types=relations,
        participation_roles=roles,
        policies={
            "missing_is_false": False,
            "infer_adoption": False,
            "infer_trl": False,
            "model_generated_sql": False,
            "repeat_groups": "相同对象同一重复行共享 qualifiers.row_key",
            "semantic_completeness": (
                "type_template 字段仅完成类型级契约；歧义内容进入 unmapped，不强填。"
            ),
        },
    )
    payload["sha256"] = hashlib.sha256(encode(payload).encode()).hexdigest()
    return payload


def install_contract(store):
    """Install additions on an explicitly supplied staging/committing store."""
    contract = build_contract(store)
    by_id = {f["id"]: f for f in contract["fields"]}
    with store.connect(write=True) as db:
        for row in _new_rows():
            definition = dict(by_id[row["id"]])
            db.execute(
                "INSERT OR IGNORE INTO field_definitions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["subject_kind"],
                    row["label"],
                    row["legacy_label"],
                    row["value_type"],
                    None,
                    0,
                    0,
                    CONTRACT_VERSION,
                    encode(definition),
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO field_storage_mappings VALUES(?,?,?,?,?)",
                (
                    row["id"],
                    row["element_id"],
                    "field_values",
                    "v2 独立语义轴；有原文支持才填写",
                    "attribute",
                ),
            )
    return contract


def _measurement(value):
    if not isinstance(value, dict):
        raise ValueError("测量字段必须有独立数值、单位及原始表达")
    result = copy.deepcopy(value)
    keys = ("value", "min", "max")
    present = [k for k in keys if result.get(k) is not None]
    if not present or ("value" in present and len(present) > 1):
        raise ValueError("测量必须是单值或范围，不能混填")
    for k in present:
        if (
            isinstance(result[k], bool)
            or not isinstance(result[k], (int, float))
            or not math.isfinite(result[k])
        ):
            raise ValueError("测量值必须为有限数字")
    if (
        result.get("min") is not None
        and result.get("max") is not None
        and result["min"] > result["max"]
    ):
        raise ValueError("测量下限大于上限")
    if result.get("operator") not in (None, "=", ">", ">=", "<", "<=", "approx"):
        raise ValueError("测量限定符无效")
    if result.get("unit") is not None:
        if not isinstance(result["unit"], str) or not result["unit"].strip():
            raise ValueError("单位必须是非空字符串或省略")
        unit = result["unit"].strip()
        result["unit"] = UNIT_ALIASES.get(unit.casefold(), unit)
    return result


def _value(value, spec):
    typ = spec["value_type"]
    if value is None or (isinstance(value, str) and value.strip().casefold() in UNKNOWN):
        raise ValueError("无证据的未知值应省略，不能作为事实")
    if typ == "boolean":
        if not isinstance(value, bool):
            raise ValueError("布尔列只接受 JSON true/false")
    elif typ == "measurement":
        return _measurement(value)
    elif typ == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("数值列只接受有限数字")
    elif typ.endswith("_list"):
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(v, str) or v.strip().casefold() in UNKNOWN for v in value)
        ):
            raise ValueError("列表列需要非空字符串数组")
        value = list(dict.fromkeys(v.strip() for v in value))
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("文本/日期/对象引用列需要非空字符串")
        value = value.strip()
    if typ == "partial_date":
        if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
            raise ValueError("日期只能保留 YYYY、YYYY-MM 或 YYYY-MM-DD 精度")
        date.fromisoformat(value + {4: "-01-01", 7: "-01", 10: ""}[len(value)])
    if typ in {"url", "url_list"}:
        for u in value if isinstance(value, list) else [value]:
            parsed = urlparse(u)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("来源链接必须是完整 http(s) URL")
    allowed = spec.get("allowed_values")
    if allowed is not None and typ != "boolean":
        aliases = {str(k).casefold(): k for k in allowed}
        for k, alternatives in spec.get("synonyms", {}).items():
            aliases.update({a.casefold(): k for a in alternatives})
        vals = value if isinstance(value, list) else [value]
        normalized = []
        for v in vals:
            if str(v).casefold() not in aliases:
                raise ValueError("值不在当前词表中；保留待映射，不强套近义词")
            normalized.append(aliases[str(v).casefold()])
        value = list(dict.fromkeys(normalized)) if isinstance(value, list) else normalized[0]
    return value


def normalize_outputs(store, outputs, source):
    """Return safe typed candidates and explicit diagnostics, without writing."""
    return normalize_by_contract(build_contract(store), outputs, source)


def normalize_by_contract(contract, outputs, source):
    """Use the same contract checks for offline packages and SQL previews.

    Unsupported facts are removed only from the returned copy, with their full
    candidate preserved in diagnostics. Callers must surface every diagnostic.
    """
    from api.research_mapping import DocumentExtraction

    specs = {f["id"]: f for f in contract["fields"]}
    kinds = {k["id"] for k in contract["entity_kinds"]}
    relations = {r["id"]: r for r in contract["relation_types"]}
    roles = {r["id"]: r for r in contract["participation_roles"]}
    normalized, issues = [], []

    def issue(path, raw, reason):
        issues.append(dict(path=path, code="normalization_rejected", reason=str(reason), raw=raw))

    def allowed_types(definition, key):
        values = definition.get(key, definition.get(key + "_json", []))
        return set(json.loads(values) if isinstance(values, str) else values)

    for oi, raw in enumerate(outputs):
        output = DocumentExtraction.model_validate(raw).model_dump()
        counts = {}
        for entity in output["entities"]:
            counts[entity["entity_key"]] = counts.get(entity["entity_key"], 0) + 1
        kept_entities = []
        for ei, entity in enumerate(output["entities"]):
            try:
                if entity["kind"] not in kinds:
                    raise ValueError(f"未知对象类型：{entity['kind']}")
                if not entity["entity_key"].strip() or counts[entity["entity_key"]] != 1:
                    raise ValueError("局部实体编号必须非空且唯一")
                if not entity["name"].strip() or len(entity["name"]) > 300:
                    raise ValueError("对象名称为空或过长")
                source.quote(entity["identity_quote"])
            except ValueError as exc:
                issue(f"outputs[{oi}].entities[{ei}]", entity, exc)
                continue
            accepted = []
            for fi, field in enumerate(entity["fields"]):
                path = f"outputs[{oi}].entities[{ei}].fields[{fi}]"
                try:
                    spec = specs.get(field["field_id"])
                    if not spec or spec["subject_kind"] != entity["kind"] or spec["is_derived"]:
                        raise ValueError("字段不适用于此对象或为反向派生字段")
                    source.quote(field["quote"])
                    value = _value(field["value"], spec)
                    if spec["repeat_group"] and not field["qualifiers"].get("row_key"):
                        raise ValueError("重复行需要 qualifiers.row_key")
                    if spec["value_type"] == "boolean":
                        quote = field["quote"]
                        if re.search(
                            r"\b(planned|plans? to|will support|expected to|partially supports?)\b"
                            r"|计划|拟支持|将支持|部分支持",
                            quote,
                            re.I,
                        ):
                            raise ValueError("计划或部分支持不能写成已支持/不支持布尔事实")
                        if value is False and not re.search(
                            r"\b(not|no|without|cannot|doesn't)\b|不|无|未|无法", quote, re.I
                        ):
                            raise ValueError("false 缺少明确否定证据")
                    if value != field["value"]:
                        field["qualifiers"]["original_value"] = field["value"]
                    field["value"] = value
                    accepted.append(field)
                except (ValueError, TypeError) as exc:
                    issue(path, field, exc)
            entity["fields"] = accepted
            kept_entities.append(entity)
        output["entities"] = kept_entities
        entities = {entity["entity_key"]: entity for entity in kept_entities}
        kept_relations = []
        for ri, relation in enumerate(output["relations"]):
            try:
                source.quote(relation["quote"])
                definition = relations.get(relation["predicate"])
                if definition is None:
                    raise ValueError(f"未知关系类型：{relation['predicate']}")
                subject = entities.get(relation["source_key"])
                target = entities.get(relation["target_key"])
                if subject is None or target is None:
                    raise ValueError("关系两端必须引用同一输出中的有效实体编号")
                if subject["kind"] not in allowed_types(definition, "source_kinds"):
                    raise ValueError("关系起点对象类型不符合契约")
                if target["kind"] not in allowed_types(definition, "target_kinds"):
                    raise ValueError("关系终点对象类型不符合契约")
                if relation["field_id"]:
                    field = specs.get(relation["field_id"])
                    if (
                        not field
                        or field["subject_kind"] != "product"
                        or not (
                            field["is_function"]
                            or field["value_type"] == "boolean"
                            or field["id"].endswith("_other_features")
                        )
                    ):
                        raise ValueError("关系附带的功能字段编号无效")
                kept_relations.append(relation)
            except ValueError as exc:
                issue(f"outputs[{oi}].relations[{ri}]", relation, exc)
        output["relations"] = kept_relations
        kept = []
        for ri, record in enumerate(output["records"]):
            try:
                source.quote(record["quote"])
                subject = entities.get(record["subject_key"])
                target = entities.get(record["target_key"])
                if subject is None or (record["target_key"] and target is None):
                    raise ValueError("记录引用了不存在或无效的实体编号")
                if record["kind"] == "metric":
                    if (
                        subject["kind"]
                        not in {
                            "technology",
                            "route",
                            "product",
                            "component",
                            "process",
                            "capability",
                        }
                        or not record["label"].strip()
                    ):
                        raise ValueError("指标主体类型或指标名称无效")
                    before = record["value"]
                    record["value"] = _measurement(before)
                    if record["value"] != before:
                        record["qualifiers"]["original_value"] = before
                elif record["kind"] == "participation":
                    role = roles.get(record["label"])
                    if role is None:
                        raise ValueError(f"未知参与角色：{record['label']}")
                    if target is None:
                        raise ValueError("参与记录需要有效 target_key")
                    if subject["kind"] not in allowed_types(role, "subject_kinds") or (
                        target["kind"] not in allowed_types(role, "object_kinds")
                    ):
                        raise ValueError("参与角色的主体/对象类型不符合契约")
                elif record["kind"] == "event":
                    if subject["kind"] != "event" or not record["label"].strip():
                        raise ValueError("事件记录的主体必须是事件，且需要事件说明")
                    if record["value"] is not None:
                        record["value"] = _value(record["value"], {"value_type": "partial_date"})
                elif record["kind"] == "assessment":
                    if record["label"] not in {
                        "technical_level",
                        "advantage",
                        "limitation",
                        "performance_change",
                        "route_feature",
                        "route_status",
                        "innovation",
                    }:
                        raise ValueError("评价类型不符合契约")
                    _value(record["value"], {"value_type": "text"})
                elif record["kind"] == "route_step":
                    q = record["qualifiers"]
                    ordinal = q.get("ordinal")
                    if (
                        subject["kind"] != "route"
                        or not isinstance(ordinal, int)
                        or (isinstance(ordinal, bool) or ordinal < 1 or not q.get("row_key"))
                    ):
                        raise ValueError("路线步骤需要路线主体、正整数 ordinal 和 row_key")
                    _value(record["value"], {"value_type": "text"})
                elif record["kind"] == "maturity":
                    scale = record["qualifiers"].get("scale", "source_stage")
                    level = record["value"]
                    levels = {
                        "trl": {str(n) for n in range(1, 10)},
                        "source_stage": {
                            "concept",
                            "research",
                            "prototype",
                            "testing",
                            "production",
                            "delivered",
                            "in_use",
                            "retired",
                        },
                    }
                    if (
                        scale not in levels
                        or isinstance(level, bool)
                        or str(level) not in levels[scale]
                    ):
                        raise ValueError("成熟度量表或等级无效")
                else:
                    raise ValueError(f"未知记录类型：{record['kind']}")
                kept.append(record)
            except (ValueError, TypeError) as exc:
                issue(f"outputs[{oi}].records[{ri}]", record, exc)
        output["records"] = kept
        normalized.append(output)
    return normalized, issues
