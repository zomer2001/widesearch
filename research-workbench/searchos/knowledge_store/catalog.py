"""Reference-image coverage and explicit routing of the legacy field catalog."""

DIMENSIONS = [
    (1, "技术基础信息", "技术身份、定义、分类与标签"),
    (2, "代表性技术演化", "技术事件、代际、路线与指标随时间的演变"),
    (3, "核心技术路线", "路线原理、组成、特点、状态、主体与成果"),
    (4, "技术组成与关联", "技术层级、组件、材料、工艺、依赖和接口"),
    (5, "技术能力与指标", "能力、指标体系、条件化观测与有依据的评价"),
    (6, "研究团队", "专家、团队、研究方向、成果、合作和机构归属"),
    (7, "产业化分析", "产品型号、产品性能、场景、案例、用户和应用主体"),
]

# Each small box in the supplied image has a stable key and real SQL storage.
ELEMENTS = [
    (
        1,
        "identity",
        "技术标识",
        "identity_assertions",
        "稳定实体ID；名称、别名和外部标识分别登记并关联证据",
    ),
    (1, "definition", "技术定义", "field_values", "定义与原理用有证据的字段事实保存"),
    (1, "classification", "技术分类", "classifications", "受控分类词条和可多选的分类事实"),
    (1, "tags", "技术标签", "classifications", "标签词表与技术之间的关联；与学科分类分开"),
    (2, "events", "技术事件", "technology_events", "事件实体及event_details、参与方与时间"),
    (
        2,
        "breakthroughs",
        "技术突破",
        "technology_events",
        "突破事件关联成果和指标；不由宣传文字自动认定",
    ),
    (
        2,
        "generations",
        "技术代际",
        "technology_generations",
        "代际独立记录，通过带证据关系连接技术或产品",
    ),
    (
        2,
        "route_evolution",
        "路线演化",
        "entity_relations",
        "evolves_from、replaces等关系，保留时间和条件",
    ),
    (
        2,
        "metric_jumps",
        "指标跃迁",
        "assessments",
        "performance_change评价通过assessment_basis关联前后观测",
    ),
    (2, "relations", "技术关系", "entity_relations", "关系类型和两端对象独立登记"),
    (2, "milestones", "关键里程碑", "technology_events", "事件类型标记里程碑，附原文和适用对象"),
    (2, "time", "时间信息", "event_details", "事实有效时间、发生日期精度和系统收录时间分别保存"),
    (
        3,
        "name",
        "路线名称",
        "technology_routes",
        "路线独立实体，名称在entities和entity_names中登记",
    ),
    (3, "principle", "路线原理", "field_values", "路线原理字段事实，不与技术通用定义混同"),
    (3, "composition", "路线组成", "route_steps", "有序步骤、子路线或相关技术，每行有自己的证据"),
    (3, "features", "路线特点", "field_values", "来源明确的特点；分析性结论另存assessments"),
    (3, "state", "路线状态", "assessments", "带时间、依据、评估者的路线状态"),
    (3, "relations", "路线关系", "entity_relations", "替代、演进、依赖、实现等关系分别表达"),
    (3, "actors", "代表主体", "participations", "机构、团队或人员对路线的研究、研发等角色"),
    (3, "outputs", "代表成果", "achievements", "成果独立建档，通过关系连接路线和研究主体"),
    (4, "parents", "上位技术", "entity_relations", "subtechnology_of；与组成关系分开"),
    (4, "children", "下位技术", "entity_relations", "由subtechnology_of反向查询，不重复抽取"),
    (
        4,
        "components",
        "关键组件",
        "composition_items",
        "组件实体、数量、配置、版本约束和重复行标识",
    ),
    (4, "materials", "关键材料", "materials", "材料独立实体，通过uses_material关联"),
    (4, "processes", "关键工艺", "processes", "工艺独立实体，通过uses_process关联"),
    (
        4,
        "support",
        "支撑技术",
        "entity_relations",
        "depends_on或uses_technology；产品采用须有独立证据",
    ),
    (4, "interfaces", "技术接口", "interface_connections", "接口实体、协议、方向、版本和兼容对象"),
    (5, "system", "指标体系", "metric_sets", "指标集及metric_set_members；定义与观测分开"),
    (5, "values", "指标值", "metric_observations", "原值、单位、统计量、测试条件、版本和来源性质"),
    (5, "level", "技术水平", "assessments", "technical_level评价，记录比较对象、方法和依据"),
    (5, "subject", "指标主体", "claims", "subject_id指向实际被测技术、路线或具体产品版本"),
    (
        5,
        "change",
        "性能变化",
        "assessments",
        "performance_change关联可比观测；条件不同不得直接比较",
    ),
    (5, "maturity", "技术成熟度", "maturity_assessments", "量表、等级、情境、评估人、时间与依据"),
    (5, "advantages", "技术优势", "assessments", "advantage评价，不把模型推测当来源事实"),
    (5, "weaknesses", "技术短板", "assessments", "limitation评价及适用条件、比较对象和依据"),
    (6, "experts", "核心专家信息", "persons", "人员身份、别名及任职和研究关系"),
    (6, "teams", "主要研究团队", "research_teams", "团队独立建档，与机构和人员通过角色联系"),
    (6, "directions", "团队研究方向", "participations", "researcher角色连接团队和技术、路线"),
    (6, "outputs", "代表成果", "achievements", "论文、专利、标准、原型或其他有来源成果"),
    (
        6,
        "collaboration",
        "合作关系",
        "entity_relations",
        "collaborates_with；保留角色、有效期和证据",
    ),
    (6, "affiliation", "机构归属", "participations", "member、affiliated等角色，支持随时间改变"),
    (7, "products", "产品信息", "products", "产品及通用信息；专题成员关系不代表厂商采用"),
    (7, "models", "产品型号", "products", "家族、型号、版本、配置独立实体，通过variant_of关联"),
    (
        7,
        "performance",
        "产品性能",
        "metric_observations",
        "指标主体指向具体产品粒度，保留条件与日期",
    ),
    (7, "scenarios", "应用场景", "application_scenarios", "场景独立建档，支持多产品和多技术共用"),
    (
        7,
        "cases",
        "典型应用案例",
        "application_cases",
        "实际案例与计划、合同、交付和使用阶段分别记录",
    ),
    (7, "users", "主要用户", "participations", "user角色连接用户机构与产品或应用案例"),
    (7, "actors", "应用主体", "participations", "operator、integrator、supplier等角色及有效期"),
]

ENTITY_KINDS = {
    "technology": ("技术", "technologies"),
    "route": ("技术路线", "technology_routes"),
    "generation": ("技术代际", "technology_generations"),
    "product": ("产品/型号/版本", "products"),
    "component": ("组件", "components"),
    "material": ("材料", "materials"),
    "process": ("工艺", "processes"),
    "interface": ("接口", "interfaces"),
    "person": ("人员", "persons"),
    "team": ("研究团队", "research_teams"),
    "organization": ("机构", "organizations"),
    "achievement": ("成果", "achievements"),
    "scenario": ("应用场景", "application_scenarios"),
    "application": ("应用案例", "application_cases"),
    "event": ("事件", "technology_events"),
    "reference": ("参考实现", "reference_implementations"),
    "metric": ("指标定义", "metric_definitions"),
    "capability": ("能力定义", "capabilities"),
}

ACTORS = ["organization", "team", "person"]
TECHNICAL = ["technology", "route", "product", "component", "process"]
RELATIONS = [
    ("subtechnology_of", "下位技术属于", ["technology"], ["technology"]),
    ("part_of", "组成部分", TECHNICAL + ["material"], TECHNICAL),
    ("uses_technology", "采用技术", ["product", "component", "route"], ["technology"]),
    ("supports_function", "具体功能的技术支撑", ["product"], ["technology"]),
    ("depends_on", "依赖", TECHNICAL, TECHNICAL + ["material", "reference"]),
    ("uses_material", "采用材料", TECHNICAL, ["material"]),
    ("uses_process", "采用工艺", TECHNICAL, ["process"]),
    ("exposes_interface", "提供接口", TECHNICAL, ["interface"]),
    ("compatible_with", "接口兼容", ["interface"], ["interface"]),
    ("has_route", "关联技术路线", ["technology", "product"], ["route"]),
    ("alternative_to", "替代路线", ["technology", "route"], ["technology", "route"]),
    ("variant_of", "型号/版本属于", ["product"], ["product"]),
    ("evolves_from", "演进自", TECHNICAL + ["generation"], TECHNICAL + ["generation"]),
    ("replaces", "替代", TECHNICAL, TECHNICAL),
    ("integrates", "集成", ["product", "component"], ["product", "component", "interface"]),
    ("carries", "可搭载", ["product"], ["product", "component"]),
    ("deploys", "可部署", ["product"], ["product", "component"]),
    ("implements", "实现", ["reference", "route", "product"], ["technology", "route"]),
    ("reference_for", "独立参考", ["reference"], ["technology", "route"]),
    ("collaborates_with", "合作", ACTORS, ACTORS),
    ("produces", "形成成果", ACTORS + ["technology", "route"], ["achievement"]),
    ("validates", "验证", ["achievement", "event"], TECHNICAL),
    ("applies_to", "适用场景", TECHNICAL + ["application"], ["scenario"]),
    ("upstream_of", "技术上游", ["technology"], ["technology"]),
    ("shares_platform_with", "共用平台", ["product"], ["product"]),
    ("generation_of", "所属技术代际", ["generation"], ["technology"]),
]
ROLES = [
    ("researcher", "研究", ACTORS, ["technology", "route"]),
    ("developer", "研发", ACTORS, TECHNICAL),
    ("manufacturer", "制造", ["organization"], ["product", "component", "material"]),
    ("associated_organization", "相关机构（角色待核）", ["organization"], TECHNICAL),
    ("member", "团队成员", ["person"], ["team"]),
    ("affiliated", "机构归属", ["person", "team", "organization"], ["organization"]),
    ("author", "成果作者", ACTORS, ["achievement"]),
    ("user", "使用方", ACTORS, ["product", "application"]),
    ("operator", "运营/应用实施方", ACTORS, ["product", "application"]),
    ("integrator", "集成方", ACTORS, ["product", "application"]),
    ("supplier", "供应方", ["organization"], ["product", "application", "component"]),
    ("event_subject", "事件涉及对象", list(ENTITY_KINDS), ["event"]),
    ("event_actor", "事件参与方", ACTORS, ["event"]),
    ("applied_product", "案例所用产品", ["product"], ["application"]),
    ("reference_vendor", "参考实现与企业的有据关联", ["reference"], ["organization"]),
]

EXTRA_FIELDS = [
    ("route_principle", "route", "路线原理", "text", "3.principle"),
    ("route_features", "route", "路线特点", "text_list", "3.features"),
    ("route_problem", "route", "路线解决的问题", "text", "3.principle"),
    ("route_inputs", "route", "路线输入", "text_list", "3.principle"),
    ("route_outputs", "route", "路线输出", "text_list", "3.principle"),
    ("team_summary", "team", "团队说明", "text", "6.teams"),
    ("person_summary", "person", "专家说明", "text", "6.experts"),
    ("organization_summary", "organization", "机构说明", "text", "6.affiliation"),
    ("achievement_summary", "achievement", "成果说明", "text", "6.outputs"),
    ("application_summary", "application", "应用案例说明", "text", "7.cases"),
    ("scenario_summary", "scenario", "场景说明", "text", "7.scenarios"),
    ("component_summary", "component", "组件说明", "text", "4.components"),
    ("material_summary", "material", "材料说明", "text", "4.materials"),
    ("process_summary", "process", "工艺说明", "text", "4.processes"),
    ("interface_summary", "interface", "接口说明", "text", "4.interfaces"),
    ("reference_license", "reference", "参考实现许可证", "short_text", "4.support"),
    ("reference_pinned_revision", "reference", "参考实现固定版本", "short_text", "4.support"),
]


def map_legacy_field(field):
    """Return a storage contract, not an automatic semantic converter."""
    fid, group, kind = field["field_id"], field["group"], field["kind"]
    table, mode = "field_values", "attribute"
    element = "7.products" if kind == "product" else "1.definition"
    rule = "带逐条证据的字段事实；保留原始值类型、适用条件和版本。"
    if field.get("derived"):
        return (
            "4.children" if kind == "technology" else "7.models",
            "entity_relations",
            "derived",
            ("从已有采纳关系反向查询；不生成第二份抽取事实。"),
        )
    if group == "sources_notes" and fid != "product_notes":
        return (
            "2.time",
            "source_revisions",
            "provenance",
            ("写入来源元数据或复核流水；资料日期不当产品发布日期。"),
        )
    if fid.endswith(
        ("_name", "_english_name", "_abbreviations", "_aliases", "_codenames")
    ) and not field.get("repeat_group"):
        return (
            ("7.products" if kind == "product" else "1.identity"),
            "identity_assertions",
            "identity",
            ("先登记对象和名称，再用身份断言绑定证据；同名不自动合并。"),
        )
    if field.get("repeat_group"):
        repeat = field["repeat_group"]
        destinations = {
            "components": ("4.components", "composition_items"),
            "payloads": ("4.components", "composition_items"),
            "dependencies": ("4.support", "entity_relations"),
            "metrics": ("5.values", "metric_observations"),
            "implementations": ("4.support", "reference_implementations"),
        }
        element, table = destinations[repeat]
        return (
            element,
            table,
            "repeat_row",
            (f"按来源、主体和row_key组装同一{repeat}行；关联名称需消歧，不能逐字段拼成独立行。"),
        )
    if field["value_type"] in {"measurement", "number"}:
        return (
            "5.values",
            "metric_observations",
            "metric",
            ("登记指标定义后写一次观测；保留原值、单位、统计量、条件、来源性质和具体主体。"),
        )
    if (
        kind == "product"
        and field["value_type"] == "boolean"
        and (field.get("is_function") or fid.startswith(("product_supports_", "product_provides_")))
    ):
        return (
            "5.system",
            "product_capabilities",
            "capability",
            ("对应能力词条及支持状态；false必须有明确否定证据，空白不等于不支持。"),
        )
    if field["value_type"] == "partial_date":
        return (
            "2.time",
            "event_details",
            "event",
            ("先识别事件及其涉及对象，保留年/月/日精度；不得补齐未知月日。"),
        )
    if fid in {"product_company", "product_partners", "product_suppliers", "product_users"}:
        element = "7.users" if fid == "product_users" else "7.actors"
        return (
            element,
            "participations",
            "participation",
            ("解析为机构/团队/人员与产品之间的角色记录；原文未说明角色时保留待核，不猜制造方。"),
        )
    if fid in {
        "technology_advantages",
        "technology_limitations",
        "product_limitations",
        "product_differentiators",
        "technology_barriers",
        "technology_failure_conditions",
        "technology_failure_modes",
        "technology_innovations",
        "product_innovations",
    }:
        element = (
            "5.advantages"
            if "advantage" in fid or "differentiator" in fid or "innovation" in fid
            else "5.weaknesses"
        )
        return (
            element,
            "assessments",
            "assessment",
            ("区分来源声称和分析评价；保留比较对象、条件、方法和支持依据。"),
        )
    if fid in {
        "technology_maturity_stage",
        "technology_assessment_context",
        "product_development_stage",
    }:
        return (
            "5.maturity",
            "maturity_assessments",
            "assessment",
            ("按同一评估的情境组装成熟度记录；未明示TRL不得由部署或合同自动推导。"),
        )
    if fid in {"product_topics", "technology_topics"}:
        return (
            "1.classification",
            "topic_entities",
            "identity",
            ("专题归属为浏览成员关系，不构成企业采用技术的事实。"),
        )
    if field["value_type"] in {"entity_reference", "entity_reference_list"}:
        element = "7.models" if kind == "product" else "4.support"
        if "parent_technology" in fid:
            element = "4.parents"
        if fid in {"technology_domain", "technology_direction", "technology_task"}:
            return (
                "1.classification",
                "classifications",
                "relation",
                "解析为分类词条；领域不当作产品采用关系。",
            )
        return (
            element,
            "entity_relations",
            "relation",
            ("解析实体及明确关系类型后写入；仅同名或同页出现不能生成关联。"),
        )
    if fid in {"technology_technical_route", "technology_route_branch"}:
        return (
            "3.name",
            "technology_routes",
            "relation",
            "独立登记路线并连接技术；名称本身不证明采用或替代。",
        )
    if fid in {"product_deployments", "product_contracts"}:
        return (
            "7.cases",
            "technology_events",
            "event",
            "按事件建档；合同、计划、交付和实际使用分别保留。",
        )
    if group == "scenarios":
        element = "7.scenarios"
    elif group in {"identity", "hierarchy"}:
        element = "1.classification" if group == "hierarchy" else "1.definition"
    elif group in {"input_output", "dependencies", "compute_deployment"}:
        element = "4.support"
    elif group == "applicability":
        element = "7.scenarios"
    elif group == "maturity_evolution":
        element = "2.route_evolution"
    if fid == "technology_anduril_association":
        rule = "名称泛化为与专题企业的关联；仅为关联状态说明，不能自动转成采用关系。"
    return element, table, mode, rule
