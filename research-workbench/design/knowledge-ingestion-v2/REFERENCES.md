# 可借鉴的公开项目

本次框架直接复用本项目原有结构化记录、Mapper 和 SQLite 写入器。下面是方法与可替换组件参考，不是已安装或已运行的依赖。

| 项目 | 地址 | 可借鉴部分 |
|---|---|---|
| LangExtract | https://github.com/google/langextract | 抽取与原文位置对齐；可选 Ollama 本地模型适配 |
| DocETL | https://github.com/ucbepic/docetl | 分块、批量抽取、实体归一和断点批处理 |
| DBMosaic | https://github.com/HKUSTDial/DBMosaic | 面向关系数据库的抽取、约束检查与定点修复；代码复用前需确认其许可证 |
| LOTUS | https://github.com/lotus-data/lotus | 文本和表格上的语义抽取、过滤、关联 |

Doc2DB 论文：https://www.vldb.org/pvldb/vol19/p2522-tang.pdf ，Document-to-Database: Extraction Meets Relational Semantics，PVLDB 2026。

Text-to-SQL 通常是将问题转换为 SELECT 查询；本任务是正文到带证据的记录，再由程序编译数据库写入。Workbuddy 不负责安装上述框架，不应临时增加多轮智能体或额外裁判调用。
