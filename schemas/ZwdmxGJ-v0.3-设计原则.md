# ZwdmxGJ Schema v0.3 设计原则

## 1. 一个 Namespace，多个业务领域

所有法人服务、个人事务以及后续扩展领域共用：

```text
namespace ZwdmxGJ
```

领域差异通过以下实体表达：

```text
ServiceDomain
ServiceCategory
KnowledgeModel
```

因此：

```text
同一个物理知识图谱
    + 不同领域
    + 不同分类体系
    + 不同知识模型
```

不为法律、药品、法人服务、个人事务分别创建 Namespace。

## 2. 固定骨架与开放知识分离

稳定、高频、需要精确查询的事实使用固定实体和关系：

```text
GovernmentService -> handledBy -> Department
GovernmentService -> requiresMaterial -> Material
GovernmentService -> hasProcessStep -> ProcessStep
GovernmentService -> producesResult -> ServiceResult
GovernmentService -> citesLegal -> LegalCitation
```

长尾事实、隐含条件和新出现的语义不直接扩展固定关系，而进入：

```text
Proposition
```

这样可以避免每出现一个新谓词就修改 Schema。

## 3. 结构化数据优先使用规则，不让 LLM 替代确定性事实

以下字段优先通过 CSV Mapping 导入：

```text
事项名称、事项编码、部门、材料、步骤、结果、FAQ、渠道、收费、法规文号
```

LLM 只用于：

```text
长文本分块后的去语境化
受理条件的 5W1H 展开
长文本命题抽取
实体归一候选生成
```

原始结构化值和原始文本必须保留。

## 4. 法规和条款分层

法规文件与事项引用的具体条款不是同一个对象：

```text
LegalBasis      = 整部法规、规章或规范性文件
LegalCitation   = 某事项引用的具体条款
```

关系：

```text
GovernmentService -> citesLegal -> LegalCitation -> partOf -> LegalBasis
```

原因：同一法规可能被多个事项引用不同条款，不能让一个法规节点承载所有事项上下文，也不能让同一条边覆盖多个条款。

## 5. 节点保存稳定属性，关系保存上下文属性

例如材料节点只保存材料自身稳定信息：

```text
Material.name
Material.materialType
Material.submissionFormat
```

事项特有的要求放在关系上：

```text
GovernmentService -[requiresMaterial {
    required,
    orderNo,
    materialDescription,
    acceptanceStandard
}]-> Material
```

这样同一材料在不同事项中可以有不同的份数、必要性和受理标准。

## 6. Chunk 是证据，不是事实替代品

所有长文本抽取结果必须能回到原始 Chunk：

```text
Proposition -> extractedFrom -> Chunk
ServiceCondition.sourceChunkId -> Chunk.id
LegalCitation.sourceChunkId -> Chunk.id
```

Chunk 至少保留：

```text
sourceId
sourceType
sourceField
serviceId
sourceUrl
sourceFile
sourceLine
```

不能只保留 LLM 改写后的命题而删除原文。

## 7. ServiceCondition 处理复杂业务条件

受理条件中的：

```text
申请对象
资格要求
数量限制
禁止情形
例外条件
```

先经过 SocraticKG 风格的 5W1H 展开，再形成：

```text
ServiceCondition
Proposition
```

第一阶段只使用粗粒度 `conditionType` 和完整 `statement`，不急于固化复杂逻辑表达式。

## 8. Proposition 的谓词保持开放

Proposition 允许：

```text
predicate = 申请资格
predicate = 禁止情形
predicate = 特殊办理要求
predicate = 补充材料条件
```

这些谓词不直接进入 Schema 固定关系白名单。

当某类谓词经过长期统计、人工确认并成为高频稳定业务关系后，才考虑将它提升为固定关系。

## 9. 实体归一优先于自动合并

优先处理：

```text
Department
Material
LegalBasis
```

归一流程：

```text
原始名称
    ↓
规则清洗
    ↓
候选别名
    ↓
人工确认或高置信确认
    ↓
canonicalId
    ↓
导入图谱
```

不允许只凭字符串相似度直接合并：

```text
营业执照
营业执照副本
营业执照复印件
```

也不能只凭部门名称推断权威层级。

## 10. 关系必须具备域和值域约束

固定关系的主体类型和客体类型必须明确：

```text
handledBy:
    GovernmentService -> Department

requiresMaterial:
    GovernmentService -> Material

hasCondition:
    GovernmentService -> ServiceCondition

citesLegal:
    GovernmentService -> LegalCitation

partOf:
    LegalCitation -> LegalBasis

nextStep:
    ProcessStep -> ProcessStep
```

违反约束的数据不能静默丢弃，应进入：

```text
rejects/
review/
```

并记录：

```text
missingEndpointCount
ontologyMismatchRate
duplicateFactCount
conflictCount
manualReviewCount
```

## 11. 关系命名要有语义区分度

关系名称使用明确动词，不使用含义模糊的：

```text
relatedTo
hasInfo
belongs
```

推荐：

```text
handledBy
collaboratesWith
requiresMaterial
hasProcessStep
producesResult
citesLegal
classifiedAs
belongsToDomain
```

这样后续可以支持问题语义与关系类型的匹配。

## 12. 先做可验证的 v0.3a，再做 LLM 知识层 v0.3b

### v0.3a

```text
确定性实体
确定性关系
LegalBasis / LegalCitation
FAQ
Chunk
ID 校验
关系域值域校验
1000 条法人 + 1000 条个人试点
```

### v0.3b

```text
Proposition
5W1H 条件展开
长文本去语境化
LLM 命题抽取
别名归一审核
抽取质量评估
```

在 v0.3a 没有通过试点验收前，不执行全量 LLM 抽取。

## 13. 保守扩展，不直接设计完整领域本体

当前不建立数百类的封闭“办事指南本体”。

先用：

```text
稳定骨架
开放命题
来源证据
```

等命题谓词和实体分布稳定后，再根据真实数据统计决定是否新增固定类型或关系。

## 14. 可回滚和可审计

每个抽取结果都应能通过以下字段定位：

```text
sourceChunkId
sourceUrl
sourceFile
sourceLine
extractionMethod
extractionModel
extractionVersion
reviewStatus
```

Schema、抽取结果和导入任务都需要版本化：

```text
Schema v0.3
抽取流水线 v0.3
Prompt v0.3
数据快照 2026-08-29
```

## 15. 当前落地顺序

```text
1. 发布 ZwdmxGJ-v0.3.schema
2. 建立约束表和实体归一表
3. 用法人 1000 条 + 个人 1000 条验证固定骨架
4. 检查图查询、关系属性和 Chunk 检索
5. 加入 Proposition
6. 用 SocraticKG 处理 ServiceCondition
7. 用 SynthKG 处理长文本命题
8. 用 Wikontic 方法做归一、约束和质量 dashboard
```
