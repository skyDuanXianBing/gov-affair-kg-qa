# 全量节点向量化监控与收尾验证报告（部分验证，未跑完）

> 任务：监控 `run_vec_resilient.sh` 守护的全量节点向量化 → 覆盖率验证 → 向量检索命中验证 → 报告
> 执行：impl_green_coder（子代理），监控起止 09:13 → 11:11；**按主代理指示提前收尾**（全量未完成，最终验证由主代理另行派发）
> 依赖文档：`.pi/specs/2026-08-09-vectorize/需求规格说明书.md`（验收 V1-V4）；`kg/pilot/qa_probe/probe_search.py`；`HANDOFF.md` §三待办1
> 库：Neo4j `govaffair`；向量属性 `_name_vector`（bge-m3，1024 维，本地 ollama）

## 0. 结论（TL;DR）

- **守护进程健康**：09:12:15 启动，全程 **restart #0、0 次冻结 kill**，indexer PID 19414 与守护 PID 19412 存活至收尾时刻；无 PID 消失但 final 缺失的情况。
- **完成度（11:11 快照）**：`_name_vector` 节点 **57,564 / 107,068（53.8%）**；9/12 类型已过完（其中 7 类 100%、LegalCitation 99.96%），ProcessStep 进行中（68.9%），ResultDocument/CrossRegionHandling/Affair 尚未开始。
- **已发现 1 个真实失败**：LegalCitation 2 个节点向量化失败（含全文 content 属性超 bge-m3 上下文长度），非幻影失败，图中确认无向量；详见 §5。
- **修正预期公式**：概念父节点（"行政权力"/"法人"）**实测有向量**（AffairType 9/9、ServiceTarget 8/8，dim=1024），推翻规格书"父节点不会生成向量"的假设 → 全量完成后预期 = 107,068（无 name 节点数 = 0）− 2（LegalCitation 失败项）= **107,066**，覆盖率 99.998%。
- **检索验证（部分）**：OpenSPG 搜索 API 正常，"申领居住证" 文本搜索命中 Affair 节点（score 27.7）；`_name_vector` 余弦抽查 3 类型（Material/LegalBasis/LegalCitation）同型相似对 cos 0.79–0.89、跨型对照 0.42，全部 dim=1024。Affair 阶段未跑，"申领居住证" 向量路径命中留待全量完成后终验。

## 1. 运行过程监控摘要

| 项目 | 实测 |
|---|---|
| 启动 | 2026-08-10 09:12:15（`run_vec_resilient.status`：start vectorize-indexer pid=19414, restart #0） |
| 守护脚本 | `csv_v2/run_vec_resilient.sh`（CPU 冻结 5 分钟 kill -9，最多 4 次重启；由主代理 nohup+disown 启动，宿主重启除外不会死） |
| 重启/冻结 | **0 次重启、0 次冻结**（收尾时刻 status 仅 1 行 start 记录） |
| 完成标记 | `csv_v2/run_vec_resilient.final` **尚未出现**（收尾时刻，进程仍在跑） |
| 速率 | 09:15–10:19 ≈ 530 节点/分；10:19 后 ≈ 250–470 节点/分（ProcessStep 阶段衰减）；tqdm 当前窗口 ~8 it/s |
| 预计完成 | 按当前速率约 **13:10 前后**（ProcessStep 余 ~34 分钟 + ResultDocument 7,370 + CrossRegionHandling 15,831 + Affair 10,000 ≈ 33,200 项 ≈ 70 分钟） |
| 进度采样 | `csv_v2/index_vec_progress.log` 共 21 条（09:15 → 11:03，1,392 → 54,138），收尾时手动补采 57,564 |

## 2. 部分覆盖率表（V2，cypher 实测，11:11 快照）

| 类型 | 节点总数 | 有 `_name_vector` | 覆盖率 | 状态 |
|---|---|---|---|---|
| GovAffair.ProcessStep | 52,396 | 36,095 | 68.9% | 🔄 进行中 |
| GovAffair.CrossRegionHandling | 15,831 | 0 | 0% | ⏳ 未开始 |
| GovAffair.Affair | 10,000 | 0 | 0% | ⏳ 未开始 |
| GovAffair.Material | 9,435 | 9,435 | 100% | ✅ |
| GovAffair.ResultDocument | 7,370 | 0 | 0% | ⏳ 未开始 |
| GovAffair.ImplementingOrg | 5,561 | 5,561 | 100% | ✅ |
| GovAffair.LegalCitation | 5,185 | 5,183 | 99.96% | ⚠ 2 失败（§5） |
| GovAffair.LegalBasis | 1,219 | 1,219 | 100% | ✅ |
| GovAffair.ThemeCategory | 49 | 49 | 100% | ✅ |
| GovAffair.AffairType | 9 | 9 | 100% | ✅（含概念父节点"行政权力"） |
| GovAffair.ServiceTarget | 8 | 8 | 100% | ✅（含概念父节点"法人"） |
| GovAffair.ExerciseLevel | 5 | 5 | 100% | ✅ |
| **合计** | **107,068** | **57,564** | **53.8%** | 运行中 |

> 表注：节点总数 107,068 与 v0.2 对账一致（12 类含 2 个 isA 概念父节点）；"无 name 节点数" 全图 = **0**（`MATCH (n) WHERE n.name IS NULL`）。

### 2.1 全局计数核对

- `_name_vector` 总数：**57,564**（= 分类型求和，一致）
- `_content_vector` 总数：**5,183**（= LegalCitation 5,183/5,185；仅该类型有 content 长文本属性，同样 2 个失败节点缺失）
- 无 name 节点：**0**
- 预期公式修正：`预期最终向量数 = 107,068（全节点，含概念父节点）− 0（无 name）− 2（LegalCitation 真实失败）= 107,066`（原规格书 "-2 概念父节点" 假设被实测推翻，父节点有向量）
- 已过完的 8 个节点类型（AffairType/ServiceTarget/ExerciseLevel/ThemeCategory/ImplementingOrg/Material/LegalBasis/LegalCitation）：**累计 21,471 项中 21,469 有向量（99.99%）**，仅 2 项失败

## 3. 向量检索命中验证（V1 规模化验证，部分）

采用任务允许的**双路径**方式（任选其一即可，此处两种都做了）：

### 3.1 OpenSPG 搜索 API（probe_search.py 原样复用，只读、无 LLM 计费）

```
TEXT 申领居住证   -> score 27.74，命中 GovAffair.Affair 节点（"申领居住证"，公共服务，5工作日，windowProcess 完整）✅
TEXT 居住证      -> score 22.06，命中 Affair（"注销居住证"）✅
TEXT 营业执照    -> score 13.76，命中 Affair（中山市残疾人创业场地租金补贴）✅
TEXT 申领居住证 (nolabel) -> 同 top1 ✅
```
说明：`search_text` 走文本索引（Lucene），与向量无关；**"申领居住证" 的向量路径命中（实体链接向量检索）须待 Affair 阶段完成后终验**——图中已确认存在名为"申领居住证"的 Affair 节点（且 10,000 个 Affair 均无向量，现时点向量路径必然 miss，属预期）。

### 3.2 Neo4j `_name_vector` 余弦相似度抽查（小样已验证的兜底方式，3 类型 + 跨型对照）

| 对 | 类型 | cos 相似度 | 维度 |
|---|---|---|---|
| 广东省政府"将一批省级行政职权事项调整由…实施的决定"（广州/深圳版 vs 前海版） | LegalCitation | **0.829** | 1024 |
| "身份证" vs "居民身份证" | Material | **0.889** | 1024 |
| "广东省人民政府令第307号" vs "广东省人民政府令第283号" | LegalBasis | **0.785** | 1024 |
| 跨类对照："身份证"(Material) vs "广东省人民政府令第307号"(LegalBasis) | 跨类 | **0.425** | 1024 |

结论：同类型相似文本 cos 0.79–0.89 ≫ 跨类对照 0.42，向量语义区分度成立；抽查全部节点 `size(_name_vector)=1024`（含概念父节点、含引号污染的旧名称），向量非空。与 v0.1 冒烟结论（同类 0.68 > 异类 0.42）方向一致且更优。

## 4. 已发现异常与根因

### 4.1 LegalCitation 2 个真实失败（非幻影）

- 失败项：`LC-564e89f008bcf2a0d843e70a`（"市场监管总局关于公布《食品经营许可审查通则》的公告"，name 长 27 字符）与 `LC-1c9db8c39b04aae4bee1d989`（"国家税务总局关于《出口货物劳务增值税和消费税管理办法》有关问题的公告 第五条"，name 长 40 字符）。
- 日志证据（`indexer_vec_resilient.log`，09:48:27–09:49:26）：
  - `Done process 5185 records, with 5183 successfully processed and 2 failures encountered.`
  - `ERROR - kag.common.vectorize_model.openai_model - Error: Error code: 400 - {'error': {'message': 'the input length exceeds the context length', ...}}`（×6，两次重试）
  - 错误输入即 **name + 节点全文 content 属性**（如《食品经营许可审查通则》全文，约 1.5 万+ 字符），远超 bge-m3 上下文（8192 tokens）→ `TypeError: 'NoneType' object is not iterable` → 整项失败。
- 影响：该 2 节点 `_name_vector` 与 `_content_vector` 均缺失（图中实测无向量，非服务端已写、客户端丢响应的幻影失败）；占 LegalCitation 0.04%、全图 0.0019%。**根因在数据侧（content 超长）而非守护/管线侧**。
- 修复建议（后续任务，本次禁止改动）：向量化前对 content 截断/分块，或 schema 中排除 content 属性的向量化（实体链接只用 name 向量，content 向量非必需）；重跑该 2 项幂等可补（SPG upsert）。

### 4.2 规格书假设修正：概念父节点有向量

"行政权力"（AffairType 父）与"法人"（ServiceTarget 父）实测均带 `_name_vector`（dim 1024），AffairType 9/9、ServiceTarget 8/8。规格书"已知盲区：概念父节点不会生成向量"不成立（小样试跑时即已如此：任务背景称 AffairType 9/9）。对覆盖率是利好，V2 验收公式需按 §2.1 修正。

### 4.3 速率衰减（观察项，非异常）

ProcessStep 阶段（长 name 少、但项数 52,396 最大）实际速率 ~250–470 节点/分，低于早段 ~530；tqdm 当前窗口 ~8 it/s。未触发冻结判定（60s 采样 CPU 时间仍在增长）。

## 5. 证据清单

- 守护状态：`csv_v2/run_vec_resilient.status`（仅 1 行：09:12:15 start pid=19414 restart #0；**无 FREEZE 记录**）
- 完成标记：`csv_v2/run_vec_resilient.final` 收尾时不存在（任务未完成，进程存活）
- 进程：ps 实测 indexer PID 19414（`indexer_vec.pid`）、守护 19412、启动壳 19410 均存活
- 日志：`csv_v2/indexer_vec_resilient.log`（157KB+；Done 记录 8 条：49/5561/9435/1219/5185(2 failures)/8/7/5；LegalCitation 失败 Traceback + openai_model 400 错误）
- 进度采样：`csv_v2/index_vec_progress.log`（21 条，09:15→11:03）
- 环境健康：ollama `127.0.0.1:11434` 与 `192.168.111.177:11434` 均 200；OpenSPG `127.0.0.1:8887` 200；en0=192.168.111.177（与 kag_config.yaml 一致）
- 对账/抽查 cypher 均在 `govaffair` 库执行；probe 输出见 `/tmp/probe_out.txt`（本次运行）
- 范围遵守：未重跑 indexer/向量化、未跑问答/LLM、未改 schema/配置/容器/data/、未装依赖、未删 builder/ckpt；本目录非 git 仓库（无暂存概念）

## 6. 风险与遗留

1. **全量未完成**：ResultDocument/CrossRegionHandling/Affair 3 类 + ProcessStep 余量待跑（约 49,502 项，预计 13:10 前后）。最终 V2 对账（期望 107,066/107,068）与 V1 "申领居住证" 向量命中终验、V3 问答回归（移除 exclude_types: Material）需主代理在 `run_vec_resilient.final`（rc=0）出现后另行派发。守护脚本继续运行，无需干预。
2. **LegalCitation 2 个超长 content 失败**（§4.1）：终验时若 rc=0，此 2 项仍会缺向量；覆盖率 99.998% > 98% 达标线，但需在 V3 前决定是否补（建议：向量化排除 content 或截断，属单独变更，需审批）。
3. **幻影失败风险**：v0.2 曾出现服务端已写、客户端丢响应的 IncompleteRead 幻影失败；本次 LegalCitation 2 失败已核验为真实失败（图中无向量），ProcessStep 及后续阶段若报 failures 需同样以图内实测为准核验。
4. 概念父节点带向量使最终预期从 107,066 提升到 107,068−2；规格书 V2 验收公式需更新（已在本报告 §2.1 给出修正）。

## 7. 验收对照（部分，V1-V4）

| 标准 | 现时点结果 |
|---|---|
| V1 小样（AffairType 9/9 + 搜索命中） | ✅ 已由任务背景确认；本轮补充：搜索 API 现验命中 Affair（文本路径）、_name_vector 余弦语义成立 |
| V2 全量覆盖率 > 98% | 🔄 进行中（53.8%）；已过完 8 个类型 99.99%；预期最终 99.998%（修正公式），**待 final 后终验** |
| V3 问答回归 | ⏳ 另一任务（计费项，本任务禁止） |
| V4 0 indexer failures | ⚠ 目前 2 failures（LegalCitation 超长 content，真实失败已核验），ProcessStep 及后续待观察 |

**收尾结论：守护进程健康（restart #0、0 冻结），向量化进度 53.8% 且无异常减速；已过完类型覆盖率 99.99%（唯一缺口 2 个 LegalCitation 超长 content 项，根因明确）；按主代理指示提前收尾，最终对账/检索/回归验证待全量完成后另行派发。**
