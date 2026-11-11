# 业务知识抽取交接包

入口是 `WORKBUDDY_TASK.md`。Workbuddy 只做抽取，工程和数据库由 Codex 维护。这个包只包含所选专题的归档正文和抽取契约，不包含密钥、数据库、实验资料或项目运行环境。

```text
manifest.json                 原文编号、版本、路径与契约哈希
sources/*.md                  只读归档正文
evidence/*.json               可选：已有业务会话中与本次原文匹配的证据摘录
schemas/field_contract.json   全量字段及类型/词表/存储约定
schemas/field_contract.csv    同一契约的便读表
schemas/result.schema.json    Workbuddy 结果 JSON Schema
templates/*.json              尚未填写的模板，不是结果
examples/                     虚构示例，禁止作为真实数据导入
results/*.json                Workbuddy 逐文档结果
summaries/                    可选摘要阅读副本
progress.json                 批次进度
errors.jsonl                  逐条失败/疑难记录
```

正文和摘要分开。结果 JSON 的 summary 是摘要提交入口，后端保存到独立摘要存储；正文永远不会被摘要覆盖。数据库里每条事实仍关联原文引用，而不是仅关联摘要。

现有 SearchOS 摘录是任务相关证据，不保证覆盖全文。包只读取所选文档明确关联的业务会话；只附加 active、http(s) 来源一致、原句能在本次原文重新定位的摘录。旧窗口坐标不会直接沿用，agent:// 派生摘要不会被当作原文。`research_evidence=not_exported` 表示没有附加可匹配会话证据，不意味着原文没有事实。直接读取包内归档正文即可，不需要重新联网。

交接完成后，由本地 `scripts/knowledge_handoff.py` 执行结果检查、写入预览和确认提交。具体 CLI 参数以该脚本 `--help` 为准；Workbuddy 不执行 commit。一次文档失败不应导致其他文档成果丢失。

现有图谱不改，业务数据与 widesearch 实验完全隔离。打包和校验均不调用模型；批量抽取由 Workbuddy 执行。
