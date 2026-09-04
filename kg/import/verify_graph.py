#!/usr/bin/env python3
"""pilot 骨架层对账：Neo4j 实际数 vs 源 CSV 期望数 + 多跳链抽查。

节点期望 = 源表去重主键数；边期望 = 源表 distinct (start, end) 对数
（SPG (s,p,o) UPSERT 语义：同三元组多次出现合并为一条边，且同名边属性取
后写覆盖；带属性边在同 (s,o) 多次出现不同属性值时也只保留一条边）。

大表 distinct 用 sqlite 落盘计算，避免内存爆炸。

输出：kg/import/reports/pilot_reconciliation.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_FILE = ROOT / "kg" / "import" / "reports" / "pilot_reconciliation.md"
ROUTING_DIR = ROOT / "kg" / "import" / "routing_metadata"
SHARED_DIR = ROOT / "build" / "shared_ids" / "pilot"
PILOT_DIR = ROOT / "data" / "pilot"

NEO4J_CONTAINER = "release-openspg-neo4j"
NEO4J_AUTH = ("neo4j", "neo4j@openspg")
NEO4J_DB = "zwdmxgj"
NS = "ZwdmxGJ"


def cypher(query: str) -> list[tuple]:
    """docker exec cypher-shell 执行查询，返回行元组列表。"""
    args = [
        "docker", "exec", NEO4J_CONTAINER, "cypher-shell",
        "-u", NEO4J_AUTH[0], "-p", NEO4J_AUTH[1], "-d", NEO4J_DB,
        "--format", "plain", query,
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"cypher 失败: {result.stderr[:500]}")
    rows: list[tuple] = []
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) >= 2:
        for line in lines[1:]:  # 首行是列名头
            rows.append(tuple(part.strip().strip('"') for part in _split_plain(line)))
    return rows


def _split_plain(line: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def distinct_pairs(path: Path, start_col: str, end_col: str) -> int:
    """sqlite 落盘统计 distinct (start, end)。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pairs.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("CREATE TABLE p(s TEXT, o TEXT, PRIMARY KEY(s,o)) WITHOUT ROWID")
        batch: list[tuple[str, str]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                start = (row.get(start_col) or "").strip()
                end = (row.get(end_col) or "").strip()
                if start and end:
                    batch.append((start, end))
                if len(batch) >= 100_000:
                    conn.executemany("INSERT OR IGNORE INTO p VALUES(?,?)", batch)
                    batch.clear()
        if batch:
            conn.executemany("INSERT OR IGNORE INTO p VALUES(?,?)", batch)
        count = conn.execute("SELECT count(*) FROM p").fetchone()[0]
        conn.close()
        return int(count)


def distinct_ids(path: Path, id_col: str) -> int:
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            value = (row.get(id_col) or "").strip()
            if value:
                seen.add(value)
    return len(seen)


def row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


# --------------------------------------------------------------------------
# 期望与实际定义
# --------------------------------------------------------------------------

def node_checks() -> list[dict]:
    shared = SHARED_DIR
    pilot = PILOT_DIR
    routing = ROUTING_DIR
    return [
        {"type": "ServiceDomain", "label": f"{NS}.ServiceDomain", "expect": 1, "source": "routing_metadata/service_domains.csv"},
        {"type": "CategoryScheme", "label": f"{NS}.CategoryScheme", "expect": 1, "source": "routing_metadata/category_schemes.csv"},
        {"type": "ServiceCategory", "label": f"{NS}.ServiceCategory", "expect": 50, "source": "routing_metadata/service_categories.csv"},
        {"type": "KnowledgeModel", "label": f"{NS}.KnowledgeModel", "expect": 50, "source": "routing_metadata/knowledge_models.csv"},
        {"type": "Department", "label": f"{NS}.Department", "expect_kind": "ids", "path": pilot / "departments.csv", "id_col": "department_id"},
        {"type": "Material", "label": f"{NS}.Material", "expect": 30111, "source": "build/shared_ids stats.material_shared_nodes"},
        {"type": "LegalBasis", "label": f"{NS}.LegalBasis", "expect": 2758, "source": "build/shared_ids stats.legal_bases"},
        {"type": "LegalCitation", "label": f"{NS}.LegalCitation", "expect": 9394, "source": "build/shared_ids stats.legal_citations"},
        {"type": "ServiceCondition", "label": f"{NS}.ServiceCondition", "expect_kind": "ids", "path": pilot / "conditions.csv", "id_col": "condition_id"},
        {"type": "ProcessStep", "label": f"{NS}.ProcessStep", "expect_kind": "ids", "path": pilot / "process_steps.csv", "id_col": "process_step_id"},
        {"type": "ServiceResult", "label": f"{NS}.ServiceResult", "expect_kind": "ids", "path": pilot / "results.csv", "id_col": "result_id"},
        {"type": "FAQ", "label": f"{NS}.FAQ", "expect_kind": "ids", "path": pilot / "faqs.csv", "id_col": "faq_id"},
        {"type": "ServiceChannel", "label": f"{NS}.ServiceChannel", "expect_kind": "ids", "path": pilot / "service_channels.csv", "id_col": "channel_id"},
        {"type": "Fee", "label": f"{NS}.Fee", "expect_kind": "ids", "path": pilot / "fees.csv", "id_col": "fee_id"},
        {"type": "GovernmentService", "label": f"{NS}.GovernmentService", "expect_kind": "ids", "path": pilot / "services.csv", "id_col": "service_id"},
    ]


def edge_checks() -> list[dict]:
    shared = SHARED_DIR
    pilot = PILOT_DIR
    routing = ROUTING_DIR
    return [
        {"edge": "partOf", "path": shared / "part_of.csv", "s": "legal_citation_id", "o": "legal_basis_id", "src_rows": 9394},
        {"edge": "handledBy", "path": pilot / "service_handled_by.csv", "s": "service_id", "o": "department_id", "src_rows": None},
        {"edge": "collaboratesWith", "path": pilot / "service_collaborates_with.csv", "s": "service_id", "o": "department_id", "src_rows": None},
        {"edge": "requiresMaterial", "path": shared / "service_requires_material_out.csv", "s": "service_id", "o": "material_id", "src_rows": 2153892},
        {"edge": "hasCondition", "path": pilot / "service_has_condition.csv", "s": "service_id", "o": "condition_id", "src_rows": None},
        {"edge": "hasProcessStep", "path": pilot / "service_has_process_step.csv", "s": "service_id", "o": "process_step_id", "src_rows": 1556046},
        {"edge": "nextStep", "path": pilot / "process_step_next.csv", "s": "from_process_step_id", "o": "to_process_step_id", "src_rows": None},
        {"edge": "producesResult", "path": pilot / "service_produces_result.csv", "s": "service_id", "o": "result_id", "src_rows": None},
        {"edge": "citesLegal", "path": shared / "service_based_on_out.csv", "s": "service_id", "o": "legal_citation_id", "src_rows": 6305710},
        {"edge": "hasFaq", "path": pilot / "service_has_faq.csv", "s": "service_id", "o": "faq_id", "src_rows": None},
        {"edge": "hasChannel", "path": pilot / "service_has_channel.csv", "s": "service_id", "o": "channel_id", "src_rows": None},
        {"edge": "hasFee", "path": pilot / "service_has_fee.csv", "s": "service_id", "o": "fee_id", "src_rows": None},
        {"edge": "belongsToDomain", "path": routing / "service_belongs_to_domain.csv", "s": "start_id", "o": "end_id", "src_rows": 481501},
        {"edge": "classifiedAs", "path": routing / "service_classified_as.csv", "s": "start_id", "o": "end_id", "src_rows": 481501},
        {"edge": "usesModel", "path": routing / "service_uses_model.csv", "s": "start_id", "o": "end_id", "src_rows": 481501},
    ]


SAMPLE_QUERY = f"""
MATCH (s:`{NS}.GovernmentService`)-[:handledBy]->(d:`{NS}.Department`)
MATCH (s)-[:requiresMaterial]->(m:`{NS}.Material`)
MATCH (s)-[:citesLegal]->(lc:`{NS}.LegalCitation`)-[:partOf]->(lb:`{NS}.LegalBasis`)
RETURN s.serviceId AS service, d.name AS dept, m.name AS material, lc.name AS citation, lb.name AS law
LIMIT 5
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-samples", action="store_true")
    args = parser.parse_args()

    lines: list[str] = ["# pilot 骨架层对账报告", ""]
    lines.append(f"- Neo4j 库: {NEO4J_DB}（docker {NEO4J_CONTAINER}）")
    lines.append(f"- 节点期望 = 源表去重主键数；边期望 = 源表 distinct (start,end) 对数（SPG (s,p,o) UPSERT 去重）")
    lines.append("")

    # 节点对账
    lines.append("## 一、节点对账")
    lines.append("")
    lines.append("| 实体 | Neo4j 实际 | 期望 | 差异 | 说明 |")
    lines.append("|---|---:|---:|---:|---|")
    node_mismatch = 0
    for check in node_checks():
        actual = int(cypher(f"MATCH (n:`{check['label']}`) RETURN count(n)")[0][0])
        if check.get("expect_kind") == "ids":
            expect = distinct_ids(check["path"], check["id_col"])
            source = f"{check['path'].relative_to(ROOT)} distinct {check['id_col']}"
        else:
            expect = int(check["expect"])
            source = check["source"]
        diff = actual - expect
        mark = "" if diff == 0 else " ⚠️"
        if diff != 0:
            node_mismatch += 1
        lines.append(f"| {check['type']} | {actual:,} | {expect:,} | {diff:+,} | {source}{mark} |")
    lines.append("")

    # 边对账
    lines.append("## 二、边对账")
    lines.append("")
    lines.append("| 关系 | Neo4j 实际 | 期望 distinct(s,o) | 源行数 | 去重率 | 说明 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    edge_mismatch = 0
    edge_stats: dict[str, dict] = {}
    for check in edge_checks():
        actual = int(cypher(f"MATCH ()-[r:`{check['edge']}`]->() RETURN count(r)")[0][0])
        expect = distinct_pairs(check["path"], check["s"], check["o"])
        if check["src_rows"]:
            src_rows = check["src_rows"]
        else:
            src_rows = row_count(check["path"])
        dedup = f"{(1 - expect / src_rows) * 100:.2f}%" if src_rows else "-"
        mark = "" if actual == expect else " ⚠️"
        if actual != expect:
            edge_mismatch += 1
        lines.append(
            f"| {check['edge']} | {actual:,} | {expect:,} | {src_rows:,} | {dedup} | "
            f"{check['path'].relative_to(ROOT)}{mark} |"
        )
        edge_stats[check["edge"]] = {"actual": actual, "expect": expect, "src_rows": src_rows}
    lines.append("")

    # 抽查
    lines.append("## 三、多跳链抽查（5 条）")
    lines.append("")
    lines.append("查询：事项→handledBy→部门，事项→requiresMaterial→材料，事项→citesLegal→条款→partOf→法规文件")
    lines.append("")
    if not args.skip_samples:
        lines.append("```cypher")
        lines.append(SAMPLE_QUERY.strip())
        lines.append("```")
        lines.append("")
        samples = cypher(SAMPLE_QUERY)
        lines.append("| # | serviceId | 部门 | 材料 | 引用条款 | 法规文件 |")
        lines.append("|---|---|---|---|---|---|")
        for index, row in enumerate(samples[:5], 1):
            cells = [str(item).replace("|", "\\|")[:40] for item in row]
            lines.append(f"| {index} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## 四、结论")
    lines.append("")
    lines.append(f"- 节点差异实体数: {node_mismatch}")
    lines.append(f"- 边差异关系数: {edge_mismatch}")
    lines.append("")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告: {REPORT_FILE}")
    print(f"节点差异实体数={node_mismatch} 边差异关系数={edge_mismatch}")
    return 0 if node_mismatch == 0 and edge_mismatch == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
