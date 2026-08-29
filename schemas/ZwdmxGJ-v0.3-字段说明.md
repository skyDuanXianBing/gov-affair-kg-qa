# ZwdmxGJ Schema v0.3 字段说明

> 对应 Schema：`schemas/ZwdmxGJ-v0.3.schema`  
> 目标 Namespace：`ZwdmxGJ`  
> 适用数据：`data/pilot/`、`data/personal/`

## 1. 通用字段规则

| 字段 | 含义 | 规则 |
|---|---|---|
| `id` | OpenSPG 实体业务 ID | 由导入 Mapping 将 CSV 的业务主键映射为实体 ID；必须稳定、非空、唯一 |
| `name` | 面向阅读和检索的名称 | 使用规范化后的可读名称；不承担唯一标识职责 |
| `description` | 对实体的补充说明或原始长文本 | 只在没有更明确字段时使用；原文优先保留 |
| `alias` | 实体的别名集合 | 多值字段；只写已审核或高置信的别名，不直接用字符串相似度无条件合并 |
| `canonicalName` | 归一化后的标准名称 | 用于实体归一后的展示和检索 |
| `confidence` | 抽取、归一或匹配置信度 | 范围建议为 `0~1`；规则导入可留空 |
| `reviewStatus` | 审核状态 | 建议值：`pending`、`approved`、`rejected`、`needs_review` |

### 1.1 实体 ID 生成规则（共享 / 私有）

导入期由 `scripts/build_shared_ids.py` 统一重写并输出到 `build/shared_ids/<dataset>/`，原始 CSV 不修改。名称规范化统一为：NFKC 全角归一 + 去除全部空白 + casefold（只影响 ID 哈希键，不改动展示名称）。

| 类别 | 实体 | ID 规则 | 说明 |
|---|---|---|---|
| 共享（材料） | `Material` | `M-{md5(规范化材料名称)[:16]}` | 同名（规范化后）材料跨事项、跨数据域合并为一个节点；材料原始 id 是事项私有 digest，必须重写 |
| 共享（法规） | `LegalBasis` | `LB-{md5(规范化法规名\|规范化文号)[:16]}` | 文号缺失时文档键退化为规范化法规名称；同名不同文号、同文号不同法规名均不合并（同一修改决定文号可同时修改多部法规，法规名必须参与键） |
| 共享（法规） | `LegalCitation` | `LC-{md5(规范化法规名\|规范化文号\|规范化条款)[:16]}` | 文号缺失时同样退化；`legal_bases.csv` 一行拆为 LegalCitation + `partOf` 指向 LegalBasis；`service_based_on` 引用改写为 LC id |
| 共享（部门） | `Department` | 按部门名称（辅以编码）归一 | 现有 `departments.csv` 已是共享层 |
| 事项私有弱实体 | `ProcessStep`、`ServiceCondition`、`ServiceResult`、`FAQ`、`Fee`、`ServiceChannel`、`Chunk` | id 保留事项编码（service_id）前缀，不跨事项共享 | 同名步骤/条件在不同事项下是不同实体 |

同名合并后的冲突处理：`LegalCitation` 同 id 但 `clause_content` 不一致时保留首见并计数；`LegalBasis` 同 id 的法规名称因参与哈希键必然一致，仅检测发布日期/链接不一致并计数（`basis_law_name_conflicts` 在当前键设计下恒为 0，保留仅作回归监控）。计数与压缩比写入 `build/shared_ids/<dataset>/stats.json`。

## 2. 领域、分类和知识模型

### 2.1 `ServiceDomain`

| 字段 | 含义 |
|---|---|
| `name` | 领域名称，例如“法人服务”“个人事务” |
| `domainId` | 领域稳定 ID，例如 `domain:corporate` |
| `description` | 领域说明 |
| `status` | 领域是否启用 |
| `version` | 领域定义版本 |

### 2.2 `CategoryScheme`

| 字段 | 含义 |
|---|---|
| `name` | 分类体系名称 |
| `schemeId` | 分类体系稳定 ID |
| `domainId` | 所属领域 ID |
| `description` | 分类体系说明 |
| `version` | 分类体系版本 |

关系：

```text
CategoryScheme --belongsToDomain--> ServiceDomain
```

### 2.3 `ServiceCategory`

| 字段 | 含义 |
|---|---|
| `name` | 分类名称 |
| `categoryId` | 分类稳定 ID |
| `categoryLevel` | 分类层级，建议根分类为 `1`，二级分类为 `2` |
| `parentCategoryId` | 父分类 ID |
| `schemeId` | 所属分类体系 ID |
| `domainId` | 所属领域 ID |
| `description` | 分类说明 |
| `status` | 分类状态 |
| `version` | 分类版本 |

关系：

```text
ServiceCategory --parentCategory--> ServiceCategory
ServiceCategory --belongsToScheme--> CategoryScheme
ServiceCategory --belongsToDomain--> ServiceDomain
```

### 2.4 `KnowledgeModel`

| 字段 | 含义 |
|---|---|
| `name` | 模型名称 |
| `modelId` | 知识模型稳定 ID |
| `modelType` | 模型类型，例如 `DOMAIN_BASE`、`CATEGORY_PROFILE` |
| `domainId` | 所属领域 ID |
| `categoryId` | 适用分类 ID；领域基础模型可为空 |
| `version` | 模型版本 |
| `schemaVersion` | 依赖的 Schema 版本 |
| `description` | 模型说明 |
| `enabledEntityTypes` | 允许使用的实体类型列表 |
| `enabledRelationTypes` | 允许使用的关系类型列表 |
| `retrievalFilter` | 检索时使用的过滤条件 |
| `validationProfile` | 数据校验配置名称或 JSON 配置 |
| `status` | 模型状态 |

关系：

```text
KnowledgeModel --appliesToCategory--> ServiceCategory
KnowledgeModel --belongsToDomain--> ServiceDomain
KnowledgeModel --extendsModel--> KnowledgeModel
```

## 3. GovernmentService

| 字段 | 含义 | 来源示例 |
|---|---|---|
| `name` | 政务事项名称 | `service_name` |
| `serviceId` | 事项业务编码 | `service_id` |
| `categoryL1` | 原始一级分类 | `category_l1` |
| `categoryL2` | 原始二级分类 | `category_l2` |
| `domainId` | 统一领域 ID | 元数据生成结果 |
| `categoryId` | 统一分类 ID | 元数据生成结果 |
| `modelId` | 负责该事项的知识模型 ID | 元数据生成结果 |
| `departmentName` | 主管部门名称 | `department_name` |
| `departmentCode` | 主管部门编码 | `department_code` |
| `serviceObject` | 服务对象 | **当前无 CSV 来源列**，导入期从 documents content/extras 派生或置空（见修订版 §2.2.1） |
| `exerciseLevel` | 行使层级 | **当前无 CSV 来源列**，导入期派生或置空 |
| `serviceStatus` | 事项状态 | **当前无 CSV 来源列**，导入期派生或置空 |
| `onlineDepth` | 网上办理深度 | **当前无 CSV 来源列**，导入期派生或置空 |
| `onlineAvailable` | 是否可以网上办理 | **当前无 CSV 来源列**，导入期派生或置空 |
| `promiseTimeLimit` | 承诺办结时限 | **当前无 CSV 来源列**，导入期从 content"办理时限"派生或置空 |
| `legalTimeLimit` | 法定办结时限 | **当前无 CSV 来源列**，导入期从 content"办理时限"派生或置空 |
| `officialListCount` | 官方列表中出现的次数 | **当前无 CSV 来源列**，导入期置空 |
| `publishDate` | 发布日期 | `publish_date` |
| `versionDate` | 版本日期 | `version_date` |
| `sourceUrl` | 官方详情页链接 | `source_url` |
| `sourceFile` | 来源文件 | `source_file` |
| `sourceLine` | 来源行号 | `source_line` |
| `sourceRecordSha256` | 结构化记录指纹 | **当前无 CSV 来源列**，导入期置空 |
| `officialJsonSha256` | 官方详情 JSON 指纹 | **当前无 CSV 来源列**，导入期置空 |

关系：

```text
GovernmentService --belongsToDomain--> ServiceDomain
GovernmentService --classifiedAs--> ServiceCategory
GovernmentService --usesModel--> KnowledgeModel
GovernmentService --handledBy--> Department
GovernmentService --collaboratesWith--> Department
GovernmentService --requiresMaterial--> Material
GovernmentService --hasCondition--> ServiceCondition
GovernmentService --hasProcessStep--> ProcessStep
GovernmentService --producesResult--> ServiceResult
GovernmentService --citesLegal--> LegalCitation
GovernmentService --hasFaq--> FAQ
GovernmentService --hasChannel--> ServiceChannel
GovernmentService --hasFee--> Fee
GovernmentService --hasChunk--> Chunk
GovernmentService --statesProposition--> Proposition
```

## 4. Department

| 字段 | 含义 |
|---|---|
| `name` | 部门名称 |
| `departmentId` | 部门业务 ID |
| `departmentCode` | 部门编码 |
| `alias` | 部门简称、历史名称或其他已确认别名 |
| `canonicalName` | 部门标准名称 |
| `organizationLevel` | 省、市、区县、街道等机构层级 |
| `regionCode` | 行政区划编码 |

关系：

```text
Department --belongsTo--> Department
```

`belongsTo` 第一阶段只作为预留关系。没有权威层级数据时，不应仅凭部门名称直接导入。

## 5. Material

| 字段 | 含义 |
|---|---|
| `name` | 材料名称 |
| `materialId` | 材料稳定 ID，共享 id：`M-{md5(规范化材料名称)[:16]}`，见 1.1 |
| `alias` | 已确认的同义材料名称 |
| `canonicalName` | 标准材料名称（规范化后的哈希键文本） |

`Material` 是共享实体：同名（规范化后）材料跨事项合并，节点只保留共享层字段。

`required`、`orderNo`、`materialDescription`、`acceptanceStandard`、`materialType`、`sourceType`、`submissionFormat` 属于事项和材料之间的关系属性，不属于 Material 节点本身。其中 `materialType`（材料类型）、`sourceType`（材料来源，例如申请人自备、政府部门核发）、`submissionFormat`（提交形式，例如纸质、电子化）来自 `materials.csv` 的逐事项字段，重写时通过 JOIN 私有材料行逐对下沉到 `requiresMaterial` 边（`service_requires_material_out.csv` 的对应列）。

## 6. ServiceCondition

| 字段 | 含义 |
|---|---|
| `name` | 条件名称 |
| `conditionId` | 条件 ID |
| `description` | 原始条件文本，不能被抽取结果替代 |
| `conditionType` | 粗粒度条件类型，例如申请对象、资格要求、禁止情形 |
| `statement` | 经过 5W1H 展开的上下文独立陈述 |
| `sourceChunkId` | 产生该条件的 Chunk ID |
| `confidence` | 条件抽取或匹配置信度 |
| `reviewStatus` | 条件审核状态 |

## 7. ProcessStep

| 字段 | 含义 |
|---|---|
| `name` | 步骤名称 |
| `processStepId` | 步骤 ID |
| `stepCode` | 来源系统中的步骤编码 |
| `handler` | 办理人员或机构 |
| `timeLimit` | 步骤办理时限 |
| `checkStandard` | 核验或审查标准 |
| `description` | 步骤结果或补充说明 |

`orderNo` 属于 `GovernmentService --hasProcessStep--> ProcessStep` 关系属性；`nextStep` 只表示步骤之间的先后连接。

## 8. ServiceResult

| 字段 | 含义 |
|---|---|
| `name` | 结果名称 |
| `resultId` | 结果 ID |
| `resultType` | 结果类型，例如批文、证照、通知 |
| `description` | 结果说明 |
| `licenseCode` | 证照编码 |
| `validityPeriod` | 有效期 |

## 9. LegalBasis

| 字段 | 含义 |
|---|---|
| `name` | 法规名称 |
| `legalBasisId` | 法规文件稳定 ID，共享 id：`LB-{md5(规范化法规名\|规范化文号)[:16]}`，文号缺失时文档键退化为规范化法规名称，见 1.1 |
| `documentNumber` | 法规文号 |
| `legalType` | 法律、行政法规、地方性法规、部门规章、规范性文件等 |
| `issuingAuthority` | 发布机关 |
| `publishedDate` | 发布日期 |
| `effectiveDate` | 生效日期 |
| `lawUrl` | 法规链接 |
| `alias` | 法规别名或历史名称 |
| `canonicalName` | 规范化法规名称 |

## 10. LegalCitation

| 字段 | 含义 |
|---|---|
| `name` | 法规名称和条款组成的可读名称 |
| `citationId` | 条款引用 ID，共享 id：`LC-{md5(规范化法规名\|规范化文号\|规范化条款)[:16]}`，见 1.1 |
| `article` | 条款号 |
| `content` | 条文内容 |
| `sourceChunkId` | 条文来源 Chunk ID |
| `confidence` | 法条匹配或抽取置信度 |
| `reviewStatus` | 审核状态 |

关系：

```text
GovernmentService --citesLegal--> LegalCitation
LegalCitation --partOf--> LegalBasis
LegalCitation --hasChunk--> Chunk
LegalCitation --statesProposition--> Proposition
```

同一法规不同条款必须保持为不同的 LegalCitation，不能因为文号相同而覆盖条款内容；同名不同文号的法规也不能因为名称相同而合并。`legal_bases.csv` 的一行（法规 + 条款）在导入前由 `scripts/build_shared_ids.py` 拆分为 `legal_citations_out.csv`（条款级）与 `legal_bases_out.csv`（文号级去重），并生成 `part_of.csv`（LegalCitation --partOf--> LegalBasis）；原始 `legal_basis_id` 保留为 `first_source_legal_basis_id` / `source_legal_basis_id` 来源追踪字段。同 id 但 `clause_content` 不一致的行保留首见并计入 stats。

## 11. FAQ

| 字段 | 含义 |
|---|---|
| `name` | 问题 |
| `faqId` | FAQ ID |
| `answer` | 答案 |
| `description` | 答案补充说明 |
| `orderNo` | 在事项中的显示顺序 |

FAQ 保留为结构化实体，同时可以将问题和答案组合成 Chunk 用于语义检索。

## 12. ServiceChannel

| 字段 | 含义 |
|---|---|
| `name` | 渠道内容 |
| `channelId` | 渠道 ID |
| `channelType` | 渠道类型 |
| `serviceTime` | 服务时间 |
| `description` | 渠道说明 |

## 13. Fee

| 字段 | 含义 |
|---|---|
| `name` | 收费项目 |
| `feeId` | 收费 ID |
| `feeStandard` | 收费标准 |
| `description` | 收费依据 |
| `feeStatus` | 收费状态 |

## 14. Chunk

| 字段 | 含义 |
|---|---|
| `name` | 文本块可读名称 |
| `content` | 文本块正文，建立 TextAndVector 索引 |
| `chunkId` | 文本块 ID |
| `docId` | 原始文档 ID |
| `sourceId` | 来源实体 ID |
| `sourceType` | 来源实体类型 |
| `sourceField` | 来源字段，例如 `acceptCondition`、`faq`、`legalContent` |
| `chunkIndex` | 当前文档中的序号 |
| `chunkCount` | 当前文档的总块数 |
| `serviceId` | 关联事项 ID |
| `categoryL1` | 原始一级分类 |
| `categoryL2` | 原始二级分类 |
| `departmentName` | 主管部门名称 |
| `sourceUrl` | 来源链接 |
| `sourceFile` | 来源文件 |
| `sourceLine` | 来源行号 |
| `extractionVersion` | 文本切分版本 |

## 15. Proposition

| 字段 | 含义 |
|---|---|
| `name` | 命题可读名称 |
| `propositionId` | 命题稳定 ID |
| `subjectId` | 主体实体 ID |
| `subjectType` | 主体实体类型 |
| `predicate` | 开放谓词文本，不进入固定关系白名单 |
| `objectId` | 客体实体 ID；客体为字面量时为空 |
| `objectType` | 客体实体类型 |
| `objectValue` | 客体字面值 |
| `statement` | 上下文独立的完整陈述 |
| `sourceChunkId` | 证据 Chunk ID |
| `confidence` | 抽取置信度 |
| `extractionMethod` | 抽取方法，例如 `rule`、`socratic_5w1h`、`synthkg` |
| `extractionModel` | 使用的模型名称 |
| `extractionVersion` | 抽取提示词或流水线版本 |
| `reviewStatus` | 审核状态 |

命题关系：

```text
GovernmentService --statesProposition--> Proposition
LegalCitation --statesProposition--> Proposition
Proposition --extractedFrom--> Chunk
```

---

## 16. 当前 CSV 到 Schema 的核心映射

| 当前 CSV | 目标实体或关系 |
|---|---|
| `services.csv` | `GovernmentService` |
| `departments.csv` | `Department` |
| `materials.csv` | `Material`（经 `build_shared_ids.py` 重写为共享 ID：`materials_out.csv`） |
| `conditions.csv` | `ServiceCondition` |
| `process_steps.csv` | `ProcessStep` |
| `results.csv` | `ServiceResult` |
| `legal_bases.csv` | 拆分为 `LegalCitation`（`legal_citations_out.csv`）+ `LegalBasis`（`legal_bases_out.csv`）+ `partOf` 边（`part_of.csv`） |
| `faqs.csv` | `FAQ` |
| `service_channels.csv` | `ServiceChannel` |
| `fees.csv` | `Fee` |
| `documents_chunks.csv` | `Chunk` |

关系表：

| 当前 CSV | Schema 关系 |
|---|---|
| `service_handled_by.csv` | `GovernmentService.handledBy` |
| `service_collaborates_with.csv` | `GovernmentService.collaboratesWith` |
| `service_requires_material.csv` | `GovernmentService.requiresMaterial`（重写产物 `service_requires_material_out.csv` 补逐事项材料字段列） |
| `service_has_condition.csv` | `GovernmentService.hasCondition` |
| `service_has_process_step.csv` | `GovernmentService.hasProcessStep` |
| `process_step_next.csv` | `ProcessStep.nextStep` |
| `service_produces_result.csv` | `GovernmentService.producesResult` |
| `service_based_on.csv` | 转换后为 `GovernmentService.citesLegal`（重写产物 `service_based_on_out.csv` 的引用已改为 LC id） |
| `service_has_faq.csv` | `GovernmentService.hasFaq` |
| `service_has_channel.csv` | `GovernmentService.hasChannel` |
| `service_has_fee.csv` | `GovernmentService.hasFee` |

---

## 17. 导入和抽取阶段

### v0.3a：先导入稳定骨架

```text
领域/分类/模型
    ↓
业务实体
    ↓
业务关系
    ↓
Chunk / FAQ
```

### v0.3b：再加入开放知识

```text
Chunk
    ↓
去语境化
    ↓
SocraticKG 5W1H
    ↓
SynthKG 命题抽取
    ↓
Proposition / ServiceCondition
    ↓
约束校验和人工审核
```
