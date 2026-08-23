from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import chunk_openspg_documents as chunker
from scripts import enrich_openspg_services as enrich
from scripts import generate_kg_model_metadata as model_metadata
from scripts import import_openspg_csvs as importer
from scripts import shard_openspg_csvs as sharder


class ImportPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {"project_id": 1, "namespace": "ZwdmxGJ", "create_user": "openspg"}
        self.entity_target = {
            "kind": "entity",
            "name": "GovernmentService",
            "qualified_name": "ZwdmxGJ.GovernmentService",
            "name_zh": "政务事项",
            "id": 123,
            "properties": {"id", "name", "serviceId", "datasetScope"},
        }
        self.relation_target = {
            "kind": "relation",
            "name": "handledBy",
            "name_zh": "主管部门",
            "id": 456,
            "source_type": "ZwdmxGJ.GovernmentService",
            "target_type": "ZwdmxGJ.Department",
            "properties": {"departmentRole"},
        }

    def test_entity_mapping_supports_business_id_and_open_spg_id(self) -> None:
        job = {
            "key": "services",
            "mapping": {
                "service_id": ["id", "serviceId"],
                "service_name": ["name"],
                "dataset_scope": ["datasetScope"],
            },
            "schema_target": {"kind": "entity", "type": "GovernmentService"},
        }
        columns = list(job["mapping"])
        importer.validate_job(job, columns)
        importer.validate_schema_mapping(job, self.entity_target)
        payload = importer.build_payload(
            self.manifest,
            job,
            None,
            self.entity_target,
            columns,
            "minio://test.csv",
            mode="clone",
            name_suffix="-part-00001",
            file_name="test.csv",
        )
        extension = json.loads(payload["extension"])
        self.assertEqual(payload["projectId"], 1)
        self.assertEqual(extension["mappingConfig"]["mappingType"], "entityMapping")
        self.assertEqual(extension["mappingConfig"]["filter"][0]["s"], "ZwdmxGJ.GovernmentService")
        self.assertEqual(extension["mappingConfig"]["config"][0]["mapping"], job["mapping"])

    def test_relation_mapping_contains_semantic_endpoints(self) -> None:
        job = {
            "key": "service_handled_by",
            "mapping": {
                "service_id": ["start_id"],
                "department_id": ["end_id"],
                "department_role": ["departmentRole"],
            },
            "schema_target": {"kind": "relation"},
        }
        columns = list(job["mapping"])
        importer.validate_job(job, columns)
        importer.validate_schema_mapping(job, self.relation_target)
        payload = importer.build_payload(
            self.manifest,
            job,
            None,
            self.relation_target,
            columns,
            "minio://test.csv",
            mode="clone",
            name_suffix="",
            file_name="test.csv",
        )
        extension = json.loads(payload["extension"])
        config = extension["mappingConfig"]
        self.assertEqual(config["mappingType"], "relationMapping")
        self.assertEqual(config["filter"][0]["p"], "handledBy")
        self.assertEqual(config["filter"][0]["s"], "ZwdmxGJ.GovernmentService")
        self.assertEqual(config["filter"][0]["o"], "ZwdmxGJ.Department")

    def test_unknown_schema_target_is_rejected(self) -> None:
        with self.assertRaises(importer.ImportErrorWithContext):
            importer.resolve_schema_target(
                {"key": "bad", "schema_target": {"kind": "entity", "type": "Missing"}},
                self.manifest,
                {"entities": {}, "relations": {}},
            )

    def test_chunk_job_requires_retrieval_index(self) -> None:
        chunk_job = {
            "key": "documents_chunks",
            "schema_target": {"kind": "entity", "type": "Chunk"},
        }
        with self.assertRaisesRegex(importer.ImportErrorWithContext, "必须配置 retrievals"):
            importer.validate_retrieval_config([chunk_job])

    def test_chunk_payload_serializes_retrieval_ids(self) -> None:
        chunk_job = {
            "key": "documents_chunks",
            "name": "文档分片",
            "file": "documents_chunks.csv",
            "mapping": {"content": ["content"]},
            "retrievals": [1, "1"],
            "schema_target": {"kind": "entity", "type": "Chunk"},
        }
        chunk_target = {
            "kind": "entity",
            "name": "Chunk",
            "qualified_name": "ZwdmxGJ.Chunk",
            "name_zh": "文档分片",
            "id": 789,
            "properties": {"content"},
        }
        importer.validate_retrieval_config([chunk_job])
        payload = importer.build_payload(
            self.manifest,
            chunk_job,
            None,
            chunk_target,
            ["content"],
            "minio://test.csv",
            mode="clone",
            name_suffix="",
            file_name="test.csv",
        )
        self.assertEqual(payload["retrievals"], "[1]")


class RecordingProgress(importer.NullProgress):
    def __init__(self) -> None:
        self.updates: list[int | float] = []
        self.postfixes: list[str] = []

    def update(self, amount: int | float = 1) -> None:
        self.updates.append(amount)

    def set_postfix_str(self, value: str, refresh: bool = True) -> None:
        self.postfixes.append(value)


class ImportProgressTests(unittest.TestCase):
    def test_disabled_progress_does_not_require_tqdm(self) -> None:
        with patch.object(importer, "tqdm", None):
            progress = importer.create_progress(enabled=False, total=10)
            self.assertIsInstance(progress, importer.NullProgress)

    def test_enabled_progress_requires_tqdm_dependency(self) -> None:
        with patch.object(importer, "tqdm", None):
            with self.assertRaisesRegex(importer.ImportErrorWithContext, "requirements-import.txt"):
                importer.create_progress(enabled=True, total=10)

    def test_multipart_progress_counts_only_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "upload.csv"
            content = b"id,name\n1,test\n2,demo\n"
            source.write_bytes(content)
            progress = RecordingProgress()
            body = b"".join(
                importer.multipart_chunks(
                    b"prefix",
                    source,
                    b"suffix",
                    chunk_size=5,
                    progress=progress,
                )
            )
            self.assertEqual(body, b"prefix" + content + b"suffix")
            self.assertEqual(sum(progress.updates), len(content))

    def test_sha256_progress_reaches_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.csv"
            content = b"id,name\n1,alpha\n"
            source.write_bytes(content)
            progress = RecordingProgress()
            with patch.object(importer, "create_progress", return_value=progress):
                digest = importer.sha256sum(source, progress_enabled=True)
            self.assertEqual(
                digest,
                hashlib.sha256(content).hexdigest(),
            )
            self.assertEqual(sum(progress.updates), len(content))

    def test_full_dry_run_works_without_tqdm_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "departments.part-00001.csv"
            source.write_text(
                "department_id,department_name\nd1,测试部门\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "project_id": 1,
                        "namespace": "ZwdmxGJ",
                        "jobs": [
                            {
                                "key": "departments",
                                "name": "部门",
                                "group": "entities",
                                "file": str(source),
                                "mapping": {
                                    "department_id": ["id", "departmentId"],
                                    "department_name": ["name"],
                                },
                                "schema_target": {"kind": "entity", "type": "Department"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            shard_manifest = root / "shards.json"
            shard_manifest.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "key": "departments",
                                "parts": [
                                    {
                                        "part_file": str(source),
                                        "part_no": 1,
                                        "row_count": 1,
                                        "bytes": source.stat().st_size,
                                        "sha256": digest,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            preview_dir = root / "preview"
            state_file = root / "state.json"
            schema_catalog = {
                "entities": {
                    "Department": {
                        "kind": "entity",
                        "name": "Department",
                        "qualified_name": "ZwdmxGJ.Department",
                        "name_zh": "部门",
                        "id": 1,
                        "properties": {"id", "departmentId", "name"},
                    }
                },
                "relations": {},
            }
            argv = [
                "import_openspg_csvs.py",
                "--manifest",
                str(manifest),
                "--shard-manifest",
                str(shard_manifest),
                "--base-url",
                "http://127.0.0.1:8887",
                "--all",
                "--no-progress",
                "--preview-dir",
                str(preview_dir),
                "--state-file",
                str(state_file),
            ]
            with (
                patch("sys.argv", argv),
                patch.object(importer, "tqdm", None),
                patch.object(importer, "load_schema_catalog", return_value=schema_catalog),
            ):
                self.assertEqual(importer.main(), 0)
            report = json.loads((preview_dir / "import-report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["execute"])
            self.assertEqual(report["jobs"][0]["parts"][0]["status"], "DRY_RUN")


class KnowledgeModelMetadataTests(unittest.TestCase):
    def test_generates_domain_category_models_and_service_relations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot_services = root / "pilot-services.csv"
            personal_services = root / "personal-services.csv"
            pilot_services.write_text(
                "service_id,service_name,category_l1,category_l2\n"
                "p1,事项一,法人服务,财务税务\n"
                "p2,事项二,法人服务,农林牧渔\n",
                encoding="utf-8",
            )
            personal_services.write_text(
                "service_id,service_name,category_l1,category_l2\n"
                "u1,事项三,个人事务,行政许可\n",
                encoding="utf-8",
            )
            config = {
                "pilot": {
                    "domain_id": "domain:corporate",
                    "domain_name": "法人服务",
                    "scheme_id": "scheme:corporate_subject",
                    "scheme_name": "法人服务主题分类体系",
                    "services": pilot_services,
                    "documents": root / "pilot-documents.csv",
                },
                "personal": {
                    "domain_id": "domain:personal",
                    "domain_name": "个人事务",
                    "scheme_id": "scheme:personal_service_type",
                    "scheme_name": "个人事务事项类型分类体系",
                    "services": personal_services,
                    "documents": root / "personal-documents.csv",
                },
            }
            output = root / "output"
            with patch.object(model_metadata, "DATASET_CONFIG", config):
                categories = model_metadata.collect_categories()
                common = model_metadata.build_common_rows(categories)
                model_metadata.enrich_services("pilot", output)
                model_metadata.write_dataset_relations("pilot", output)

            self.assertEqual(len(common["service_domains"]), 2)
            self.assertEqual(len(common["service_categories"]), 5)
            self.assertEqual(len(common["knowledge_models"]), 5)
            category_models = [
                row for row in common["knowledge_models"]
                if row["model_type"] == "CATEGORY_PROFILE"
            ]
            self.assertEqual(len(category_models), 3)
            for row in category_models:
                self.assertIn("category_id", json.loads(row["retrieval_filter"]))
                self.assertIn("GovernmentService", json.loads(row["enabled_entity_types"]))

            with (output / "pilot" / "services.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                services = list(csv.DictReader(handle))
            self.assertEqual(services[0]["domain_id"], "domain:corporate")
            self.assertEqual(services[0]["category_id"], "category:corporate:财务税务")
            self.assertEqual(services[0]["model_id"], "model:corporate:财务税务")

            with (output / "pilot" / "service_classified_as.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                relations = list(csv.DictReader(handle))
            self.assertEqual(len(relations), 2)
            self.assertEqual(relations[1]["end_id"], "category:corporate:农林牧渔")

    def test_chunk_rows_include_model_routing_fields(self) -> None:
        rows = list(
            chunker.chunk_rows(
                [
                    {
                        "doc_id": "doc-1",
                        "title": "事项",
                        "content": "测试内容",
                        "category_l1": "个人事务",
                        "category_l2": "行政许可",
                    }
                ],
                max_chars=2000,
                overlap_chars=200,
            )
        )
        self.assertEqual(rows[0]["domain_id"], "domain:personal")
        self.assertEqual(rows[0]["category_id"], "category:personal:行政许可")
        self.assertEqual(rows[0]["model_id"], "model:personal:行政许可")

    def test_manifests_keep_metadata_before_services_and_model_relations_before_business_relations(self) -> None:
        for manifest_path in (
            Path("schema/openspg_import_manifest.json"),
            Path("schema/openspg_personal_import_manifest.json"),
        ):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            keys = [job["key"] for job in manifest["jobs"]]
            self.assertLess(keys.index("knowledge_models"), keys.index("services"))
            self.assertLess(keys.index("service_uses_model"), keys.index("service_handled_by"))
            self.assertEqual(manifest["namespace"], "ZwdmxGJ")
            self.assertEqual(manifest["version"], 3)


class EnrichmentTests(unittest.TestCase):
    def test_personal_enrichment_preserves_rows_and_extracts_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            services = root / "services.csv"
            documents = root / "documents.csv"
            output = root / "prepared.csv"
            services.write_text("service_id,service_name\ns1,事项一\n", encoding="utf-8")
            with documents.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["service_id", "extras_json"])
                writer.writeheader()
                writer.writerow(
                    {
                        "service_id": "s1",
                        "extras_json": json.dumps(
                            {"service_object": "自然人", "official_list_count": 2},
                            ensure_ascii=False,
                        ),
                    }
                )
            stats = enrich.enrich_personal(services, documents, output)
            self.assertEqual(stats["rows"], 1)
            with output.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["dataset_scope"], "personal")
            self.assertEqual(row["service_object"], "自然人")
            self.assertEqual(row["official_list_count"], "2")

    def test_personal_enrichment_rejects_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "services.csv").write_text("service_id\ns1\n", encoding="utf-8")
            (root / "documents.csv").write_text(
                "service_id,extras_json\ns2,{}\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                enrich.enrich_personal(root / "services.csv", root / "documents.csv", root / "out.csv")


class ShardTests(unittest.TestCase):
    def test_csv_shards_keep_header_and_record_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            source.write_text("id,text\n1,hello\n2,世界\n3,\"line1\nline2\"\n", encoding="utf-8")
            shards = list(
                sharder.iter_csv_shards(
                    source,
                    root / "parts",
                    target_bytes=45,
                    min_free_gib=0,
                    overwrite=True,
                )
            )
            self.assertGreater(len(shards), 1)
            rows = []
            for shard in shards:
                with Path(shard.part_file).open(encoding="utf-8-sig", newline="") as handle:
                    rows.extend(list(csv.DictReader(handle)))
            self.assertEqual([row["id"] for row in rows], ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
