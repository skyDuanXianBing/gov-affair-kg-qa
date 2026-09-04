#!/usr/bin/env python3
"""生成 pilot 骨架层导入 manifest（kg/import/manifests/pilot_import_manifest.json）。

列映射依据 schemas/ZwdmxGJ-v0.3-字段说明.md §16 与 schemas/ZwdmxGJ-v0.3.schema：
- 路由元数据 ← kg/import/routing_metadata/
- 共享实体（重写产物）← build/shared_ids/pilot/{materials,legal_bases,legal_citations}_out.csv
- 弱实体/事项 ← data/pilot/*.csv
- services 分片时追加 domain_id/category_id/model_id 三列（routing 脚本同款算法）

导入顺序（manifest 内 job 顺序即执行顺序）：
路由实体 → 路由关系 → 共享实体 → 弱实体 → services → 业务关系。
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "kg" / "import" / "manifests" / "pilot_import_manifest.json"
ROUTING_DIR = ROOT / "kg" / "import" / "routing_metadata"
SHARED_DIR = ROOT / "build" / "shared_ids" / "pilot"
PILOT_DIR = ROOT / "data" / "pilot"

BASE_URL = "http://127.0.0.1:8887"
PROJECT_ID = 2
NAMESPACE = "ZwdmxGJ"
CREATE_USER = "openspg"

# 分片阈值（MiB）：超过则滚动分片上传（Builder 单文件上限约 200 MiB）
SHARD_THRESHOLD_MIB = 150.0


def entity(key: str, name: str, group: str, file: Path, type_name: str, mapping: dict, shard: bool = False) -> dict:
    return {
        "key": key, "name": name, "group": group, "file": str(file.relative_to(ROOT)),
        "schema_target": {"kind": "entity", "type": type_name},
        "mapping": mapping, "shard": shard,
    }


def relation(key: str, name: str, group: str, file: Path, source: str, rel: str, target: str, mapping: dict, shard: bool = False) -> dict:
    return {
        "key": key, "name": name, "group": group, "file": str(file.relative_to(ROOT)),
        "schema_target": {"kind": "relation", "source_type": source, "relation": rel, "target_type": target},
        "mapping": mapping, "shard": shard,
    }


def build_jobs() -> list[dict]:
    jobs: list[dict] = []

    # ---- 1. 路由层实体
    jobs.append(entity("service_domains", "路由-服务领域", "routing", ROUTING_DIR / "service_domains.csv", "ServiceDomain", {
        "domain_id": ["id", "domainId"], "name": ["name"], "description": ["description"],
        "status": ["status"], "version": ["version"],
    }))
    jobs.append(entity("category_schemes", "路由-分类体系", "routing", ROUTING_DIR / "category_schemes.csv", "CategoryScheme", {
        "scheme_id": ["id", "schemeId"], "name": ["name"], "domain_id": ["domainId"],
        "description": ["description"], "version": ["version"],
    }))
    jobs.append(entity("service_categories", "路由-服务分类", "routing", ROUTING_DIR / "service_categories.csv", "ServiceCategory", {
        "category_id": ["id", "categoryId"], "name": ["name"], "category_level": ["categoryLevel"],
        "parent_category_id": ["parentCategoryId"], "scheme_id": ["schemeId"],
        "domain_id": ["domainId"], "description": ["description"], "status": ["status"],
        "version": ["version"],
    }))
    jobs.append(entity("knowledge_models", "路由-知识模型", "routing", ROUTING_DIR / "knowledge_models.csv", "KnowledgeModel", {
        "model_id": ["id", "modelId"], "name": ["name"], "model_type": ["modelType"],
        "domain_id": ["domainId"], "category_id": ["categoryId"], "version": ["version"],
        "description": ["description"], "schema_version": ["schemaVersion"],
        "enabled_entity_types": ["enabledEntityTypes"], "enabled_relation_types": ["enabledRelationTypes"],
        "retrieval_filter": ["retrievalFilter"], "validation_profile": ["validationProfile"],
        "status": ["status"],
    }))

    # ---- 2. 路由层关系
    jobs.append(relation("category_parent", "路由-父分类", "routing_rel", ROUTING_DIR / "category_parent.csv",
                         "ServiceCategory", "parentCategory", "ServiceCategory",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))
    jobs.append(relation("category_belongs_to_scheme", "路由-分类属体系", "routing_rel", ROUTING_DIR / "category_belongs_to_scheme.csv",
                         "ServiceCategory", "belongsToScheme", "CategoryScheme",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))
    jobs.append(relation("category_belongs_to_domain", "路由-分类属领域", "routing_rel", ROUTING_DIR / "category_belongs_to_domain.csv",
                         "ServiceCategory", "belongsToDomain", "ServiceDomain",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))
    jobs.append(relation("model_applies_to_category", "路由-模型适用分类", "routing_rel", ROUTING_DIR / "model_applies_to_category.csv",
                         "KnowledgeModel", "appliesToCategory", "ServiceCategory",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))
    jobs.append(relation("model_belongs_to_domain", "路由-模型属领域", "routing_rel", ROUTING_DIR / "model_belongs_to_domain.csv",
                         "KnowledgeModel", "belongsToDomain", "ServiceDomain",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))
    jobs.append(relation("model_extends_model", "路由-模型继承", "routing_rel", ROUTING_DIR / "model_extends_model.csv",
                         "KnowledgeModel", "extendsModel", "KnowledgeModel",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}))

    # ---- 3. 共享实体（共享 id 重写产物）
    jobs.append(entity("departments", "共享-部门", "shared", PILOT_DIR / "departments.csv", "Department", {
        "department_id": ["id", "departmentId"], "department_name": ["name"],
        "department_code": ["departmentCode"],
    }))
    jobs.append(entity("materials", "共享-材料", "shared", SHARED_DIR / "materials_out.csv", "Material", {
        "material_id": ["id", "materialId"], "material_name": ["name"],
        "canonical_name": ["canonicalName"], "merged_count": [],
    }))
    jobs.append(entity("legal_bases", "共享-法规文件", "shared", SHARED_DIR / "legal_bases_out.csv", "LegalBasis", {
        "legal_basis_id": ["id", "legalBasisId"], "law_name": ["name"],
        "document_number": ["documentNumber"], "published_date": ["publishedDate"],
        "law_url": ["lawUrl"], "citation_count": [],
    }))
    jobs.append(entity("legal_citations", "共享-法规条款", "shared", SHARED_DIR / "legal_citations_out.csv", "LegalCitation", {
        "legal_citation_id": ["id", "citationId"], "name": ["name"], "article": ["article"],
        "clause_content": ["content"], "law_name": [], "document_number": [],
        "published_date": [], "law_url": [], "first_source_legal_basis_id": [],
        "source_id_count": [],
    }))

    # ---- 4. 弱实体
    jobs.append(entity("conditions", "弱实体-办理条件", "weak", PILOT_DIR / "conditions.csv", "ServiceCondition", {
        "condition_id": ["id", "conditionId"], "condition_text": ["description"],
        "condition_name": ["name"],
    }, shard=True))
    jobs.append(entity("process_steps", "弱实体-办理步骤", "weak", PILOT_DIR / "process_steps.csv", "ProcessStep", {
        "process_step_id": ["id", "processStepId"], "step_name": ["name"],
        "step_code": ["stepCode"], "handler": ["handler"], "time_limit": ["timeLimit"],
        "check_standard": ["checkStandard"], "step_result": ["description"],
    }, shard=True))
    jobs.append(entity("results", "弱实体-办理结果", "weak", PILOT_DIR / "results.csv", "ServiceResult", {
        "result_id": ["id", "resultId"], "result_name": ["name"], "result_type": ["resultType"],
        "result_description": ["description"], "license_code": ["licenseCode"],
        "validity_period": ["validityPeriod"],
    }))
    jobs.append(entity("faqs", "弱实体-问答", "weak", PILOT_DIR / "faqs.csv", "FAQ", {
        "faq_id": ["id", "faqId"], "question": ["name"], "answer": ["answer"], "order_no": [],
    }))
    jobs.append(entity("service_channels", "弱实体-办理渠道", "weak", PILOT_DIR / "service_channels.csv", "ServiceChannel", {
        "channel_id": ["id", "channelId"], "channel_type": ["channelType"],
        "channel_value": ["name"], "service_time": ["serviceTime"],
    }, shard=True))
    jobs.append(entity("fees", "弱实体-收费", "weak", PILOT_DIR / "fees.csv", "Fee", {
        "fee_id": ["id", "feeId"], "fee_name": ["name"], "fee_standard": ["feeStandard"],
        "fee_basis": ["description"], "fee_status": ["feeStatus"],
    }))

    # ---- 5. services（分片时追加路由三列）
    jobs.append(entity("services", "事项-法人服务", "services", PILOT_DIR / "services.csv", "GovernmentService", {
        "service_id": ["id", "serviceId"], "service_name": ["name"],
        "category_l1": ["categoryL1"], "category_l2": ["categoryL2"],
        "department_name": ["departmentName"], "department_code": ["departmentCode"],
        "source_url": ["sourceUrl"], "source_file": ["sourceFile"], "source_line": ["sourceLine"],
        "publish_date": ["publishDate"], "version_date": ["versionDate"],
        "domain_id": ["domainId"], "category_id": ["categoryId"], "model_id": ["modelId"],
    }, shard=True))

    # ---- 6. 业务关系
    jobs.append(relation("service_handled_by", "关系-主管部门", "relations", PILOT_DIR / "service_handled_by.csv",
                         "GovernmentService", "handledBy", "Department",
                         {"service_id": ["start_id"], "department_id": ["end_id"], "department_role": ["departmentRole"]}))
    jobs.append(relation("service_collaborates_with", "关系-协同部门", "relations", PILOT_DIR / "service_collaborates_with.csv",
                         "GovernmentService", "collaboratesWith", "Department",
                         {"service_id": ["start_id"], "department_id": ["end_id"], "participates_in_step": ["participatesInStep"]}))
    jobs.append(relation("service_requires_material", "关系-需要材料", "relations", SHARED_DIR / "service_requires_material_out.csv",
                         "GovernmentService", "requiresMaterial", "Material",
                         {"service_id": ["start_id"], "material_id": ["end_id"], "required": ["required"],
                          "order_no": ["orderNo"], "material_description": ["materialDescription"],
                          "acceptance_standard": ["acceptanceStandard"], "material_type": ["materialType"],
                          "source_type": ["sourceType"], "submission_format": ["submissionFormat"]}, shard=True))
    jobs.append(relation("service_has_condition", "关系-办理条件", "relations", PILOT_DIR / "service_has_condition.csv",
                         "GovernmentService", "hasCondition", "ServiceCondition",
                         {"service_id": ["start_id"], "condition_id": ["end_id"], "condition_source": ["conditionSource"]}, shard=True))
    jobs.append(relation("service_has_process_step", "关系-办理步骤", "relations", PILOT_DIR / "service_has_process_step.csv",
                         "GovernmentService", "hasProcessStep", "ProcessStep",
                         {"service_id": ["start_id"], "process_step_id": ["end_id"], "order_no": ["orderNo"]}, shard=True))
    jobs.append(relation("process_step_next", "关系-下一步骤", "relations", PILOT_DIR / "process_step_next.csv",
                         "ProcessStep", "nextStep", "ProcessStep",
                         {"service_id": [], "from_process_step_id": ["start_id"], "to_process_step_id": ["end_id"]}, shard=True))
    jobs.append(relation("service_produces_result", "关系-办理结果", "relations", PILOT_DIR / "service_produces_result.csv",
                         "GovernmentService", "producesResult", "ServiceResult",
                         {"service_id": ["start_id"], "result_id": ["end_id"], "order_no": ["orderNo"]}))
    jobs.append(relation("service_based_on", "关系-引用条款", "relations", SHARED_DIR / "service_based_on_out.csv",
                         "GovernmentService", "citesLegal", "LegalCitation",
                         {"service_id": ["start_id"], "legal_citation_id": ["end_id"],
                          "order_no": ["orderNo"], "basis_source": ["basisSource"],
                          "source_legal_basis_id": []}, shard=True))
    jobs.append(relation("part_of", "关系-条款属法规", "relations", SHARED_DIR / "part_of.csv",
                         "LegalCitation", "partOf", "LegalBasis",
                         {"legal_citation_id": ["start_id"], "legal_basis_id": ["end_id"]}))
    jobs.append(relation("service_has_faq", "关系-问答", "relations", PILOT_DIR / "service_has_faq.csv",
                         "GovernmentService", "hasFaq", "FAQ",
                         {"service_id": ["start_id"], "faq_id": ["end_id"], "order_no": ["orderNo"]}))
    jobs.append(relation("service_has_channel", "关系-渠道", "relations", PILOT_DIR / "service_has_channel.csv",
                         "GovernmentService", "hasChannel", "ServiceChannel",
                         {"service_id": ["start_id"], "channel_id": ["end_id"]}, shard=True))
    jobs.append(relation("service_has_fee", "关系-收费", "relations", PILOT_DIR / "service_has_fee.csv",
                         "GovernmentService", "hasFee", "Fee",
                         {"service_id": ["start_id"], "fee_id": ["end_id"]}, shard=True))

    # ---- 7. 事项路由关系
    jobs.append(relation("service_belongs_to_domain", "关系-事项属领域", "service_routing", ROUTING_DIR / "service_belongs_to_domain.csv",
                         "GovernmentService", "belongsToDomain", "ServiceDomain",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}, shard=True))
    jobs.append(relation("service_classified_as", "关系-事项分类", "service_routing", ROUTING_DIR / "service_classified_as.csv",
                         "GovernmentService", "classifiedAs", "ServiceCategory",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}, shard=True))
    jobs.append(relation("service_uses_model", "关系-事项用模型", "service_routing", ROUTING_DIR / "service_uses_model.csv",
                         "GovernmentService", "usesModel", "KnowledgeModel",
                         {"start_id": ["start_id"], "end_id": ["end_id"]}, shard=True))

    return jobs


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        row = next(csv.reader(fh), None)
    if not row:
        raise SystemExit(f"CSV 无表头: {path}")
    return [item.strip() for item in row]


def validate(jobs: list[dict]) -> None:
    keys = [job["key"] for job in jobs]
    if len(set(keys)) != len(keys):
        raise SystemExit("manifest job key 重复")
    problems: list[str] = []
    for job in jobs:
        path = ROOT / job["file"]
        if not path.is_file():
            problems.append(f"{job['key']}: 文件不存在 {path}")
            continue
        header = read_header(path)
        mapping_keys = set(job["mapping"])
        header_set = set(header)
        # services 的路由三列由分片器追加，源文件不含
        expected_extra = {"domain_id", "category_id", "model_id"} if job["key"] == "services" else set()
        missing = mapping_keys - header_set - expected_extra
        extra = header_set - mapping_keys
        if missing:
            problems.append(f"{job['key']}: mapping 列不在 CSV: {sorted(missing)}")
        if extra:
            problems.append(f"{job['key']}: CSV 列未映射: {sorted(extra)}")
        targets = [t for values in job["mapping"].values() for t in values]
        for pk in ("id", "start_id", "end_id"):
            if targets.count(pk) > 1:
                problems.append(f"{job['key']}: {pk} 重复映射")
        size_mib = path.stat().st_size / (1024 * 1024)
        if size_mib > SHARD_THRESHOLD_MIB and not job.get("shard"):
            problems.append(f"{job['key']}: {size_mib:.0f}MiB 未启用分片")
    if problems:
        raise SystemExit("manifest 校验失败:\n  " + "\n  ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()

    jobs = build_jobs()
    validate(jobs)
    manifest = {
        "base_url": BASE_URL,
        "project_id": PROJECT_ID,
        "namespace": NAMESPACE,
        "create_user": CREATE_USER,
        "shard_target_mib": 128,
        "shard_threshold_mib": SHARD_THRESHOLD_MIB,
        "jobs": jobs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = 0
    print(f"manifest: {args.output}")
    print(f"{'#':>3} {'key':<28} {'group':<15} {'MiB':>8} shard")
    for index, job in enumerate(jobs, 1):
        path = ROOT / job["file"]
        size_mib = path.stat().st_size / (1024 * 1024)
        total += size_mib
        print(f"{index:>3} {job['key']:<28} {job['group']:<15} {size_mib:>8.1f} {'yes' if job.get('shard') else ''}")
    print(f"共 {len(jobs)} 个任务，源数据 {total:.0f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
