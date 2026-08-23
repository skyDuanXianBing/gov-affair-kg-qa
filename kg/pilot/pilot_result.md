# 试点建图结果报告（1 万条，2026-08-09）

> 数据：`kg/pilot/pilot_10000.jsonl`（个人 5,000 + 法人 5,000，seed=42 分层抽样）
> 输入：`kg/pilot/csv/`（14 个 CSV，adapter_stats.json 为准）
> 链路：`SafeCSVScanner → SPGTypeMapping/RelationMapping → KGWriter`（纯结构，无向量化、无 LLM 抽取）
> 环境：OpenSPG docker 全家桶（kg/deploy/），Neo4j 库名 `govaffair`，项目 id=1
> 执行：`kg/pilot/GovAffair/` 项目目录内 `python kg/build/indexer.py --data-dir kg/pilot/csv`

## 1. 建图总耗时：75 分钟（16:45:22 → 18:00:25，exit 0，全部文件 0 failures）

| 阶段 | 内容 | 大致耗时 |
|---|---|---|
| 概念+共享实体 | AffairType/ServiceTarget/ExerciseLevel/ThemeCategory、ImplementingOrg/Material/LegalBasis | ~10 min |
| 弱实体 | ProcessStep(52,396)/ResultDocument/CrossRegionHandling | ~25 min |
| Affair 节点+语义边 | 10,000 节点及 139k 语义边 | ~10 min |
| requireMaterial | 53,508 边 | ~8 min |
| hasLegalBasis | 135,318 行（热点法条节点阶段明显变慢，最低 ~500 边/分） | ~13 min |
| nextStep | 42,410 边（78 it/s 匀速） | ~9 min |

吞吐瓶颈在 openspg-server 写路径（server CPU 63%、mysql 38%、neo4j 9.5%、客户端 6%），
热点法条节点（1,219 节点承载 13.5 万引用）是 hasLegalBasis 阶段变慢主因。

## 2. 节点/边对账表（Neo4j cypher 实测 vs 适配器期望值）

| 项 | 期望（expected_counts.py） | 实测 | 结论 |
|---|---|---|---|
| **节点合计** | 101,883 | **101,883** | ✅ 完全一致 |
| Affair | 10,000 | 10,000 | ✅ |
| ImplementingOrg | 5,561 | 5,561 | ✅ |
| Material | 9,435 | 9,435 | ✅ |
| LegalBasis | 1,219 | 1,219 | ✅ |
| ProcessStep | 52,396 | 52,396 | ✅ |
| ResultDocument | 7,370 | 7,370 | ✅ |
| CrossRegionHandling | 15,831 | 15,831 | ✅ |
| 概念节点（含 isA 父节点） | 71 | 71（9+8+5+49） | ✅ |
| **边合计** | 370,275（按行）/ 262,995（按(s,p,o)去重） | **262,995** | ✅ 见下注 |
| implementedBy | 10,000 | 10,000 | ✅ |
| hasStep | 52,396 | 52,396 | ✅ |
| produceResult | 7,370 | 7,370 | ✅ |
| supportCrossRegion | 15,831 | 15,831 | ✅ |
| affairType / theme | 10,000 / 5,000 | 10,000 / 5,000 | ✅ |
| serviceTarget / exerciseLevel | 28,432 / 10,000 | 28,432 / 10,000 | ✅ |
| requireMaterial | 53,508 | 53,508 | ✅ |
| nextStep | 42,410 | 42,410 | ✅ |
| isA | 10 | 10 | ✅ |
| **hasLegalBasis** | 行 135,318，**去重对 28,038** | **28,038** | ⚠️ 边塌缩（§4.1） |

## 3. 抽样比对（3 个事项，cypher 展开 vs pilot_10000.jsonl 原始记录）

基准抽取存于 `kg/pilot/verify_baseline.json`。

| 样本 | 校验点 | 结果 |
|---|---|---|
| 11440100007483180K344010901400901（港澳律所联营设立许可） | 节点属性（名称/状态/承诺1工作日/法定40工作日/不收费/可网办）全对；实施主体=广州市司法局✓；概念=行政权力-行政许可+市级+{自然人,法人-企业法人,法人-社会组织法人}✓；6 环节按序、nextStep 链 5 条（收件→受理→审查→决定→制证→送达）✓；材料边属性（必要/份数/纸质电子化）✓；受理条件 1100→1119 字符（JSON 转义差异，内容完整）✓ | ✅ 通过 |
| 114401005799639727344011203500219（建设工程规划类许可证核发） | 部门=广州空港经济区管理委员会✓；材料边 18/18✓；环节 6、nextStep 5✓；法条 2 引用→1 边（同文号塌缩，符合§4.1） | ✅ 通过 |
| 114401005799639727344011203500229（同名事项另一实施码） | 部门=广州空港经济区管理委员会✓；材料边 4/4✓；环节 6、nextStep 5✓；法条 2 引用→1 边 | ✅ 通过 |

概念树 isA 抽查：10/10 正确（行政权力-{7 类}→行政权力，法人-{企业/事业/社会组织法人}→法人）。

## 4. 实测发现（pilot_plan.md 不确定点的实测结论）

### 4.1 ⚠️ hasLegalBasis 边塌缩（最重要发现，证实 LegalCitation 必须演进）

SPG 边按 (s,p,o) upsert：同一事项引用同一文号的多条款（不同 article）塌缩为 **1 条边**，
article 子属性只保留最后写入值。135,318 行 → 28,038 条边，**79% 条款级引用信息丢失**。
与适配器 `legal_content_conflicts=116,975`（86% 引用条文文本与首见不同）相互印证：
**法条引用必须建模为独立弱实体 LegalCitation（文号+条款级）**，方案已更新至
`kg/design/schema_design.md` §4。requireMaterial 无此问题（53,508 行全部唯一对）。

### 4.2 其余不确定点结论

- **RelationMapping 端点缺失行为**：未触发（先点后边导入顺序有效），无需依赖服务端补节点。
- **TextAndVector 索引**：schema 带标注提交成功；不配真实向量模型时文本侧可用
  （SearchClient 全文检索命中正常），向量侧未生效（符合预期，属下一阶段）。
- **KGWriter upsert 语义**：同 id 覆盖、同 (s,p,o) 覆盖——共享节点设计（只放稳定属性）验证有效。
- **Text 属性在 Neo4j 中以 JSON 引用形式存储**（如 `"20工作日"` 带引号），为 KAG 标准序列化行为，
  UI/检索层正常展示，cypher 直查需注意引号。
- **数据落库位置**：Neo4j 独立库 `govaffair`（小写 namespace），非默认 neo4j 库。

## 5. UI 验证

产品模式 UI（http://127.0.0.1:8887，账号 openspg / 密码已改为 openspg@kag2026，见
`kg/deploy/README.md` §8）→ GovAffair → 知识探查：按名称"建设工程规划类许可证核发"
查询返回 **50 条 GovAffair.Affair** 记录，列表正确展示自定义属性列（投诉电话/事项类型/
法定办结时限/窗口办理流程等），事项类型列为概念路径"行政权力-行政许可"。
截图：`kg/pilot/ui_exploration.png`。SearchClient API 层检索同步验证通过。

## 6. Mock 方案说明

- `kg/deploy/mock_llm.py`（0.0.0.0:18999，nohup 常驻）：OpenAI 兼容
  /v1/chat/completions（固定 "mock-ok"）与 /v1/embeddings（1024 维零向量）。
- `kag_config.yaml` 中 chat_llm/openie_llm/vectorizer 均指向
  `http://192.168.31.80:18999/v1`（macOS en0 IP——宿主机与容器双侧可达的唯一同址方案；
  127.0.0.1 容器内不可达、colima 网关 192.168.5.2 宿主机侧不可达）。
- **仅用于通过 knext project create/update 的连通性校验**，试点纯结构建图未消费任何 LLM 输出。
  正式问答/抽取前必须换真实端点（已备注 kg/deploy/README.md §7）。
- 网络变更导致 en0 IP 变化时需同步更新 base_url 并 `knext project update`。

## 7. 遇到的问题与解法

| 问题 | 解法 |
|---|---|
| `pip install` 后 `import kag` 报 socksio 缺失（ollama 包被 all_proxy 触发） | venv 补装 socksio；运行 kag/knext 的 shell 一律不带代理变量 |
| `knext project create` 服务端校验 vectorizer 失败（容器够不到宿主 127.0.0.1 mock） | mock 改绑 0.0.0.0，base_url 用 en0 IP（双侧可达），§6 |
| UI 首次登录强制改密 | 已重置为 openspg@kag2026 并记录 |
| Neo4j 默认库查不到数据 | OpenSPG 按 namespace 建独立库 `govaffair` |
| 行使层级"镇（乡、街道）级"被顿号误切（适配器 bug，试跑阶段发现） | split_multi 增加 allow_dun=False，该字段不按顿号切 |

## 8. 遗留问题（进入下一阶段前）

1. **实施 LegalCitation 演进**（§4.1，含适配器/Schema/重跑 hasLegalBasis 部分）——最高优先级。
2. 向量能力生效化：换真实 embedding 端点，`knext project update`，评估 TextAndVector 索引。
3. 问答链路：换真实 chat_llm 后跑 solver；当前 mock 下一切 LLM 输出无意义。
4. 全量 157 万条灌图性能预估：按试点吞吐（峰值 ~5k 边/分、热点阶段 ~0.5k/分），
   全量约 2,000 万边，需分批+扩容评估（热点法条节点是主要瓶颈，LegalCitation 拆分后可缓解）。
5. checkpoint 位于 `GovAffair/builder/ckpt/`；重跑需先清理。
