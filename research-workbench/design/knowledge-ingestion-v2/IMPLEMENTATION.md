# 抽取交接与数据库写入框架

本轮工程不调用模型、不执行批量抽取，不修改现有图谱。Workbuddy 只按任务包输出候选事实与独立摘要。

## 已实现模块

| 模块 | 文件 | 职责 |
|---|---|---|
| 交接格式 | `web/api/knowledge_handoff/models.py` | 版本化结果、处理状态、独立摘要 |
| 字段规范 | `contracts.py` | 原有 425 项类型契约、6 项独立语义轴、词表与单位规范、关系和角色约束 |
| 打包 | `package.py` | 只选业务专题原文，校验版本，按白名单创建目录和 ZIP，附可匹配的已有业务证据 |
| SQL 写入 | `review.py` | SQLite 副本预演、录制参数化 SQL、签名、身份对齐、冲突拦截、事务提交与回执 |
| 摘要 | `summaries.py` | 独立文件存储、原文版本绑定、引句定位、历史标识 |
| 业务边界 | `service.py` | 当前专题文档核对、提交前原文检查、摘要提交绑定、进度汇总 |
| 接口 | `routes.py` | `/api/research-database/handoff/*` |
| 命令 | `scripts/knowledge_handoff.py` | pack / check / status / preview / commit / save-summary |
| 网页 | `HandoffReviewPanel.tsx` / `HandoffSourceReader.tsx` | 结果预览确认、身份选择、正文与摘要分别查看 |

相对代码路径均以 `research-workbench` 为根；契约不声称 425 项全部完成独立语义审查。`reviewed` 是已具体审查的列，`type_template` 是完成类型约束的列；不清楚的资料进入 unmapped，不填假值。6 个新增字段在预览副本中安装，正式确认时才通过计划 SQL 安装到业务库，不修改旧事实。

## 写入边界

模型输出结构化记录，不输出 SQL。程序读取当前数据库副本，以既有类型化 Mapper 执行并录制 `sql + params`；副本执行失败的子项会回滚，不进入可提交计划。计划中列出新增、重复、冲突和待定实体，模型结果不直接写正式库。

确认时验证本地签名、原文身份/版本、数据库指纹。任何其他写入使旧预览过期，需要重新预览，不需要重新调用模型抽取。正常确认只提交可用记录；有待定或未映射项必须明确选择部分提交。数据库事务保存权威回执，磁盘回执保存失败不导致重复入库。

跨来源同名对象不会静默合并；可在网页选择现有对象或独立建档后重新预览。同一事实、同一来源重复导入不会增加重复事实，其他来源可以追加证据。关系/指标冲突保留待核，不覆盖旧值。格式、引句和数据库约束不等于已证明语义正确，页面确认仍用于判断归属和关系。

## 文件分层

原文沿用 `knowledge_base` 中的既有归档，不迁移、不覆盖。默认新产物位于：

```text
data/knowledge/knowledge_handoff/
  batches/<专题-时间>/       交接目录；同名 ZIP 在旁边
  plans/                    带参数 SQL 计划
  submissions/              与计划签名绑定的抽取与摘要提交
  receipts/                 入库回执缓存
  summaries/                独立摘要及原文引用
```

`.plan-signing-key` 仅供本地计划校验，不进入交接 ZIP。交接包不含数据库、密钥、环境文件、模型或实验资料。摘要更新不改变正文；摘录式摘要只接受原句，改写摘要标记为派生产物。

## 操作

在 `research-workbench` 目录执行：

```bash
.venv/bin/python scripts/knowledge_handoff.py pack --topic anduril
.venv/bin/python scripts/knowledge_handoff.py check --package '/完整交接目录'
.venv/bin/python scripts/knowledge_handoff.py status --package '/完整交接目录'
.venv/bin/python scripts/knowledge_handoff.py preview --topic anduril --result '/交接目录/results/某份结果.json'
.venv/bin/python scripts/knowledge_handoff.py commit --topic anduril --plan 'plan_预览返回的编号' --confirm
```

`pack --limit 5` 可只打包前 5 份候选资料；失败页会被排除，排除原因进入清单，因此可用数量可能少于 5。`--document 文档编号` 可重复指定。省略限额时导出该专题全部可用业务正文，仅复制资料，不调用模型。

`check` 依据真实文件重新计算进度；不相信 progress.json 自报完成数。缺结果但 errors.jsonl 有失败记录时显示失败，已有有效结果优先。校验错误写 `validation-report.json`。`preview` 不写业务数据库；`commit` 才执行写入。Workbuddy 不执行 commit。

网页入口：专题 → 维护工具 → 字段数据库 → 接收抽取结果。原文与摘要在专题的“原文资料”中切换。现有图谱接口和布局保持不变。

## 验证

用隔离临时数据库和虚构正文验证：副本预览零写入、参数值不插入 SQL、重复提交、过期计划、原文删除、摘要换版/篡改、身份选择、冲突与部分提交、失败回滚、回执磁盘异常、未知类型/关系/角色、真假与计划状态、包内原文哈希、实验隔离及原文/摘要分开。验证不运行模型或采集实验。
