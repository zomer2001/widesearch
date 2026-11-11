# Workbuddy 抽取任务

你负责从这个任务包里的业务原文抽取结构化记录。工程、字段设计、SQL、入库和图谱由 Codex 维护。不要修改项目代码、正式数据库、字段目录或正文，不联网补资料，不运行另一个研究任务。

## 输入和执行顺序

1. 读取 `manifest.json`，仅处理 `sources` 中 `status=ready` 的文档。专题在 `topic`，每条记录给出了正文 `path`、结果 `result_path`、文档编号和版本。
2. 读取 `schemas/result.schema.json` 和 `schemas/field_contract.json`。CSV 是便于浏览的同一份字段目录。七个维度组织同一套事实，不要分别跑七次抽取。
3. 阅读正文，按其中实际出现的产品/技术/机构等对象选择相关字段。若 manifest 提供 evidence_path，可先阅读已有事实摘录辅助定位，但它们只覆盖原任务关心的信息。长文按标题/段落/完整表格处理，带上必要表头、主体与上下文；可以在一份结果的 `outputs` 中放多个正文窗口的结果。不得为了省事只读开头或只用摘要。
4. 每处理完一篇，用模板中的真实 `document_id` 与 `source_revision` 输出到指定 `result_path`。不要把 examples 虚构案例当成真实资料。不要把未填写模板复制成完成结果。
5. 逐文档保存结果，继续时跳过已完成且正文版本相同的结果；保留已有产物，不全批重跑。更新 `progress.json`；失败或疑难项追加 `errors.jsonl`，每行一个 JSON 对象。

## 输出结构

外层是 `schema_version/document_id/source_revision/outputs/summary/status/notes`，必须遵守 schema，不输出 SQL。

- `outputs`：现有 `DocumentExtraction` 对象数组，每项包含 `entities/relations/records/notes/unmapped`。
- `entities`：`entity_key/kind/name/namespace/identity_quote/fields`。`entity_key` 是当前 outputs 元素内的局部引用，不是数据库 ID。产品保留原专名及版本；通用技术可用 `shared` 命名空间；不要自行合并同名对象。
- `fields`：`field_id/value/quote/qualifiers`。只用契约里的 ID；按类型输出 JSON 布尔、数字、字符串、数组或测量对象，不要一律输出文字。
- `relations`：`source_key/target_key/predicate/quote/field_id`，两端必须在同一个 outputs 元素的 entities 中。关系名参考 field_contract 的 relation_types。supports/contradicts 是任务证据用途，不是产品—技术关系。
- `records`：特殊事实类型 `metric/participation/event/assessment/maturity/route_step`，使用 `subject_key/kind/label/target_key/value/quote/qualifiers`；参与角色见 participation_roles。
- `unmapped`：有价值但无法可靠放入字段的信息，保留主体、原句、内容和原因。新词不强套封闭词表；可以在这里建议词条。
- `status`：`completed` 表示已处理全文，不表示所有字段都有值；`partial` 表示还有正文/疑难项未处理；无相关内容才用 `no_relevant_content`，此时不能同时包含事实。

## 必须遵守的事实要求

- 每个对象有 identity_quote，每个字段、关系、记录有 quote；引用必须是这份原文中的连续原句。保留原词，不改写引用。跨句关联可引用包含必要上下文的连续段落。
- 只填写原文明说的事实。保留时间、版本、目标、条件、否定、比较对象；没有信息就省略，不填 null、未知、0、false 凑完整率。原文明示的 0 或 false 是有效值。
- 真假字段不是“找到了/没找到”。计划支持、部分支持、未经验证的宣称不等于已经完整支持；先进入 unmapped 或使用准确的事件/评价记录。
- 测量对象使用 `{value,unit,raw,operator}` 或 `{min,max,unit,raw}`。例如最高 5 km 是上限，不是所有条件下的实际距离；缺单位不得猜测。目标与版本等放在 qualifiers。
- 重复行（载荷、组件、依赖、指标、外部实现）使用稳定的 `qualifiers.row_key`，同一个实体的同一行共享 key。不要把不同载荷的名称、质量、数量拼到一行。
- “支持目标检测”不能推导采用 YOLO；“支持发布订阅”不能推导采用 DDS。“外部仓库可参考”使用 reference_for，不能写成厂商已经采用。
- 研究、发布、签约、计划交付、实际交付、使用分别记录；不把文章发布时间当事件时间。原文只给年份就保留年份。
- 没有来源明确等级就不填写 TRL；不把自己分析的优势或成熟度包装成来源事实。适用条件不同的参数不要直接比较。
- 产品家族、型号、版本分清；同名但厂商/版本不同的对象保留独立局部实体，交由后续对齐器处理。
- `type_template` 字段只有类型级规则，碰到含义不清的情况进入 unmapped；不要自行改变列定义。

## 正文和摘要分离

正文文件在 `sources/`，始终只读。摘要写在结果 envelope 的独立 `summary` 对象中：`text/kind/quotes`；入库程序会将它保存为独立摘要产物，不会覆盖正文。

默认用 `kind=extractive`：挑选完整原句组成简洁信息卡，`text` 由这些原句组成，`quotes` 列出原句。可以额外在 `summaries/<source_id>.md` 放阅读副本，但导入以结果 JSON 的 summary 为准。若使用自己的综合表述，必须标 `abstractive` 并附支持原句；摘要不能作为字段 quote。

摘要不要求单独再调用一轮模型，可在整理同一篇抽取结果时形成。不要写长篇报告代替数据库记录。没有值得摘要的内容可以不提交 summary。

## 进度和失败记录

`progress.json`：`package_id/total/completed/partial/failed/current_source_id/updated_at`。`errors.jsonl` 每条包含 `source_id/stage/reason/retryable`。失败不删除此前已完成结果；需要重试时只处理相关文档，不无限循环。

完成后提交整个任务目录（至少包含 manifest、results、progress、errors），说明完成和剩余数量。不要执行数据库导入或自行更改 SQL。Codex 会运行程序校验、展示写入计划并处理确认入库。
