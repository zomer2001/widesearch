"""Convert the reviewed HTML field design into an extraction-only field catalog.

Standard-library only. Does not read or write the workbench database or collector.
"""
import hashlib
import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "anduril-field-design-20261104.html"
VERSION = "anduril_knowledge_fields_v1_20261104"


class Node:
    def __init__(self, tag="", attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children, self.parts = [], []

    def walk(self, tag=None):
        for child in self.children:
            if tag is None or child.tag == tag:
                yield child
            yield from child.walk(tag)

    def text(self):
        return "".join(p if isinstance(p, str) else p.text() for p in self.parts)


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = self.node = Node()

    def handle_starttag(self, tag, attrs):
        child = Node(tag, attrs, self.node)
        self.node.children.append(child)
        self.node.parts.append(child)
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.node = child

    def handle_endtag(self, tag):
        node = self.node
        while node.parent and node.tag != tag:
            node = node.parent
        if node.parent:
            self.node = node.parent

    def handle_data(self, data):
        self.node.parts.append(data)


# IDs are authored once; subsequent revisions must preserve existing IDs.
# The HTML label/group remains available for readable extraction and mapping.
LABEL_IDS = """
产品名|name
英文名|english_name
缩写|abbreviations
代号|codenames
所属公司|company
所属专题|topics
产品层级|product_level
产品形态|product_forms
一句话定位|positioning
总体概括|summary
主要用户|users
官方页面|official_url
上级产品线|parent_product_line
前代产品|predecessor
后继产品|successor
衍生自产品|derived_from
变体类型|variant_type
共用平台|shared_platform
版本号|version
代次|generation
相比前代的变化|changes_from_predecessor
研发来源|development_origin
合作方|partners
供应商|suppliers
研发阶段|development_stage
首次公开|first_disclosed_date
首次交付日期|first_delivery_date
正式发布日期|release_date
首次部署|first_deployment_date
停产日期|production_end_date
停止支持日期|support_end_date
已知部署|deployments
已知合同|contracts
运行环境（产品自己在哪）|operating_domains
作用环境（盯或处理的目标在哪）|target_domains
用途场景|use_cases
作用对象|target_types
部署位置|deployment_locations
部署方式|deployment_mode
部署规模|deployment_scale
昼夜适应|day_night_conditions
天气适应|weather_conditions
适用海况|operating_sea_state
适用地形|terrain
工作温度|operating_temperature
受限环境运行|constrained_environments
支持无人值守|supports_unattended_operation
无人值守时长|unattended_duration
支持目标探测|supports_target_sensing
支持雷达探测|supports_radar_sensing
支持光电（可见光）探测|supports_visible_light_sensing
支持红外探测|supports_infrared_sensing
支持射频探测|supports_rf_sensing
支持信号探测|supports_signal_sensing
支持声学探测|supports_acoustic_sensing
支持水声探测|supports_underwater_acoustic_sensing
支持广域搜索|supports_wide_area_search
支持夜间探测|supports_night_sensing
可探测目标类型|detectable_target_types
其他功能|other_features
支持目标检测（框出目标）|supports_object_detection
支持分类识别（是什么类）|supports_classification
支持型号级细分识别|supports_fine_grained_recognition
支持敌我识别|supports_identification_friend_foe
支持行为判断|supports_behavior_recognition
支持意图判断|supports_intent_recognition
支持异常检测|supports_anomaly_detection
可识别类别|recognizable_classes
识别运行位置|recognition_runtime_locations
支持单目标跟踪|supports_single_object_tracking
支持多目标跟踪|supports_multi_object_tracking
支持跨传感器接力跟踪|supports_cross_sensor_tracking
支持丢失重捕|supports_track_reacquisition
支持轨迹预测|supports_trajectory_prediction
支持自动视线跟随|supports_automatic_gaze_tracking
最大同时跟踪数|max_simultaneous_tracks
支持多源融合|supports_multi_source_fusion
融合数据源|fusion_sources
融合层级|fusion_level
支持目标关联去重|supports_target_association_deduplication
支持跨域融合（空/地/海）|supports_cross_domain_fusion
支持态势图生成|supports_situation_picture
支持发布订阅|supports_publish_subscribe
支持主题过滤|supports_topic_filtering
支持优先级分发|supports_priority_delivery
支持断连运行（断网时本地继续工作）|supports_disconnected_operation
支持断连后自动同步|supports_reconnection_sync
支持自组网|supports_self_organizing_network
支持Mesh组网|supports_mesh_network
支持多链路切换|supports_multi_link_switching
支持加密通信|supports_encrypted_communication
支持抗干扰通信|supports_jam_resistant_communication
支持卫星链路|supports_satellite_link
支持协议转换|supports_protocol_conversion
支持网关|supports_gateway
通信链路类型|communication_link_types
支持多平台协同|supports_multi_platform_coordination
支持集群协同|supports_swarm_coordination
支持编队|supports_formation
支持任务动态分配|supports_dynamic_task_allocation
支持异构平台接入|supports_heterogeneous_platforms
支持人机协同|supports_human_machine_teaming
支持跨域协同|supports_cross_domain_coordination
协同规模上限|max_coordinated_platforms
支持任务规划|supports_mission_planning
支持航线规划|supports_route_planning
支持路径规划|supports_path_planning
支持避障|supports_obstacle_avoidance
支持动态重规划|supports_dynamic_replanning
支持传感器调度|supports_sensor_scheduling
支持资源分配|supports_resource_allocation
支持推演|supports_scenario_reasoning
支持仿真|supports_simulation
支持地图构建|supports_mapping
自主程度|autonomy_level
人员角色|human_role
支持自动起飞|supports_automatic_takeoff
支持自动降落|supports_automatic_landing
支持自动返航|supports_automatic_return
支持无GNSS导航|supports_gnss_denied_navigation
支持远程操控|supports_remote_control
支持自动拦截|supports_automatic_interception
支持边缘本地处理|supports_edge_processing
支持数据存储|supports_data_storage
支持数据录制|supports_data_recording
支持回放|supports_replay
支持历史查询|supports_historical_query
支持数据导入|supports_data_import
支持数据导出|supports_data_export
支持软件OTA更新|supports_software_ota
支持模型OTA更新|supports_model_ota
支持训练数据回流|supports_training_data_feedback
支持分级权限|supports_role_permissions
支持多密级域|supports_multiple_classification_domains
数据格式|data_formats
数据协议|data_protocols
提供SDK|provides_sdk
提供API|provides_api
提供开发文档|provides_developer_documentation
支持第三方传感器接入|supports_third_party_sensors
支持第三方平台接入|supports_third_party_platforms
支持第三方效应器接入|supports_third_party_effectors
支持插件扩展|supports_plugins
支持应用扩展|supports_apps
支持的标准接口|standard_interfaces
提供模拟器|provides_simulator
提供数字孪生|provides_digital_twin
支持身份认证|supports_authentication
支持零信任|supports_zero_trust
支持远程诊断|supports_remote_diagnostics
支持自检|supports_self_test
支持健康管理|supports_health_management
通过的认证|certifications
模块名|module_name
模块类型|module_type
数量|quantity
标配/选配|configuration_status
对应产品记录|linked_product
载荷名|payload_name
载荷类型|payload_type
载荷重量|payload_mass
可更换|replaceable
挂载接口|mount_interface
长度|length
宽度|width
高度|height
翼展|wingspan
直径|diameter
空重|empty_mass
最大总重|max_gross_mass
有效载荷能力|payload_capacity
续航时长|endurance
留空时长|loiter_duration
工作时长|operating_duration
航程|range
作用距离|effective_range
最大速度|max_speed
巡航速度|cruise_speed
供电方式|power_sources
动力方式|propulsion_types
功耗|power_consumption
防护等级|protection_rating
展开时间|setup_duration
部署时间|deployment_duration
操作人数|operator_count
公开单价|published_unit_price
合同金额|contract_amount
软件形态|software_form
运行层级|runtime_layers
宿主系统|host_operating_systems
处理器架构|processor_architectures
主要编程语言|primary_languages
数据类型|data_types
接入数据源类型|input_source_types
一致性方式|consistency_model
同步方式|synchronization_mode
最大接入节点数|max_connected_nodes
最大接入传感器数|max_connected_sensors
端到端延迟|end_to_end_latency
吞吐量|throughput
扩展方式|extension_methods
开源状态|open_source_status
许可方式|licensing_model
商业模式|business_model
云依赖|requires_cloud
可视化界面|visual_interfaces
与之集成的产品|integrated_products
传感器类型|sensor_types
光学波段|optical_band
射频频段|rf_band
探测距离（分目标类型）|detection_range
视场角|field_of_view
覆盖范围|coverage
分辨率|resolution
刷新速率|refresh_rate
扫描速率|scan_rate
最小可探测目标|minimum_detectable_target
内置边缘处理|embedded_edge_processing
输出数据类型|output_data_types
输出接口|output_interfaces
架设方式|mounting_type
塔高|tower_height
桅杆高度|mast_height
构型|airframe_configuration
起飞方式|takeoff_methods
降落方式|landing_methods
回收方式|recovery_methods
最大飞行高度|max_flight_altitude
抗风能力|wind_resistance
导航方式|navigation_methods
是否可重复使用|reusable
编队规模|formation_size
平台类别|marine_platform_type
排水量|displacement
最大航速|max_marine_speed
水声通信|supports_underwater_acoustic_communication
布放方式|launch_methods
耐海况|sea_state_rating
作业模式|operation_modes
最大潜深|max_operating_depth
水下导航|underwater_navigation_methods
效应方式|effect_types
拦截对象|interception_targets
最大拦截距离|max_interception_range
最大拦截高度|max_interception_altitude
引导方式|guidance_methods
是否需要人员授权|requires_human_authorization
创新点|innovations
产品特点|product_features
差异化卖点|differentiators
已知限制或短板|limitations
同类对比|comparisons
来源链接|source_urls
来源类型|source_type
资料日期|source_date
最后核对日期|last_verified_date
备注|notes
领域|domain
技术方向|direction
任务|task
方法或模型家族|method_family
具体模型或算法|specific_model
关键技术点|technical_points
本条记录所在层|hierarchy_level
上级技术|parent_technology
下级技术|child_technologies
技术名|name
别名|aliases
技术类型|technology_type
要解决的问题|problem
一句话说明|brief
原理说明|mechanism
关联产品|linked_products
输入数据类型|input_data_types
输入规格|input_specification
输入来源传感器|input_sensor_types
输出类型|output_types
输出频率|output_frequency
输出实时性要求|output_realtime_requirement
上游技术|upstream_technologies
下游技术|downstream_technologies
训练方式|training_method
需要标注|requires_annotation
数据规模量级|data_scale
常用公开数据集|public_datasets
支持合成数据|supports_synthetic_data
支持数据增强|supports_data_augmentation
数据获取难度|data_acquisition_difficulty
运行位置|runtime_locations
典型硬件|typical_hardware
算力需求|compute_requirement
内存需求|ram_requirement
显存需求|vram_requirement
推理延迟|inference_latency
模型大小|model_size
支持量化|supports_quantization
支持剪枝|supports_pruning
常用实现框架|implementation_frameworks
推理部署工具|inference_tools
模型交换格式|model_formats
实时性等级|realtime_class
依赖技术|dependency_technology
依赖类型|dependency_types
必需或可选|dependency_requirement
依赖外部设施|external_infrastructure
依赖标准|required_standards
依赖协议|required_protocols
适用场景|use_cases
适用环境条件|environment_conditions
适用目标类型|target_types
适用目标尺度|target_scales
规模上限|scale_limit
已知限制|limitations
失效条件|failure_conditions
失败模式|failure_modes
对抗脆弱性|adversarial_vulnerability
指标名|metric_name
指标数值|metric_value
指标单位|metric_unit
测试数据集|test_dataset
测试条件|test_conditions
硬件条件|hardware_conditions
数值类型|measurement_source_type
成熟阶段|maturity_stage
评估情境|assessment_context
阻碍因素|barriers
技术路线|technical_route
路线分支|route_branch
替代路线|alternative_routes
前身技术|predecessor
后继技术|successor
首次出现年份|first_appearance_year
标准或规范编号|standard_identifier
技术特点|technical_features
优势|advantages
仓库名|repository_name
仓库链接|repository_url
对应技术|linked_technology
项目类型|project_type
固定版本|pinned_revision
许可证|license
编程语言|languages
实现框架|frameworks
最近更新|last_updated_date
覆盖到哪一层|implementation_coverage
可运行|runnable
有示例测试|has_example_tests
复现难度|reproduction_difficulty
所需硬件|required_hardware
与 Anduril 的关联|anduril_association
视觉任务子类|vision_tasks
图像波段|image_bands
骨干网络|backbone
检测头类型|detection_head
后处理|postprocessing
小目标能力|small_object_capability
支持低照度|supports_low_light
支持夜间|supports_night_operation
利用视频时序|uses_temporal_information
协议栈层|protocol_layer
通信模式|communication_patterns
传播方式|delivery_modes
拓扑|topology
链路介质|link_media
频段|frequency_band
带宽|bandwidth
送达语义|delivery_semantics
有序性|ordering
优先级机制|priority_mechanisms
服务质量机制|qos_mechanisms
弱网机制|weak_network_mechanisms
组网方式|network_formation_methods
发现方式|discovery_methods
安全机制|security_mechanisms
融合对象|fusion_targets
传感器组合|sensor_combinations
融合方法|fusion_methods
数据关联方法|data_association_method
时间配准|temporal_registration
空间配准|spatial_registration
计算架构|compute_architecture
不确定性表达|uncertainty_representation
规划层级|planning_levels
方法类型|method_types
约束类型|constraint_types
在线重规划|online_replanning
多智能体|multi_agent
决策模式|decision_mode
环境建模|environment_models
验证方式|validation_methods
"""
IDS = dict(line.split("|") for line in LABEL_IDS.strip().splitlines())
GROUP_IDS = dict(line.split("|") for line in """
基础与谱系 · 所有产品|identity_lineage
场景 · 所有产品|scenarios
感知|sensing
识别|recognition
跟踪|tracking
融合|fusion
通信|communication
协同|coordination
规划|planning
控制|control
数据管理|data_management
开放集成|integration
运维安全|operations_security
组成与载荷 · 可添加多行|components_payloads
硬件通用参数|hardware
软件平台|software
传感器|sensors
固定站/地面|ground_station
空中平台|air_platform
水面平台|surface_platform
水下平台|underwater_platform
拦截器与效应装置|effectors
创新、特点与限制|characteristics
资料与备注 · 每条产品或技术记录共用|sources_notes
技术层级与连接|hierarchy
基础|identity
输入与输出|input_output
数据|data
计算与部署|compute_deployment
依赖 · 一项依赖一行|dependencies
适用条件与限制|applicability
性能指标 · 一项指标一行|metrics
成熟阶段、路线与演进|maturity_evolution
创新与特点|characteristics
学习与复现 · 一个仓库一行|implementations
视觉|vision
自主规划|autonomous_planning
""".strip().splitlines())


def value_type(hint, label):
    if "自动" in hint:
        return "entity_reference_list"
    if "是／否／未知" in hint:
        return "boolean"
    if "数字+" in hint:
        return "measurement"
    if hint == "数字":
        return "number"
    if "日期" in hint or hint == "年份":
        return "partial_date"
    many = any(x in hint for x in ["多选", "多个", "多条"])
    if "关联" in hint or "选择已有" in hint:
        return "entity_reference_list" if many else "entity_reference"
    if "链接" in hint:
        return "url_list" if many else "url"
    if many:
        return "text_list"
    if "长文本" in hint or "句" in hint:
        return "text"
    return "short_text"


def repeat_group(kind, group, label):
    if kind == "product" and group == "components_payloads":
        return "components" if label in {"模块名", "模块类型", "数量", "标配/选配", "对应产品记录"} else "payloads"
    if kind == "technology" and group in {"dependencies", "metrics", "implementations"}:
        return group
    return None


def write_json(name, payload):
    (HERE / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = Parser()
    parser.feed(SOURCE.read_text())
    fields, groups, row_map = {}, [], []
    for kind in ["product", "technology"]:
        section = next(n for n in parser.root.walk("section") if n.attrs.get("id") == kind + "-fields")
        for table in section.walk("table"):
            rows = [r for r in table.walk("tr") if "data-field" in r.attrs]
            if not rows:
                continue
            group_label = next(table.walk("caption")).text()
            group_id = GROUP_IDS[group_label]
            group = {"kind": kind, "group_id": group_id, "label": group_label, "field_ids": []}
            groups.append(group)
            for row in rows:
                label = row.attrs["data-field"]
                hint, examples = [x.text() for x in row.children if x.tag == "td"]
                stem = IDS[label]
                # These duplicate labels represent separate semantics and must remain separate.
                if label == "其他功能":
                    stem = group_id + "_other_features"
                if label == "部署方式":
                    stem = group_id + "_deployment_mode"
                field_id = kind + "_" + stem
                field_type = value_type(hint, label)
                function = any(x.attrs.get("class") == "function-detail" for x in row.walk("details"))
                occurrence = {"group_id": group_id, "group_label": group_label, "input_hint": hint, "examples_text": examples}
                if field_id not in fields:
                    fields[field_id] = {
                        "field_id": field_id, "kind": kind, "label": label, "group": group_id,
                        "groups": [], "value_type": field_type, "nullable": True,
                        "extraction_unknown_policy": "omit",
                        "default": None, "allowed_values": None, "examples_are_restrictions": False,
                        "examples": [], "unit": None,
                        "unit_rule": "保留原文单位、范围、上限下限及测试条件；没有单位时不得猜测。" if field_type == "measurement" else None,
                        "repeat_group": repeat_group(kind, group_id, label),
                        "is_function": function,
                        "derived": "自动" in hint,
                        "source_required": "自动" not in hint,
                        "applicability": [], "occurrences": [],
                    }
                field = fields[field_id]
                if field["value_type"] != field_type:
                    # Recovery methods is singular in air profile but plural in maritime profiles.
                    if {field["value_type"], field_type} == {"short_text", "text_list"}:
                        field["value_type"] = "text_list"
                    else:
                        raise ValueError((field_id, field["value_type"], field_type))
                field["groups"].append(group_id)
                field["applicability"].append(group_label)
                field["occurrences"].append(occurrence)
                field["is_function"] |= function
                if examples not in {"—", "未知", "是／否／未知"} and examples not in field["examples"]:
                    field["examples"].append(examples)
                group["field_ids"].append(field_id)
                row_map.append({"kind": kind, "group": group_label, "label": label, "field_id": field_id})
    for field in fields.values():
        if field["field_id"] == "technology_frequency_band":
            field["value_type"] = "text_list"
            field["additional_rule"] = "同一协议或实现可覆盖多个频段，逐个保存短词；原文只提一个频段时也用单项数组。"
        if field["value_type"] == "boolean":
            field["allowed_values"] = [True, False]
            field["examples"] = []
        if field["field_id"] == "product_target_domains":
            field["examples"] = ["空中", "地面", "水面", "水下", "太空"]
        if field["field_id"] in {"technology_runnable", "technology_has_example_tests"}:
            field["additional_rule"] = "资料陈述可运行与本地实际运行不同；没有运行记录不得声称本库已复现。"
        if field["field_id"] == "technology_anduril_association":
            field["additional_rule"] = "没有产品采用证据时保持空；不得因同类技术/可用开源实现而填产品已采用。"
        if field["field_id"] == "product_source_type":
            field["additional_rule"] = "本轮只抽取资料中的信息，不生成“本库推测”。"
        if field["field_id"] == "product_last_verified_date":
            field["additional_rule"] = "只在实际完成核对时填写核对日期，不得把采集日期当成核对日期。"
    assert len(fields) == len(set(fields))
    assert all(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", fid) for fid in fields)
    counts = {kind: sum(f["kind"] == kind for f in fields.values()) for kind in ["product", "technology"]}
    counts.update({"total_fields": len(fields), "source_rows": len(row_map), "source_product_rows": sum(x["kind"] == "product" for x in row_map), "source_technology_rows": sum(x["kind"] == "technology" for x in row_map), "specific_functions": sum(f["is_function"] for f in fields.values()), "groups": len(groups)})
    catalog = {
        "schema_version": VERSION,
        "scope": "knowledge_extraction_only",
        "source_design": SOURCE.name,
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "rules": [
            "不得改动 SearchOS 规划、搜索、抓取或采集表字段。",
            "仅从提供的原始资料抽取；表单示例不等于事实，禁止依据模型记忆补齐。",
            "无资料字段在抽取结果中省略；空白表单与入库展示补 null。不得输出 value:null、“未知”“暂无”“—”或 false。",
            "false 必须有原文明确否定证据；未提到不等于否。",
            "明确未公开或不适用的字段不输出字段事实；另在 notes 保留状态、字段与证据，空白表单仍为空。",
            "每项非空事实必须带 source_id 与连续原文 quote；参数保留版本、条件和日期。",
            "按适用分组抽取；技术层级链接、产品功能采用关系和外部参考实现分开保存。",
            "不同型号、版本、测试条件或矛盾来源不得覆盖或揉成一个数值。",
            "重复行组用 row_key 标识同一模块、载荷、依赖、指标或仓库；不要跨行拼接。",
            "关系须有资料证据，不得由字段相邻、名称相似、支持某功能推定具体算法采用。"
        ],
        "counts": counts, "groups": groups, "fields": list(fields.values()),
    }
    write_json("field_catalog.v1.json", catalog)
    write_json("html_row_mapping.v1.json", {"schema_version": VERSION, "rows": row_map})
    for kind in ["product", "technology"]:
        selected = [f for f in fields.values() if f["kind"] == kind]
        write_json("blank_" + kind + ".v1.json", {
            "schema_version": VERSION, "kind": kind, "entity_key": None, "name": None,
            "fields": {f["field_id"]: None for f in selected if not f["repeat_group"] and not f["derived"]},
            "repeatables": {g: [] for g in sorted({f["repeat_group"] for f in selected if f["repeat_group"]})},
            "evidence": [], "field_notes": [], "relations": []
        })
    write_json("catalog_validation.json", {
        "schema_version": VERSION, "counts": counts,
        "unmapped_rows": 0, "duplicate_field_ids": 0,
        "distinct_duplicate_labels": "其他功能按11组分别保留；场景部署方式与软件部署方式分别保留。",
        "shared_fields": [{"field_id": f["field_id"], "label": f["label"], "groups": f["groups"]} for f in fields.values() if len(f["groups"]) > 1],
        "count_note": "HTML原统计406项不作为强制目标。430行经语义处理为408字段：产品268、技术140。22行是跨形态复用的同一字段。"
    })
    # Keep result-level rules consistent when regenerating the field catalog.
    result_path = HERE / "extraction_result.schema.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        result["$defs"]["measurement"]["properties"]["operator"] = {
            "enum": ["=", ">", ">=", "<", "<=", "approx"]
        }
        result["$defs"]["measurement"]["description"] = "保留原始数字、单位及范围；operator保留大于、小于、近似等限定。1080p等非纯数字规格可用raw保存。未知字段须省略，不输出null或空measurement对象。"
        result["$defs"]["field"]["properties"]["value"] = {
            "description": "实际类型须匹配字段目录value_type。未知字段直接省略，不提交value:null。",
            "anyOf": [
                {"type": "string", "minLength": 1, "not": {"enum": ["未知", "未找到", "暂无", "—", "unknown", "null"]}},
                {"type": ["number", "boolean"]},
                {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                {"$ref": "#/$defs/measurement"},
            ],
        }
        result["$defs"]["relation"]["properties"]["predicate"] = {
            "enum": ["uses_technology", "supports_function", "subtechnology_of", "implements", "reference_for", "variant_of", "part_of", "integrates", "carries", "deploys", "evolves_from", "replaces"]
        }
        result["$defs"]["relation"]["properties"]["field_id"] = {
            "description": "可为空；非空时须为目录中可挂技术关联的产品具体功能、boolean能力或其他功能字段。",
            "enum": [None] + [f["field_id"] for f in fields.values() if f["kind"] == "product" and (f["is_function"] or f["value_type"] == "boolean" or f["label"] == "其他功能")],
        }
        write_json("extraction_result.schema.json", result)
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
