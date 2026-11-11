#!/usr/bin/env python3
# ruff: noqa: E501
"""Apply the source-grounded Chinese editorial pass to this reader artifact.

This makes no model or network calls. Raw model caches and the pre-edit artifact
are retained. The review records every changed field and its archived evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_anduril_reader as builder  # noqa: E402

PRODUCT_EDITS = {
    "Lattice Mesh": {
        "category": "分布式网状数据网络",
        "details": [
            (
                "网络定位",
                "厂商将 Lattice Mesh 描述为去中心化网状网络，用于跨军种、域、平台和远距离分发数据，面向战术边缘的偏远、网络退化与低带宽环境。",
            ),
            (
                "分布式数据分发",
                "厂商称系统在自有、政府与第三方节点间动态确定数据优先级并进行分发。",
            ),
            (
                "异构数据归一化",
                "厂商介绍，系统实时集成并归一化异构数据，转换不同军种、平台与机器人的数据表述。",
            ),
            (
                "受限带宽下的优先传输",
                "原文描述对数据进行分类、评估与优先排序，以在受限带宽下向相应人员传递所需数据。",
            ),
            (
                "传输与访问控制",
                "原文列出身份认证、行业标准加密和访问授权，用于跨安全分级的数据传输；未披露具体加密套件与跨域审批实现。",
            ),
        ],
        "limitations": [
            "未披露数据模型、路由决策算法、断联重连策略和独立性能测量。",
            "本次选段没有可用于认定试验、交付或使用阶段的具体声明。",
        ],
    },
    "Lattice Command & Control": {
        "category": "指挥控制软件",
        "details": [
            (
                "统一态势显示",
                "厂商称系统将大量传感器和数据源的数据解析为共同态势图，并在单一界面中显示。",
            ),
            (
                "辅助决策",
                "原文称系统使用人工智能和深度学习模型向操作员提供决策建议；未说明具体模型结构、训练方式与决策准确率。",
            ),
            (
                "跨域系统接入",
                "厂商称系统可跨域集成和管理传感器、效应器、网络与任务系统，公开规模描述为数千个。",
            ),
            ("第三方与既有系统集成", "原文描述将第三方、既有和自主系统接入同一指挥控制任务引擎。"),
            (
                "网状网络传输",
                "原文将任务关键数据与战术边缘之间的传输归于智能网状网络；其可靠性与安全性是厂商公开描述。",
            ),
            (
                "人机界面与机器间任务下达",
                "原文描述以人机界面管理有人与无人系统，将多路数据融合为操作提示，并通过机器间任务下达执行操作。",
            ),
            (
                "分布式终端部署",
                "原文介绍利用 Lattice Mesh 连接战术边缘与作战中心，并支持笔记本、平板和操作员佩戴式终端等部署形态。",
            ),
            (
                "双向接入与共同态势图",
                "原文描述通过 Lattice SDK 双向接入既有与新一代传感器、效应器、网络和任务系统，形成共同态势图。",
            ),
        ],
        "limitations": [
            "未公开具体融合算法、建议生成模型、人机权限分配和规模化性能测试条件。",
            "公开功能介绍不足以认定本产品的具体交付或使用阶段。",
        ],
    },
    "Wisp": {
        "category": "被动红外感知系统",
        "details": [
            (
                "厂商公布的探测距离",
                "原文参数表列出：徒步人员 5 千米、车辆 15 千米、无人机 20 千米、商用飞机 150 千米。选段未给出目标特征、气象、虚警率或判定标准。",
            ),
            ("传感器视场", "原文参数表列出水平视场 360 度、垂直视场 125 度。"),
            (
                "外形尺寸",
                "原文列出传感器尺寸为 15×15×22 英寸，处理器为 19×8×26 英寸；这是厂商公开参数。",
            ),
        ],
        "limitations": [
            "原文未披露红外处理、检测与跟踪算法。",
            "厂商称被动工作时不发射信号；原文的绝对抗干扰表述没有独立试验条件与结果支持，不能据此认定不受干扰。",
            "探测距离缺少环境与判定条件，也未形成有引句支持的成熟度判断。",
        ],
    },
    "Menace-I": {
        "category": "可部署边缘计算基础设施",
        "details": [
            ("指挥任务用途", "原文参数表将指挥控制与出动生成列为任务用途。"),
            (
                "安全规范声明",
                "原文列出 T-SCIF/SAPF，并声明符合 ICD 705 与 TEMPEST；本条记录厂商声明，不代表独立认证审查。",
            ),
            ("电源配置", "原文参数表列出外接电源、多燃料发电机与电池备份。"),
            ("计算容量条目一", "原文计算配置表列出 40U 的机架容量。"),
            ("计算任务用途", "原文将人工智能工厂、传感器融合与目标指示列为计算任务用途。"),
            (
                "计算容量条目二",
                "同一参数表另列出 168U 的机架容量；该数值应与原文对应配置一并阅读。",
            ),
        ],
        "limitations": [
            "40U 与 168U 来自不同配置条目，不能将二者相加或套用于所有配置。",
            "未披露实际负载性能、断联运行边界和安全规范的独立认证材料。",
            "原文累计部署时间等厂商自述不能换算为技术成熟度等级。",
        ],
    },
    "Seabed Sentry": {
        "category": "海底感知与通信节点",
        "details": [
            (
                "厂商公布的系统参数",
                "原文列出任务寿命为数月至数年、额定深度大于 500 米、通信为低频/甚低频声通信中继，载荷容积大于 0.5 立方米。上述参数未附独立测试条件。",
            ),
        ],
        "limitations": [
            "未公开水声链路预算、网络协议、定位误差、自校准算法与实测续航条件。",
            "原文设计描述不能单独认定完成试验、交付或实际使用。",
        ],
    },
    "Lattice": {
        "category": "多源数据集成与公共安全软件",
        "details": [
            (
                "数据集成层",
                "原文介绍将分散传感器、数据流和系统的数据汇入单一集成层，并用人工智能、机器学习与传感器及数据处理筛选高价值信息。",
            ),
        ],
        "limitations": [
            "边境监控、基础设施巡检和搜救属于原文列举的应用场景，不作为具体技术路线计数。",
            "未公开具体算法、性能指标及独立效果验证。",
        ],
    },
    "ArsenalOS": {
        "category": "数字制造与生产协同软件",
        "details": [
            (
                "制造信息交接问题",
                "原文认为，设计在相互割裂的系统间需要重复录入、重绘和重新解释，使制造交接耗时且容易出错。",
            ),
            (
                "统一生产信息环境",
                "厂商介绍，ArsenalOS 将工程师的初始设计与工厂技术人员的执行连接到同一集成系统，贯通生产阶段。",
            ),
            (
                "制造链阶段定义",
                "原文将制造链划分为需求定义、设计、采购、装配、测试与部署六个阶段。",
            ),
            (
                "设计版本变化处理",
                "原文称系统支持设计在学习阶段保持可修改、量产阶段锁定，并在现场需要变化时重新开放；未提供量化成本验证。",
            ),
            (
                "自研与第三方系统协同",
                "原文介绍以自研 Forge 承担制造执行、维护与质量功能，并结合商用第三方系统，在统一环境中连接设计与现场保障。",
            ),
        ],
        "limitations": [
            "公开材料说明的是制造软件集成与版本流转思路，未披露完整数据模型、接口和性能测量。",
            "本次选段没有可用于认定具体交付或使用规模的阶段声明。",
        ],
    },
}

TECHNOLOGY_EDITS = [
    (
        "分布式网状数据分发",
        "厂商介绍，以去中心化网状结构在自有、政府和第三方节点间动态确定数据优先级并分发数据。",
        "未披露拓扑维护、路由算法与故障恢复实现。",
    ),
    (
        "异构数据归一化",
        "原文描述实时集成与归一化异构数据，转换不同服务和平台的数据表述。",
        "未披露统一数据模型、字段映射和语义冲突处理机制。",
    ),
    (
        "受限带宽下的数据优先排序",
        "原文描述先对数据分类、评估和确定优先级，再在受限带宽环境中向相应人员分发。",
        "未给出优先级规则、拥塞条件和时延测量。",
    ),
    (
        "身份认证、加密与访问授权",
        "原文列出身份认证、行业标准加密与访问授权，作为数据传输的控制措施。",
        "未披露具体密码套件、密钥管理与跨安全域处理实现。",
    ),
    (
        "人工智能辅助决策",
        "厂商称 Lattice 使用人工智能和深度学习模型，为动态环境中的操作员提供决策建议。",
        "未公开具体模型结构、训练数据及决策效果验证。",
    ),
    (
        "智能网状网络接入",
        "原文将战术边缘与任务系统之间的数据传输归于智能网状网络。",
        "原文仅作能力描述，未给出可靠性与安全性试验结果。",
    ),
    (
        "被动红外探测与跟踪",
        "厂商介绍，Wisp 使用被动红外探测与跟踪提供大范围态势感知。",
        "未公开算法；不能由被动工作推导不受干扰或任何环境下均能达到标称距离。",
    ),
    (
        "可部署数据中心集成",
        "原文描述通过冷却、电力和移动性支撑边缘高强度人工智能负载，或恢复受损的固定站点数据基础设施。",
        "未给出负载基准、能耗与恢复时间测量。",
    ),
    (
        "任务指挥基础设施集成",
        "原文描述通过集成工作站、通信、计算、网络、安全和电力，将指挥应用扩展到前沿位置。",
        "未披露各子系统接口、配置边界和独立集成测试结果。",
    ),
    (
        "深海耐压壳体与节点集成",
        "原文介绍以承压碳纤维壳体集成感知、通信和计算能力，面向深海环境。",
        "未公开壳体验证条件、器件选型与可靠性试验数据。",
    ),
    (
        "开放传感器与载荷接入",
        "原文描述采用开放系统架构，接入自有或第三方传感器与载荷。",
        "未披露接入协议、接口定义与互操作测试。",
    ),
    (
        "集成层信息筛选",
        "原文称在统一集成层使用人工智能、机器学习及传感器与数据处理，向用户筛选高价值信息。",
        "没有具体模型、融合算法、筛选指标与独立效果数据。",
    ),
    (
        "制造生产阶段贯通",
        "原文介绍在同一集成系统中连接工程师的初始设计与工厂技术人员的生产执行。",
        "未披露生产数据接口、完整架构与量化成效。",
    ),
]


def review(directory: Path = builder.DEFAULT_READER_ROOT) -> dict:
    path = directory / "knowledge.json"
    raw = path.read_bytes()
    original_hash = hashlib.sha256(raw).hexdigest()
    before = json.loads(raw)
    data = copy.deepcopy(before)
    service = builder.LocalLibraryService(ROOT / "knowledge_base")
    source_map = {item["id"]: item for item in data["sources"]}
    products = {item["name"]: item for item in data["products"]}
    old_routes = {item["name"]: item for item in data["routes"]}

    for name, edit in PRODUCT_EDITS.items():
        product = products[name]
        if len(product["technical_details"]) != len(edit["details"]):
            raise ValueError(f"Unexpected technical detail count: {name}")
        product["category"] = edit["category"]
        product["family"] = "原文未注明家族关系"
        product["limitations"] = edit["limitations"]
        for detail, (label, text) in zip(
            product["technical_details"], edit["details"], strict=True
        ):
            detail.update(label=label, text=text)
        if product["maturity"]["source_ids"]:
            product["maturity"]["basis"] = (
                "厂商原文称其作为既有系统的集成与网络层，并自动化数百个已部署机器人系统；这是厂商使用情况声明，未作独立核验。"
            )
        else:
            product["maturity"].update(
                stage="未知", basis="本次选段没有形成有直接原文依据的试验、交付或使用阶段判断。"
            )

    for technology, (name, mechanism, limitation) in zip(
        data["technologies"], TECHNOLOGY_EDITS, strict=True
    ):
        technology.update(name=name, mechanism=mechanism, limitations=[limitation], route_ids=[])

    # Wisp's unqualified advertising claim is retained only inside the original quote.
    passive_source = old_routes["Passive"]["source_ids"][0]
    passive_technology = products["Wisp"]["technology_ids"][0]
    infrared_source = next(
        item for item in data["technologies"] if item["id"] == passive_technology
    )["source_ids"][0]
    products["Wisp"]["technical_details"].insert(
        0,
        {
            "label": "被动工作方式",
            "text": "厂商称 Wisp 采用被动红外探测与跟踪，工作时不主动发射信号。被动工作方式本身不足以证明不受干扰。",
            "source_ids": [infrared_source, passive_source],
        },
    )

    def add_original_quote(spec, quote: str) -> str:
        bundle = builder.select_source(service, spec)
        pair = next(
            (pair for pair in bundle.blocks.values() if builder.quote_matches(quote, pair[1].text)),
            None,
        )
        if pair is None:
            raise ValueError("Editorial quote does not match an archived block")
        record, block = pair
        ref = builder.build_ref_id(
            library_id=record.library_id,
            document_id=record.document_id,
            source_revision=record.source_revision,
            locators=list(block.source_locators),
        )
        citation = service.resolve_citation(ref)
        if not builder.quote_matches(quote, citation["content"]):
            raise ValueError("Editorial quote cannot resolve")
        source_id = builder._id(
            "source", record.document_id, record.source_revision, block.block_id, quote
        )
        source_map[source_id] = {
            "id": source_id,
            "title": spec.name,
            "url": bundle.url,
            "library_id": record.library_id,
            "document_id": record.document_id,
            "source_revision": record.source_revision,
            "segment_id": block.block_id,
            "ref_id": ref,
            "quote": quote,
            "location": citation["location_label"],
            "available": True,
            "assurance": "归档原文连续引句；本地人工编辑核对。",
        }
        return source_id

    arsenal_bundle = builder.select_source(service, builder.SOURCES[-1])
    arsenal_text = arsenal_bundle.excerpts[0]["text"]
    start = arsenal_text.index("Underneath, the ArsenalOS platform is a system of systems")
    end = arsenal_text.index("\n\nThen those instructions", start)
    arsenal_source = add_original_quote(builder.SOURCES[-1], arsenal_text[start:end].strip())

    def route(key, name, problem, mechanism, product_name, source_ids, limitations):
        return {
            "id": builder._id("route", "editorial-v1", key),
            "name": name,
            "problem": problem,
            "mechanism": mechanism,
            "tradeoffs": [],
            "product_ids": [products[product_name]["id"]],
            "source_ids": source_ids,
            "limitations": limitations,
        }

    mesh = products["Lattice Mesh"]
    data["routes"] = [
        route(
            "normalize",
            "异构数据归一化",
            "不同军种、平台和机器人使用不同数据表述，跨系统交换需要转换。",
            "原文明确描述实时集成并归一化异构数据，用转换层处理不同系统的数据表述。",
            "Lattice Mesh",
            mesh["technical_details"][2]["source_ids"],
            ["未披露统一数据模型、映射规则与冲突处理方法。"],
        ),
        route(
            "priority",
            "受限带宽下的数据优先传输",
            "战术边缘带宽受限，需要确定数据传输的先后顺序。",
            "原文描述分类、评估并确定数据优先级，使所需数据在受限带宽条件下传递给相应人员。",
            "Lattice Mesh",
            mesh["technical_details"][3]["source_ids"],
            ["未公开优先级策略、拥塞行为和时延边界。"],
        ),
        route(
            "passive",
            "被动红外感知",
            "在不主动发射信号的条件下探测并跟踪目标。",
            "厂商介绍，Wisp 通过被动红外探测与跟踪提供态势感知，并称其工作时不发射信号。",
            "Wisp",
            [infrared_source, passive_source],
            ["原文未给出算法与环境条件；绝对抗干扰营销表述没有独立试验证据。"],
        ),
        route(
            "open-interface",
            "开放架构接入指挥与传感器系统",
            "将感知设备接入既有指挥控制或传感器系统。",
            "原文描述通过开放架构接入政府或商用现成指挥控制及传感器系统。",
            "Wisp",
            old_routes["Interoperable"]["source_ids"],
            ["未公开接口定义、适配过程与互操作验证范围。"],
        ),
        route(
            "container-edge",
            "容器化边缘基础设施集成",
            "可部署计算需要同时运输和维持高密度负载及其配套基础设施。",
            "原文描述在容器化单元内集成计算、通信、冷却、网络、安全与电力，支撑可运输的高密度负载。",
            "Menace-I",
            old_routes["Fully Integrated"]["source_ids"],
            ["未披露各子系统的耦合设计、能耗、散热和负载测试条件。"],
        ),
        route(
            "underwater-network",
            "海底节点部署、自校准与联网",
            "将水下节点布设到海底并形成可供操作员控制的网络。",
            "原文描述由 Dive-XL 携带并自主布放节点，节点就位后自校准和连接，形成可指挥水下资产的网络。",
            "Seabed Sentry",
            old_routes["Autonomous Operations"]["source_ids"],
            ["未公开布放定位、自校准及水下组网的具体算法与协议。"],
        ),
        route(
            "build-chain",
            "制造链贯通与版本变化回传",
            "制造链各阶段相互孤立，现场需求变化在设计、采购与工厂间传递时受到交接阻碍。",
            "原文介绍将自研 Forge 的制造执行、维护、质量功能与商用第三方系统组成统一环境，连接设计与现场保障，让变化沿设计、采购和工厂环节回传。",
            "ArsenalOS",
            [arsenal_source],
            ["原文说明集成机制与制造流程，未披露完整数据模型、接口和量化成效。"],
        ),
    ]
    for technology in data["technologies"]:
        technology["route_ids"] = [
            item["id"]
            for item in data["routes"]
            if set(item["product_ids"]) & set(technology["product_ids"])
            and set(item["source_ids"]) & set(technology["source_ids"])
        ]
    for product in data["products"]:
        product["overview"] = " ".join(item["text"] for item in product["technical_details"][:3])
        product["source_ids"] = list(
            dict.fromkeys(
                key for item in product["technical_details"] for key in item["source_ids"]
            )
        )
    data["sources"] = list(source_map.values())
    edited_at = datetime.now(UTC).isoformat()
    data["updated_at"] = edited_at
    data["audit"]["editorial_review"] = {
        "file": "editorial-review.json",
        "edited_at": edited_at,
        "policy": "中文解释、原文直述机制、能力与用途不计作技术路线、营销绝对词不作为已验证结论。",
    }
    directory.joinpath("revisions").mkdir(exist_ok=True)
    backup = directory / "revisions" / f"knowledge-{original_hash}.json"
    if not backup.exists():
        backup.write_bytes(raw)
    builder._write_json(path, data)
    view = builder.load_anduril_knowledge(service.data_root, directory, service=service)
    builder._write_json(path, view)
    builder.write_markdown(directory / "knowledge.md", view)
    builder.write_markdown(directory / "知识概览.md", view)

    edits = []
    old_sources = {item["id"]: item for item in before["sources"]}
    evidence = {**old_sources, **source_map}
    for category in ("products", "technologies", "routes"):
        previous = {item["id"]: item for item in before[category]}
        current = {item["id"]: item for item in view[category]}
        for key in sorted(previous.keys() | current.keys()):
            old, new = previous.get(key), current.get(key)
            fields = {
                field: {
                    "before": old.get(field) if old else None,
                    "after": new.get(field) if new else None,
                }
                for field in (old or new)
                if (old.get(field) if old else None) != (new.get(field) if new else None)
            }
            if not fields:
                continue
            ids = list(
                dict.fromkeys((old or {}).get("source_ids", []) + (new or {}).get("source_ids", []))
            )
            if category == "products":
                ids = list(
                    dict.fromkeys(
                        ids
                        + [
                            source_id
                            for item in (new or {}).get("technical_details", [])
                            for source_id in item["source_ids"]
                        ]
                    )
                )
            edits.append(
                {
                    "category": category,
                    "id": key,
                    "name": (new or old)["name"],
                    "fields": fields,
                    "reason": "中文化并将结论限定于原文明述机制；删除的路线属于能力、特性或用途。",
                    "evidence": [
                        {
                            field: evidence[source_id].get(field, "")
                            for field in (
                                "id",
                                "title",
                                "quote",
                                "ref_id",
                                "source_revision",
                                "segment_id",
                                "location",
                            )
                        }
                        for source_id in ids
                        if source_id in evidence
                    ],
                }
            )
    review_data = {
        "review_version": 1,
        "original_knowledge_sha256": original_hash,
        "edited_knowledge_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "edited_at": edited_at,
        "original_artifact": str(backup.relative_to(directory)),
        "raw_model_cache": "cache/ 原样保留，未作改写",
        "model_requests": 0,
        "counts_before": before["counts"],
        "counts_after": view["counts"],
        "policy": "主体为中文；名称和原文引句保留原文。逐条对照已归档原文，未把厂商自述视为独立验证。",
        "edits": edits,
    }
    builder._write_json(directory / "editorial-review.json", review_data)
    return review_data


if __name__ == "__main__":
    result = review()
    print(
        json.dumps(
            {
                "counts": result["counts_after"],
                "changed_records": len(result["edits"]),
                "model_requests": 0,
            },
            ensure_ascii=False,
        )
    )
