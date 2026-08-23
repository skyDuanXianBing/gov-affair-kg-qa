# gov-affair-kg-qa · 政务事项知识图谱 + RAG 问答系统

基于 [OpenSPG/KAG](https://github.com/OpenSPG/KAG) 的政务事项知识图谱构建与自建 RAG 问答系统。以广东政务服务网采集的 **157 万条政务服务事项**（个人服务 109 万 + 法人服务 60.5 万）为数据底座，走**结构化建图**路线（schema 优先 + mapping，不做 LLM 抽取），并在图上实现向量检索 + 图扩展 + LLM 生成的可溯源问答。

```
原始采集数据(~48GB) ──► 清洗/物化/归一 ──► 适配器(CSV) ──► OpenSPG 结构化建图 ──► Neo4j + 向量索引
     (不入库)            cleaning/          kg/build/        kg/pilot/              │
                                                                             ┌──────────┴──────────┐
                                                              用户提问 ─► bge-m3 向量检索 ─► Cypher 图扩展 ─► DeepSeek 生成（引用来源）
                                                                          qa/retriever.py          qa/generator.py
```

## 核心特性

- **完整数据清洗管线**（`cleaning/`）：法人侧 AUDIT_* 137 字段物化为中文 v1 格式；HTML 实体迭代解码、控制字符清理、跨集去重，全量对账闭合，产出 157.2 万条统一数据集
- **Schema v0.2 图谱设计**（`kg/design/`）：8 实体 + 4 概念（isA 树）；亮点是 **LegalCitation 条款级弱实体**——解决 SPG (s,p,o) upsert 导致的 79% 条款边塌缩与 86% 条文冲突
- **1 万条试点图谱**：107,068 节点 / 375,415 边，与适配器期望值 100% 对账一致（`kg/pilot/pilot_result.md`）
- **全图向量化**：bge-m3（1024 维）覆盖 100% 节点，Neo4j 5 类向量索引
- **自建 RAG 问答**（`qa/`，当前主路径）：四类索引并行检索 → 图扩展（材料/流程链/法条→法规含文号）→ DeepSeek `deepseek-v4-flash` 生成；多轮对话指代消解改写；拒答边界与来源引用

## 目录结构

| 目录 | 内容 |
|---|---|
| `cleaning/` | 清洗/物化/归一脚本 + `reports/`（9 份清洗、字段勘察、映射设计、审计报告） |
| `data/`（不入库） | 原始数据（只读）、清洗产物 `cleaned/`、归一化数据集 `unified/`（157 万条） |
| `kg/design/` | `GovAffair.schema`（v0.2）+ schema 设计说明 + KAG 框架研读笔记 |
| `kg/build/` | `adapter.py`（JSONL→CSV 适配器）、`indexer.py`（建图入口） |
| `kg/pilot/` | 1 万条试点：抽样脚本、对账基线、`GovAffair/` 项目目录（含问答管道 prompt） |
| `kg/deploy/` | docker-compose（OpenSPG 全家桶）+ 运维 README |
| `qa/` | 自建 RAG：`retriever.py` / `generator.py` / `server.py` / `web/` |
| `.pi/specs/` | 专项交付报告（DeepSeek 接入、v0.2 重灌图、全图向量化） |

## 快速开始

前提：Python 3.10、Docker（或 colima）、[ollama](https://ollama.com)。

```bash
# 1) 运行环境
python3.10 -m venv kg/venv
kg/venv/bin/pip install neo4j openai fastapi "uvicorn[standard]"   # 仅问答链路依赖

# 2) 密钥配置（DeepSeek）
cp .env.example .env   # 填入 DEEPSEEK_API_KEY

# 3) 图存储与向量服务
cd kg/deploy && docker compose up -d        # OpenSPG 全家桶（Neo4j 等）
OLLAMA_HOST=0.0.0.0 ollama serve &          # 另一终端
ollama pull bge-m3

# 4) 建图（数据由 cleaning/ 管线重建后执行）
kg/venv/bin/python kg/build/adapter.py      # JSONL → CSV
kg/venv/bin/python kg/build/indexer.py      # CSV → 图（注意先清 builder/ckpt/）

# 5) 启动问答服务
kg/venv/bin/python qa/server.py             # http://127.0.0.1:8210/
```

> 本仓库**不含大体量数据与图数据**（原始+清洗约 107GB），均可由 `cleaning/` 脚本与建图流程重建；依赖的 KAG 框架源码参考见 [OpenSPG/KAG](https://github.com/OpenSPG/KAG)。

## 关键文档

- 图谱设计（含 LegalCitation 演进动因）：`kg/design/schema_design.md`
- 法人侧字段勘察与映射：`cleaning/reports/legal_field_survey.md`、`legal_mapping.md`
- 试点建图结果与对账：`kg/pilot/pilot_result.md`
- 运维（OpenSPG 容器踩坑）：`kg/deploy/README.md`

## 数据说明

数据来源于广东政务服务网公开发布的政务服务事项（个人服务 / 法人服务），仅用于科研与教学用途。
