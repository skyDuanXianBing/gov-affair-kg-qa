# 问答链路回归报告 — 移除 exclude_types 变通后的向量实体链接 + 法律依据提问（HANDOFF 待办1收尾）

> 执行：quality_assurance · 2026-08-10 13:09–13:15 · KAG 0.8.0（`kg/_ref/KAG`，venv 可编辑安装）
> 前置：全量节点向量化终验通过（107,066/107,068 有 `_name_vector`，见 vectorize-final-report.md；"申领居住证" OpenSPG type=vector 命中 Affair score 0.9984）
> 判定：**VALID** — Q1/Q2 全链路通过（向量实体链接生效），Q3 法律依据链路可答（法规名+条款，图数据一致），**唯一缺口：文号（LegalBasis.docNo）未进答案**（单跳规划限制，根因与最小修复建议见 §4，未盲改）
> 运行方式：`cwd=kg/pilot/GovAffair`，`env -u https_proxy -u http_proxy -u all_proxy ../../venv/bin/python /tmp/qa_regress.py`（复用 `solver/qa_taskb.py` 的打点与 TraceLogReporter 设施，Q3 换为法律依据提问），日志 `/tmp/qa_regress_run.log`、结果 `/tmp/qa_regress_result.json`（临时材料，未入仓库）

---

## 1. 结论摘要

| 验收点 | 判定 | 说明 |
|---|---|---|
| exclude_types 变通移除 | **PASS** | `kg_cs.entity_linking.exclude_types` 仅剩 benchmark 基线 `Chunk`；Material 项及变通注释已清理，其余配置零改动（§2） |
| Q1 申领居住证材料（向量路径回归） | **PASS** | 实体链接**类型化向量搜索命中 10 节点（Affair）**，无 Entity 兜底、无文本兜底；召回 requireMaterial 16 条边；答案与图内 5 个 Material 节点一致（§3.1） |
| Q2 食品经营许可流程 | **PASS** | 向量命中 Affair；召回 hasStep 30 条边；答案"收件、受理、审查、决定、制证、送达"与图内 6 个 ProcessStep 节点逐一一致（§3.2） |
| Q3 法律依据（v0.2 新解锁） | **PASS（有缺口）** | 规划 `citeLegal→LegalCitation` 成功，向量命中 Affair，召回 40 条 citeLegal 边；答案含法规名《广东省流动人口服务管理条例》+ 第十四~二十一条，与图内 8 个 LegalCitation 节点一致；**文号未进答案**（§3.3、§4） |
| LLM 预算 | **PASS** | 真实调用 **9 次**（3 题 × 规划/摘要/生成各 1），≤ 15 上限，无兜底/隐藏调用 |

---

## 2. exclude_types 移除说明（唯一改动文件：`kg/pilot/GovAffair/kag_config.yaml`）

改动前（任务B变通）：

```yaml
    recognition_threshold: 0.9
    # 任务B验证期变通（非正式修复）：试点图仅 50 个 Material 节点写入 _name_vector，...
    exclude_types:
      - "Chunk"
      - "Material"
```

改动后：

```yaml
    recognition_threshold: 0.9
    # 2026-08-10 全量节点向量化完成（107,066/107,068 节点有 _name_vector）后，
    # 移除任务B验证期变通 exclude_types: Material；实体链接向量路径已生效（本回归验证）。
    # Chunk 为 benchmark 基线排除项（结构化建图无 Chunk 节点），保留。
    exclude_types:
      - "Chunk"
```

- **diff 仅 2 处**：删除 `- "Material"` 一行 + 4 行变通注释替换为 3 行新注释；`Chunk` 保留（对照 `kg/_ref/KAG/kag/open_benchmark/AffairQA/kag_config.yaml` 基线，其默认即为 `exclude_types: ["Chunk"]`）。
- **未触碰**：`openie_llm`（保持 mock）、`enable_summary: true`、`recognition_threshold: 0.9`、`solver_llm`（deepseek-v4-flash，`max_tokens: 2048`）、`max_iteration: 1`、管道组件清单。
- 配置文件在本次运行中被实际加载（管道构建成功），YAML 合法性由运行本身证明。
- 备份：`/tmp/kag_config.yaml.bak_taskb`（改动前副本）。

## 3. 每题召回证据与答案

> 通用日志证据（每题均有，来自 entity_linking INFO 日志）：`<实体名> Vector-based search completed ... Found 10 nodes` — 即**类型化（GovAffair.Affair）向量检索命中**；对比任务B run2 的"Found 0 nodes → Entity 标签 10 个 Material → search_text 兜底"，本回归**未出现** `Vector-based search with label: Entity` 与文本兜底，证明向量路径生效、变通已无必要。
> 向量分数（0-LLM 探针，2026-08-10 13:14，镜像 entity_linking 同款 embed+search_vector 路径，ollama bge-m3 + OpenSPG 8887）：

| 查询 | label=GovAffair.Affair top5 | Entity 标签 top1 |
|---|---|---|
| 申领居住证 | 5/5 命中 `"申领居住证"`，score **0.9984** | 0.9985 |
| 食品经营许可 | 5/5 命中 `"食品经营许可"`，score **0.9995** | 0.9996 |

### Q1「申领居住证需要提交哪些材料？」— PASS（全链路）

1. **LF 规划**（真实 DeepSeek）：`Retriever(s=s1:Affair[申领居住证], p=p1:requireMaterial, o=o1:Material)`。
2. **实体链接**：`申领居住证` 类型化向量搜索 **Found 10 nodes**（全为 Affair，score 0.9984 ≥ recognition_threshold 0.9）；无 Entity 兜底、无 search_text 兜底。
3. **图召回**：GQL 一跳执行，`selected_rels=16`（链接实体为多个 `"申领居住证"` 区县变体 Affair 节点，图内 13 变体各 3–4 条边，16 = 4 变体 × 4；全图该事项 requireMaterial 共 43 条）。
4. **summary + 最终答案**：本人居民身份证或其他有效身份证明原件及复印件；《广东省居住证业务申请表》；广东省流动人口居住登记申报表；代办情形下委托人/代办人身份证明、相片回执、就业/住所/就读证明。
5. **图数据对账**（只读 cypher）：图内 5 个 distinct Material 节点 = 身份证材料 /《广东省居住证业务申请表》/ 广东省流动人口居住登记申报表 / 代办材料组 / "居住证-申领居住证"（表格类）——答案与图一致（4/5 项显式列出；"居住证-申领居住证"一项为摘要省略，见 §5 遗留 3）。

### Q2「食品经营许可的办理流程是什么？」— PASS（全链路，真实生成）

1. **LF 规划**：`Retriever(s=s1:Affair[食品经营许可], p=p1:hasStep, o=o1:ProcessStep)`。
2. **实体链接**：类型化向量搜索 **Found 10 nodes**（score 0.9995）。
3. **图召回**：`selected_rels=30`（全图该事项 hasStep 共 356 条，60 变体各 5–6 条；30 = 5–6 变体聚合）。
4. **summary + 最终答案**："收件、受理、审查、决定、制证、送达"。
5. **图数据对账**：图内 distinct ProcessStep 名称恰为这 6 项，**逐一对应**（区别于任务B降配轮的 LLM 先验泛化答案，本次 grounded）。
6. 备注：该题耗时 68.6s（LLM 单次响应慢，非链路故障；另有 `aiolimiter` AsyncLimiter 跨 loop 复用告警，框架既有告警，无功能影响）。

### Q3「申领居住证的法律依据是什么？」— PASS（链路可答）/ 文号缺口（详见 §4）

1. **LF 规划**：`Retriever(s=s1:Affair[申领居住证], p=p1:citeLegal, o=o1:LegalCitation)`（few-shot 中"特种设备使用登记的法律依据"示例模式被正确泛化复用）。
2. **实体链接**：类型化向量搜索 **Found 10 nodes**（score 0.9984）。
3. **图召回**：`selected_rels=40`（全图该事项 citeLegal 共 104 条，13 变体各 8 条；40 = 5 变体 × 8）。
4. **summary + 最终答案**：《广东省流动人口服务管理条例》第十四条（第一款、第二款）…第二十一条，含各条款明细。
5. **图数据对账**：图内 8 个 distinct LegalCitation 名称与答案条文完全一致（第十四~二十一条，条款号全对）。
6. **缺口**：图内 `LegalCitation -partOf-> LegalBasis` 存在且唯一：title=《广东省流动人口服务管理条例》，docNo=**广东省第十三届人民代表大会常务委员会公告第50号**——但 **docNo 未进入最终答案**（§4）。

## 4. Q3 文号缺口：根因与最小修复建议（未盲改）

- **现象**：Q3 答案含法规名+条款（图数据 grounded），但缺文号；链路停留在 LegalCitation 一跳，未触达 `partOf → LegalBasis.docNo`。
- **根因**：规划层与执行层均为**单跳**——
  1. `solver/prompt/logic_form_plan.py` 的法律依据 few-shot 只示范 `Retrieval(s1:政务事项, p1:引用法条, o1:法条引用)` 单步 + output，未示范 `Retrieval(s=o1, p2:所属法规, o2:法律依据)` 第二跳 → 规划器按示例产出单跳 LF；
  2. 执行器 `path_select: exact_one_hop_select` 只做精确一跳（`Retriever` 任务内不会再探索 partOf）；
  3. `max_iteration: 1` + 静态管道下，规划未产出第二步 Retrieval 就没有后续补齐机制。
- **最小修复建议**（参考 `logic_form_plan.py` 现有结构，下轮实施，本轮不擅改）：
  - 在法律依据 few-shot 中改为两跳示例：
    ```
    Step1: Retrieval(s=s1:政务事项[`申领居住证`], p=p1:引用法条, o=o1:法条引用)
    Step2: Retrieval(s=o1, p=p2:所属法规, o=o2:法律依据)
    Step3: output(o2)
    ```
  - 若两跳 LF 下 `exact_one_hop_select` 对 alias 主语（o1）的链式召回不生效，再评估 `path_select` 换 `multi_hop_select` 或放开 `max_iteration`——需先以小样本验证，避免盲目扩大 LLM 调用。
- **影响面**：仅"文号"缺失；法规名与条款已答且与图一致，对"法律依据是什么"类提问的主体部分已可回答。

## 5. 约束自查与遗留

| 约束 | 结果 |
|---|---|
| 真实 LLM 调用 ≤ 15 | ✅ **9 次**（3/题，规划+摘要+生成；打点方式与任务B一致，无 Output 兜底调用） |
| Neo4j 只读 | ✅ 仅 MATCH 查询（`govaffair` 库），无写操作 |
| 不动 schema/容器/data/ckpt | ✅ 未触碰 |
| 不装依赖 / 不提交 git | ✅ 未安装；仓库非 git（无提交） |
| key 脱敏 | ✅ 本报告不含任何 key；LLM 端点沿用既有 `solver_llm`（deepseek-v4-flash） |
| changed files | ✅ **仅 `kg/pilot/GovAffair/kag_config.yaml`**；临时脚本/日志均在 /tmp |

遗留：

1. **Q3 文号**：按 §4 建议补两跳 few-shot 后回归（预计 +0 LLM 调用/题）。
2. **pre-existing 注释偏差**：`kag_config.yaml` 头部 solver 段注释仍写 "enable_summary: false"（实际 true，任务B遗留），本次按"仅此一处改动"未动，建议下轮顺手更正。
3. **摘要完整性**：Q1 图内材料 "居住证-申领居住证"（表格类节点）被生成摘要省略；召回本身完整（16 边），属 LLM 摘要取舍，非链路缺陷。
4. **rc chunk 召回恒空**：结构化建图无 Chunk 节点，预期内，未变。
5. **LegalCitation 2 节点缺向量**（向量化轮遗留，全图 0.0019%）：与 Q3 无关（申领居住证引用的 8 个法条节点全部命中）。
6. 证据文件：`/tmp/qa_regress_run.log`（含每题 INFO 检索痕迹 + LF + summary + 最终答案）、`/tmp/qa_regress_result.json`（trace_summary + LLM 计数）、`/tmp/kag_config.yaml.bak_taskb`（改动前配置）。

## 6. 实际命令与退出码

| 命令 | 退出码 | 说明 |
|---|---|---|
| `cd kg/pilot/GovAffair && cp kag_config.yaml /tmp/kag_config.yaml.bak_taskb` + 定点替换（python，含断言） | 0 | 配置改动，diff 已核（§2） |
| `env -u https_proxy -u http_proxy -u all_proxy ../../venv/bin/python /tmp/qa_regress.py`（tee /tmp/qa_regress_run.log） | 0 | 3 题端到端，9 次真实 LLM 调用 |
| 只读 cypher 对账（neo4j driver，govaffair 库） | 0 | Q1/Q2/Q3 图数据 grounding 核验 |
| 0-LLM 向量分数探针（ollama embed + knext SearchClient.search_vector） | 0 | 申领居住证 0.9984 / 食品经营许可 0.9995 |

**回归结论：exclude_types 变通移除后，实体链接向量路径对 Q1/Q2/Q3 全部生效（类型化向量搜索命中 Affair、无文本兜底、分数 0.9984+）；Q1/Q2 全链路通过且答案与图一致；Q3 法律依据链路可答（法规名+条款 grounded），文号缺口已定位根因并给出最小修复建议，未盲改。HANDOFF 待办1 收尾完成。**
