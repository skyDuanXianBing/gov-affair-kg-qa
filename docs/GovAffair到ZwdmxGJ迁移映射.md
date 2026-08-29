# GovAffair → ZwdmxGJ 迁移映射表

日期：2026-08-29。需求来源：`.zcode/specs/2026-08-29-schema-v03-fix/需求规格说明书.md:15`（需求 5）。
用途：qa/ 检索端（qa/multihop.py、qa/retriever.py）从旧图模型 GovAffair 迁到新图模型 ZwdmxGJ v0.3 时的**逐条照抄映射**。本文档只给映射与处置，不改 qa/ 代码（允许范围见需求规格说明书.md:26）。

**文件简称与证据约定**（下文 `文件:行号` 均为撰写当日实测行号）：

| 简称 | 文件 |
| --- | --- |
| 旧schema | kg/design/GovAffair.schema（namespace GovAffair，v0.2） |
| 新schema | schemas/ZwdmxGJ-v0.3.schema（namespace ZwdmxGJ，17 实体 / 23 个去重关系名） |
| MH | qa/multihop.py |
| RT | qa/retriever.py |
| SPEC | .zcode/specs/2026-08-29-schema-v03-fix/需求规格说明书.md |

qa/ 侧被写死旧命名的消费点（改造时需全部覆盖）：

- MH:43-52 `TYPE_INFO`（8 个 `GovAffair.*` 标签）、MH:53 `LOCATABLE`、MH:56-65 `REL_INFO`（8 关系白名单）、MH:68-84 `_TYPE_ALIAS`/`_REL_ALIAS` 别名表、MH:92-115 `PLAN_SYSTEM` 提示词内嵌的图模型说明、MH:301/304-314 遍历 Cypher 的标签与 `o.docNo/o.article/o.content` 返回列；
- RT:32 `NEO4J_DB = "govaffair"`、RT:39-43 五个 `_gov_affair_*` 向量索引名、RT:217-282 `expand_affair`（标签 + Affair 属性 + 四段扩展 Cypher）、RT:284-296 `expand_material`、RT:298-321 `expand_citation`、RT:372 法条去重键 `article+basis`、RT:388-401 法规种子查询 `b.title/b.docNo`。

---

## 1. 节点标签映射表

### 1.1 实体标签（8 条）

| # | 旧标签（GovAffair.*） | 旧schema证据 | 新标签（ZwdmxGJ.*） | 新schema证据 | qa/ 消费点 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `GovAffair.Affair`（政务事项） | 旧schema:85 | `ZwdmxGJ.GovernmentService`（政务事项） | 新schema:117 | MH:44、RT:220/238/250/261/274/292/310 | 重命名。id 属性由 KAG 业务 id 改为 `serviceId`（新schema:121） |
| 2 | `GovAffair.Material`（申请材料） | 旧schema:32 | `ZwdmxGJ.Material`（申请材料） | 新schema:209 | MH:45、RT:238/287/292 | 同名保留。逐事项字段不在节点上（见 §3）；id 改共享 `materialId`（SPEC:11，规范化名称重生成） |
| 3 | `GovAffair.LegalCitation`（法条引用） | 旧schema:44 | `ZwdmxGJ.LegalCitation`（法规条款引用） | 新schema:292 | MH:46、RT:261/301/310 | 同名保留。id 改共享 `citationId`，规则 `LC-{hash(文号\|条款)}`（SPEC:11）；新增 `sourceChunkId/confidence/reviewStatus` |
| 4 | `GovAffair.LegalBasis`（法律依据） | 旧schema:37 | `ZwdmxGJ.LegalBasis`（法规文件） | 新schema:272 | MH:47、RT:262/302/390 | 同名保留。属性改名：`title→name`（旧schema:39 → 新schema:274）、`docNo→documentNumber`（旧schema:41 → 新schema:278）；id 改 `legalBasisId`，规则 `LB-{hash(docNo)}`（SPEC:11） |
| 5 | `GovAffair.ProcessStep`（办理环节） | 旧schema:53 | `ZwdmxGJ.ProcessStep`（办理步骤） | 新schema:244 | MH:48、RT:250 | 同名保留。`stepIndex`（旧schema:56）→ 边属性 `orderNo`（新schema:176）；`reviewStandard`（旧schema:62）→ `checkStandard`（新schema:254-255） |
| 6 | `GovAffair.ResultDocument`（办理结果） | 旧schema:67 | `ZwdmxGJ.ServiceResult`（办理结果） | 新schema:260 | MH:49 | 重命名。`docType→resultType`（旧schema:71 → 新schema:266）、`validNote→validityPeriod`（旧schema:72 → 新schema:270）；id 改 `resultId` |
| 7 | `GovAffair.CrossRegionHandling`（跨域通办） | 旧schema:76 | **无对应（实体已删除）** | — | MH:50、RT:274 | **声明为已知缺口**：源 JSONL 有、CSV 未承载，延后处理（SPEC:13）。qa/ 改造时删除 `TYPE_INFO["cross"]`、`REL_INFO` 的 `supportCrossRegion`、RT:272-281 通办扩展段、RT:127-136 提示词渲染段；“通办范围”类问题降级为 Chunk 全文检索 |
| 8 | `GovAffair.ImplementingOrg`（实施主体） | 旧schema:27 | `ZwdmxGJ.Department`（部门） | 新schema:192 | MH:51 | 重命名。`name` 保留（新schema:194）并新增 `departmentId/departmentCode/alias(MultiValue)/canonicalName/organizationLevel/regionCode`（新schema:196-205）；id 改 `departmentId` |

### 1.2 概念类型处置（4 条，isA 树整体消失）

| # | 旧 ConceptType | 旧schema证据 | 新去向 | 新schema证据 | 处置 |
| --- | --- | --- | --- | --- | --- |
| 9 | `AffairType`（事项类型，isA 树） | 旧schema:13-14 | 无 ConceptType；改标量 `categoryL1/categoryL2` + 边 `classifiedAs→ServiceCategory` | 新schema:123-125、157 | 概念上卷（hypernym）检索能力消失；类型过滤改用标量/分类边。检索影响见 §6 未决项 4 |
| 10 | `ServiceTarget`（服务对象） | 旧schema:16-17 | 无对应实体；改标量 `GovernmentService.serviceObject` | 新schema:137-138 | "按服务对象筛事项"改为对标量做 WHERE 过滤（值域收敛靠导入期规范化） |
| 11 | `ExerciseLevel`（行使层级） | 旧schema:19-20 | 无对应实体；改标量 `GovernmentService.exerciseLevel` | 新schema:139-140 | 同上，标量过滤 |
| 12 | `ThemeCategory`（主题分类） | 旧schema:22-23 | `ServiceCategory` 层级树（`parentCategory` 自环） | 新schema:36、51 | 分类导航改走 `classifiedAs` + `parentCategory` 多跳；qa/ 可选支持 |

### 1.3 新 schema 中 qa/ 视角的新增实体（无旧对应，10 条）

| # | 新实体 | 新schema证据 | qa/ 建议 |
| --- | --- | --- | --- |
| 13 | `ServiceDomain`（服务领域） | 新schema:12 | 遍历目标（领域过滤/两域区分法人-个人） |
| 14 | `CategoryScheme`（分类体系） | 新schema:23 | 可暂不接入检索 |
| 15 | `ServiceCategory`（服务分类） | 新schema:36 | 遍历目标（分类多跳） |
| 16 | `KnowledgeModel`（知识模型） | 新schema:55 | 暂不接入；`retrievalFilter`（新schema:70）预留给 qa/ 做域过滤 |
| 17 | `Chunk`（文本块） | 新schema:83 | **高价值**：长文本（受理条件/流程）的新承载，`content` 带 TextAndVector（新schema:87），可 locate |
| 18 | `ServiceCondition`（办理条件） | 新schema:224 | **高价值**：`statement` 带 TextAndVector（新schema:235），条件类问答主入口 |
| 19 | `FAQ`（常见问答） | 新schema:311 | `answer` 带 TextAndVector（新schema:318），问答直命中 |
| 20 | `ServiceChannel`（办理渠道） | 新schema:322 | "去哪办/怎么办"问答 |
| 21 | `Fee`（收费信息） | 新schema:333 | 承接旧 `isCharge`（旧schema:110）的结构化版本 |
| 22 | `Proposition`（知识命题） | 新schema:348 | 开放知识层；`statement` 带 TextAndVector（新schema:365），长尾事实问答 |

---

## 2. 关系映射表

### 2.1 旧 REL_INFO 8 关系 → 新名（8 条）

| # | 旧关系 | 旧方向（MH 键） | MH证据 | 新关系 | 新方向 | 新schema证据 | 处置 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `requireMaterial` | affair→material | MH:57 | `requiresMaterial` | GovernmentService→Material | 新schema:165 | 仅改名。边属性变化见 §3 表 3-2 |
| 2 | `hasStep` | affair→step | MH:58 | `hasProcessStep` | GovernmentService→ProcessStep | 新schema:174 | 改名；节点 `stepIndex` 移为边 `orderNo`（新schema:176），RT:250-252 排序改用边属性 |
| 3 | `nextStep` | step→step | MH:59 | `nextStep` | ProcessStep→ProcessStep | 新schema:258 | 同名同向，零改动 |
| 4 | `citeLegal` | affair→citation | MH:60 | `citesLegal` | GovernmentService→LegalCitation | 新schema:180 | 改名；新增边属性 `orderNo/basisSource`（新schema:182-183） |
| 5 | `partOf` | citation→basis | MH:61 | `partOf` | LegalCitation→LegalBasis | 新schema:307 | 同名同向；对端属性改名（title→name、docNo→documentNumber，§1.1#4） |
| 6 | `produceResult` | affair→result | MH:62 | `producesResult` | GovernmentService→ServiceResult | 新schema:177 | 改名；新增边属性 `orderNo`（新schema:179） |
| 7 | `supportCrossRegion` | affair→cross | MH:63 | **无对应** | — | — | CrossRegionHandling 已删除（§1.1#7）：qa/ 摘除该关系及别名（MH:82）；已声明缺口（SPEC:13） |
| 8 | `implementedBy` | affair→org | MH:64 | `handledBy` | GovernmentService→Department | 新schema:159 | 改名；新增边属性 `departmentRole`（新schema:161）；另可选用 `collaboratesWith`（新schema:162，边属性 `participatesInStep`）覆盖"协同部门"问法 |

同步改造点：MH:74-84 `_REL_ALIAS` 的 8 个键与中文别名全部按本表改名（如 `"所需材料"→"requiresMaterial"`、`"实施部门"→"handledBy"`）；MH:92-115 `PLAN_SYSTEM` 提示词中的关系白名单段落（MH:97-101）整段按本表 + §2.2 重写。

### 2.2 新 schema 中 qa/ 应新增支持的高价值关系（13 条）

| # | 新关系 | 方向 | 新schema证据 | 多跳价值（一句话） |
| --- | --- | --- | --- | --- |
| 9 | `hasCondition` | GovernmentService→ServiceCondition | 新schema:171 | 条件陈述可被向量命中后 in 向反查事项，"什么人/什么情形可以办"单跳即答 |
| 10 | `hasChunk`（事项侧） | GovernmentService→Chunk | 新schema:189 | 承接旧 `acceptCondition/windowProcess/onlineProcess` 长文本上下文（§3 表 3-1），Chunk 命中可反查事项 |
| 11 | `hasChunk`（条件/法条侧） | ServiceCondition→Chunk、LegalCitation→Chunk | 新schema:242、308 | 命中条件/法条后取原文证据块，做答案溯源 |
| 12 | `belongsToDomain` | GovernmentService→ServiceDomain（另 CategoryScheme/ServiceCategory/KnowledgeModel 亦出该边） | 新schema:156（另 34/53/76） | 法人/个人两域数据同库共存后的域过滤与域内聚合统计 |
| 13 | `classifiedAs` | GovernmentService→ServiceCategory | 新schema:157 | "同分类下还有哪些事项"的比较型多跳（接 `parentCategory` 可上卷一级分类） |
| 14 | `usesModel` | GovernmentService→KnowledgeModel | 新schema:158 | 配合 `KnowledgeModel.retrievalFilter`（新schema:70）做检索期路由，qa/ 可后置接入 |
| 15 | `hasFaq` | GovernmentService→FAQ | 新schema:184 | FAQ answer 向量直命中后 in 向定位事项，高频咨询零跳转写答案 |
| 16 | `hasChannel` | GovernmentService→ServiceChannel | 新schema:187 | "去哪办/窗口时间/网上入口"渠道问答 |
| 17 | `hasFee` | GovernmentService→Fee | 新schema:188 | 承接旧标量 `isCharge`，收费依据/标准结构化问答 |
| 18 | `collaboratesWith` | GovernmentService→Department | 新schema:162 | "这事除了牵头局还有谁参与"的多部门协同多跳 |
| 19 | `statesProposition` | GovernmentService→Proposition（另 LegalCitation 亦出） | 新schema:190、309 | LLM 抽取的开放事实进入检索面，覆盖 schema 未建模的长尾属性 |
| 20 | `extractedFrom` | Proposition→Chunk | 新schema:375 | 命中命题后一步取证据原文，支撑带出处的回答 |
| 21 | `belongsTo`（Department 自环）/ `parentCategory`（分类自环） | Department→Department / ServiceCategory→ServiceCategory | 新schema:207、51 | "该科室上级是谁/上级分类下还有哪些事项"的层级上卷多跳 |

### 2.3 建议的新 REL_INFO / TYPE_INFO 常量（供 qa/ 改造直接照抄，最终以灌图实测为准）

```python
# TYPE_INFO：类型键 → (Neo4j 标签, 向量索引, 中文名, 定位分数下限)；None=仅遍历
TYPE_INFO = {
    "service":     ("ZwdmxGJ.GovernmentService", IDX_SERVICE_NAME,     "事项",     0.45),
    "material":    ("ZwdmxGJ.Material",           IDX_MATERIAL,        "材料",     0.45),
    "citation":    ("ZwdmxGJ.LegalCitation",      IDX_CITATION_CONTENT,"法条",     0.45),
    "basis":       ("ZwdmxGJ.LegalBasis",         IDX_BASIS,           "法规",     0.72),
    "chunk":       ("ZwdmxGJ.Chunk",              IDX_CHUNK,           "原文块",   0.45),
    "condition":   ("ZwdmxGJ.ServiceCondition",   IDX_CONDITION,       "办理条件", 0.45),
    "faq":         ("ZwdmxGJ.FAQ",                IDX_FAQ,             "常见问答", 0.45),
    "proposition": ("ZwdmxGJ.Proposition",        IDX_PROPOSITION,     "命题",     0.45),
    "step":        ("ZwdmxGJ.ProcessStep",        None,                "环节",     0.45),
    "result":      ("ZwdmxGJ.ServiceResult",      None,                "办理结果", 0.45),
    "department":  ("ZwdmxGJ.Department",         None,                "部门",     0.45),
    "channel":     ("ZwdmxGJ.ServiceChannel",     None,                "渠道",     0.45),
    "fee":         ("ZwdmxGJ.Fee",                None,                "收费",     0.45),
    "category":    ("ZwdmxGJ.ServiceCategory",    None,                "分类",     0.45),
    "domain":      ("ZwdmxGJ.ServiceDomain",      None,                "领域",     0.45),
}
LOCATABLE = {"service", "material", "citation", "basis",
             "chunk", "condition", "faq", "proposition"}   # cross 已删除，不再可定位

REL_INFO = {
    "requiresMaterial":  ("service", "material",    "所需材料"),
    "hasProcessStep":    ("service", "step",        "办理步骤"),
    "nextStep":          ("step",    "step",        "下一步骤"),
    "citesLegal":        ("service", "citation",    "引用法条"),
    "partOf":            ("citation", "basis",      "所属法规"),
    "producesResult":    ("service", "result",      "办理结果"),
    "handledBy":         ("service", "department",  "主管部门"),
    "collaboratesWith":  ("service", "department",  "协同部门"),
    "hasCondition":      ("service", "condition",   "办理条件"),
    "hasChunk":          ("service", "chunk",       "原文块"),
    "hasFaq":            ("service", "faq",         "常见问答"),
    "hasChannel":        ("service", "channel",     "办理渠道"),
    "hasFee":            ("service", "fee",         "收费信息"),
    "classifiedAs":      ("service", "category",    "所属分类"),
    "belongsToDomain":   ("service", "domain",      "所属领域"),
    "statesProposition": ("service", "proposition", "事实命题"),
}
```

注意：旧 `affair` 键改名为 `service`（或保留键名 `affair` 仅换标签，二选一，全文件一致即可）；`_TYPE_ALIAS`/`_REL_ALIAS`（MH:68-84）与 `PLAN_SYSTEM`（MH:92-115）需同步重写。标签前缀 `ZwdmxGJ.` 为按旧图惯例（`GovAffair.Affair`）的假设，以灌图后 `CALL db.labels()` 实测为准（§6 未决项 3）。

---

## 3. 属性迁移表

### 3.1 RT expand_affair / to_prompt_context 消费的旧 Affair 上下文（13 条）

消费点：RT:225-235（ctx 构造）、RT:87-147（to_prompt_context 渲染）、RT:272-281（通办）、RT:237-247（材料）、RT:249-258（步骤）、RT:259-271（法条）。

| # | 旧属性/来源 | RT消费行 | 新去向 | 新schema证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | `Affair.name` | RT:226 | `GovernmentService.name` | 新schema:119-120 | 直接改名 |
| 2 | `Affair.acceptCondition`（受理条件长文本，TextAndVector） | RT:227、97-98 | **Chunk.content**（经 `hasChunk` 边，`sourceField` 标来源字段）或 **ServiceCondition.description/statement**（经 `hasCondition` 边） | 新schema:87、96-97、230、234-235 | 节点属性已移除；qa/ 改为事项→hasChunk/hasCondition 取文本。属 SPEC:13 的 9 个"从 documents content/extras 派生"字段 |
| 3 | `Affair.windowProcess`（窗口办理流程长文本） | RT:228、110-111 | Chunk.content（sourceField=windowProcess 类） | 新schema:87、96-97 | 同上 |
| 4 | `Affair.onlineProcess`（网上办理流程长文本） | RT:229、112-113 | Chunk.content（sourceField=onlineProcess 类） | 新schema:87、96-97 | 同上 |
| 5 | `Affair.legalTimeLimit` | RT:230、115-116 | `GovernmentService.legalTimeLimit` | 新schema:146 | 同名保留 |
| 6 | `Affair.promiseTimeLimit` | RT:231、117-118 | `GovernmentService.promiseTimeLimit` | 新schema:145 | 同名保留 |
| 7 | `Affair.isCharge` | RT:232、119-120 | `Fee` 节点（经 `hasFee` 边；`feeStatus/feeStandard`） | 新schema:188、339-341 | 标量→结构化；数据可用性见 §6 未决项 8 |
| 8 | `Affair.handleAddress` | RT:233、121-122 | `ServiceChannel`（经 `hasChannel` 边；name/description） | 新schema:187、324-325、330 | **建议映射**：渠道节点无地址专字段，CSV 列对应未验证（§6 未决项 6） |
| 9 | `Affair.consultPhone` | RT:234、123-124 | 无直接承载 | — | 属 SPEC:13 的无来源字段，待 documents/extras 派生；qa/ 暂以 Chunk 兜底（§6 未决项 5） |
| 10 | `Affair.supportCrossRegion→CrossRegionHandling.name/coverRegion/throughForm` | RT:272-281、127-136 | **无承载（已知缺口）** | — | SPEC:13；qa/ 摘除该段渲染 |
| 11 | 材料清单 `requireMaterial→Material.name`（DISTINCT 去重） | RT:237-247 | `requiresMaterial→Material.name` | 新schema:165、211-212 | 改关系名；共享 materialId 后同名天然合并，DISTINCT 保留无害 |
| 12 | 步骤 `hasStep→s.stepIndex/s.name/s.timeLimit`（按 stepIndex 排序） | RT:249-258 | `hasProcessStep` 边 `orderNo` + `s.name/s.timeLimit` | 新schema:174、176、246、253 | 排序键从节点 `stepIndex`（旧schema:56）改为边 `orderNo` |
| 13 | 法条 `citeLegal→partOf→c.article/c.content/b.title/b.docNo` | RT:259-271、298-321、372、388-401 | `citesLegal→partOf→c.article/c.content + b.name/b.documentNumber` | 新schema:180、298、300-301、274、278 | 对端属性改名；去重键 `article+basis`（RT:372）建议改 `citationId`（共享 id，SPEC:11） |

另：MH:304-314（遍历 Cypher）`RETURN ... o.docNo` —— 新图中 LegalBasis 的文号属性是 `documentNumber`，需按目标类型返回或两者都取（LegalCitation 本无 docNo，旧行为即 null）。

### 3.2 旧 requireMaterial 边属性 → 新去向（7 条）

旧边属性定义：旧schema:129-136。新边属性：新schema:166-170（required/orderNo/materialDescription/acceptanceStandard）；材料节点侧共享字段：新schema:215-218（materialType/sourceType/submissionFormat）。SPEC:11 注明"以实际 CSV 列为准"，本表按 schema 语义给建议映射。

| # | 旧边属性 | 旧schema证据 | 新去向 | 新schema证据 |
| --- | --- | --- | --- | --- |
| 1 | `seq`（材料序号） | 旧schema:130 | 边 `requiresMaterial.orderNo`（Integer） | 新schema:168 |
| 2 | `copies`（份数） | 旧schema:131 | **无对应**——份数/页数声明为已知缺口（SPEC:13） | — |
| 3 | `submitForm`（提交形式） | 旧schema:132 | `Material.submissionFormat`（节点级，建议对应） | 新schema:218 |
| 4 | `materialType`（材料类型） | 旧schema:133 | `Material.materialType`（节点级规范值） | 新schema:215 |
| 5 | `materialSource`（材料来源） | 旧schema:134 | `Material.sourceType`（节点级，建议对应） | 新schema:217 |
| 6 | `isRequired`（是否必要） | 旧schema:135 | 边 `requiresMaterial.required` | 新schema:167 |
| 7 | `note`（材料说明） | 旧schema:136 | 边 `requiresMaterial.materialDescription`（受理标准另立 `acceptanceStandard`） | 新schema:169-170 |

### 3.3 旧 Affair 其余标量（qa/ 未消费，qa 改造时可忽略，列此备全量对照，11 条）

| # | 旧属性 | 旧schema证据 | 新去向 | 新schema证据 |
| --- | --- | --- | --- | --- |
| 1 | `status` | 旧schema:105 | `serviceStatus` | 新schema:141 |
| 2 | `officialListCount` | 旧schema:106 | `officialListCount` | 新schema:147 |
| 3 | `handleDepth`（办理深度） | 旧schema:107 | `onlineDepth`（网上办理深度） | 新schema:143 |
| 4 | `handleMethods`（办理方式） | 旧schema:108 | 无直接承载 → ServiceChannel/Chunk 兜底（派生字段） | — |
| 5 | `isOnline`（可网上办理） | 旧schema:109 | `onlineAvailable` | 新schema:144 |
| 6 | `complaintPhone` | 旧schema:112 | 无直接承载（派生字段） | — |
| 7 | `promiseTimeNote` | 旧schema:114 | 无直接承载（派生字段） | — |
| 8 | `legalTimeNote` | 旧schema:116 | 无直接承载（派生字段） | — |
| 9 | `onlineLimitNote` | 旧schema:118 | 无直接承载（派生字段） | — |
| 10 | `sourceUrl`（详情页地址） | 旧schema:126 | `sourceUrl`（另有 sourceFile/sourceLine 溯源） | 新schema:150-152 |
| 11 | 概念属性 `affairType/serviceTarget/exerciseLevel/theme` | 旧schema:90-95 | 标量 `categoryL1/categoryL2/domainId/categoryId/modelId` + 边 `classifiedAs/belongsToDomain/usesModel` | 新schema:123-131、156-158 |

无直接承载的 5 个（#4/6/7/8/9）均属 SPEC:13 声明的 9 个无来源字段（services.csv 11 列 vs schema 24 属性），按 SPEC 处理为"从 documents content/extras 派生"，qa/ 侧暂以 Chunk 检索兜底。

---

## 4. 基础设施映射

### 4.1 Neo4j 库名（1 条）

| 旧 | RT证据 | 新建议 | 说明 |
| --- | --- | --- | --- |
| `NEO4J_DB = "govaffair"` | RT:32 | `"zwdmxgj"` | 法人/个人两域同库共 namespace，靠 ServiceDomain/`domainId` 区分（新schema:5、12、127）；不必按数据集分库。改造时读环境变量（§5 步骤 3） |

### 4.2 向量索引名（10 条）

命名沿用旧惯例 `_gov_affair_{实体}_{字段}_vector_index` → 前缀改 `_zwdmxgj_`。

**A. 新 schema TextAndVector 标注、必建（6 条）**：

| # | 建议索引名 | 字段 | 新schema证据 | 旧对应 |
| --- | --- | --- | --- | --- |
| 1 | `_zwdmxgj_chunk_content_vector_index` | Chunk.content | 新schema:86-87 | 无（新增能力） |
| 2 | `_zwdmxgj_service_condition_statement_vector_index` | ServiceCondition.statement | 新schema:234-235 | 无（承接旧 acceptCondition 语义，§3.1#2） |
| 3 | `_zwdmxgj_process_step_check_standard_vector_index` | ProcessStep.checkStandard | 新schema:254-255 | 旧 ProcessStep.reviewStandard（旧schema:62-63，旧图未给 qa 用） |
| 4 | `_zwdmxgj_legal_citation_content_vector_index` | LegalCitation.content | 新schema:300-301 | `_gov_affair_legal_citation_content_vector_index`（RT:42，IDX_CITATION_CONTENT） |
| 5 | `_zwdmxgj_faq_answer_vector_index` | FAQ.answer | 新schema:317-318 | 无（新增能力） |
| 6 | `_zwdmxgj_proposition_statement_vector_index` | Proposition.statement | 新schema:364-365 | 无（新增能力） |

**B. 名称级索引、复刻旧 locate 能力需手工建（4 条）**——新 schema 对这三个 name 只标 `Text`（新schema:119、211、274），但旧检索的"按名称定位事项/材料/法规"依赖名称向量索引（RT:39-43）：

| # | 建议索引名 | 字段 | 旧对应（RT证据） |
| --- | --- | --- | --- |
| 7 | `_zwdmxgj_government_service_name_vector_index` | GovernmentService.name | `_gov_affair_affair_name_vector_index`（RT:39，IDX_AFFAIR） |
| 8 | `_zwdmxgj_material_name_vector_index` | Material.name | `_gov_affair_material_name_vector_index`（RT:40，IDX_MATERIAL） |
| 9 | `_zwdmxgj_legal_basis_name_vector_index` | LegalBasis.name | `_gov_affair_legal_basis_name_vector_index`（RT:43，IDX_BASIS；注意旧索引打在 title 上） |
| 10 | `_zwdmxgj_legal_citation_name_vector_index`（可选，低优先） | LegalCitation.name | `_gov_affair_legal_citation_name_vector_index`（RT:41，IDX_CITATION_NAME） |

B 组是 qa/ 改造的前置依赖：不建则 `LOCATABLE` 里的 service/material/basis 三类定位直接失效（§6 未决项 2）。

### 4.3 multihop locate 可绑定类型扩展建议

- 现 `LOCATABLE = {"affair","material","citation","basis"}`（MH:53）→ 建议扩为 `{"service","material","citation","basis","chunk","condition","faq","proposition"}`（常量见 §2.3）。
- 优先级：`chunk`（长文本问答主力）、`condition`（条件类）、`faq`（高频咨询）先行；`proposition` 待抽取管线产出后再开。
- 每类配 §4.2-A 的对应索引；`_do_locate` 的"事项同名多区县按名去重绑 1 个"逻辑（MH:258-259）在新图同样适用（同名事项跨区县实例仍存在，以 serviceId 区分）。
- 旧 `basis` 门槛 0.72（MH:47）沿用：法规名索引易混入无关文号，RT:385 还有 0.8 的二次门槛，两处一并保留。

---

## 5. 迁移步骤建议（顺序、回退点、并存开关）

**开关设计（贯穿全程）**：把 RT:32、RT:39-43、MH:43-65 的图模型常量收敛为按 `KG_SCHEMA` 环境变量二选一（`govaffair` | `zwdmxgj`），至少参数化三件事——库名 `KG_DB_NAME`、索引前缀 `KG_INDEX_PREFIX`（`_gov_affair_` | `_zwdmxgj_`）、TYPE_INFO/REL_INFO 双表（`SCHEMA_VARIANTS` 字典）。默认值取 `govaffair`，保证不改环境即行为不变。

1. **建库建索引**（回退点 A：不触旧库）：`CREATE DATABASE zwdmxgj`；按 §4.2 建 10 个向量索引（B 组手工 `CREATE VECTOR INDEX`，维度与 bge-m3 一致）。
2. **灌图**（回退点 B：可整库重灌）：导入 Wave1-A id 重写层产物；验收——17 实体/23 关系计数、materialId/citationId/legalBasisId 唯一性（SPEC:19 的合并数/压缩比/违例数统计）、`CALL db.labels()` 实测标签前缀并回填 §2.3。
3. **qa/ 改造（配置化，双图并存）**（回退点 C：开关切回）：按 §1-§4 改 TYPE_INFO/REL_INFO/别名表/PLAN_SYSTEM/expand_*；`KG_SCHEMA` 默认 `govaffair`。注意摘除项——cross/supportCrossRegion（MH:50、63、82；RT:272-281、127-136）。
4. **切换评测**：`KG_SCHEMA=zwdmxgj` 跑 testset + `scripts/score_testset.py`（SPEC:14），与旧图基线对比；分法人/个人两域看（personal 缺 documents_chunks 的项单独标注，SPEC:13）。指标不回退才把默认值切 `zwdmxgj`。
5. **收尾**：旧库 `govaffair` 只读保留观察期（建议 2 周）；期满 `DROP DATABASE govaffair` 属不可逆操作，需人工确认后执行；随后删双表开关、固定新图常量。

---

## 6. 映射未决项

1. **跨域通办无承载**：CrossRegionHandling 实体与 supportCrossRegion 关系在新 schema 均删除，源 JSONL 有、CSV 未承载（SPEC:13 已声明延后）。qa/ 摘除后"通办范围"类问题降级为 Chunk 全文检索，评测集中如有该类题需标注能力变化。
2. **名称级向量索引缺失**：GovernmentService.name / Material.name / LegalBasis.name 新 schema 仅标 `Text`（新schema:119、211、274），而旧检索的 locate 依赖名称向量（RT:39-43）。需灌图后手工建（§4.2-B）或 schema 升标 TextAndVector——qa/ 改造的前置依赖，未建则三类定位失效。
3. **Neo4j 实际标签前缀未验证**：本文按旧图惯例假设 `ZwdmxGJ.GovernmentService` 形式；以灌图后 `CALL db.labels()` 实测为准，若导入器只写短名（如 `GovernmentService`）需全局替换前缀。
4. **概念树 isA 消失的检索影响**：旧 ConceptType 四棵 isA 树（旧schema:13-23）支持概念上卷（hypernym）检索；新 schema 无 ConceptType，仅剩标量 categoryL1/L2（新schema:123-125）+ ServiceCategory.parentCategory 层级（新schema:51）。"按事项类型/服务对象/行使层级泛化过滤"从图遍历退化为标量 WHERE，值域规范化程度决定召回质量。
5. **六个旧标量无属性承载**：consultPhone/complaintPhone/handleMethods/promiseTimeNote/legalTimeNote/onlineLimitNote（§3.3#4/6/7/8/9），属 SPEC:13 的 9 个无来源字段，派生规则未定；qa/ 侧暂由 Chunk 兜底，答案里这些字段可能缺失。
6. **handleAddress→ServiceChannel 为建议映射**：渠道节点无地址专字段（新schema:322-331），CSV 列对应未验证，迁移后"办理地点"字段可能需要从渠道 description 或 Chunk 取。
7. **材料边属性列名未定案**：submitForm→submissionFormat、materialSource→sourceType 为语义建议映射，SPEC:11 注明"以实际 CSV 列为准"，qa/ 改造若需消费边属性应先对照重写层产物列名。
8. **Fee 数据可用性**：isCharge 由标量改经 hasFee→Fee 结构化承载（新schema:188）；若 pilot/personal 的 CSV 无费用列导致 Fee 节点未灌，"是否收费"问答上下文将缺该事实——需在灌图统计中核对 Fee 节点数。
9. **法条去重键与返回列**：共享 citationId 落地后 RT:372 的 `article+basis` 去重键应改 `citationId`；MH:304-314 的 `o.docNo` 需为 LegalBasis 改 `documentNumber`（LegalCitation 本无 docNo，旧行为即 null）。
10. **personal 域缺 documents_chunks.csv**（SPEC:13 分阶段）：Chunk/hasChunk/条件派生类检索在 personal 域暂不可用，两图并存期评测必须分域统计，避免个人域分数拉低误判迁移失败。
