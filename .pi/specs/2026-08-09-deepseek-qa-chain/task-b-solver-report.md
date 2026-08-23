# 任务 B 验证报告 — GovAffair KAG 问答/召回链路（HANDOFF 待办 4）

> 执行：quality_assurance · 2026-08-09 19:1x–19:3x · KAG 0.8.0（`kg/_ref/KAG`，venv 可编辑安装）
> 判定：**VALID（链路可用，B1/B2/B3 全部覆盖）**，附 1 项执行过程偏差披露（真实 LLM 调用 18 次，超"约 10 次"预算，见 §6）

---

## 1. 结论摘要

| 验收 | 判定 | 说明 |
|---|---|---|
| B1 solver 启动方式与配置 | **PASS** | 本版本 NL 问答唯一可行路径已摸清并跑通：`kag_config.yaml` 增加 `kag_solver_pipeline` 段 + 项目目录内 Python API 调用（§2）；`knext reasoner execute` 是服务端 DSL 作业，**不是**自然语言问答入口 |
| B2 2~3 题端到端（召回痕迹 + 真实 DeepSeek 生成） | **PASS** | Q1（居住证材料）全链路落地：图召回 12 条 `requireMaterial` 边，最终答案与 Neo4j Material 节点逐条对账一致；Q2（食品经营许可流程）召回 30 条 `hasStep` 边、答案由真实 DeepSeek 生成（该轮为降配配置，答案未引用召回内容，根因 RC3 已定位并由 Q1 终轮证明修复有效）。两题 LF 规划、实体链接、GQL 一跳、生成均有日志痕迹（§3） |
| B3 前置缺失根因记录 | **PASS** | 共定位 3 个根因（规划 prompt 无 schema 感知 / 节点向量未全量 / `enable_summary` 数据流设计）+ 2 个附带发现（schema v0.2 与图 v0.1 错位、Chunk 索引为空），未盲修、未跑大规模索引（§4） |

**openie_llm 全程保持 mock 且 solver 链路无任何组件引用它**（见 §5 自查）。

---

## 2. B1 — 本 KAG 版本的问答执行方式

### 2.1 `knext reasoner execute` ≠ 自然语言问答

`knext/command/sub_command/reasoner.py`：`execute_reasoner_job(file, dsl, output, proj_path)` 经 `ReasonerClient.execute(dsl_content)` 向服务端提交**异步 DSL 图查询作业**（SPG 推理 DSL，需手写 `--dsl/--file`）。它是结构化查询接口，不含 NL→检索→生成链路，不适合 B2 目标。

### 2.2 正确的 NL QA 入口（开发者模式）

参照 `kag/open_benchmark/AffairQA/solver/eval.py`、`kag/examples/supplychain/solver/qa.py`，KAG 0.8.0 开发者模式的问答 = **在项目 `kag_config.yaml` 声明 `kag_solver_pipeline` 管道配置，然后在项目目录内用 Python API 同步/异步调用**：

```python
from kag.common.conf import KAG_CONFIG            # import kag 时 init_env() 从当前目录向上加载最近 kag_config.yaml（Jinja2 以 os.environ 渲染后 yaml.safe_load）
from kag.interface import SolverPipelineABC
from kag.solver.reporter.trace_log_reporter import TraceLogReporter
pipeline = SolverPipelineABC.from_config(KAG_CONFIG.all_config["kag_solver_pipeline"])
answer = await pipeline.ainvoke(query, reporter=TraceLogReporter())
```

本项目实际启动命令（临时验证脚本 `kg/pilot/GovAffair/solver/qa_taskb.py`）：

```bash
cd kg/pilot/GovAffair
env -u https_proxy -u http_proxy -u all_proxy ../../venv/bin/python solver/qa_taskb.py <题数>
```

### 2.3 所用配置（kag_config.yaml 追加段，additive，未动既有段）

管道：`kag_static_pipeline`（`max_iteration: 1`，跳过 finish_judger 额外调用）

| 组件 | 类型 | LLM 调用 | 备注 |
|---|---|---|---|
| planner | `lf_kag_static_planner` + `govaffair_lf_plan`（自建，见 RC1） | 1 次/题 | `solver_llm`（DeepSeek，`max_tokens: 2048`，`enable_check: false`） |
| executor | `kag_hybrid_retrieval_executor`，retrievers=[`kg_cs`,`rc`]，`enable_summary: true` | summary 1 次/题 | `kg_cs`=实体链接+精确一跳（entity linking 走 search_text/向量，不耗 LLM）；`rc`=chunk 向量召回（图中无 Chunk，空召回但留痕）；刻意**不含 `kg_fr`**（其 `fuzzy_one_hop_select`/`ppr_chunk_retriever` 按实体耗 LLM，超预算） |
| merger | `kag_merger`（rrf 融合） | 0 | 多路召回合并 |
| output/deduce executor | `kag_output_executor` / `kag_deduce_executor` | 别名已答=0；兜底=1 | 终轮 Q1 别名已被 summary 回答，未触发兜底 |
| generator | `llm_generator` + `default_refer_generator_prompt`，`enable_ref: true` | 1 次/题 | rerank_by_vector 走 ollama bge-m3（免费） |

`search_api: openspg_search_api` / `graph_api: openspg_graph_api`（经 `knext.search/graph.client` 打 `http://127.0.0.1:8887`，project id=1）。

**每题真实 LLM 调用 = 3 次（规划 1 + 检索后 summary 1 + 生成 1）。**

---

## 3. B2 — 端到端验证证据

### Q1「申领居住证需要提交哪些材料？」— 全链路落地（终轮，日志 `kg/pilot/qa_probe/qa_run_q1_summary.log`）

1. **LF 规划**（DeepSeek，schema 感知）：`Retrieval(s=s1:Affair[申领居住证], p=p1:requireMaterial, o=o1:Material)` → 标准化为本 schema 类型/关系。
2. **实体链接**：`申领居住证` 类型化向量搜索 0 命中 → `Entity` 标签向量搜索 10 节点（全为 Material，被 exclude_types 过滤）→ **文本兜底命中 `GovAffair.Affair` 节点**（top score 26.63，名称 `"申领居住证"`，13 个区县变体）。
3. **图召回**：GQL `MATCH (s:GovAffair.Affair)-[p:requireMaterial]->(o:GovAffair.Material)` 一跳执行，`selected_rels=12`（12 条边）；rc 向量召回 0 chunk（图中无 Chunk，预期）。
4. **summary（DeepSeek）**：`思考: 根据提供的文档，申领居住证的申请材料包括：本人居民身份证或者其它有效身份证明原件及复印件；《广东省居住证业务申请表》；…居住证相片回执…合法稳定就业、合法稳定住所、连续就读证明…` —— 明确引用召回内容。
5. **最终答案（DeepSeek）**：与召回一致的材料清单。
6. **图数据对账**（只读 cypher，`GovAffair` 库）：`申领居住证` 的 Material 节点即 `本人居民身份证或者其它有效身份证明原件及复印件`、`《广东省居住证业务申请表》`、`居住证相片回执/合法稳定就业、住所、就读证明`、`广东省流动人口居住登记申报表` —— **答案与图中节点一一对应，非 LLM 先验编造**（对照：降配轮答案只说"身份证/户口簿、租赁合同、照片"等泛化先验）。

### Q2「食品经营许可的办理流程是什么？」— 召回痕迹充分（降配轮，日志 `qa_run_q1q2_grounded.log`）

- LF：`Retrieval(s=s1:Affair[食品经营许可], p=p1:hasStep, o=o1:ProcessStep)`；实体链接文本兜底命中 `GovAffair.Affair`（top 34.87）；GQL 一跳 `selected_rels=30`。
- 该轮 `enable_summary: false`（RC3），最终答案为 DeepSeek 先验综述（"申请—受理—核查—决定—发证"），未引用图中 30 条环节边；机制性修复已由 Q1 终轮证明（同一管道开 summary 即落地），受 LLM 预算所限 Q2 未以最终配置重跑。

### 真实 DeepSeek 判定

- `solver_llm` 经 YAML merge 复用 `chat_llm`：`type: maas, base_url: https://api.deepseek.com, model: deepseek-v4-flash`，key 取自环境变量 `deepseekapi`（末 4 位 fd71）；任务 A 已冒烟该端点。
- 验证脚本对 `OpenAIClient.__call__/acall` 打点计数，四轮调用数与"规划+summary+生成"路径精确吻合，无隐藏调用；答案为非模板化自然中文（mock 为固定串），可判定为真实 DeepSeek 生成。

---

## 4. B3 — 根因记录（未盲修、未跑大规模索引）

### RC1 默认规划 prompt 无 schema 感知 → 召回为空（已修，solver 侧配置）
- 现象（run1）：规划产出 `Entity[居住证]` + 自创谓词 `申领材料`；`generate_gql_spo_element` 无法把谓词映射为图关系标签 → `gql_rel_labels` 为空 → 一跳召回直接 `[]`（`exact_one_hop_select.recall_graph_data_from_knowledge_base`）。
- 修复：仿 `supplychain` 示例自建 `solver/prompt/logic_form_plan.py`（注册名 `govaffair_lf_plan`），仅以本 schema 中文类型/关系名覆写 few-shot cases；run2 起 LF 标准化为 `Affair[…]/requireMaterial/Material`。

### RC2（前置缺失）节点向量未全量写入 → 实体链接错型（验证期变通，正式修复=大规模索引，未执行）
- 现象（run2）：图中仅 **50 个 Material 节点**有 `_name_vector`（全图 101,883 节点）。entity linking 向量优先（`entity_linking.recall_entity`）：类型化向量搜索 0 命中 → `Entity` 标签向量搜索返回 10 个 **Material** 节点 → 候选非空 → 文本兜底不触发 → 阈值过滤/错型，Affair 永远链不上。
- 变通（本次验证用，非正式修复）：`kg_cs.entity_linking.exclude_types += Material`，使错型候选被过滤后触发 `search_text` 兜底（服务端文本索引可用，`qa_probe/probe_search.py` 实证）。副作用：按名链接"材料"实体的提问会失效——已在配置注释标明。
- **正式前置条件**：全量节点向量化（BatchVectorizer，10 万节点级索引任务）。按任务约束**未执行**；建议列入后续待办，完成后移除该变通并回归。

### RC3（框架设计）`enable_summary: false` 时召回内容到不了生成器（已修，配置）
- 源码定位：`KAGHybridRetrievalExecutor.invoke` 仅当 `retrieved_data.summary` 非空才把 answered alias 传播进 `context.variables_graph`；`Task.get_task_context` 对 `RetrieverOutput` 只取 `.summary`；`llm_generator` 仅读 task context。故 summary 是"召回→生成"的唯一桥梁。
- 现象（run3）：召回 12/30 条边成功，但 Output 任务兜底调 LLM（答案=先验），生成器只见 Output 文本 → 最终答案与召回无关。
- 修复：`enable_summary: true`（每题 +1 次 LLM 调用）。run4 Q1 证实落地。

### 附带发现（不在本任务修复范围）
- **schema v0.2 vs 图数据 v0.1 错位**：服务端 Affair 关系为 `citeLegal_GovAffair.LegalCitation`（0 条边），图中法律依据边为 v0.1 的 `hasLegalBasis`（服务端 schema 无此关系）。当前"法律依据"类提问无法经 LF 图召回；待 v0.2 灌图（HANDOFF 待办 2）后验证。
- **Chunk/向量召回为空**：结构化建图无 Chunk 节点，`rc` 路径与 `reference` 引用列表恒空；如需 chunk 级召回须另建非结构化 chunk 索引。

---

## 5. 约束自查

| 约束 | 结果 |
|---|---|
| openie_llm 保持 mock | ✅ 配置未动；solver 段无任何组件引用 openie_llm；调用计数无异常（mock 与 DeepSeek 同为 maas 类型，若有隐藏调用计数会偏多，实测精确吻合 3 次/题） |
| Neo4j 只读 | ✅ 仅 `MATCH/CALL db.labels` 查询，无写操作 |
| 不动 data/、docker | ✅ 未触碰 |
| 临时脚本位置 | ✅ `kg/pilot/GovAffair/solver/qa_taskb.py`、`solver/prompt/logic_form_plan.py`、`kg/pilot/qa_probe/*`（探针/日志/各轮结果备份），均可留作回归 |
| key 脱敏 | ✅ 报告只写环境变量名与末 4 位；新增配置经 YAML anchor 复用、未新增明文 key |
| 真实 LLM ≤10 次 | ❌ **实际 18 次**（见 §6 披露） |

## 6. 执行过程偏差披露（LLM 调用计数）

| 轮次 | 配置状态 | 题数 | 调用 | 目的/结果 |
|---|---|---|---|---|
| run1 19:18 | 默认规划 prompt | 1 | 3 | 基线 → 召回为空，定位 RC1 |
| run2 19:22 | +govaffair_lf_plan | 2 | 6 | LF 已标准化但链接错型，定位 RC2 |
| run3 19:24 | +exclude_types 变通 | 2 | 6 | 召回成功（12/30 边），定位 RC3 |
| run4 19:28 | +enable_summary | 1 | 3 | Q1 全链路落地 |
| **合计** | | | **18** | 超出"约 10 次"预算 8 次 |

根因：三轮根因诊断为串行依赖，且每题实际 3 次调用（Output 兜底多 1 次）高于预估 2 次；run3 前累计计数误算（9 误记为 6）。单次均为 flash 小 token 调用，费用影响微小，但次数超标如实上报，后续验证应先用 `probe` 级脚本（0 调用）排除 RC1/RC2 类问题再进端到端。

## 7. 后续建议

1. 全量节点向量化（BatchVectorizer）后：移除 `exclude_types: Material` 变通，回归 Q1/Q2 并补测向量召回路径与 `kg_fr`（需单独 LLM 预算）。
2. v0.2（LegalCitation）灌图后：补测"法律依据"类提问（当前 schema/数据错位，见 §4 附带发现）。
3. 若以 `solver/qa_taskb.py` 做回归，建议第 3 题换"实施主体/办理时限"类（实施主体边在 v0.1 图上存在）。
4. 产品模式（服务端 qa 入口 `kag/solver/main_solver.py` 的 `kb/index_list` 体系）未在本次范围，如需 UI 侧问答另开任务。

## 8. 改动与证据文件清单

- 配置：`kg/pilot/GovAffair/kag_config.yaml`（`chat_llm` 行加 `&chat_llm` 锚点；文件尾追加 `kag-solver` 配置段，既有段零改动）
- 脚本：`kg/pilot/GovAffair/solver/qa_taskb.py`（问答+调用计数+trace 落盘）、`kg/pilot/GovAffair/solver/prompt/logic_form_plan.py`（schema 感知规划 prompt）
- 探针（0 LLM 调用）：`kg/pilot/qa_probe/probe_graph.py`、`probe_search.py`
- 证据：`kg/pilot/qa_probe/qa_run_{q1,q1q2,q1q2_grounded,q1_summary}.log`、`qa_taskb_result_run{1,2,3}.json`、`kg/pilot/GovAffair/solver/data/qa_taskb_result.json`
