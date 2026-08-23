# 政务事项知识图谱 Schema 设计说明

> 对应 Schema 文件：`kg/design/GovAffair.schema`（OpenSPG SPG MarkLang，namespace `GovAffair`）。
> 数据来源：`data/cleaned/{个人服务,法人服务}/*.jsonl`（schema gdzwfw-large-human-readable-v1），
> 后续可无缝切到 `data/unified/` 归一化分片。
> 建图路线：KAG 结构化构建（schema 优先 + mapping，不走 LLM 抽取）。

## 1. 实体 / 关系 / 概念总览

| 类型 | 类别 | 主键（id） | 说明 |
|---|---|---|---|
| Affair 政务事项 | EntityType | 事项编码（缺失时 `GA-`+md5(名称\|实施主体\|来源)[:24]） | 核心实体，承载全部标量与长文本属性 |
| ImplementingOrg 实施主体 | EntityType | 规范化部门名称 | 跨事项共享 |
| Material 申请材料 | EntityType | 规范化材料名称 | 跨事项共享；份数/形式/必要性等差异信息放边属性 |
| LegalBasis 法律依据 | EntityType | 文号（缺失时 `LH-`+md5(法规名称)[:24]） | 跨事项共享；只放 title/docNo |
| LegalCitation 法条引用 | EntityType | `LC-`+md5(文号\|条款)[:24] | 文号+条款级弱实体（v0.2），承载条文内容 |
| ProcessStep 办理环节 | EntityType | `{事项编码}#P{序号:02d}` | 事项私有弱实体，nextStep 顺序链接 |
| ResultDocument 办理结果 | EntityType | `{事项编码}#R{序号:02d}` | 事项私有弱实体 |
| CrossRegionHandling 跨域通办 | EntityType | `{事项编码}#C{序号:02d}` | 事项私有弱实体（数据稀疏，多数事项为空） |
| AffairType 事项类型 | ConceptType | 概念路径 id | isA 树：`行政权力-行政许可` 等二级 |
| ServiceTarget 服务对象 | ConceptType | 概念路径 id | isA 树：`法人-企业法人/事业法人/社会组织法人` 二级，其余一级 |
| ExerciseLevel 行使层级 | ConceptType | 概念 id | 一级平铺（省级/市级/县级/镇（乡、街道）级/村（社区）级/国家级） |
| ThemeCategory 主题分类 | ConceptType | 概念 id | 一级平铺（法人 48 主题） |

| 关系 | 端点 | 边属性 | 实现方式 |
|---|---|---|---|
| implementedBy 实施主体 | Affair → ImplementingOrg | — | Affair 语义属性（CSV 列=部门 id） |
| requireMaterial 申请材料 | Affair → Material | seq/copies/submitForm/materialType/materialSource/isRequired/note | RelationMapping 关系 CSV |
| citeLegal 引用法条 | Affair → LegalCitation | — | RelationMapping 关系 CSV |
| partOf 所属法规 | LegalCitation → LegalBasis | — | RelationMapping 关系 CSV |
| hasStep 办理环节 | Affair → ProcessStep | — | Affair 语义属性（多值，逗号分隔环节 id） |
| nextStep 下一环节 | ProcessStep → ProcessStep | — | RelationMapping 关系 CSV |
| produceResult 办理结果 | Affair → ResultDocument | — | Affair 语义属性（多值） |
| supportCrossRegion 跨域通办 | Affair → CrossRegionHandling | — | Affair 语义属性（多值） |
| affairType / serviceTarget / exerciseLevel / theme | Affair → 概念 | — | Affair 概念语义属性（自动 isA 挂载） |

## 2. 关键设计决策

### 2.1 什么作实体、什么作属性、什么作概念

- **作实体**（需要被多点复用、或自身还有结构/长文本）：实施主体、材料、法律依据（跨事项共享）；
  办理环节、办理结果、跨域通办（事项私有但本身是重复型复合结构，作实体才能挂属性、连顺序）。
- **作属性**（单值、无复用、无语义层级）：时限、电话、深度、收费、地址、流程文本等全部标量与长文本。
- **作概念**（取值封闭、有分类学意义、需要 isA 推理）：事项类型、服务对象、行使层级、主题分类。
  概念 id 即层级路径（`-` 连接），KAG mapping 自动建 isA 链；概念取值不能含 `-`（已核实现有取值均无）。
- **办理方式**（网上办理/窗口办理/快递申请）取值仅 3 种且无层级，作多值标量属性 `handleMethods`，
  不建概念——减少概念树噪音，后续需要时可平滑升级。
- **常见问答** 89% 为空，试点不入图；后续可增 FAQ 实体或走非结构化 chunk 挂载。
- **状态**字段当前全为"在用"，保留为属性备将来过滤。

### 2.2 主键选取与跨事项共享策略

- **Affair.id = 事项编码**：官方编码天然跨来源唯一（个人侧为 34 位统一编码，法人侧为 32 位 hex）；
  抽样中未发现空编码，适配器仍保留 `GA-`+哈希兜底。
- **ImplementingOrg.id = 规范化部门名称**（去首尾空白、全角空格归一）。不做激进归一
  （如"广东省教育厅"与"省教育厅"视为不同节点），避免错误合并；同名字符串精确匹配才共享。
- **Material.id = 规范化材料名称**。同一材料名在不同事项下的份数/必要性/说明经常不同，
  故共享节点只放名称，**一切随事项变化的字段全部放 requireMaterial 边属性**——这是
  "共享节点 + 边属性承载上下文"的核心模式。
- **LegalBasis.id = 文号**（规范化：去空白）。文号是法规的天然跨事项唯一键，节点只放 title/docNo。
  文号缺失时退化为 `LH-`+md5(名称)，此时共享粒度降为"同名法规"。
- **LegalCitation.id = `LC-`+md5(文号|条款)**（v0.2 落地）。条款级引用拆为独立弱实体：
  条文内容、条款归属于 Citation 节点（`content` 标 TextAndVector），`Affair -[citeLegal]-> Citation
  -[partOf]-> LegalBasis`。试点实证必须如此：SPG 按 (s,p,o) upsert，文号级建模下 79% 条款边塌缩、
  86% 引用条文与首见冲突；拆分后 135,318 条引用全保留（去重对 135,273）。同一（文号，条款）
  文本仍不一致的残留冲突保留首见并计数（`citation_content_conflicts`，试点 1,278 条）。
- **弱实体 id 带事项编码前缀**（`#P01/#R01/#C01`），天然私有、可溯源，且不与其他事项冲突。

### 2.3 长文本与 supporting_chunks 溯源思路

法条内容（最长 3 万字）、受理条件、窗口/网上流程、审查标准这类长文本：

1. **试点（纯结构建图）**：完整文本作为节点属性存储，标注 `index: TextAndVector`。
   不配向量模型时索引仅落文本部分；文本整体可被 SPG 全文检索命中。
2. **后续增强（supporting_chunks）**：对超长文本另跑一条非结构化链（DictReader→LengthSplitter
   →Chunk），在每个 Chunk 上写 `source` 属性 = 所属节点 id（如 LegalBasis 文号、Affair 编码），
   形成"节点 ↔ Chunk"互索引——这正是 KAG 架构里图结构与原文块互索引的标准用法，
   问答阶段可沿实体回溯到原文片段，实现可解释溯源。
   节点 id 本身即溯源锚点（编码/文号/`编码#P01`），另保留 `sourceUrl`（官方详情页）作外部溯源。

### 2.4 跨域通办建模

数据为对象数组（覆盖地区/通办形式/通办范围），多数事项为空、少量有多条。
建模为**事项私有弱实体** CrossRegionHandling + 多值语义边 `supportCrossRegion`：
比三个平行的多值属性更能保持"同一条通办记录的三个字段"的组内对应关系；
又不必升为共享实体（取值组合随事项而异，无复用价值）。

### 2.5 已知数据特性的应对

- 43% 法人事项无办理环节、25.9% 无材料：语义属性列留空即可（mapping 跳过空值），不产生悬空边。
- 办理地址个人全空/法人 10.3% 空：保留 `handleAddress` 属性位，空值不落属性（配合自定义
  `SafeCSVScanner(na_filter=False)` 避免 pandas NaN 写入图，见 kag_notes.md §3.2）。
- 编码重复/一事项多来源：适配器按 id 去重（先到先得并计数），重复记录不计入节点表。

## 3. 适配器输出（KAG 结构化构建链输入）

`kg/build/adapter.py` 产出 15 个 CSV（UTF-8，标准引号转义，长文本含换行可安全解析）：

| 文件 | 链 | 关键列 |
|---|---|---|
| `Affair.csv` | SPGTypeMapping("Affair") | id,name,affairType,serviceTarget,exerciseLevel,theme,implementedBy,hasStep,produceResult,supportCrossRegion + 21 个标量/长文本列 |
| `ImplementingOrg.csv` | SPGTypeMapping | id,name |
| `Material.csv` | SPGTypeMapping | id,name |
| `LegalBasis.csv` | SPGTypeMapping | id,title,docNo |
| `LegalCitation.csv` | SPGTypeMapping | id,name,article,content |
| `ProcessStep.csv` | SPGTypeMapping | id,name,stepIndex,step,link,handler,timeLimit,result,reviewStandard |
| `ResultDocument.csv` | SPGTypeMapping | id,name,docType,validNote,note,attachments |
| `CrossRegionHandling.csv` | SPGTypeMapping | id,name,coverRegion,throughForm,throughScope |
| `AffairType.csv` / `ServiceTarget.csv` / `ExerciseLevel.csv` / `ThemeCategory.csv` | SPGTypeMapping | 仅 `id`（概念路径，isA 链自动构建） |
| `Affair_requireMaterial_Material.csv` | RelationMapping | srcId,dstId,seq,copies,submitForm,materialType,materialSource,isRequired,note |
| `Affair_citeLegal_LegalCitation.csv` | RelationMapping | srcId,dstId |
| `LegalCitation_partOf_LegalBasis.csv` | RelationMapping | srcId,dstId |
| `ProcessStep_nextStep_ProcessStep.csv` | RelationMapping | srcId,dstId |

约定：语义属性单元格 = 目标节点 id，多值用**英文逗号**分隔（材料/法条不经过此通道，
它们走关系 CSV 以携带边属性/保留逐条引用）。概念取值经映射表转为概念路径 id（如 `法人-企业法人`、
`行政权力-行政许可`）。CSV 列序即表头顺序，链导入顺序：概念 → 共享实体 → 弱实体 →
Affair → 关系表（先点后边）。

## 4. 演进记录与后续可扩展点

- **LegalCitation（v0.2 已落地）**：动因——试点实测一（内容冲突）：135,318 条文号引用中
  116,975 条（86%）的条文文本与节点首见版本不同；实测二（边塌缩）：SPG 按 (s,p,o) upsert，
  文号级建模下 135,318 行仅落 28,038 条边（79% 条款信息丢失）。落地后：
  `LegalCitation(法条引用): EntityType`，id = `LC-`+md5(文号|条款)，节点承载 article/content；
  `Affair -[citeLegal]-> LegalCitation -[partOf]-> LegalBasis`；LegalBasis 只留 title/docNo。
  适配器 v2 试点重跑：135,318 条引用全部成边（去重对 135,273），残留同（文号，条款）文本
  冲突 1,278 条（首见保留+计数）。
- 常见问答 FAQ 实体、Chunk 互索引、部门 isA 层级（省-市-县部门隶属）、材料名称的
  模糊归一（同义材料合并）、事项间"同一事项不同层级实施"的同构边、概念规则
  （concept.rule，如"可全程网办事项"= 可网上办理 是 ∧ 办理深度 IV 级）。
