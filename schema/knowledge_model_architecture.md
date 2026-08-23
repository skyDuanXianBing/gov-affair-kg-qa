# 统一 Namespace 知识模型架构

## 目标

法人服务和个人事务进入同一个 OpenSPG namespace：

```text
ZwdmxGJ
```

分类不拆成独立 namespace，而是作为图中的 `ServiceCategory` 实体和 `KnowledgeModel` Profile。这样保留跨分类共享实体和原生多跳路径：

```text
GovernmentService -> Department -> GovernmentService -> ServiceCategory
```

## 三层模型

```text
物理层：ZwdmxGJ
领域层：ServiceDomain
分类层：ServiceCategory
模型层：KnowledgeModel
实例层：GovernmentService、Department、Material、LegalBasis、Chunk 等
```

### ServiceDomain

当前领域：

```text
domain:corporate -> 法人服务
domain:personal   -> 个人事务
```

### CategoryScheme

不同领域使用不同分类体系：

```text
scheme:corporate_subject    -> 法人服务主题分类体系
scheme:personal_service_type -> 个人事务事项类型分类体系
```

### ServiceCategory

法人服务当前由 `category_l2` 生成 49 个二级分类；个人事务当前由 `category_l2` 生成 8 个二级分类。一级根分类分别为法人服务和个人事务。

### KnowledgeModel

每个领域有一个 `DOMAIN_BASE` 模型，每个二级分类有一个 `CATEGORY_PROFILE` 模型。分类模型通过 `extendsModel` 继承领域基础模型，配置字段包括：

- `enabledEntityTypes`
- `enabledRelationTypes`
- `retrievalFilter`
- `validationProfile`

## 统一图关系

```text
GovernmentService.belongsToDomain -> ServiceDomain
GovernmentService.classifiedAs    -> ServiceCategory
GovernmentService.usesModel       -> KnowledgeModel

ServiceCategory.parentCategory    -> ServiceCategory
ServiceCategory.belongsToScheme   -> CategoryScheme
ServiceCategory.belongsToDomain   -> ServiceDomain

KnowledgeModel.appliesToCategory  -> ServiceCategory
KnowledgeModel.belongsToDomain    -> ServiceDomain
KnowledgeModel.extendsModel       -> KnowledgeModel
```

## CSV 产物

生成脚本：

```bash
.venv/bin/python scripts/generate_kg_model_metadata.py
```

输出目录：

```text
build/openspg-model-metadata/
├── common/
│   ├── service_domains.csv
│   ├── category_schemes.csv
│   ├── service_categories.csv
│   ├── knowledge_models.csv
│   └── 分类/模型关系 CSV
├── pilot/
│   ├── services.csv
│   └── 事项分类/领域/模型关系 CSV
└── personal/
    ├── services.csv
    └── 事项分类/领域/模型关系 CSV
```

原有 `categoryL1/categoryL2` 字段继续保留；`domainId/categoryId/modelId` 用于检索路由和图谱过滤。

## 导入顺序

```text
ServiceDomain / CategoryScheme / ServiceCategory / KnowledgeModel
    ↓
GovernmentService 等基础实体
    ↓
分类、领域、模型关系
    ↓
原有业务关系
    ↓
Chunk
```

所有任务仍绑定 `ZwdmxGJ`，新分片为 16 MiB，manifest 版本为 3。
