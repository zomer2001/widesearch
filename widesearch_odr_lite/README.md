# ODR Lite

独立低成本版本，方法名为 `open_deep_research_lite`，不修改原 ODR、SearchOS、Ours 或旧实验结果。继续使用已保存的 DeepSeek 凭据与 `deepseek-flash / low`，复用原有 WideSearch 数据、输出转换和官方评分入口。

## 流程与改动

`原生研究简报 → 原生主管拆解 → 至多两个研究员（各至多两轮） → 一次模型出表`

| 环节 | 实现 |
|---|---|
| 搜索 | 研究员生成查询，DeepSeek Search 搜索；全任务至多 6 次实际查询请求，每次最多 3 个结果。长查询拆分后分别计数；相同查询跨研究员复用 |
| 网页读取 | DeepSeek Fetch → HTTP + Trafilatura → Crawl4AI；全任务最多尝试 12 个不同 URL，成功和失败结果均在该任务内复用。一个 URL 的备用读取可产生多个读取请求 |
| 网页摘要 | 删除逐页模型摘要；用 BM25 选相关原文片段，每页每次选择至多 2,000 字符，保留原文位置与 URL |
| 研究笔记 | 删除研究员的模型压缩；程序合并、去重工具返回的证据片段，限制上下文长度 |
| 最终输出 | 不写长报告。保留一次模型请求，直接从收集到的原文片段生成任务要求的表格；未知信息留空 |
| 保存 | 完整读取正文与选入上下文的片段分开保存。最终只使用部分片段，不影响已抓正文留存 |

研究简报、主管的任务拆解和研究员工具循环复用固定版本 ODR 原生函数。提示词允许直接搜索/委派，省去强制单独调用 `think_tool` 的轮次。主管只委派一轮，研究员第二轮仍可根据第一轮证据补搜。它是 ODR 的 Lite 变体，不能作为未修改原版 ODR 署名比较。

## 默认预算

- 规划与研究模型最多 7 次请求；并行研究共用一个硬限制，请求发出前检查。
- 单独预留 1 次最终出表请求，因此采集与出表合计最多 **8 次模型请求/题**。失败请求也占额度。
- 每次模型输出最多 4,096 token；工具上下文最多 10,000 字符，最终证据上下文最多 24,000 字符。字符限制并非 token 计数。
- 不逐页调用模型、不进行模型格式修复、不整题自动重跑；结构化输出与 SDK 不自动重试。
- 研究阶段限时 240 秒，之后仍保留一次出表机会；用户取消时不会再启动出表请求。
- 默认不评分。`--evaluate` 才启用原官方裁判，费用与调用次数另计；裁判自身的请求重试仍沿用原实现，不受上述 8 次限制。

读取失败时可使用搜索结果摘要，明确标为 `search_snippet`，不会冒充正文。原始页面可能仍有 API 返回的裁剪；保存的“完整正文”指本次 Fetch 实际收到的全部正文，不是完整 HTML 镜像。

## 运行

在项目根目录用现有环境启动。先运行一个此前的题目，只采集和出表：

```bash
widesearch_experiment/.venv/bin/python -m widesearch_odr_lite run \
  --label deepseek-odr-lite-one-v1 \
  --instances ws_zh_075
```

如果要同时官方评分，在上述命令末尾加 `--evaluate`，并换一个新 `--label`。多题可在 `--instances` 后追加题号；固定一次试验、逐题串行。运行配置和代码会被冻结，不要复用旧版本批次标签。

```bash
widesearch_experiment/.venv/bin/python -m widesearch_odr_lite status --label deepseek-odr-lite-one-v1
widesearch_experiment/.venv/bin/python -m widesearch_odr_lite analyze --label deepseek-odr-lite-one-v1
```

结果目录：

```text
widesearch_experiment/results/<label>/raw/open_deep_research_lite/<题号>/trial_1/attempt_1/
  prediction/prediction.md       可阅读的最终表格
  prediction/prediction.csv      可导入表格软件的数据
  pages/*.json                  URL + 本次读取的全部正文
  search_receipts/               搜索接口返回记录
  fetch_receipts/                主读取接口返回记录
  fallback_receipts/             实际使用备用读取时的记录
  evidence_fragments.json        被选择的原文片段、来源和位置
  final_evidence.json            最终出表实际使用的证据
  workflow_checkpoint.json       已提交的研究状态
  workflow_result.json           结束时的研究状态
  events.jsonl                  执行与调用记录
  budget.json                   预算与实际消耗次数
  result.json                   状态、表格回答、停止原因
```

即便中途出错或超时，已有证据仍保存，并在未取消且还没出表时尝试那一次预留请求。失败/超时状态保留；表格无法解析时保留模型原回答，不再花费调用修格式。没有可用证据时不调用最终模型。

## 验证范围

仅本地检查：用虚构资料和模拟 HTTP 返回运行原生 ODR 图、实际研究 worker 和表格导出；禁用网络连接。检查并行硬上限、重复读取复用、失败/超时后保留资料、取消不续费、格式错误不重试、默认跳过评分和评分显式启用。

```bash
widesearch_experiment/.venv/bin/python -m pytest widesearch_odr_lite/test_offline.py -q --disable-warnings
```

尚未启动真实题目或付费 API 验证。摘录和预算会降低覆盖率；后续应将此版本作为独立方法比较质量与调用量。
