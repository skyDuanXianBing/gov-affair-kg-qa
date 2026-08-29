# ZwdmxGJ Schema 文档

本目录保存当前项目的知识建模文件，不包含大体量数据。

## 文件说明

| 文件 | 用途 |
|---|---|
| `ZwdmxGJ-v0.3.schema` | 可提交到 OpenSPG 的 SPG MarkLang Schema |
| `ZwdmxGJ-v0.3-字段说明.md` | 每个实体、属性和关系的含义，以及当前 CSV 映射 |
| `ZwdmxGJ-v0.3-设计原则.md` | Schema 分层、论文方法落地、约束和版本化原则 |

## 与旧设计文档的关系

本目录是新的可执行 Schema 设计，不删除或覆盖以下历史/方案文档：

```text
docs/知识建模方案-v0.3.md
docs/知识建模方案-v0.3-修订版.md
kg/design/GovAffair.schema
```

旧文档保留用于设计背景和历史对照。本目录中的 `ZwdmxGJ-v0.3.schema` 才是本阶段面向自有 Namespace 的 Schema 草案。

## 使用范围

目标 Namespace：

```text
ZwdmxGJ
```

当前 Schema 设计包含：

```text
领域/分类/知识模型路由层
稳定业务骨架层
Chunk 证据层
Proposition 开放知识层
```

发布前必须完成：

1. OpenSPG MarkLang 语法校验；
2. Schema 实体和关系与导入 manifest 对齐；
3. LegalBasis / LegalCitation 的 CSV 转换（`scripts/build_shared_ids.py`，产物在 `build/shared_ids/`）；
4. 共享 ID 重写产物导入验收（pilot 481,501 事项 + personal 1,090,612 事项，实测规模见数据 manifests）；
5. 关系域值域约束校验；
6. 图查询试点验收（data/pilot 与 data/personal 各域 testset）。
