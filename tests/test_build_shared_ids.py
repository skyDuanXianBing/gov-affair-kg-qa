from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_shared_ids as bsi


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


MATERIALS_HEADER = [
    "material_id",
    "material_name",
    "material_type",
    "source_type",
    "submission_format",
]
EDGES_HEADER = [
    "service_id",
    "material_id",
    "required",
    "order_no",
    "material_description",
    "acceptance_standard",
]
LEGAL_HEADER = [
    "legal_basis_id",
    "law_name",
    "article",
    "document_number",
    "clause_content",
    "published_date",
    "law_url",
]
BASED_ON_HEADER = ["service_id", "legal_basis_id", "order_no", "basis_source"]


class CanonicalizeTests(unittest.TestCase):
    def test_nfkc_and_whitespace_and_case(self) -> None:
        self.assertEqual(bsi.canonicalize("身份证"), bsi.canonicalize(" 身 份 证 "))
        # 全角字母数字归一为半角，并 casefold。
        self.assertEqual(bsi.canonicalize("ＡＢＣ123"), "abc123")
        self.assertEqual(bsi.canonicalize("道路运输证"), bsi.canonicalize("道路 运输 证"))
        self.assertEqual(bsi.canonicalize(""), "")


class SharedIdTests(unittest.TestCase):
    def test_material_id_is_deterministic_and_hex16(self) -> None:
        first = bsi.material_shared_id("身份证")
        second = bsi.material_shared_id(bsi.canonicalize(" 身份证 "))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^M-[0-9a-f]{16}$")

    def test_different_names_get_different_ids(self) -> None:
        self.assertNotEqual(
            bsi.material_shared_id("身份证"),
            bsi.material_shared_id("行驶证"),
        )

    def test_citation_and_basis_ids(self) -> None:
        law = bsi.canonicalize("高等教育法")
        other_law = bsi.canonicalize("城市房地产管理法")
        doc = bsi.canonicalize("主席令第23号")
        citation = bsi.citation_shared_id(law, doc, "第二十四条")
        self.assertRegex(citation, r"^LC-[0-9a-f]{16}$")
        basis = bsi.basis_shared_id(law, doc)
        self.assertRegex(basis, r"^LB-[0-9a-f]{16}$")
        # 同文号不同条款必须是不同 citation。
        self.assertNotEqual(citation, bsi.citation_shared_id(law, doc, "第二十五条"))
        # 同文号不同法规名不得合并（一个修改决定文号可同时修改多部法规）。
        self.assertNotEqual(citation, bsi.citation_shared_id(other_law, doc, "第二十四条"))
        self.assertNotEqual(basis, bsi.basis_shared_id(other_law, doc))

    def test_citation_display_name(self) -> None:
        self.assertEqual(
            bsi.citation_display_name("高等教育法", "第二十四条"),
            "高等教育法 第二十四条",
        )
        self.assertEqual(bsi.citation_display_name("高等教育法", ""), "高等教育法")


class ProcessMaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "pilot"
        self.out_dir = Path(self.tmp.name) / "out"
        write_csv(
            self.data_dir / "materials.csv",
            MATERIALS_HEADER,
            [
                ["material:a", "营业执照", "证件证书证明", "政府部门核发", "纸质"],
                ["material:b", "营业执照", "证件证书证明", "申请人自备", "电子化"],
                ["material:c", "营 业 执照", "其他", "申请人自备", "纸质/电子化"],
                ["material:d", "行驶证", "证件证书证明", "政府部门核发", "纸质"],
            ],
        )
        write_csv(
            self.data_dir / "service_requires_material.csv",
            EDGES_HEADER,
            [
                ["svc-1", "material:a", "必要", "1", "", ""],
                ["svc-1", "material:b", "容缺后补", "2", "", ""],
                ["svc-2", "material:c", "必要", "1", "", ""],
                ["svc-2", "material:zzz", "必要", "2", "", ""],
            ],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_by_canonical_name_and_join_pair_fields(self) -> None:
        stats = bsi.process_materials(self.data_dir, self.out_dir, None)
        # 3 个旧 id 归一到同一名称（含空白差异），行驶证独立。
        self.assertEqual(stats["material_input_rows"], 4)
        self.assertEqual(stats["material_distinct_old_ids"], 4)
        self.assertEqual(stats["material_shared_nodes"], 2)
        self.assertEqual(stats["material_merge_groups"], 1)
        self.assertAlmostEqual(stats["material_compression_ratio"], 0.5)
        self.assertEqual(
            stats["edge_per_pair_columns"],
            ["material_type", "source_type", "submission_format"],
        )
        self.assertEqual(stats["edge_unmapped"], 1)

        nodes_header, nodes = read_csv(self.out_dir / "materials_out.csv")
        self.assertEqual(
            nodes_header,
            ["material_id", "material_name", "canonical_name", "merged_count"],
        )
        by_name = {row[1]: row for row in nodes}
        self.assertEqual(by_name["营业执照"][3], "3")
        self.assertEqual(by_name["行驶证"][3], "1")

        edges_header, edges = read_csv(
            self.out_dir / "service_requires_material_out.csv"
        )
        self.assertEqual(
            edges_header,
            EDGES_HEADER + ["material_type", "source_type", "submission_format"],
        )
        first = dict(zip(edges_header, edges[0]))
        self.assertRegex(first["material_id"], r"^M-[0-9a-f]{16}$")
        self.assertEqual(first["material_type"], "证件证书证明")
        self.assertEqual(first["submission_format"], "纸质")
        # 同名材料的逐事项字段逐对保留在边上（第二条是电子化）。
        second = dict(zip(edges_header, edges[1]))
        self.assertEqual(edges[0][1], edges[1][1])
        self.assertEqual(second["submission_format"], "电子化")
        self.assertEqual(second["source_type"], "申请人自备")
        # 未映射的引用保留原 id。
        self.assertEqual(edges[3][1], "material:zzz")

    def test_limit_samples_first_rows_only(self) -> None:
        stats = bsi.process_materials(self.data_dir, self.out_dir, 2)
        self.assertEqual(stats["material_input_rows"], 2)
        self.assertEqual(stats["material_shared_nodes"], 1)
        self.assertEqual(stats["edge_input_rows"], 2)
        # 前两行边引用的材料都在前 2 行材料内。
        self.assertEqual(stats["edge_unmapped"], 0)


class ProcessLegalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "pilot"
        self.out_dir = Path(self.tmp.name) / "out"
        write_csv(
            self.data_dir / "legal_bases.csv",
            LEGAL_HEADER,
            [
                ["uuid-1", "高等教育法", "第二十四条", "主席令第23号", "内容A", "2018-12-29", "http://a"],
                ["uuid-2", "高等教育法", "第二十四条", "主席令第23号", "内容A", "2018-12-29", "http://a"],
                ["uuid-3", "高等教育法", "第二十五条", "主席令第23号", "内容B", "2018-12-29", "http://a"],
                ["uuid-4", "高等教育法", "第二十四条", "主席令第23号", "内容C", "2018-12-29", "http://a"],
                # 同名不同文号：不得合并。
                ["uuid-5", "高等教育法", "第二十四条", "主席令第99号", "内容D", "", ""],
                # 文号缺失：退化为法规名称。
                ["uuid-6", "民办教育促进法", "第五条", "", "内容E", "", ""],
                # 同文号不同法规名：不得合并（一个修改决定文号可同时修改多部法规）。
                ["uuid-7", "城市房地产管理法", "第二十四条", "主席令第23号", "内容F", "", ""],
            ],
        )
        write_csv(
            self.data_dir / "service_based_on.csv",
            BASED_ON_HEADER,
            [
                ["svc-1", "uuid-1", "1", "AUDIT_ITEM.LAW"],
                ["svc-1", "uuid-3", "2", "AUDIT_ITEM.LAW"],
                ["svc-2", "uuid-5", "1", "法律依据"],
                ["svc-2", "uuid-missing", "2", "法律依据"],
            ],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_dedup_conflict_and_fallback(self) -> None:
        stats = bsi.process_legal(self.data_dir, self.out_dir, None)
        self.assertEqual(stats["legal_input_rows"], 7)
        self.assertEqual(stats["legal_distinct_old_ids"], 7)
        # 条款级：主席令23号×2条 + 主席令99号×1 + 名称退化×1 + 同文号异名×1 = 5。
        self.assertEqual(stats["legal_citations"], 5)
        # 文号级：(高教法,23号)、(高教法,99号)、(民办教育促进法,退化)、(城房法,23号) = 4。
        self.assertEqual(stats["legal_bases"], 4)
        self.assertEqual(stats["part_of_edges"], 5)
        self.assertEqual(stats["citations_multi_source"], 1)
        # uuid-4 与首见 clause_content 不一致，保留首见并计数。
        self.assertEqual(stats["citation_content_conflicts"], 1)
        self.assertEqual(stats["basis_doc_fallback_rows"], 1)
        self.assertEqual(stats["service_based_on_rows"], 4)
        self.assertEqual(stats["service_based_on_unmapped"], 1)

        cit_header, citations = read_csv(self.out_dir / "legal_citations_out.csv")
        self.assertEqual(
            cit_header,
            [
                "legal_citation_id",
                "name",
                "law_name",
                "article",
                "document_number",
                "clause_content",
                "published_date",
                "law_url",
                "first_source_legal_basis_id",
                "source_id_count",
            ],
        )
        by_source = {row[8]: row for row in citations}
        first = by_source["uuid-1"]
        self.assertRegex(first[0], r"^LC-[0-9a-f]{16}$")
        self.assertEqual(first[5], "内容A")
        # uuid-1/2/4 同键合并，其中 uuid-4 内容冲突保留首见。
        self.assertEqual(first[9], "3")
        # 同名不同文号不得合并。
        self.assertNotEqual(first[0], by_source["uuid-5"][0])
        # 同文号不同法规名不得合并。
        self.assertNotEqual(first[0], by_source["uuid-7"][0])
        # 文号缺失退化为名称的引用：document_number 为空。
        fallback = by_source["uuid-6"]
        self.assertEqual(fallback[4], "")

        basis_header, bases = read_csv(self.out_dir / "legal_bases_out.csv")
        self.assertEqual(
            basis_header,
            [
                "legal_basis_id",
                "law_name",
                "document_number",
                "published_date",
                "law_url",
                "citation_count",
            ],
        )
        basis_by_key = {(row[1], row[2]): row for row in bases}
        self.assertEqual(basis_by_key[("高等教育法", "主席令第23号")][5], "2")
        # 同文号不同法规名是两个 LB。
        self.assertIn(("城市房地产管理法", "主席令第23号"), basis_by_key)

        part_header, part_of = read_csv(self.out_dir / "part_of.csv")
        self.assertEqual(part_header, ["legal_citation_id", "legal_basis_id"])
        part_map = {row[0]: row[1] for row in part_of}
        self.assertEqual(len(part_map), 5)
        self.assertEqual(
            part_map[first[0]],
            bsi.basis_shared_id(
                bsi.canonicalize("高等教育法"), bsi.canonicalize("主席令第23号")
            ),
        )
        # 文号缺失的 citation 归到以法规名称为文档键的 LB（法规名本身也在键中）。
        self.assertEqual(
            part_map[fallback[0]],
            bsi.basis_shared_id(
                bsi.canonicalize("民办教育促进法"), bsi.canonicalize("民办教育促进法")
            ),
        )

        edge_header, edges = read_csv(self.out_dir / "service_based_on_out.csv")
        self.assertEqual(
            edge_header,
            ["service_id", "legal_citation_id", "order_no", "basis_source", "source_legal_basis_id"],
        )
        rewritten = dict(zip(edge_header, edges[0]))
        self.assertEqual(rewritten["legal_citation_id"], first[0])
        self.assertEqual(rewritten["source_legal_basis_id"], "uuid-1")
        # 未映射引用保留原 id，不丢边。
        self.assertEqual(edges[3][1], "uuid-missing")
        self.assertEqual(edges[3][4], "uuid-missing")

    def test_limit_samples_first_rows_only(self) -> None:
        stats = bsi.process_legal(self.data_dir, self.out_dir, 2)
        self.assertEqual(stats["legal_input_rows"], 2)
        self.assertEqual(stats["legal_citations"], 1)
        self.assertEqual(stats["citations_multi_source"], 1)
        self.assertEqual(stats["legal_bases"], 1)


class RunDatasetTests(unittest.TestCase):
    def test_stats_json_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data" / "pilot"
            out_dir = Path(tmp) / "out"
            write_csv(
                data_dir / "materials.csv",
                MATERIALS_HEADER,
                [["material:a", "营业执照", "证件证书证明", "政府部门核发", "纸质"]],
            )
            write_csv(
                data_dir / "service_requires_material.csv",
                EDGES_HEADER,
                [["svc-1", "material:a", "必要", "1", "", ""]],
            )
            args = bsi.parse_args(
                ["--dataset", "pilot", "--part", "materials", "--data-root", str(Path(tmp) / "data"), "--out-root", str(out_dir)]
            )
            stats = bsi.run_dataset("pilot", args)
            saved = json.loads((out_dir / "pilot" / "stats.json").read_text("utf-8"))
            self.assertEqual(saved["dataset"], "pilot")
            self.assertEqual(saved["parts"], ["materials"])
            self.assertEqual(saved["materials"]["material_shared_nodes"], 1)
            self.assertEqual(stats["materials"]["material_shared_nodes"], 1)
            # 分两次跑 --part 时 stats.json 增量合并，不丢已跑部分。
            write_csv(
                data_dir / "legal_bases.csv",
                ["legal_basis_id", "law_name", "article", "document_number", "clause_content", "published_date", "law_url"],
                [["lb-1", "高等教育法", "第一条", "主席令第23号", "内容", "", ""]],
            )
            write_csv(
                data_dir / "service_based_on.csv",
                ["service_id", "legal_basis_id", "order_no", "basis_source"],
                [["svc-1", "lb-1", "1", "AUDIT_ITEM.LAW"]],
            )
            args = bsi.parse_args(
                ["--dataset", "pilot", "--part", "legal", "--data-root", str(Path(tmp) / "data"), "--out-root", str(out_dir)]
            )
            bsi.run_dataset("pilot", args)
            saved = json.loads((out_dir / "pilot" / "stats.json").read_text("utf-8"))
            self.assertEqual(saved["parts"], ["materials", "legal"])
            self.assertEqual(saved["materials"]["material_shared_nodes"], 1)
            self.assertEqual(saved["legal"]["legal_citations"], 1)
            self.assertIsNone(saved["materials"]["limit"])


if __name__ == "__main__":
    unittest.main()
