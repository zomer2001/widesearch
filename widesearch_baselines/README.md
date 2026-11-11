# DeepSeek 对照方法接入

本目录是现有 WideSearch 实验的独立扩展。研究模型、表格转换模型、评分裁判均为 `deepseek-flash`，默认思考档位为 `low`。复用已保存的 DeepSeek 密钥，不需要额外的 Tavily、Bing 或 OpenAI 密钥。

## 方法与接入范围

| 启动名称 | 保留的原生方法 | 搜索 / 网页读取 | 本次处理 |
|---|---|---|---|
| `open_deep_research` | LangGraph 主管规划、并行研究、网页摘要、研究压缩、最终综合 | DeepSeek Search / Fetch，适配为原工具的返回格式 | DeepSeek 工具轮次、结构化输出与动态配置兼容；保存已完成研究笔记 |
| `mindsearch` | MindSearch 图规划、子问题搜索、依赖执行、综合回答 | DeepSeek Search / Fetch，接到原 WebBrowser | 已知浏览器工具标签的格式兼容；保留图事件与已完成节点；异常工具指令不作为成功答案 |
| `scrapy` | 原 Scrapy 链接遍历，加已有统一字段抽取 | DeepSeek Search 提供种子；Scrapy 原生抓取 | 复用已有适配器 |
| `crawl4ai` | 原 Crawl4AI 统计式自适应抓取，加已有字段抽取 | DeepSeek Search 提供种子；Crawl4AI 原生浏览器抓取 | 复用已有适配器 |
| `searchos` / `ours` / `ours_off` | 当前已有版本 | 已有公共接口 | 复用已有适配器 |

这些是统一模型配置下的适配版本。Scrapy 和 Crawl4AI 保留自身抓取实现；Crawl4AI 的此项对照是统计式自适应抓取。各方法的原生规划方式、停止规则、并发研究方式不同，结果中仍须同时比较正确率、耗时和调用量。

框架源码版本继续使用 `widesearch_experiment/manifest.json` 中固定的提交：Open Deep Research `1b7d2e80db9f`，MindSearch `7952c5f8a956`。未修改 vendor 源码或原实验包。

## 数据与评分流程

`公开题目（只有问题） → 原生方法研究 → 已有表格转换 → WideSearch 官方评分 → 原有统计`

表格转换只整理已有研究内容，不补搜、不添加事实。标准答案仍只交给评分进程。得到 Item / Row Precision、Recall、F1，以及原有耗时、模型与搜索调用统计。

Open Deep Research 的强制工具调用使用 `none` 完成该工具轮次，下一用户轮次恢复 `low`；这是已知 DeepSeek 接口约束。MindSearch 仍使用原文本工具协议，格式适配仅处理明确写出的已知工具名称及完整合法参数，不推断新指令。

研究失败时，Open Deep Research 会返回已提交的顶层研究笔记，MindSearch 会返回已完成图节点的回答，供原转换和评分流程使用。状态仍记为失败或超时。尚未提交的流式片段不计为完成内容。跨重试的历史表格仍可使用已有 `widesearch_recovery.scoring` 单独恢复计分。

## 后续运行命令（本次没有执行）

在项目根目录运行。下面沿用之前相同的 5 个题号，只新增两种规划方法，共 10 个任务；依次执行，完成每个任务后自动评分。新标签避免覆盖旧实验。

```bash
widesearch_experiment/.venv/bin/python -m widesearch_baselines run \
  --label deepseek-baselines-random5-v1 \
  --systems open_deep_research mindsearch \
  --instances ws_en_033 ws_zh_075 ws_en_039 ws_zh_055 ws_en_026 \
  --trials 1 --concurrency 1
```

只运行某个方法时，缩短 `--systems` 列表即可。需要爬虫对照时改为 `--systems scrapy crawl4ai`，同时使用新的 `--label`。

```bash
widesearch_experiment/.venv/bin/python -m widesearch_baselines status --label deepseek-baselines-random5-v1
widesearch_experiment/.venv/bin/python -m widesearch_baselines analyze --label deepseek-baselines-random5-v1
```

结果保存在 `widesearch_experiment/results/<label>/`，统计报告保存在 `widesearch_experiment/reports/<label>/`。配置在本目录 `deepseek.json`。新启动入口将本扩展的源码校验值一并写入实验冻结记录；只在新进程内切换适配器，正在运行的旧实验不受影响。

## 离线检查

```bash
widesearch_experiment/.venv/bin/python -m pytest widesearch_baselines/test_offline.py -q
widesearch_experiment/.venv-mindsearch/bin/python -m unittest widesearch_baselines.test_mindsearch_offline -v
```

检查使用模拟返回和虚拟密钥，包括请求格式、结构化输出、工具轮次、解析、失败内容保留和评分进程路由；不会运行数据集题目、调用模型或搜索 API。尚未进行这两个新适配器在 DeepSeek 上的真实题目端到端验证。
