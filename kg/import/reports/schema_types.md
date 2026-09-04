# ZwdmxGJ schema 发布核对报告

- 项目: id=1 namespace=ZwdmxGJ
- schema 文件: `schemas\ZwdmxGJ-v0.3.schema`
- 声明实体: 17；已发布实体: 17
- 声明关系: 29；已发布关系: 29

适配变更：Double -> Float（3 行，confidence 字段，本机基础类型无 Double）

| 实体 | 属性数（含 id） | 关系 | 缺失 |
|---|---|---|---|
| CategoryScheme | 6 | belongsToDomain->ServiceDomain | |
| Chunk | 19 | - | |
| Department | 9 | belongsTo->Department | |
| FAQ | 6 | - | |
| Fee | 6 | - | |
| GovernmentService | 26 | belongsToDomain->ServiceDomain, citesLegal->LegalCitation, classifiedAs->ServiceCategory, collaboratesWith->Department, handledBy->Department, hasChannel->ServiceChannel, hasChunk->Chunk, hasCondition->ServiceCondition, hasFaq->FAQ, hasFee->Fee, hasProcessStep->ProcessStep, producesResult->ServiceResult, requiresMaterial->Material, statesProposition->Proposition, usesModel->KnowledgeModel | |
| KnowledgeModel | 14 | appliesToCategory->ServiceCategory, belongsToDomain->ServiceDomain, extendsModel->KnowledgeModel | |
| LegalBasis | 12 | - | |
| LegalCitation | 9 | hasChunk->Chunk, partOf->LegalBasis, statesProposition->Proposition | |
| Material | 6 | - | |
| ProcessStep | 8 | nextStep->ProcessStep | |
| Proposition | 17 | extractedFrom->Chunk | |
| ServiceCategory | 10 | belongsToDomain->ServiceDomain, belongsToScheme->CategoryScheme, parentCategory->ServiceCategory | |
| ServiceChannel | 6 | - | |
| ServiceCondition | 9 | hasChunk->Chunk | |
| ServiceDomain | 6 | - | |
| ServiceResult | 7 | - | |

项目内多余实体（不删除）: 无
缺失关系: 无（声明关系全部已发布）
