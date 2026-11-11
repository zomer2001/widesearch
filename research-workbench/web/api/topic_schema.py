"""Topic-specific, provenance-preserving read projections.

Taxonomy membership is a browsing aid, never a factual graph edge. Source
attributes stay untouched; aliases only resolve how a field is displayed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter

SCHEMA_VERSION = 2
DEFAULT_TOPICS = (
    ("Anduril", "产品谱系、技术机制、技术路线与独立开源参考", ["Anduril Industries", "安杜里尔"]),
    ("Palantir", "软件平台、数据模型、部署方式与技术路线", ["Palantir Technologies", "帕兰提尔"]),
    ("华为", "芯片、通信、云平台及技术演进", ["Huawei", "Huawei Technologies"]),
)


def field(key, label, group, *aliases, unit=None):
    value = {
        "key": key,
        "label": label,
        "group": group,
        "type": "text",
        "aliases": list(dict.fromkeys([label, *aliases])),
    }
    if unit:
        value["unit"] = unit
    return value


COMMON = [
    field("version", "适用版本", "身份与时间", "版本", "型号版本", "release_version"),
    field(
        "announced_at",
        "首次公布时间",
        "身份与时间",
        "发布时间",
        "发布日",
        "首次发布时间",
        "release_date",
        "announcement_date",
    ),
    field(
        "effective_at", "信息适用时间", "身份与时间", "截至时间", "参数日期", "信息日期", "as_of"
    ),
    field("maturity", "成熟度依据", "验证与成熟度", "成熟度", "成熟阶段", "maturity_evidence"),
    field(
        "validation", "验证记录", "验证与成熟度", "试验记录", "测试依据", "验证依据", "test_results"
    ),
]
KINDS = {
    "product": [
        field("manufacturer", "厂商", "身份与时间", "制造商", "开发商", "供应商", "vendor"),
        field("family", "产品系列", "谱系与迭代", "产品家族", "所属系列", "product_family"),
        field("model", "型号", "谱系与迭代", "产品型号", "model_number"),
        field("predecessor", "前代型号", "谱系与迭代", "前代产品", "上一代", "predecessor_model"),
        field("iteration_changes", "迭代变化", "谱系与迭代", "版本变化", "升级内容", "迭代改进"),
        field(
            "parameters", "技术参数", "参数与条件", "参数", "关键参数", "性能参数", "specifications"
        ),
        field("operating_conditions", "工作条件", "参数与条件", "适用条件", "测试条件", "环境条件"),
        field(
            "deployment_status",
            "部署状态",
            "验证与成熟度",
            "交付状态",
            "部署情况",
            "服役状态",
            "deployment",
        ),
        field("delivery_date", "交付时间", "验证与成熟度", "首次交付时间", "delivery_at"),
    ],
    "technology": [
        field("mechanism", "技术原理", "机制与路线", "原理", "实现机制", "技术机制", "核心原理"),
        field("inputs", "输入", "机制与路线", "输入数据", "输入条件", "input"),
        field("outputs", "输出", "机制与路线", "输出数据", "输出结果", "output"),
        field("conditions", "适用条件", "参数与条件", "约束", "使用条件", "边界条件"),
        field("components", "组成模块", "机制与路线", "技术组成", "关键组件", "模块组成"),
        field("dependencies", "依赖条件", "机制与路线", "技术依赖", "外部依赖", "前置条件"),
        field("metrics", "评估指标", "参数与条件", "性能指标", "评价指标", "技术指标"),
        field("limitations", "技术局限", "参数与条件", "局限性", "限制", "未解决问题"),
        field("trl", "技术成熟度等级", "验证与成熟度", "TRL", "TRL等级"),
    ],
    "route": [
        field("problem", "目标问题", "机制与路线", "解决问题", "技术问题"),
        field("mechanism", "实现机制", "机制与路线", "技术原理", "机制"),
        field("steps", "关键步骤", "机制与路线", "实现步骤", "流程", "技术步骤"),
        field("inputs", "输入", "机制与路线", "输入数据", "输入条件"),
        field("outputs", "输出", "机制与路线", "输出结果"),
        field("dependencies", "依赖条件", "机制与路线", "前置条件", "技术依赖"),
        field("tradeoffs", "取舍与约束", "参数与条件", "约束", "权衡", "技术取舍"),
        field("alternatives", "备选路线", "机制与路线", "替代路线", "替代技术"),
    ],
    "reference": [
        field("repo_url", "项目地址", "复现单元", "仓库地址", "repository_url"),
        field("license", "许可证", "复现单元", "开源协议"),
        field("commit", "固定版本", "复现单元", "commit_sha", "固定commit", "ref"),
        field("code_paths", "代码入口", "复现单元", "代码路径", "入口文件"),
        field("inputs", "输入", "复现单元", "输入数据"),
        field("outputs", "输出", "复现单元", "输出结果"),
        field("dependencies", "环境与依赖", "复现单元", "依赖条件", "运行环境", "安装依赖"),
        field("reproduction_steps", "复现步骤", "复现单元", "运行步骤", "复现命令"),
        field("execution_status", "复现状态", "复现单元", "执行状态"),
        field("test_result", "复现结果", "复现单元", "运行结果", "测试结果"),
        field("relationship", "参考关系", "复现单元", "适用参考", "参考用途"),
    ],
}
SOFTWARE = [
    field("architecture", "系统架构", "软件与接口", "架构", "软件架构", "系统组成"),
    field("deployment_mode", "部署方式", "软件与接口", "部署模式", "部署形态"),
    field("interfaces", "接口与协议", "软件与接口", "接口", "API", "接口协议", "通信协议", "api"),
    field("data_model", "数据模型", "软件与接口", "数据结构", "数据格式", "数据模式", "ontology"),
    field("access_control", "权限与隔离", "软件与接口", "权限控制", "访问控制", "安全机制"),
    field("integration", "集成方式", "软件与接口", "系统集成", "接入方式", "连接器"),
    field("latency", "处理时延", "性能与资源", "时延", "延迟", unit="原始单位"),
    field("throughput", "处理吞吐量", "性能与资源", "吞吐量", "处理速率", unit="原始单位"),
    field("compute_requirements", "计算资源需求", "性能与资源", "算力需求", "硬件要求", "资源需求"),
]
LATTICE = [
    field("sensor_fusion", "传感器融合", "Lattice 技术点", "融合机制", "多传感器融合"),
    field("track_management", "目标航迹管理", "Lattice 技术点", "航迹管理", "目标管理"),
    field("task_planning", "任务规划机制", "Lattice 技术点", "任务规划", "自主任务规划"),
    field(
        "human_control",
        "人工控制机制",
        "Lattice 技术点",
        "人机协同",
        "人工介入",
        "human_in_the_loop",
    ),
    field("mesh_routing", "网络路由机制", "Lattice 技术点", "网状路由", "路由机制"),
    field(
        "bandwidth_policy", "带宽分配策略", "Lattice 技术点", "带宽管理", "数据优先级", "传输优先级"
    ),
    field("offline_behavior", "断连运行机制", "Lattice 技术点", "离线运行", "断网能力", "失联处置"),
    field("sdk_version", "SDK 版本", "软件与接口", "SDK版本", "sdk_release"),
]
UNDERWATER = [
    field("platform_type", "平台类型", "水下平台", "平台形态", "航行器类型"),
    field("length", "长度", "尺寸与性能", "艇长", "平台长度", unit="原始单位"),
    field("diameter", "直径", "尺寸与性能", "艇体直径", unit="原始单位"),
    field("weight", "重量", "尺寸与性能", "质量", "排水量", unit="原始单位"),
    field(
        "operating_depth",
        "工作深度",
        "尺寸与性能",
        "最大潜深",
        "潜深",
        "工作水深",
        "耐压深度",
        unit="原始单位",
    ),
    field("speed", "航速", "尺寸与性能", "最大航速", "速度", unit="原始单位"),
    field("range", "航程", "尺寸与性能", "最大航程", "续航距离", unit="原始单位"),
    field("endurance", "续航时间", "尺寸与性能", "续航", "任务时长", "续航能力", unit="原始单位"),
    field("payload", "载荷组成", "载荷与接口", "载荷", "任务载荷", "有效载荷"),
    field("payload_capacity", "载荷能力", "载荷与接口", "载荷重量", "载荷容量", unit="原始单位"),
    field("payload_interface", "载荷接口", "载荷与接口", "模块接口"),
    field("propulsion", "推进方式", "能源与导航", "推进系统", "动力系统"),
    field("energy_source", "能源与电池", "能源与导航", "能源", "电池", "供电方式"),
    field("navigation", "导航定位", "能源与导航", "导航方式", "定位方式", "导航系统"),
    field("communication", "水下通信", "能源与导航", "通信方式", "通信系统", "声学通信"),
    field("launch_recovery", "布放与回收", "水下平台", "部署回收", "布放方式", "回收方式"),
    field("pressure_hull", "耐压结构", "水下平台", "耐压壳体", "壳体材料"),
]
PALANTIR = [
    field("ontology_objects", "本体对象模型", "数据与本体", "对象类型", "本体模型"),
    field("ontology_relations", "本体关系模型", "数据与本体", "关系类型", "本体关系"),
    field("data_lineage", "数据血缘", "数据与本体", "血缘追踪", "数据溯源"),
    field("data_connectors", "数据连接器", "数据与本体", "数据源连接", "数据接入"),
    field("workflow", "工作流与动作", "分析与执行", "工作流", "行动编排", "actions"),
    field("model_integration", "模型接入", "分析与执行", "模型集成", "LLM接入"),
    field("auditability", "审计与治理", "分析与执行", "审计", "数据治理"),
    field("deployment_isolation", "部署隔离", "部署与运维", "隔离部署", "离线部署"),
]
HUAWEI_CHIP = [
    field("chip_architecture", "芯片架构", "芯片与计算", "处理器架构", "指令集"),
    field("process_node", "制造工艺", "芯片与计算", "工艺节点", "制程"),
    field("compute_precision", "计算精度", "芯片与计算", "支持精度", "数值精度"),
    field("compute_performance", "计算性能", "芯片与计算", "算力", "计算吞吐", unit="原始单位"),
    field("memory_capacity", "存储容量", "芯片与计算", "显存容量", "内存容量", unit="原始单位"),
    field("memory_bandwidth", "存储带宽", "芯片与计算", "显存带宽", "内存带宽", unit="原始单位"),
    field("interconnect", "互连方案", "芯片与计算", "互连协议", "芯片互连"),
    field("power_consumption", "功耗", "芯片与计算", "功率", "TDP", unit="原始单位"),
]
HUAWEI_TELECOM = [
    field("standards", "标准与版本", "通信系统", "通信标准", "标准版本"),
    field("frequency_bands", "频段", "通信系统", "工作频段", "支持频段"),
    field("channel_bandwidth", "信道带宽", "通信系统", "带宽", unit="原始单位"),
    field("antenna_configuration", "天线配置", "通信系统", "天线数量", "MIMO"),
    field("network_capacity", "网络容量", "通信系统", "连接容量", "用户容量", unit="原始单位"),
    field("energy_efficiency", "能效", "通信系统", "能耗", "节能指标"),
]

# id, label, parent, explicit lexical anchors. These are navigation categories.
TAXONOMIES = {
    "anduril": [
        ("lattice", "Lattice", None, r"\blattice\b"),
        (
            "lattice.network",
            "网络与数据",
            "lattice",
            r"lattice\s+(mesh|data|sdk|api)|网状|异构数据|数据优先|带宽|身份认证",
        ),
        ("lattice.autonomy", "任务自主", "lattice", r"lattice.*autonomy|自主任务|任务规划"),
        (
            "lattice.command",
            "指挥与控制",
            "lattice",
            r"lattice.*(command|control)|指挥控制|辅助决策",
        ),
        (
            "underwater",
            "水下系统",
            None,
            r"dive[ -]?(ld|xl)|ghost shark|copperhead|seabed sentry|水下|深海|潜航|\bauv\b|\buuv\b",
        ),
        (
            "underwater.vehicles",
            "潜航器与平台",
            "underwater",
            r"dive[ -]?(ld|xl)|ghost shark|copperhead|潜航器|\bauv\b|\buuv\b",
        ),
        (
            "underwater.sensing",
            "感知与节点",
            "underwater",
            r"seabed sentry|水下感知|深海.*节点|声呐",
        ),
        (
            "aviation",
            "无人航空",
            None,
            r"\bfury\b|\baltius\b|\bbolt\b|\bghost(?:-x)?\b(?!\s+shark)|\byfq\b|无人机|无人航空",
        ),
        ("sensing", "感知与通信", None, r"\bwisp\b|\bpulsar\b|\bsentry\b|传感器|雷达|电子战"),
        ("manufacturing", "制造与生产", None, r"\barsenal|制造|生产"),
    ],
    "palantir": [
        ("foundry", "Foundry", None, r"\bfoundry\b"),
        ("foundry.ontology", "本体与数据", "foundry", r"ontology|本体|数据血缘|数据连接器"),
        ("aip", "AIP", None, r"\baip\b|artificial intelligence platform"),
        ("gotham", "Gotham", None, r"\bgotham\b"),
        ("apollo", "Apollo", None, r"\bapollo\b"),
    ],
    "huawei": [
        ("computing", "芯片与计算", None, r"昇腾|鲲鹏|麒麟|ascend|kunpeng|kirin|芯片|处理器"),
        ("computing.ascend", "昇腾", "computing", r"昇腾|ascend|cann"),
        ("computing.kunpeng", "鲲鹏", "computing", r"鲲鹏|kunpeng"),
        ("telecom", "通信与网络", None, r"基站|通信|\b5g\b|\b6g\b|路由器|交换机|无线"),
        ("cloud", "云与软件", None, r"华为云|huawei cloud|harmony|鸿蒙|mindspore|云平台|opengauss"),
    ],
    "generic": [],
}
INTERNAL_KEYS = {
    "document_observations",
    "field_evidence",
    "field_overrides",
    "field_statuses",
    "independence",
    "namespace",
    "source_id",
    "source_ids",
    "evidence_ids",
    "technology_ids",
    "product_ids",
    "route_ids",
    "reference_ids",
    "source_refs",
    "sources",
    "id",
    "kind",
    "name",
    "category",
    "schema_version",
    "source_revision",
    "document_id",
    "library_id",
    "record_id",
    "extraction_id",
}
UNKNOWN = {
    "",
    "未知",
    "未记录",
    "待采集",
    "未采集",
    "待补充",
    "unknown",
    "null",
    "none",
    "n/a",
    "未提供",
    "原文未注明家族关系",
    "原文未说明",
    "原文未注明",
    "未找到",
}
NOT_PUBLIC = {"未公开", "不公开", "未披露", "not public", "not disclosed", "undisclosed"}
NOT_APPLICABLE = {"不适用", "not applicable"}
EXTRA_LABELS = {
    "overview": "补充概述",
    "description": "用途说明",
    "summary": "摘要",
    "aliases": "别名",
    "original_category": "原始类别",
    "status": "公开状态",
    "as_of": "截至日期",
    "release_notes": "版本说明",
    "support_policy": "支持政策",
}


def profile_for(topic):
    text = " ".join(
        [topic.get("title", ""), topic.get("namespace", ""), *topic.get("aliases", [])]
    ).casefold()
    for identity, pattern, label in [
        ("anduril", r"anduril|安杜里尔", "Anduril"),
        ("palantir", r"palantir|帕兰提尔", "Palantir"),
        ("huawei", r"huawei|华为", "华为"),
    ]:
        if re.search(pattern, text):
            return {"id": identity, "label": label, "schema_version": SCHEMA_VERSION}
    return {"id": "generic", "label": topic.get("title", "专题"), "schema_version": SCHEMA_VERSION}


def cluster_entities(profile, entities, relations, overrides=None):
    taxonomy = TAXONOMIES[profile["id"]]
    parents = {key: parent for key, _, parent, _ in taxonomy}
    memberships = {}
    for entity in entities:
        attrs = entity.get("attributes", {})
        # Avoid incidental page prose/navigation classifying an unrelated product.
        text = " ".join(
            str(v)
            for v in [
                entity.get("name", ""),
                entity.get("category", ""),
                attrs.get("family", ""),
                attrs.get("产品系列", ""),
                attrs.get("platform_type", ""),
            ]
        )
        memberships[entity["id"]] = {
            key for key, _, _, pattern in taxonomy if re.search(pattern, text, re.I)
        }
    # Existing evidence-backed relationships can place technical details alongside
    # their explicit product. Do not propagate through arbitrary connected chains.
    direct = copy.deepcopy(memberships)
    by_id = {entity["id"]: entity for entity in entities}
    for relation in relations:
        if relation.get("status") != "source_backed" or not relation.get("evidence_ids"):
            continue
        source, target = relation.get("source_id"), relation.get("target_id")
        if source not in by_id or target not in by_id:
            continue
        if relation.get("predicate") in {"uses_technology", "has_route"}:
            if by_id[source]["kind"] == "product" and by_id[target]["kind"] in {
                "technology",
                "route",
            }:
                memberships[target].update(direct[source])
    for identity, ids in (overrides or {}).items():
        if identity in memberships:
            memberships[identity] = set(ids) - {"unclassified"}
    for ids in memberships.values():
        for key in list(ids):
            parent = parents.get(key)
            while parent:
                ids.add(parent)
                parent = parents.get(parent)
        if not ids:
            ids.add("unclassified")
    clusters = [
        {
            "id": key,
            "label": label,
            "parent_id": parent,
            "entity_ids": [eid for eid, ids in memberships.items() if key in ids],
            "origin": "classification_rule",
        }
        for key, label, parent, _ in taxonomy
    ]
    clusters.append(
        {
            "id": "unclassified",
            "label": "待分类",
            "parent_id": None,
            "entity_ids": [eid for eid, ids in memberships.items() if "unclassified" in ids],
            "origin": "classification_rule",
        }
    )
    keywords = {
        "lattice": ["lattice"],
        "lattice.network": ["mesh", "sdk", "api", "网状", "数据模型"],
        "lattice.autonomy": ["autonomy", "任务自主"],
        "lattice.command": ["command", "指挥控制"],
        "underwater": ["水下", "潜航", "auv", "uuv", "dive", "copperhead", "ghost shark"],
        "underwater.vehicles": ["潜航器", "dive", "copperhead"],
        "underwater.sensing": ["seabed", "水下感知"],
        "aviation": ["航空", "无人机", "fury", "altius"],
        "sensing": ["感知", "传感器", "wisp"],
        "manufacturing": ["制造", "生产", "arsenal"],
        "foundry": ["foundry"],
        "foundry.ontology": ["ontology", "本体"],
        "aip": ["aip"],
        "gotham": ["gotham"],
        "apollo": ["apollo"],
        "computing": ["芯片", "计算"],
        "computing.ascend": ["昇腾", "ascend"],
        "computing.kunpeng": ["鲲鹏", "kunpeng"],
        "telecom": ["通信", "5g", "6g"],
        "cloud": ["云", "harmony", "鸿蒙"],
    }
    for item in clusters:
        item["count"] = len(item["entity_ids"])
        item["keywords"] = keywords.get(item["id"], [])
    return clusters, {key: sorted(value) for key, value in memberships.items()}


def fields_for(profile, entity, clusters):
    kind = entity.get("kind", "")
    fields = copy.deepcopy(KINDS.get(kind, []))
    if kind != "reference":
        fields += copy.deepcopy(COMMON)
    if kind in {"product", "technology", "route"}:
        if "lattice" in clusters:
            fields += copy.deepcopy(SOFTWARE + LATTICE)
        if "underwater" in clusters:
            # Whole-platform dimensions are irrelevant to component mechanisms.
            if kind == "product":
                fields += copy.deepcopy(UNDERWATER)
            else:
                fields += copy.deepcopy(
                    [
                        f
                        for f in UNDERWATER
                        if f["key"]
                        in {"operating_depth", "navigation", "communication", "pressure_hull"}
                    ]
                )
        if profile["id"] == "palantir":
            fields += copy.deepcopy(SOFTWARE + PALANTIR)
        if profile["id"] == "huawei":
            if "computing" in clusters:
                fields += copy.deepcopy(HUAWEI_CHIP)
            if "telecom" in clusters:
                fields += copy.deepcopy(HUAWEI_TELECOM)
            if "cloud" in clusters:
                fields += copy.deepcopy(SOFTWARE)
    return list({item["key"]: item for item in fields}.values())


def _signature(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold().strip()


def _status(value, explicit):
    if value is None or value == [] or value == {}:
        return "unknown"
    if isinstance(value, dict):
        if "value" in value:
            return _status(value["value"], explicit)
        states = [_status(item, explicit) for item in value.values()]
        return (
            "known"
            if "known" in states
            else next((state for state in states if state != "unknown"), "unknown")
        )
    if isinstance(value, list):
        states = [_status(item, explicit) for item in value]
        return (
            "known"
            if "known" in states
            else next((state for state in states if state != "unknown"), "unknown")
        )
    normalized = str(value).strip().casefold()
    if normalized in UNKNOWN:
        return "unknown"
    if normalized in NOT_PUBLIC:
        return "not_public" if explicit else "unknown"
    if normalized in NOT_APPLICABLE:
        return "not_applicable" if explicit else "unknown"
    return "known"


def _field_status(definition, value, explicit):
    if definition["key"] == "maturity" and isinstance(value, dict):
        # An explanatory basis cannot turn an explicitly unknown stage into a
        # maturity finding. Keep that basis intact in the displayed observation.
        for key in ("stage", "阶段", "成熟阶段", "成熟度阶段"):
            if key in value:
                return _status(value[key], explicit)
    return _status(value, explicit)


def _global_definitions():
    definitions = {}
    for item in [
        *COMMON,
        *(f for fields in KINDS.values() for f in fields),
        *SOFTWARE,
        *LATTICE,
        *UNDERWATER,
        *PALANTIR,
        *HUAWEI_CHIP,
        *HUAWEI_TELECOM,
    ]:
        if item["key"] not in definitions:
            definitions[item["key"]] = copy.deepcopy(item)
        else:
            existing = definitions[item["key"]]
            existing["aliases"] = list(dict.fromkeys([*existing["aliases"], *item["aliases"]]))
    return definitions


def _context(attributes):
    result = {}
    for canonical, aliases in {
        "version": ["version", "适用版本", "版本"],
        "effective_at": ["effective_at", "信息适用时间", "截至时间"],
        "operating_conditions": ["operating_conditions", "测试条件", "工作条件"],
    }.items():
        for key in aliases:
            if key in attributes and _status(attributes[key], False) == "known":
                result[canonical] = copy.deepcopy(attributes[key])
                break
    return result


def _comparable(left, right):
    """Known different versions/times/conditions are separate observations."""
    a, b = left.get("context", {}), right.get("context", {})
    return not any(_signature(a[key]) != _signature(b[key]) for key in a.keys() & b.keys())


def project_field(definition, entity, evidence_index):
    attrs = entity.get("attributes", {})
    aliases = [definition["key"], *definition.get("aliases", [])]
    mapping = entity.get("field_evidence", {})
    overrides = entity.get("field_overrides", {})
    observations = []
    manual_keys = [key for key in aliases if key in overrides]
    source_keys = []

    def add(key, value, ids, manual=False, context=None):
        available = [
            identity
            for identity in ids
            if identity in evidence_index and evidence_index[identity].get("available", True)
        ]
        if key not in source_keys:
            source_keys.append(key)
        item = {
            "value": copy.deepcopy(value),
            "source_key": key,
            "evidence_ids": sorted(set(available)),
            "manual": manual,
            "status": _field_status(definition, value, manual or bool(available)),
        }
        if context:
            item["context"] = context
        for old in observations:
            if (old["source_key"], old["value"], old["manual"], old.get("context")) == (
                key,
                value,
                manual,
                context,
            ):
                old["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(available))
                return
        observations.append(item)

    for key in aliases:
        if key in attrs:
            add(
                key,
                attrs[key],
                mapping.get(key, []),
                key in manual_keys or entity.get("origin") == "manual",
                context=_context(attrs) or None,
            )
    primary_observations = list(observations)
    for observation in attrs.get("document_observations", []):
        if not isinstance(observation, dict):
            continue
        oattrs = observation.get("attributes", {})
        omapping = observation.get("field_evidence", {})
        context = _context(oattrs)
        for key in aliases:
            if key in oattrs:
                # Entity-wide citations are not evidence for an individual field.
                add(
                    key,
                    oattrs[key],
                    omapping.get(key, []),
                    manual=key in observation.get("field_overrides", {})
                    or observation.get("origin") == "manual",
                    context=context or None,
                )
    # A legacy primary row can repeat a contextual observation without having
    # its own field citation. Prefer the contextual record; do not invent context
    # for a distinct original citation.
    observations = [
        item
        for item in observations
        if not (
            any(item is primary for primary in primary_observations)
            and not item["manual"]
            and not item.get("context")
            and any(
                other is not item
                and other.get("context")
                and _signature(other["value"]) == _signature(item["value"])
                and set(item["evidence_ids"]) <= set(other["evidence_ids"])
                for other in observations
            )
        )
    ]
    effective = (
        [
            item
            for item in observations
            if item["source_key"] in manual_keys
            and item["manual"]
            and any(item is primary for primary in primary_observations)
        ]
        if manual_keys
        else observations
    )
    for item in observations:
        item["active"] = any(item is active for active in effective)
    known = [item for item in effective if item["status"] != "unknown"]
    conflicting = any(
        _signature(left["value"]) != _signature(right["value"]) and _comparable(left, right)
        for index, left in enumerate(known)
        for right in known[index + 1 :]
    )
    status = "conflicting" if conflicting else known[0]["status"] if known else "unknown"
    best = known[0] if known else (effective[0] if effective else None)
    return {
        **copy.deepcopy(definition),
        "value": best["value"] if best else None,
        "status": status,
        "source_keys": source_keys,
        "evidence_ids": sorted({key for item in effective for key in item["evidence_ids"]}),
        "observations": observations,
        "manual": bool(manual_keys)
        or entity.get("origin") == "manual"
        or any(item["manual"] for item in effective),
    }


def _expand_details(entity):
    """Read old typed detail paragraphs as fields; never split/interpret prose."""
    projected = copy.deepcopy(entity)
    definitions = {}
    blocks = [
        (
            projected["attributes"],
            projected.setdefault("field_evidence", {}),
            projected.get("field_overrides", {}),
        )
    ]
    for observation in projected["attributes"].get("document_observations", []):
        if isinstance(observation, dict):
            blocks.append(
                (
                    observation.get("attributes", {}),
                    observation.setdefault("field_evidence", {}),
                    observation.get("field_overrides", {}),
                )
            )
    for attributes, evidence_map, overrides in blocks:
        raw = attributes.get("technical_details", [])
        if not isinstance(raw, list):
            continue
        expanded = False
        for item in raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("text"), str)
                or not item.get("label")
            ):
                continue
            label = str(item["label"])
            key = "detail_" + hashlib.sha256(label.encode()).hexdigest()[:16]
            definitions[key] = field(key, label, "已采技术细节")
            if key not in attributes:
                attributes[key] = item["text"]
                evidence_map[key] = list(item.get("source_ids", []))
            elif key not in overrides and attributes[key] == item["text"]:
                evidence_map[key] = sorted(
                    set(evidence_map.get(key, [])) | set(item.get("source_ids", []))
                )
            expanded = True
        if expanded:
            # Original entity remains unchanged; the readable fields replace its
            # duplicate JSON blob only in this display projection.
            attributes.pop("technical_details", None)
    return projected, list(definitions.values())


def build_topic_projection(topic, entities, relations, evidence, classification_overrides=None):
    profile = profile_for(topic)
    clusters, memberships = cluster_entities(profile, entities, relations, classification_overrides)
    evidence_index = {item["id"]: item for item in evidence}
    entity_fields, gaps = {}, []
    registry = _global_definitions()
    for original_entity in entities:
        entity, detail_definitions = _expand_details(original_entity)
        definitions = fields_for(profile, entity, memberships[entity["id"]]) + detail_definitions
        # A field already present in source data keeps its canonical identity
        # outside the domain whose template originally introduced it. This does
        # not add unrelated, unobserved missing fields to the object.
        for item in definitions:
            if item["key"] in registry:
                item["aliases"] = list(
                    dict.fromkeys([*item.get("aliases", []), *registry[item["key"]]["aliases"]])
                )
        used = {key for item in definitions for key in [item["key"], *item.get("aliases", [])]}
        observed = set(entity.get("attributes", {}))
        for observation in entity.get("attributes", {}).get("document_observations", []):
            if isinstance(observation, dict):
                observed.update(observation.get("attributes", {}))
        defined = {item["key"] for item in definitions}
        for key, item in registry.items():
            aliases = {key, *item.get("aliases", [])}
            if key not in defined and aliases.intersection(observed - used):
                definitions.append(copy.deepcopy(item))
                defined.add(key)
                used.update(aliases)
        # Keep every non-template source field accessible instead of silently
        # dropping its content when normalising an old extraction.
        definitions += [
            field(key, EXTRA_LABELS.get(key, key), "其他已采字段")
            for key in sorted(observed)
            if key not in used and key not in INTERNAL_KEYS
        ]
        values = [project_field(item, entity, evidence_index) for item in definitions]
        entity_fields[entity["id"]] = values
        for item in values:
            if item["status"] == "unknown":
                gaps.append(
                    {
                        "entity_id": entity["id"],
                        "entity_name": entity["name"],
                        "kind": entity["kind"],
                        "field": item["key"],
                        "label": item["label"],
                        "group": item["group"],
                        "cluster_ids": memberships[entity["id"]],
                    }
                )
    all_fields = [item for fields in entity_fields.values() for item in fields]
    counts = Counter(item["status"] for item in all_fields)
    quality = {
        key: counts[key]
        for key in ("known", "unknown", "not_public", "not_applicable", "conflicting")
    }
    quality.update(
        total=len(all_fields),
        with_field_evidence=sum(bool(item["evidence_ids"]) for item in all_fields),
        entities_without_field_evidence=sum(
            not any(f["evidence_ids"] for f in values) for values in entity_fields.values()
        ),
    )
    return {
        "profile": profile,
        "clusters": clusters,
        "entity_clusters": memberships,
        "classification_overrides": classification_overrides or {},
        "entity_classification_origin": {
            item["id"]: "manual"
            if item["id"] in (classification_overrides or {})
            else "classification_rule"
            for item in entities
        },
        "entity_fields": entity_fields,
        "quality": quality,
        "coverage": {
            "filled": counts["known"],
            "total": len(all_fields) - counts["not_applicable"],
            "missing": counts["unknown"],
        },
        "gaps": gaps,
    }


def extraction_field_guide(namespace: str, title: str = "", scope: str = "") -> dict:
    """Vocabulary for quote-checked extraction, not a request to invent empty facts."""
    profile = profile_for({"title": title or namespace, "namespace": namespace})
    text = " ".join([title, scope])
    domain_ids = [
        key for key, _, _, pattern in TAXONOMIES[profile["id"]] if re.search(pattern, text, re.I)
    ]
    parents = {key: parent for key, _, parent, _ in TAXONOMIES[profile["id"]]}
    for key in list(domain_ids):
        parent = parents.get(key)
        while parent:
            domain_ids.append(parent)
            parent = parents.get(parent)
    fields = {}
    for kind in KINDS:
        for definition in fields_for(profile, {"kind": kind}, domain_ids):
            item = fields.setdefault(definition["key"], {**definition, "kinds": []})
            if kind not in item["kinds"]:
                item["kinds"].append(kind)
    return {
        "profile": profile,
        "fields": list(fields.values()),
        "instruction": (
            "优先使用规范字段 key；只填写原文明示且有逐字段引句的值。"
            "保留单位、适用版本、日期与测试条件。不得由模板缺项推断未公开或不适用。"
        ),
    }
