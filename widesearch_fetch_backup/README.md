# 网页读取备用路径

已实现：`DeepSeek Fetch → HTTP + Trafilatura → Crawl4AI 浏览器渲染`。只改变网页读取，不改变搜索接口、研究规划、Ours 排序、模型重试、表格转换或评分。

- DeepSeek 正常返回正文时直接使用。
- 失败、空正文或仅有脚本加载提示时，尝试本地 Trafilatura 提取，保留链接、表格及原 HTML。
- 直接读取仍没有正文时，尝试 Crawl4AI。源站明确返回 404/410、非网页格式或页面超过大小限制时结束该链。
- 每次读取最多尝试一次各备用方法。备用阶段总执行预算 35 秒（进程清理另计），同时最多两个备用读取；成功结果在本次尝试内复用，失败结果短暂缓存 60 秒。
- 每次真实读取都写入 `page_start/page_end`；备用结果保存在 `fallback_receipts/`，成功正文也保存到原有 `pages/`。缓存命中单独记录，不重复计算成功读取次数。
- 未增加新的付费服务。网页后备提取不调用模型。PDF 等非网页文档仍由原读取接口处理。

MindSearch 的隔离环境没有 Crawl4AI，浏览器读取通过现有主环境的短生命周期子进程完成，因此不需要变动正在使用的依赖。浏览器进程不接收 API 密钥。

## 使用范围

新入口覆盖使用公共 Fetch 的 SearchOS、Ours、Open Deep Research、MindSearch。Scrapy、Crawl4AI 两个爬虫对照保留各自原生读取方式。

为保持当前 `deepseek-other4-random5-v1` 批次的条件一致，本次没有修改 `widesearch_experiment/` 或 `widesearch_baselines/`，也没有启动新实验、停止或重启当前批次。当前批次继续使用原读取方式。

后续启动时，将原命令中的 `-m widesearch_baselines` 换为 `-m widesearch_fetch_backup`，并使用新标签，例如 `deepseek-fetch-backup-random5-v1`。题号、方法、串行度等参数保持原样；默认配置已开启备用读取。新批次会冻结这份扩展的源码和配置，结果仍写入原来的 results/reports 目录。

## 已执行的验证

两个运行环境分别通过 10 项离线检查：主读取成功时不回退、静态正文提取、浏览器回退、明确 404 结束、缓存隔离、取消释放、总超时、计数与来源留存、MindSearch 环境桥接、worker/评分路由隔离。

```bash
widesearch_experiment/.venv/bin/python -m unittest widesearch_fetch_backup.test_offline -q
widesearch_experiment/.venv-mindsearch/bin/python -m unittest widesearch_fetch_backup.test_offline -q
```

本机动态网页验证命令（模拟 DeepSeek 失败，无真实 API 调用）：

```bash
widesearch_experiment/.venv/bin/python -m widesearch_fetch_backup.verify_local
```

本机验证已通过：静态读取仅得到脚本提示后，成功转到 Crawl4AI，取得 1,072 字符的动态正文。

## 其余修复：仅整理，未实施

| 待办 | 现象与后续方向 |
|---|---|
| 模型请求局部重试 | 临时故障先重试当前请求；是否启用由后续决定，受同一任务预算约束 |
| 超时收尾 | 将已有恢复逻辑接进正常出口，保留部分结果；不修改本次实验结果 |
| 表格交付校验 | 按原问题检查字段、单位、日期，处理重复列映射；不使用标准答案修内容 |
| Open Deep Research 摘要格式 | 当前第一题首次尝试出现 47 次结构化输出校验失败，日志明确包含非法 JSON，触发原框架重试。后续应单独处理，不与网页读取修复混为一项 |
| 研究调用量控制 | 原框架逐页摘要、并行研究与多轮搜索会叠加调用；`max_pages` 当前只约束爬虫对照。快速配置和正式评测配置需分别命名，不能中途变更 |
| MindSearch 运行兼容 | 当前已有一次 TypeError 和一次模型流错误；先保留原始事件与部分结果，之后单独定位 |

## Ours 的轻量改进草案：仅讨论，未实施

不引入预计耗时，也不修改意图、聚类、图算法、目标缺口或信息增量的现有计算。

仅给候选网页增加一个“连续实际读取失败次数”。整条读取链均失败时加一；读到正文时清零；缓存命中不增加失败次数。可用简单规则：`新排序分 = 原排序分 / (1 + 连续失败次数)`。

这只影响候选网页的访问顺序，减少继续选择已失败路径的机会。不永久屏蔽 URL，不因某个网页打不开就把研究主题判为不相关。先限于 URL，避免直接给整个域名或主题降权。无需新增 LLM 调用、向量模型或新调度框架。
