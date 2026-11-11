# WideSearch 本地采集对照实验

入口只给参赛系统 `instance_id / query / language`。官方任务与 200 份标准答案已经固定；评分在独立进程中进行，不需要人工标注。

**当前按用户最新要求只做小样本验收。没有启动全量或自动扩量任务。** 默认选择中英文各 1 题，完整运行功能仅保留为以后显式启用的选项。实际验收状态见 `reports/LOCAL_CHECK.md`。

## 已接入的方法

| 方法 ID | 使用的实际代码 | 适配边界 |
|---|---|---|
| `searchos` | 官方固定 commit | 原生调度、覆盖图和证据流程；统一模型与搜索 provider；官方 benchmark 的直接表格导出 |
| `ours` | 当前工程 `searchos/` 的冻结快照 | 打开现有 `adaptive_collection`；关闭 P1、P2；不调用知识库入库 |
| `ours_off` | 与 Ours 同一快照 | 只关闭自适应模块，用于隔离其整体贡献 |
| `mindsearch` | 官方 Planner / Searcher / WebSearchGraph | 统一工具和 GPT 接口；兼容工具标记空白、带括号节点名及无工具节点的引用记录 |
| `open_deep_research` | 官方 LangGraph 工作流 | 只替换 Tavily 传输层，保留原生并行研究、网页摘要、压缩与报告节点 |
| `scrapy` | 官方 Scrapy | 原始问题作为固定搜索查询，随后同域广度爬取；共用抽取器，无额外 LLM 规划器 |
| `crawl4ai` | 官方 AdaptiveCrawler | statistical 自适应策略、页面发现和 Markdown 转换；官方 LLMExtractionStrategy |

Scrapy 与 Crawl4AI 共用同一 Crawl4AI LLM 抽取器、模型与最终结构化整理提示。框架源码未改写，适配均在 `adapters/` 中。SearchOS 原始仓库保留未修改状态。`reports/method_audit.json` 列出当前系统相对上游的实际文件变化。

当前正式候选配置是 A（统一模型）。B（官方原生模型）保存在 `configs/official_native.template.json`，尚未运行；它需要独立配置 InternLM/GLM 等模型服务和 Tavily。不能把 A 的结果称为官方原生配置成绩。

## 直接运行

以下命令在项目根目录（`widesearch_experiment` 的上一级）执行。当前默认沿用网页端已保存的 DeepSeek 密钥，也可设置 `DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL`。旧 OpenAI 配置仍读取 `OPENAI_API_KEY / OPENAI_BASE_URL`。密钥不写入实验配置、请求文件和报告。

```bash
# 工程验证，不计入正式成绩
widesearch_experiment/.venv/bin/python -m pytest widesearch_experiment/tests -q
widesearch_experiment/.venv/bin/python -m widesearch_experiment selftest

# 六种方法，固定 10 题（中英文各 5），每题一次
widesearch_experiment/.venv/bin/python -m widesearch_experiment run --label pilot-v1 --split pilot10 --trials 1 --concurrency 2

# 只跑指定题目和方法
widesearch_experiment/.venv/bin/python -m widesearch_experiment run --label one-task-v1 --systems searchos ours --instances ws_en_001

# 同一命令再次执行即断点继续；代码、参数、依赖变化会拒绝混入旧批次
widesearch_experiment/.venv/bin/python -m widesearch_experiment status --label pilot-v1
widesearch_experiment/.venv/bin/python -m widesearch_experiment analyze --label pilot-v1

# 只重试未成功评分的已保存结果，不重新采集
widesearch_experiment/.venv/bin/python -m widesearch_experiment.evaluation.regrade --label pilot-v1

# 正式 200 题 × 6 方法 × 3 次 = 3600 次任务
widesearch_experiment/.venv/bin/python -m widesearch_experiment run --label full-v1 --split full200 --trials 3 --concurrency 2

# 同一代码快照的模块开关消融
widesearch_experiment/.venv/bin/python -m widesearch_experiment run --label ablation-v1 --split full200 --systems ours_off ours --trials 3 --concurrency 2
```

小样本自动流程（默认仅 2 题，不会扩量）：

```bash
widesearch_experiment/.venv/bin/python -m widesearch_experiment.runners.campaign --prefix unified-v1 --concurrency 2
```

每阶段完成后自动生成报告。有未评分结果、某方法完全不能产生有效表格，或试跑中某方法执行／格式失败率超过 20%，停在工程检查阶段。质量低分不触发此门槛。进度在 `results/unified-v1-campaign.json`；具体队列在对应批次的 `progress.json`。完整的 10→30→200→消融流程必须额外显式指定 `--full`；本次没有运行。

## 结果在哪里看

```text
results/<批次>/
  frozen.json                     代码、仓库、数据、依赖、任务和模型冻结清单
  progress.json                   当前进度与正在执行的题目
  raw/<方法>/<题号>/trial_1/
    selected.json                 按执行顺序选中的尝试；不按得分挑选
    attempt_1/
      request.json                仅公开查询和允许配置
      result.json                 原始回答、耗时、计数和状态
      events.jsonl                实际模型、搜索、读取与表格快照事件
      search_receipts/            搜索接口原始响应和真实来源 URL
      fetch_receipts/             DeepSeek 网页读取接口原始响应
      pages/                     实际读取的网页正文
      prediction/prediction.md   可直接阅读的预测表
      prediction/prediction.csv  可用表格工具打开
      snapshots/                 采集当时保存的真实表格（若有）
      error.log / worker.log     本地错误与执行日志
  scores/<方法>/<题号>/trial_1/   官方得分、字段判定详情和裁判响应

reports/<批次>/
  report.md                       本批次说明
  task_scores.csv                 逐题逐次结果
  E1_overall_and_language.csv      整体及中英文指标
  repeated_trials.csv             Mean@N、Max@N、重复波动
  paired_bootstrap.json           任务配对差值及 95% 区间
  E2_final_efficiency.csv          实际搜索、阅读、耗时和信息效率
  E3_complexity.csv               字段数、表格规模分组
  figures/*.svg / *.pdf           可用于论文的图表
```

`engineering_selftest` 与 `integration-* / *adapters-* / mindsearch-v*` 是接口联调，不能作为论文主结果。保留失败尝试，不用重跑后的成功结果抹去故障记录。试跑只修安装、接口和格式问题；没有依据答案改写搜索策略。固定试跑集按题号哈希选取，不看答案或难度。

## 评分与统计边界

- 官方评分源码为 `vendor/widesearch/src/evaluation/`，评分时核对文件哈希；保留主键对齐、列映射、数值／日期处理和 LLM Judge 原规则。
- 官方 Azure/Ark 客户端换为固定 OpenAI 裁判传输层。唯一的数据适配是将 HF JSONL 中字符串形式的 `evaluation` 解码为对象，原始下载文件保持原样。
- Pandas 在零行 `apply(axis=1)` 时会错误推断成 DataFrame，导致官方单列赋值崩溃。适配层将该空结果返回空 Series，仍由官方 TP/分母公式计算。没有待评分项的 `{}` 批次直接返回空判断，不请求模型；不新增匹配、纠错或得分规则。自检包含标准答案、缺列、错值、空响应、零匹配行和只有表头六种情况。
- 裁判网络失败、缺项或 JSON 解析失败标为 `judge_error`，指标为空，不当成系统答错。重试只处理基础设施失败，不选择更高得分的裁判结果。
- 完全成功定义：六个官方 Item/Row P/R/F1 均为 1（容差 `1e-9`）。执行／格式失败单列统计；有效评分的空答案可得零分。
- 模型调用主字段统计研究过程的逻辑调用，SDK 传输重试关闭；框架自己的重试会记录新调用。模型未返回用量时记 `null`。转换、搜索服务内部模型开销、裁判开销另存，不混进研究 Token 数。
- `search_calls` 是提交给统一搜索服务的查询次数，包含失败尝试。DeepSeek 每个 Search HTTP 请求计一次；OpenAI 每次最多一个原生 web_search 工具调用。服务内部是否又检索多个索引不可观测，不虚构其调用数。
- 单次尝试的主字段对应该次预测；重启尝试的全部消耗另以 `all_attempts_*` 汇总，`retrieval_efficiency_all_attempts` 使用含失败重试的总搜索次数。不能仅展示重试成功的一次来隐藏前序开销。
- `pages_fetched` 是成功且非空的直接页面读取次数，另记唯一 URL 数。Scrapy 的 robots.txt、Crawl4AI 的链接 HEAD 预览不属于正文读取；原始下载日志保留。SearchOS 原生 aiohttp、MindSearch/ODR 共享 HTTP 正文提取器、Scrapy 下载器、Crawl4AI 浏览器的能力差异会保留，不能称作抓取条件完全相同。
- E2 只评分真实中间表，使用 `evaluation.regrade --snapshots`；没有结构化中间状态的方法只报告最终效率，不绘制虚构的历史召回曲线。
- Max@3 按每题、每指标取三次最佳；主结果使用 Mean@3。Bootstrap 对完整重复的同题均值配对，整题重采样 10000 次。缺少配对或未完成的任务不补零。
- E4 当前验证整个自适应模块的开关贡献。独立的“查询改写／联合路由／新增证据验证”未实现，故不提供虚构的对应消融。图排序、聚类、相关性及目标反馈仍属于同一个实际模块，不能仅据整体开关证明每个内部算法的独立贡献。

## 重建环境

Python 3.12。六个仓库 commit 位于 `configs/repositories.json`，HF revision 位于 `datasets/revision.txt`，依赖完整版本见两个 `requirements-*.lock`。

```bash
python3.12 widesearch_experiment/bootstrap.py --download --install
widesearch_experiment/.venv/bin/python -m widesearch_experiment prepare
widesearch_experiment/.venv/bin/python -m widesearch_experiment selftest
```

当前主环境额外复用了现有 workbench 环境中已安装的公共包，未修改原环境；冻结清单包含实际可见的全部依赖。重建时安装锁文件即可。MindSearch 使用独立旧版 Pydantic 环境。重新 `prepare` 会冻结当前 Ours 源码；旧批次的 `frozen.json` 始终保留当时配置，勿混合不同快照的结果。

## 当前模型配置（2026-11-05）

默认 `configs/unified.json` 已切换为 `deepseek-flash`、`reasoning.effort=low`：采集模型、格式转换与独立评分裁判均使用 DeepSeek Responses API；搜索使用 DeepSeek Search API，SearchOS/Ours、MindSearch、Open Deep Research 的普通网页读取使用 DeepSeek Fetch API。三种接口复用同一把已保存密钥，直接连接 DeepSeek，不经过系统代理。Scrapy/Crawl4AI 继续使用各自原生爬虫，保留其方法区别。CSS/DOM 访问技能需要原始 HTML 时，SearchOS 仍会按原生机制尝试直接抓取 HTML。

兼容适配仅放在实验层：保留 DeepSeek 工具调用所需的明文 reasoning 历史；强制指定工具时临时使用 `effort=none`，该工具轮次结束前维持非思考模式，下一个用户轮次恢复 low。配置里的 seed 用于任务顺序与统计抽样，不向思考模型发送 temperature/seed。

DeepSeek Search 的 query 限长 128 个字符。统一入口对超长查询按词边界确定性分段，逐段提交并交错去重合并结果；原始完整研究任务仍用于规划、抽取和评分。每段请求单独计入 search_calls，原始查询和分段保存在 search_query_split 事件里，不额外调用模型改写。此限制会改变超长查询的检索语义，因此新旧搜索服务的结果不能直接合并比较。搜索摘要不冒充已读取的网页正文。

原版框架、提示词、数据集与旧结果保持原样。旧 Luna + OpenAI 搜索配置保存在 `configs/unified_luna.json`，更早的 GPT-4o 配置在 `configs/unified_gpt4o.json`。新运行必须使用新批次名，例如 `--label deepseek-low-random5-v1`，不能接着 Luna 的旧批次续跑。此次只做接口小检查，没有启动五题或全量实验。

继续原来随机选出的五题（执行后自动评分并生成分析）：

```bash
widesearch_experiment/.venv/bin/python -m widesearch_experiment run --label deepseek-low-random5-v1 --systems searchos ours --instances ws_en_033 ws_zh_075 ws_en_039 ws_zh_055 ws_en_026 --trials 1 --concurrency 1 && widesearch_experiment/.venv/bin/python -m widesearch_experiment analyze --label deepseek-low-random5-v1
```

接口依据：[Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)、[Search](https://api-docs.deepseek.com/zh-cn/api/search/)、[Fetch](https://api-docs.deepseek.com/zh-cn/api/fetch/)。

评分适配层对合法的裸 JSON 对象只补官方解析器要求的代码围栏，不改动字段或判定值；原始裁判输出与补围栏后的文本都保留。无效 JSON、缺项等仍标为评分失败。

每题独立目录、进程、技能库和网页缓存。长批次需要电脑保持运行和联网；关闭终端时可使用 `nohup`，中断后仍使用原命令续跑。无需启动网页前后端。
