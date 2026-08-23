# OpenSPG 政务服务数据导入计划

> 状态：法人全量规范化与完整性复验均已完成（`valid=true`）；下一阶段为分片导入。
> 当前本地 Schema namespace：`ZwdmxGJ`
> 目标项目使用 `ZwdmxGJ`；Schema、实体导入、关系导入、Chunk 检索任务必须全部绑定到此 namespace。

## 一、导入原则

- Schema 先发布，数据后导入。
- 节点 CSV：一行对应一个实体实例，第一列 `*_id` 作为业务主键。
- 关系 CSV：一行对应一条关系，关系两端节点必须先存在。
- `documents.csv` 是完整文档源，不单独创建 `Document` 节点。
- `documents_chunks.csv` 是显式 Chunk 导入方案；若使用 KAG 自动切分，则不再导入该文件。
- 业务权威事实来自实体 CSV 和关系 CSV；Chunk 中的事项、分类、部门字段仅作为检索冗余元数据。

## 二、Schema

Schema 文件：

```text
schema/gov_service.schema
```

当前核心 EntityType：

```text
Chunk
GovernmentService
Department
Material
ServiceCondition
ProcessStep
ServiceResult
LegalBasis
FAQ
ServiceChannel
Fee
```

## 三、节点数据导入顺序

| 顺序 | CSV | EntityType | 主键列 |
|---:|---|---|---|
| 1 | `services.csv` | `GovernmentService` | `service_id` |
| 2 | `departments.csv` | `Department` | `department_id` |
| 3 | `materials.csv` | `Material` | `material_id` |
| 4 | `conditions.csv` | `ServiceCondition` | `condition_id` |
| 5 | `process_steps.csv` | `ProcessStep` | `process_step_id` |
| 6 | `results.csv` | `ServiceResult` | `result_id` |
| 7 | `legal_bases.csv` | `LegalBasis` | `legal_basis_id` |
| 8 | `faqs.csv` | `FAQ` | `faq_id` |
| 9 | `service_channels.csv` | `ServiceChannel` | `channel_id` |
| 10 | `fees.csv` | `Fee` | `fee_id` |

数据目录：

```text
dataset/normalized/openspg/pilot/
```

## 四、关系数据导入顺序

以下关系暂不导入，仅保留计划。

| 顺序 | CSV | Schema 关系 | 起点列 | 终点列 |
|---:|---|---|---|---|
| 1 | `service_handled_by.csv` | `GovernmentService.handledBy` | `service_id` | `department_id` |
| 2 | `service_collaborates_with.csv` | `GovernmentService.collaboratesWith` | `service_id` | `department_id` |
| 3 | `service_requires_material.csv` | `GovernmentService.requiresMaterial` | `service_id` | `material_id` |
| 4 | `service_has_condition.csv` | `GovernmentService.hasCondition` | `service_id` | `condition_id` |
| 5 | `service_has_process_step.csv` | `GovernmentService.hasProcessStep` | `service_id` | `process_step_id` |
| 6 | `process_step_next.csv` | `ProcessStep.nextStep` | `from_process_step_id` | `to_process_step_id` |
| 7 | `service_produces_result.csv` | `GovernmentService.producesResult` | `service_id` | `result_id` |
| 8 | `service_based_on.csv` | `GovernmentService.basedOn` | `service_id` | `legal_basis_id` |
| 9 | `service_has_faq.csv` | `GovernmentService.hasFaq` | `service_id` | `faq_id` |
| 10 | `service_has_channel.csv` | `GovernmentService.hasChannel` | `service_id` | `channel_id` |
| 11 | `service_has_fee.csv` | `GovernmentService.hasFee` | `service_id` | `fee_id` |

关系属性映射：

```text
service_handled_by.csv
  department_role       -> departmentRole

service_collaborates_with.csv
  participates_in_step  -> participatesInStep

service_requires_material.csv
  required              -> required
  order_no              -> orderNo(Integer)
  material_description  -> materialDescription
  acceptance_standard   -> acceptanceStandard

service_has_condition.csv
  condition_source      -> conditionSource

service_has_process_step.csv
  order_no              -> orderNo(Integer)

service_produces_result.csv
  order_no              -> orderNo(Integer)

service_based_on.csv
  order_no              -> orderNo(Integer)
  basis_source          -> basisSource

service_has_faq.csv
  order_no              -> orderNo(Integer)
```

`process_step_next.csv` 中的 `service_id` 是追踪字段；`ProcessStep.nextStep` 的两端使用：

```text
from_process_step_id -> to_process_step_id
```

## 五、Chunk 导入方案

### 方案 A：KAG 自动切分

```text
documents.csv -> KAG 自动切分 -> Chunk -> Retrieval/Vector Index
```

使用该方案时，不再导入：

```text
documents_chunks.csv
```

### 方案 B：显式导入预分块文件

当前全量发布采用方案 B，以保证切分结果和验收可重复。

```text
documents_chunks.csv -> Chunk
```

字段映射：

```text
chunk_id     -> Chunk 业务主键
title        -> name
content      -> content (TextAndVector)
doc_id       -> docId
service_id   -> serviceId
category_l1  -> categoryL1
category_l2  -> categoryL2
source_url   -> sourceUrl
source_file  -> sourceFile
source_line  -> sourceLine
```

导入后还需要：

```text
1. 绑定 Chunk.content 的 TextAndVector 索引
2. 配置 Embedding 模型
3. 执行向量化任务
4. 构建 Retrieval 索引
5. 用 testset.csv 验收
```

两种方案只能选一种，不能同时导入并自动切分，否则会产生两批 Chunk。

## 六、法人全量规模核对

本轮法人全量输入与规范化基线：

```text
输入记录：549,488
唯一 GovernmentService：481,501
拒绝记录：67,987
  duplicate_service_id：66,474
  missing_or_invalid_AUDIT_ITEM：1,513
```

材料 ID 冲突修复后的最终全量统计如下，以
`manifests/summary.json` 和 `validation_summary.json` 为准：

```text
Department：25,855
Material：2,153,892
ServiceCondition：481,500
ProcessStep：1,556,046
ServiceResult：316,827
LegalBasis：6,305,710
FAQ：126,849
Fee：470,559
ServiceChannel：1,593,314
Chunk：1,306,907
```

主要关系基线：

```text
handledBy：481,501
requiresMaterial：2,153,892
hasCondition：481,500
hasProcessStep：1,556,046
nextStep：1,274,564
producesResult：316,829
basedOn：6,305,710
hasFaq：126,849
hasChannel：1,593,314
hasFee：470,559
collaboratesWith：43,700
```

### 全量文件分片要求

Builder 页面单文件限制约 200 MiB，当前全量文件中 `documents.csv`、
`documents_chunks.csv`、`legal_bases.csv` 及多张关系表均超过限制，不能由现有
`upload_file()` 直接上传。导入前必须执行：

```text
1. 按 CSV 记录边界切为 100–150 MiB，每片重复表头。
2. 上传请求改为流式读取，禁止 Path.read_bytes()。
3. 生成分片 manifest，记录原表、分片序号、行数、字节数和 SHA256。
4. 实体分片全部成功后再提交关系分片，Chunk 最后提交。
5. 状态文件按分片记录 SUCCESS/FAILED，重跑时跳过 SHA256 未变化的成功分片。
6. 使用滚动分片策略：生成一批、上传验证、删除已上传本地分片，控制峰值空间。
```

## 七、导入前校验

- 所有关系起点 `service_id` 都存在于 `services.csv`。
- 所有 `material_id` 都存在于 `materials.csv`。
- 所有 `department_id` 都存在于 `departments.csv`。
- 所有 `process_step_id` 都存在于 `process_steps.csv`。
- 所有 `legal_basis_id` 都存在于 `legal_bases.csv`。
- 所有 `Chunk.serviceId` 都能关联到 `GovernmentService.serviceId`。
- `order_no` 字段只保留整数或空值。
- 不使用事项名称作为唯一 ID，优先使用 CSV 中的业务 ID。
- 所有实体 CSV 的业务主键非空且唯一；同 ID 不同属性视为构建错误。
- `documents.csv` 的 `doc_id` 唯一，且 `service_id` 均存在。
- `documents_chunks.csv` 的 `chunk_id` 唯一，文档与事项端点均存在，内容不超过 2,000 字符。
- 使用 `scripts/validate_openspg_csvs.py` 生成 `manifests/validation_summary.json`。
- 法人全量最终校验：`dataset/normalized/openspg/pilot/manifests/validation_summary.json` 中 `valid=true`。

## 八、查询验收的规范语义

问题：

```text
高等职业学校设立（含设置分校区）需要提交哪些申请材料？
```

规范查询目标：

```text
GovernmentService[service_id=2a8c7a95e01f57b8d43a558642e0cae5]
    --requiresMaterial-->
Material
```

Planner 生成的关系应接近：

```text
Retrieval(
  s=GovernmentService[高等职业学校设立（含设置分校区）],
  p=requiresMaterial,
  o=Material
)
```

不应使用当前 Schema 中不存在的类型或关系：

```text
政策/规定
所需申请材料
材料清单
```
