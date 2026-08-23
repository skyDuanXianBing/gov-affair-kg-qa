# 政务事项知识图谱项目 — 交付文档

> 更新时间：2026-08-10（下午，第三轮：架构转向——自建 RAG 问答系统上线，OpenSPG 服务端产品模式问答弃用）。交接对象：下一位接手的 agent / 工程师。
> 项目根目录：`/Volumes/f/AllMyData/MyUnderGraduate/政务大模型`（路径含中文，实测无影响）。

## 一、背景

目标：基于蚂蚁 OpenSPG/KAG 框架，用广东政务服务网采集的政务服务事项数据构建知识图谱（结构化建图路线，不走 LLM 抽取）。

原始数据两个数据集（采集已完成）：

| 数据集 | 位置 | 规模 | 原始格式 |
|---|---|---|---|
| 个人服务 | `data/个人服务/`（22 分片） | 1,090,700 行 / 21GB | 已是结构化 v1 中文格式 |
| 法人服务 | `data/法人服务/`（49 主题文件） | 605,280 行 / 27GB | 官方原始 JSON（`AUDIT_ITEM` 137 字段 + 约 20 张子表，大写英文字段 + 代码值） |

统一目标 schema：`gdzwfw-large-human-readable-v1`，顶层键：`schema_version / 事项 / 办理 / 办理结果 / 常见问答 / 来源 / 法律依据 / 申请`。

## 二、已完成的工作

### 1. 数据清洗与修复（脚本与报告均在 `cleaning/`）

- **个人服务清洗**（`clean_personal.py` + `verify_personal.py`）：输入 1,090,700 → 输出 1,090,612，拒绝 0，去重 88。输出 `data/cleaned/个人服务/`。
- **法人服务物化+清洗**（`materialize_legal.py` + `verify_legal.py`）：原始 AUDIT_* → v1 中文格式，输入 605,280 → 输出 481,357，去重 56,972，拒绝 66,951（bad_json 60,208 = 采集期 424 个 5xx 错误页碎片；no_audit_item 6,743 = 空响应）。输出 `data/cleaned/法人服务/`。
- **质量审计**（4 个独立审计代理）后修复并全量重跑：HTML 实体迭代解码（含双重编码 `&amp;amp;`）、`<br>`→`\n`、白名单标签删除、`\t`→空格、C0/C1/`\x7f` 删除；救回 157 条套餐服务（小写键 v2 schema 分支）+ 5 条缺 ITEM_ID（TASK_CODE 兜底）。终版全量扫描残留归零、对账闭合。
- 报告：`cleaning/reports/personal_clean_report.md`、`legal_materialize_report.md`、`legal_field_survey.md`、`legal_mapping.md`（字段勘察与映射设计，后续改动必查）。
- 已知保留项：16 处 `<xxx>` 脱敏占位符为源数据本身，刻意未动。

### 2. 归一化合并（`cleaning/unify_datasets.py`）

- 输出：`data/unified/政务事项-000001~000004.jsonl`（50 万×3 + 71,966，约 30GB），合计 **1,571,966 条**（1,090,612 + 481,357 − 3 条跨集重复）。
- 全量验证通过：编码全局唯一、键结构统一；`事项.数据来源` 区分服务线（纯个人 1,090,609 / 纯法人 481,354 / 兼有 3）；`事项.主题分类` 仅法人侧有值。
- 报告：`cleaning/reports/unify_report.md`。

### 3. OpenSPG 运行环境（`kg/deploy/`）

- colima（6C/14G/100G，arm64 原生）+ docker + compose；compose 项目 `kg/deploy/docker-compose.yml`，数据卷 `kg/deploy/volumes/`，运维文档 `kg/deploy/README.md`（含踩坑记录）。
- 服务端点与账号：

| 组件 | 地址 | 账号 |
|---|---|---|
| OpenSPG 产品模式 UI | http://127.0.0.1:8887 | `openspg` / **`openspg@kag2026`**（首次登录已强制改密，README §8） |
| Neo4j browser | http://127.0.0.1:7474（bolt 7687） | `neo4j` / `neo4j@openspg` |
| MySQL | 127.0.0.1:13306（3306 被用户 Podman 占用故改映射） | `root` / `openspg` |
| MinIO | 9000 / console 9001 | `minio` / `minio@openspg` |

- 重启恢复：`colima start && cd kg/deploy && docker compose up -d`。
- 辅助进程：`kg/deploy/mock_llm.py`（OpenAI 兼容 mock，端口 18999，nohup 常驻，用于过 knext 连通性校验）；ollama（端口 11434，`OLLAMA_HOST=0.0.0.0`，bge-m3 已拉取）。容器侧访问宿主机服务走 en0 IP `192.168.31.80`（IP 变化需同步）。

### 4. SPG Schema 设计（`kg/design/`，已 commit 到服务端，v0.2）

- **实体 8 个**：`Affair`（事项，id=编码）、`ImplementingOrg`（部门，按名称共享）、`Material`（材料，按名称共享）、`LegalBasis`（法条，按文号共享）、`LegalCitation`（v0.2 新增：文号+条款级弱实体，解决 86% 条文冲突与 79% 条款边塌缩）、`ProcessStep`、`ResultDocument`、`CrossRegionHandling`。
- **概念 4 个**（ConceptType + isA 树）：AffairType / ServiceTarget / ExerciseLevel / ThemeCategory。
- 关系含 `implementedBy / hasStep / nextStep / requireMaterial(7 边属性) / citeLegal / partOf` 等。
- 文件：`GovAffair.schema`（v0.2）、`schema_design.md`（§4 LegalCitation 方案）、`pilot_plan.md`、`kag_notes.md`（KAG 源码研读笔记，参考源码浅克隆在 `kg/_ref/KAG`）。

### 5. 1 万条试点建图（v0.2 已灌图，2026-08-09 晚）

- 试点数据：`kg/pilot/pilot_10000.jsonl`（个人/法人各 5000，法人覆盖 49/49 主题，seed 42，清单 `pilot_manifest.json`）。
- **当前图中数据 = v0.2**（Neo4j `govaffair` 库，2026-08-09 灌入）：节点 107,068（含 2 个 isA 概念父节点"行政权力"/"法人"，adapter_stats 口径 107,066）/**边 375,415**（语义边 236,376：citeLegal 135,273 + requireMaterial 53,508 + partOf 5,185 + nextStep 42,410；内联附加边 139,039）。全量对账 100% 一致，差异项可解释：citeLegal 源 CSV 含 45 行完全重复行（10 个事项，SPG 按 (s,p,o) upsert 塌缩，零丢失）；nextStep 2 个"幻影失败"（服务端已提交、客户端 IncompleteRead 响应丢失，图中 42,410 边齐全）。**citeLegal→LegalCitation→partOf→LegalBasis（文号）链路已 cypher 现验通过**（v0.1 不存在的能力）。报告 `.pi/specs/2026-08-09-pilot-v02-reload/v02-reload-report.md`。
- 适配器 `kg/build/adapter.py` v0.2 产出 `kg/pilot/csv_v2/`（16 个 CSV，LegalCitation 5,185 节点 / citeLegal 135,318 行 / 0 解析错误 / 0 悬空引用）。
- **⚠ 灌图韧性经验（重要）**：indexer 客户端无超时，OpenSPG server 回收空闲连接后客户端会永久挂死（2026-08-09 首轮 22:11 事故，CPU 冻结 + CLOSE_WAIT）。已写自愈包装 `kg/pilot/csv_v2/run_v02_resilient.sh`（60s 采样 CPU，冻结 5 分钟 kill -9 重启，最多 4 次；组件级 ckpt 保证续跑跳过已写 chunk）。**重跑灌图请用它，不要裸跑 indexer**；另注意 KAG runner 条目级 txt ckpt 跳过逻辑在源码中被注释掉，续跑依赖的是 KGWriter/SPGTypeMapping 的 diskcache（`builder/ckpt/` 下）。全量重跑仍需先清 ckpt。

### 6. 模型端点

- **向量 ✅ 已接入真实端点并全量完成（2026-08-10）**：ollama 本地 bge-m3（1024 维），**全图 107,066/107,068 节点有 `_name_vector`（99.998%，含概念父节点）**，唯一缺口 2 个 LegalCitation（content 超 bge-m3 上下文，真实失败）。实体链接向量路径已生效："申领居住证" type=vector 搜索 score 0.9984，问答回归 Q1/Q2/Q3 全部走向量路径（无文本兜底）。报告 `.pi/specs/2026-08-09-vectorize/`（vectorize-final-report.md + qa-regression-report.md）。
- **chat ✅ 已接入真实端点（2026-08-09 晚）**：DeepSeek `deepseek-v4-flash`（`kag_config.yaml` chat_llm 段，enable_check: true，`knext project update` 通过，KAG 客户端冒烟 PASS）。key 取自环境变量 `deepseekapi`（**有效值在 `~/.zshrc:44`**；⚠️ `~/.zprofile:6` 仍是旧失效 key，待用户同步）。直连 api.deepseek.com，**不走代理**。
- 接入报告：`.pi/specs/2026-08-09-deepseek-qa-chain/`（task-a / task-b 报告 + 需求规格说明书）。

### 7. 自建 RAG 问答系统（`qa/`，2026-08-10 上线，当前主问答路径）

**架构决策**：放弃 OpenSPG 服务端产品模式问答（Web UI 应用），改用 OpenSPG 只做图存储/向量索引，问答系统完全自建。原因：服务端产品模式连续踩坑（容器 DNS 失效、UI 模型配置不生效、应用配置缓存/发布机制不透明、pemja 嵌入式 Python 异常被 Java 吞掉无日志），黑盒调试性价比极低；而图数据、向量索引、embedding、DeepSeek 每个环节均已独立验证可用。

**交付物**（约 520 行）：

| 文件 | 作用 |
|---|---|
| `qa/retriever.py` | bge-m3 向量化 → Neo4j 5 个向量索引并行 top-k → 图扩展（事项富属性/材料去重/步骤链/法条→法规 title+docNo）→ 结构化上下文 |
| `qa/generator.py` | DeepSeek `deepseek-v4-flash` + 政务 prompt（禁编造+强制引用来源）；key 优先级 env `DEEPSEEK_API_KEY` > 项目根 `.env`（自动加载，见 `.env.example`）> `kag_config.yaml`（仓库内为 `${DEEPSEEK_API_KEY}` 占位符） |
| `qa/ask.py` | CLI：`kg/venv/bin/python qa/ask.py "问题"`（`--debug` 打印上下文） |
| `qa/server.py` | FastAPI 服务：`POST /api/ask`、`GET /api/health`、单页 Web |
| `qa/web/index.html` | 政务蓝聊天页（无外部依赖，极简 md 渲染、来源标签、检索详情折叠、耗时显示） |

**运行**：`nohup kg/venv/bin/python qa/server.py > qa/server.log 2>&1 &`，访问 **http://127.0.0.1:8210/**（刻意选不常用端口）。新增依赖：`fastapi`、`uvicorn[standard]`（已装 kg/venv）。⚠ 重启前先杀掉监听进程（`lsof -nP -iTCP:8210 -sTCP:LISTEN` 查 PID，nohup 返回的是父进程 PID，真正监听是其子进程）。

**多轮对话（2026-08-10 已上线）**：`/api/ask` 接收 `history`（前端维护最近 4 轮）；有历史时先调 DeepSeek 做**问题重写**（指代消解：“那流程呢”→“申领居住证的办理流程是什么”，temperature=0、失败降级原问题），再用改写后问题走标准 RAG；生成时也带历史保证语气连贯。前端在改写生效时展示“🔍 追问理解为：xxx”。成本：每轮追问 +1 次轻量 LLM 调用（约 2s）。浏览器三轮实测（材料→流程→收费）改写与答案全部正确。

**验证（Q1/Q2/Q3 + 拒答边界全过，浏览器实测）**：材料题 3 项与图逐条一致；流程题 6 步+法定/承诺时限；法律依据题答出《广东省流动人口服务管理条例》**含文号**（公告第50号）+条款——OpenSPG 产品模式遗留 B（文号不进答案）在自建 RAG 中自然解决（检索器直接把 docNo 放进上下文）；知识库外问题正确拒答。耗时：检索 300-650ms / 生成 2-8.6s（推理模型思考）。

**关键技术点**：
- 全链路走 `127.0.0.1`（Neo4j 7687 / ollama 11434），不受 en0 IP 漂移影响。
- `deepseek-v4-flash` 是推理型模型，reasoning 消耗 max_tokens 额度——必须给足（4096），否则 content 为空（2026-08-10 实测：max_tokens=16 时返回空串）。
- bge-m3 分数分布偏挤：无关问题命中分也可达 0.78-0.80（当前阈值 0.45 靠 LLM 拒答兑底，可微调至 ~0.82）。
- 向量索引名：`_gov_affair_{type}_name_vector_index`（事项/材料/法条/法规）+ `_gov_affair_legal_citation_content_vector_index`；Affair 另有受理条件/网上流程/窗口流程属性向量索引（未用，可供后续直查“条件/流程”类问题）。
- KAG 写入的 name 值带首尾引号，展示前需 strip。

**OpenSPG 服务端问答遗留状态（已弃用，仅存档）**：UI 模型配置已完成（MySQL `kg_user_model` id=1：chat=DeepSeek 新 key、embedding=bge-m3）；`knext project update` 已同步 chat_llm；应用已重新发布。但 solver 仍报 `LLM invoke exception`（output 空）——裸客户端（服务端同版本 openspg_kag 0.8.0.20250703 OpenAIClient 同步/异步）复现均正常返回，key/网络/参数全部排除，怀疑 pemja 嵌入环境差异，因转向自建未再深挖。compose 修复：`kg/deploy/docker-compose.yml` server 服务加 `dns: [223.5.5.5, 114.114.114.114]`（修复 Mac 睡眠唤醒后容器 DNS 转发失效）。

## 三、未完成 / 待办清单

1. ~~全量节点向量化~~ **已完成（2026-08-10，3h49m，rc=0）**：107,066/107,068 节点有 `_name_vector`（bge-m3 1024 维）；`exclude_types: Material` 变通已移除（仅剩 Chunk 基线）；问答回归 Q1/Q2/Q3 向量路径全部生效。indexer.py 新增 `--vectorize` 开关（挂 BatchVectorizer，读 chain_vectorizer 段）。**遗留 A 已闭合（2026-08-10 下午）**：2 个 LegalCitation 超长 content 节点通过 `chain_vectorizer.disable_generation: [LegalCitation.content]` + 重跑 --only LegalCitation 补齐，_name_vector 覆盖 **100%（107,068/107,068）**，服务端向量搜索命中（0.9997）；_content_vector 保持 5,183（content 按设计不生成，实体链接不依赖）。**遗留 B**：Q3 "法律依据"提问的文号（LegalBasis.docNo）未进答案——根因单跳规划+exact_one_hop_select，最小修复 = `solver/prompt/logic_form_plan.py` 法律依据 few-shot 补第二跳 `Retrieval(s=o1, p2:所属法规, o2:法律依据)` 后回归（预计 +0 LLM 调用/题）。
2. **v0.2 图谱已灌（2026-08-09 晚完成）**：详见 §二.5。**新解锁**："法律依据"类问答现在可验证（schema v0.2/图 v0.2 已对齐；原 v0.1 图 hasLegalBasis 错位问题消除）。
3. **全量建图未做（用户明确暂缓）**：全量 157 万条预估约 2000 万边；试点吞吐约 3.5k 边/分，直接外推需 100+ 小时，做之前必须先做吞吐优化（多链并行 `--num-chains/--num-threads` 参数已在 indexer.py、KGWriter batch、JVM/neo4j 调参）与分批策略。
4. ~~问答/召回链路未验证~~ **已验证（2026-08-09 晚，VALID）**：KAG 0.8.0 开发者模式问答路径 = `kag_config.yaml` 声明 `kag_solver_pipeline` + 项目目录内 `SolverPipelineABC.from_config(...).ainvoke()`（`knext reasoner execute` 是服务端 DSL 作业，非 NL 问答入口）。Q1 居住证材料全链路落地（召回 12 条 requireMaterial 边，答案与 Neo4j 节点逐条对账一致）；管道配置追加在 `kag_config.yaml` 尾部（既有段零改动），规划 prompt `solver/prompt/logic_form_plan.py`（schema 感知），证据 `kg/pilot/qa_probe/`。注意：`enable_summary` 必须为 true（否则召回内容到不了生成器，框架设计）；openie_llm 仍为 mock 且 solver 链路无引用；chunk 召回恒空（结构化建图无 Chunk 节点）。
5. **可选：424 个采集失败详情页重爬**（法人服务 bad_json 的来源，占原始数据 10%）。
6. **可选：16 处 `<xxx>` 脱敏占位符**如需统一清除。
7. **可选：citeLegal 源 CSV 45 行重复**（适配器产出问题，10 个事项）——图已按唯一对无损塌缩，如需边数=行数可在 adapter 侧去重（改后需重新生成 csv_v2）。
8. **小遗留**：`kag_config.yaml` solver 段头部注释仍写 "enable_summary: false"（实际 true，任务B遗留），顺手更正即可。

## 四、目录速查

```
data/个人服务, 法人服务     # 原始数据（只读，勿动）
data/cleaned/              # 清洗后（v1 格式，两侧 schema 统一）
data/unified/              # 归一化最终数据集 1,571,966 条（建图输入）
data/rejects/              # 个人服务 rejects；法人 rejects 在 data/cleaned/rejects/
cleaning/                  # 全部清洗/合并脚本 + reports/ + logs/ + trial/
kg/deploy/                 # docker-compose + volumes/ + README（运维必读）+ mock_llm.py
kg/design/                 # GovAffair.schema(v0.2) + 设计文档 + KAG 研读笔记
kg/build/                  # adapter.py(v0.2 JSONL→CSV) + indexer.py(建图入口)
kg/pilot/                  # 1 万试点数据/CSV/csv_v2/GovAffair 项目目录/pilot_result.md
kg/pilot/GovAffair/solver/ # 问答管道：qa_taskb.py(回归脚本) + prompt/logic_form_plan.py(schema感知规划)
kg/pilot/qa_probe/         # 问答验证证据（探针脚本0调用 + 4轮日志/结果JSON）
kg/pilot/smoke_deepseek_chat.py  # DeepSeek chat 冒烟脚本
kg/_ref/KAG                # KAG 源码浅克隆（参考用）
qa/                        # ★ 自建 RAG 问答系统（retriever/generator/CLI/server/web，当前主路径）
.pi/specs/                 # Spec Pack（2026-08-09-deepseek-qa-chain：DeepSeek接入+问答验证）
```

## 五、给接手者的注意事项

- 所有数据脚本均为 Python 标准库流式实现，大数据量操作注意内存；重跑任何 builder 任务前必须清理对应 `builder/ckpt/`。
- 宿主 Python 3.14 太新，KAG/knext 用的是 Python 3.10（brew python@3.10），虚拟环境在 `kg/` 下（具体见 agent 环境或 `kg/pilot/GovAffair` 的使用方式）。
- 代理：`export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890` 用于 GitHub/pip；**不要**对 DeepSeek/ollama/127.0.0.1/内网地址走代理；colima 拉镜像走阿里云 registry 实测无需代理。
- **⚠ en0 IP 会漂移（2026-08-09 晚实测）**：Mac 睡眠唤醒后 DHCP 换段（192.168.31.80 → **192.168.111.177**），导致 `kag_config.yaml` 里写死的容器→宿主 IP 全部失效，BatchVectorizer 挂死（TCP 超时无快速失败）。`kag_config.yaml` 已更新为新 IP。**任何 builder/solver/向量化任务前先验证**：`curl -m 3 http://<当前en0 IP>:11434/api/tags` 与 `ifconfig en0 | grep inet` 对照；不一致先改 `kag_config.yaml` 两处 base_url（openie_llm、vectorize_model）。
- **环境变量陷阱**：`zsh -lc` 等非交互登录 shell 只加载 `~/.zprofile` 不加载 `~/.zshrc`；取 `deepseekapi` 等变量时注意来源文件（当前有效 key 仅在 `.zshrc`）。
- **问答服务**：日常用 http://127.0.0.1:8210/（自建 RAG）；若 8210 无响应先 `tail qa/server.log`，重启命令见 §二.7。服务依赖 colima 容器（Neo4j）与 ollama，两者不在则先恢复（`colima start && docker compose up -d`；ollama 需 `OLLAMA_HOST=0.0.0.0` 常驻）。
- **OpenSPG 产品模式 UI（8887）仅用于看图谱/本体/模型配置**，其应用问答已弃用（§二.7 末尾有存档状态）。
- 原始数据目录（`data/个人服务`、`data/法人服务`）全程只读；任何清洗/转换输出写到新目录。
- 修改清洗或映射规则后，同步更新 `cleaning/reports/` 下对应报告与映射文档。
