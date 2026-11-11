# WideSearch 专题调查与知识维护工作台

基于 SearchOS 扩展的本地研究项目，包括专题调查、资料归档、结构化知识导入、产品与技术图谱，以及独立的数据采集对照实验。

本仓库保存代码、必要的字段契约和项目报告。采集资料、业务数据库、实验运行结果、模型密钥及本机配置保留在本地，需要单独备份；克隆仓库不会带回已有知识库内容。

## 目录

| 目录 | 内容 |
| --- | --- |
| `research-workbench/` | 业务后端、前端、采集引擎、知识库与图谱 |
| `report/` | 按日期保存的项目报告和建设方案 |
| `widesearch_experiment/` | 独立评测框架、适配器、配置与离线检查代码 |
| `widesearch_baselines/` | MindSearch、Open Deep Research 等方法适配 |
| `widesearch_fetch_backup/` | 网页读取备份适配 |
| `widesearch_odr_lite/` | 低调用量 ODR 适配 |
| `widesearch_recovery/` | 已保存实验结果的恢复与评分工具 |

实验的运行状态、采集内容及评分结果不进入业务前后端。原始 `SearchOS/` 检出和实验 vendor/snapshot 不随本仓库复制，实验依赖通过其准备流程取得。

## 当前能力

- 本地网页工作台与按主题组织的资料。
- 调查采集、网页读取备份、来源归档和过程展示。
- 可选自适应采集排序模块。
- 原文与摘要分别保存。
- 结构化结果的预览、程序校验、参数化 SQL 提交与来源引用。
- 业务字段查询、产品与技术图谱。

模型自动抽取当前关闭，已有结果可以通过独立导入流程加工。完整的调查状态管理、本体规则驱动的证据核验及版本化修订属于下一阶段建设，详见 [2026-11-11 方案](report/2026-11-11/专题调查与知识维护管线方案.md)。

## 本地启动

已经安装依赖的工作目录中：

```bash
cd research-workbench
bash web/start.sh all
```

访问 `http://127.0.0.1:3000`；后端监听 `127.0.0.1:8000`。

新设备可按以下步骤准备环境。推荐 Python 3.12 和满足 Next.js 要求的 Node.js 版本；依赖安装会访问包仓库，模型调用仍由自己的配置控制。

```bash
cd research-workbench
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[access,local-docs,crawling]'
.venv/bin/python -m playwright install chromium
cp .env.example .env
cd web/frontend
npm ci
cd ../..
bash web/start.sh all
```

按网页设置或 `.env.example` 说明配置自己的模型与搜索服务。上述步骤是依赖声明和现有启动脚本对应的安装入口；本次代码上传未执行新设备全量重装。

## 实验

先阅读 [独立实验说明](widesearch_experiment/README.md)。仓库保留代码与配置，不自动恢复官方数据集、第三方依赖仓库、旧代码快照或已完成结果。准备实验依赖和数据后再启动任务；运行命令会产生模型或搜索费用时，由使用者自行启用。

## 来源与许可

业务代码基于 [antins-labs/SearchOS](https://github.com/antins-labs/SearchOS) 修改，保留其 [MIT 许可](research-workbench/LICENSE) 与原有版权说明。项目扩展包括本地专题工作台、采集适配、知识加工接口和实验适配代码。第三方依赖遵循各自许可证。
