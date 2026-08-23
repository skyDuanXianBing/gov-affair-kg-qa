# GovService Schema 与试点 CSV 导入映射

Schema：`schema/gov_service.schema`  
试点数据：`dataset/normalized/openspg/pilot/`

## 映射原则

- 每个节点表的第一列 `*_id` 是该实体的**业务主键**，导入 OpenSPG 时映射为实体 ID；同名 `*Id` 属性同时保存为可检索属性。
- `name` 是实体的阅读名称；源 CSV 的 `*_name`、`question` 或 `channel_value` 映射至它。
- `description` 放长文本：条件文本、步骤结果、法规条文、FAQ 答案等。
- 关系表的起止 ID 分别映射为源/目标实体；其余列映射为关系属性。
- CSV 中的 `order_no` 仅含整数时映射到 `Integer`；源数据为空时留空。

## 节点表

| CSV | EntityType | 业务主键 | 关键属性映射 |
|---|---|---|---|
| `services.csv` | `GovernmentService` | `service_id` | `service_name → name`；其余蛇形字段转为同义 camelCase 属性 |
| `departments.csv` | `Department` | `department_id` | `department_name → name` |
| `materials.csv` | `Material` | `material_id` | `material_name → name` |
| `conditions.csv` | `ServiceCondition` | `condition_id` | `condition_name → name`；`condition_text → description` |
| `process_steps.csv` | `ProcessStep` | `process_step_id` | `step_name → name`；`step_result → description` |
| `results.csv` | `ServiceResult` | `result_id` | `result_name → name`；`result_description → description` |
| `legal_bases.csv` | `LegalBasis` | `legal_basis_id` | `law_name → name`；`clause_content → description` |
| `faqs.csv` | `FAQ` | `faq_id` | `question → name`；`answer → answer` 与 `description` |
| `service_channels.csv` | `ServiceChannel` | `channel_id` | `channel_value → name` |
| `fees.csv` | `Fee` | `fee_id` | `fee_name → name`；`fee_basis → description` |

`documents.csv` 是 KAG 文本导入源，而不是上述确定性节点的重复导入表：

```text
doc_id → 文档ID
 title → 名称
 content → 文本内容（TextAndVector）
 service_id、分类、来源字段 → Chunk 对应元数据
```

由 KAG 导入任务自动分块时，Chunk 通常由任务生成；若导入界面要求显式配置 Chunk，则按上述字段导入 `Chunk`。

## 关系表

| CSV | Schema relation | 源 ID | 目标 ID | 关系属性 |
|---|---|---|---|---|
| `service_handled_by.csv` | `GovernmentService.handledBy` | `service_id` | `department_id` | `department_role → departmentRole` |
| `service_collaborates_with.csv` | `GovernmentService.collaboratesWith` | `service_id` | `department_id` | `participates_in_step → participatesInStep` |
| `service_requires_material.csv` | `GovernmentService.requiresMaterial` | `service_id` | `material_id` | `required`、`order_no → orderNo`、`material_description → materialDescription`、`acceptance_standard → acceptanceStandard` |
| `service_has_condition.csv` | `GovernmentService.hasCondition` | `service_id` | `condition_id` | `condition_source → conditionSource` |
| `service_has_process_step.csv` | `GovernmentService.hasProcessStep` | `service_id` | `process_step_id` | `order_no → orderNo` |
| `process_step_next.csv` | `ProcessStep.nextStep` | `from_process_step_id` | `to_process_step_id` | 无 |
| `service_produces_result.csv` | `GovernmentService.producesResult` | `service_id` | `result_id` | `order_no → orderNo` |
| `service_based_on.csv` | `GovernmentService.basedOn` | `service_id` | `legal_basis_id` | `order_no → orderNo`、`basis_source → basisSource` |
| `service_has_faq.csv` | `GovernmentService.hasFaq` | `service_id` | `faq_id` | `order_no → orderNo` |
| `service_has_channel.csv` | `GovernmentService.hasChannel` | `service_id` | `channel_id` | 无 |
| `service_has_fee.csv` | `GovernmentService.hasFee` | `service_id` | `fee_id` | 无 |

## 建议导入顺序

1. 先创建并发布 `GovService` Schema；
2. 导入 10 张节点表；
3. 导入 11 张关系表；
4. 导入 `documents.csv`，配置 KAG 的 Chunk、向量索引和抽取任务；
5. 用“事项需要哪些材料、由谁办理、办理流程、依据什么法规”等问题做验收。
