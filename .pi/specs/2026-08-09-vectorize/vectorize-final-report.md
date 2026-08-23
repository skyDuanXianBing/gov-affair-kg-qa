# 全量节点向量化 — 最终验证报告（全量完成，终验通过）

> 任务：等待全量节点向量化完成（`run_vec_resilient.sh` 守护）→ 全量覆盖率终验（V2）→ 向量检索命中终验（V1）→ 最终报告
> 执行：impl_green_coder（子代理），监控 11:12 → 13:02，完成标记出现后执行终验
> 上游文档：`.pi/specs/2026-08-09-vectorize/需求规格说明书.md`（验收 V1-V4）；前置部分验证报告（`vectorize-report.md`，11:11 收尾，53.8% 快照）
> 库：Neo4j `govaffair`；向量属性 `_name_vector`（另有 LegalCitation 的 `_content_vector`）；bge-m3 1024 维，本地 ollama

## 0. 结论（TL;DR）

- **✅ 全量向量化完成**：2026-08-10 09:12:15 → 13:01:14（**3 小时 48 分 59 秒**），`run_vec_resilient.final` = **rc=0**，**restart #0、0 次冻结**，与守护日志一致。
- **✅ V2 全量覆盖率终验通过**：`_name_vector` 节点 **107,066 / 107,068 = 99.998%**（> 98% 达标线），与修正预期公式**完全一致**（107,068 − 0 无 name − 2 LegalCitation 真实失败）；12 类型中 11 类 100%，唯一缺口 LegalCitation 5,183/5,185（99.96%），失败项与前期报告核实的 2 个节点**完全同一**（图内实测无向量，真实失败非幻影）。
- **✅ V1 向量命中终验通过**：查询 "申领居住证" 经 bge-m3 向量化后走 OpenSPG `type=vector` 搜索（entity_linking 同款路径），**top-5 全部命中 GovAffair.Affair 的 "申领居住证" 节点，score 0.9984**（Entity 标签 0.9985）；Neo4j 图内 `_name_vector` 余弦独立复核 **cos = 1.0000**（≫ 0.6 阈值），判别对照 "注销居住证" 0.7615、"食品经营许可" 0.5889。
- **遗留（不阻塞）**：LegalCitation 2 个超长 content 节点永久缺向量（占全图 0.0019%），修复需单独变更（截断/分块或排除 content 向量化），需审批后执行；V3 问答回归属另一任务（本任务禁止）。

## 1. 运行过程与完成状态

| 项目 | 实测 |
|---|---|
| 启动 | 2026-08-10 09:12:15（`run_vec_resilient.status`：start vectorize-indexer pid=19414, restart #0） |
| 完成 | **2026-08-10 13:01:14**，`run_vec_resilient.final` = **`rc=0`**（status 第 2 行：indexer exited rc=0） |
| 总耗时 | **3h48m59s**（09:12:15 → 13:01:14；前置监控预估 13:10 前后，实际略早） |
| 重启/冻结 | **restart #0、0 次 FREEZE**（status 全程仅 2 行，无冻结记录） |
| 进程 | 守护 19412 / indexer 19414 随 rc=0 正常退出（退出后 ps 无残留） |
| 日志 Done 记录 | 12 条（8/7/5/49/5561/9435/1219/5185(2 failures)/52396/7370/15831/10000），**合计 107,068，成功 107,066** |
| 进度采样 | `csv_v2/index_vec_progress.log`：21 条（09:15→11:03，前置）+ 本次追加 17 条（11:19→12:55，61,438→105,544）= **38 条** |

## 2. 全量覆盖率终验（V2，cypher 实测，13:05 快照）

| 类型 | 节点总数 | 有 `_name_vector` | 覆盖率 | 状态 |
|---|---|---|---|---|
| GovAffair.ProcessStep | 52,396 | 52,396 | 100% | ✅ |
| GovAffair.CrossRegionHandling | 15,831 | 15,831 | 100% | ✅ |
| GovAffair.Affair | 10,000 | 10,000 | 100% | ✅ |
| GovAffair.Material | 9,435 | 9,435 | 100% | ✅ |
| GovAffair.ResultDocument | 7,370 | 7,370 | 100% | ✅ |
| GovAffair.ImplementingOrg | 5,561 | 5,561 | 100% | ✅ |
| GovAffair.LegalCitation | 5,185 | 5,183 | 99.96% | ⚠ 2 真实失败（§4） |
| GovAffair.LegalBasis | 1,219 | 1,219 | 100% | ✅ |
| GovAffair.ThemeCategory | 49 | 49 | 100% | ✅ |
| GovAffair.AffairType | 9 | 9 | 100% | ✅（含概念父节点"行政权力"） |
| GovAffair.ServiceTarget | 8 | 8 | 100% | ✅（含概念父节点"法人"） |
| GovAffair.ExerciseLevel | 5 | 5 | 100% | ✅ |
| **合计（Entity 标签）** | **107,068** | **107,066** | **99.998%** | ✅ |

### 2.1 全局计数核对（均通过）

- `_name_vector` 总数 **107,066** = 分类型求和一致；`_content_vector` 总数 **5,183**（= LegalCitation 5,183/5,185，仅该类型有长文本 content）。
- 无 name 节点：**0**（`MATCH (n) WHERE n.name IS NULL`）。
- 维度校验：**无任何节点 `size(_name_vector) ≠ 1024`**（全图 107,066 个向量均为 1024 维）。
- 非 LegalCitation 缺向量节点：**0**（`_name_vector IS NULL AND NOT (n:GovAffair.LegalCitation)` 空集）。
- **预期公式核对**：107,068（全节点，含概念父节点）− 0（无 name）− 2（LegalCitation 真实失败）= **107,066** ✅ 与实测完全一致（规格书原公式 "-2 概念父节点" 已被前置报告推翻：概念父节点实测有向量）。

## 3. 向量检索命中终验（V1）

**方法**（镜像 KAG solver entity_linking 的真实路径）：查询文本 "申领居住证" → 本地 ollama `bge-m3` embedding（1024 维）→ `knext SearchClient.search_vector(label, property_key="name", query_vector)`（OpenSPG 搜索 API 的 type=vector 检索，`knext/search/client.py` 的 `search_vector_post`）；另以 Neo4j 图内 `_name_vector` 余弦相似度独立复核（任务允许的兜底方式）。

### 3.1 OpenSPG 向量检索（entity_linking 同款路径）

```
EMBED dim: 1024 model: bge-m3
VECTOR_SEARCH label=GovAffair.Affair -> 5 hits, top-5 全部命中：
  score=0.9984  name="申领居住证"  id=TE44140344102034925442106041002
  score=0.9984  name="申领居住证"  id=TE44142444102268635442106041002
  score=0.9984  name="申领居住证"  id=TE44078344103056365442106041002
  score=0.9984  name="申领居住证"  id=TE44078344103081125442106041002
  score=0.9984  name="申领居住证"  id=11441403007215469Q5442106041002
VECTOR_SEARCH label=Entity -> 5 hits，同样全部 "申领居住证"，score=0.9985
```

→ **"申领居住证" 经向量路径命中 Affair 节点，score 0.9984 ≫ 0.6**，且 top-5 无一误报。

### 3.2 Neo4j 图内 `_name_vector` 余弦独立复核

| 对（查询 "申领居住证" vs Affair 节点） | cos 相似度 | 维度 |
|---|---|---|
| vs "申领居住证"（目标节点） | **1.0000** | 1024 |
| vs "注销居住证"（同域近义对照） | 0.7615 | 1024 |
| vs "食品经营许可"（异域对照） | 0.5889 | 1024 |

→ 目标 cos = 1.0000 ≫ 0.6 阈值；判别力成立（同域 0.76 > 异域 0.59）。注：Affair 名称在图中带字面引号（如 `"申领居住证"`），复核查询已按此处理；"营业执照" 无精确同名 Affair 节点（`CONTAINS` 命中 2 个为跨域材料名），对照集改用上表两项。

## 4. 失败项核验（V4 对账）

- **唯一失败集**：LegalCitation 2 个节点，与前置报告核实的**完全相同**：
  - `LC-564e89f008bcf2a0d843e70a`（"市场监管总局关于公布《食品经营许可审查通则》的公告"，content 9,643 字符）
  - `LC-1c9db8c39b04aae4bee1d989`（"国家税务总局关于《出口货物劳务增值税和消费税管理办法》有关问题的公告 第五条"，content 3,431 字符）
- **真实失败非幻影**：图中实测 `_name_vector` 与 `_content_vector` 均缺失（非服务端已写、客户端丢响应的 IncompleteRead 类幻影）；日志 `Done process 5185 records, with 5183 successfully processed and 2 failures encountered.` + openai_model 400 `input length exceeds the context length`（bge-m3 上下文 8192 tokens，name+content 超长）。
- **其余 11 个类型 Done 记录均 0 failures**（含 ProcessStep 52,396、CrossRegionHandling 15,831、Affair 10,000、ResultDocument 7,370 四个后段大类型）。
- 影响面：占 LegalCitation 0.04%、全图 0.0019%；不影响 V2（99.998% > 98%）与 V1 命中。

## 5. V1-V4 验收对照

| 标准 | 结果 |
|---|---|
| V1 小样 + 搜索命中 | ✅ 前置已确认；本轮**终验**：向量路径 "申领居住证" top-5 全中 Affair（0.9984），图内余弦 1.0000 |
| V2 全量覆盖率 > 98% | ✅ **99.998%（107,066/107,068）**，与修正预期公式完全一致；12 类型中 11 类 100%，无 name=0，维度全 1024 |
| V3 问答回归 | ⏳ 另一任务（计费项，本任务禁止运行）；前置条件已具备：向量路径命中已终验通过 |
| V4 0 indexer failures | ⚠ **2 failures**（LegalCitation 超长 content，真实失败已核验、与图内实测一致）；无幻影失败 |

## 6. 遗留风险与建议

1. **LegalCitation 2 项缺向量（永久遗留，需单独变更）**：修复建议——向量化前对 content 截断/分块，或 schema 排除 content 属性向量化（实体链接只用 name 向量，content 向量非必需）；重跑该 2 项幂等可补（SPG upsert）。属 schema/管线变更，需审批后另派任务。
2. **V3 问答回归未执行**（任务禁止）：建议下一步派发 QA 回归（移除 `exclude_types: Material` 变通后 Q1/Q2 + 法律依据类提问），本轮终验已证明其前置条件（Affair/Material 等全类型向量 + 向量路径命中）成立。
3. **幻影失败监控**：本次 12 个 Done 记录中仅 LegalCitation 报 failures 且已核验为真实失败，无 v0.2 式 IncompleteRead 幻影；后续重跑若再报 failures 仍应以图内实测为准。
4. 名称含字面引号（数据侧既有特征）：向量检索不受影响（bge-m3 输入为 name 原值），但对账/查询脚本需注意引号转义。

## 7. 证据清单

- 完成标记：`csv_v2/run_vec_resilient.final` = `rc=0`；状态：`csv_v2/run_vec_resilient.status`（2 行：start pid=19414 restart #0 → indexer exited rc=0，无 FREEZE）
- 日志：`csv_v2/indexer_vec_resilient.log`（12 条 Done 记录，唯一 failures 为 LegalCitation 2）
- 进度：`csv_v2/index_vec_progress.log`（38 条：09:15→12:55，1,392→105,544）
- 覆盖率/维度/失败项 cypher 实测（govaffair 库，13:05 快照）；探针脚本 `/tmp/vec_probe.py`（embed + search_vector + 余弦复核）
- 环境健康：ollama 127.0.0.1:11434 与 192.168.111.177:11434 均 200；OpenSPG 127.0.0.1:8887 200；en0=192.168.111.177
- 范围遵守：未重跑 indexer/向量化、未跑问答/LLM、未改 schema/配置/容器/data、未装依赖、未删 builder/ckpt；非 git 仓库（无暂存/提交）

**终验结论：全量向量化 rc=0 完成（3h49m，0 重启 0 冻结）；覆盖率 107,066/107,068 = 99.998% 与预期公式完全一致；"申领居住证" 向量路径命中 Affair（0.9984/余弦 1.0000）通过；唯一失败为 2 个 LegalCitation 超长 content 节点（真实失败，遗留修复建议）。V1/V2 验收达标，V4 仅 2 个已核验真实失败，V3 待另派回归任务。**
