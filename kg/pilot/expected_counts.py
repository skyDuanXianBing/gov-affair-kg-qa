#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 kg/pilot/csv 精确计算建图后对账期望值（节点数、各类边数）。"""
import csv
import json
import os

D = os.path.join(os.path.dirname(__file__), "csv")


def rows(name):
    with open(os.path.join(D, name + ".csv"), encoding="utf-8", newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]


def count_multi(data, idx):
    """语义属性单元格（逗号分隔）展开后的边数。"""
    return sum(1 for row in data for v in row[idx].split(",") if v)


expect = {"nodes": {}, "edges": {}}

for t in ["Affair", "ImplementingOrg", "Material", "LegalBasis",
          "ProcessStep", "ResultDocument", "CrossRegionHandling"]:
    expect["nodes"][t] = len(rows(t)[1])

# 概念节点：CSV id + 层级父节点（"-"路径的祖先）
concept_nodes = set()
for ct in ["AffairType", "ServiceTarget", "ExerciseLevel", "ThemeCategory"]:
    for (cid,) in rows(ct)[1]:
        parts = cid.split("-")
        for i in range(1, len(parts) + 1):
            concept_nodes.add((ct, "-".join(parts[:i])))
isa_edges = 0
for ct, cid in concept_nodes:
    if "-" in cid:
        isa_edges += 1
expect["nodes"]["Concept(合计)"] = len(concept_nodes)
expect["edges"]["isA"] = isa_edges

# 语义属性边（Affair 表展开）
h, aff = rows("Affair")
for col, edge in [("implementedBy", "implementedBy"),
                  ("hasStep", "hasStep"),
                  ("produceResult", "produceResult"),
                  ("supportCrossRegion", "supportCrossRegion"),
                  ("affairType", "affairType"),
                  ("theme", "theme")]:
    expect["edges"][edge] = count_multi(aff, h.index(col))
for col, edge in [("serviceTarget", "serviceTarget"),
                  ("exerciseLevel", "exerciseLevel")]:
    expect["edges"][edge] = count_multi(aff, h.index(col))

# 关系表边
for f, e in [("Affair_requireMaterial_Material", "requireMaterial"),
             ("Affair_hasLegalBasis_LegalBasis", "hasLegalBasis"),
             ("ProcessStep_nextStep_ProcessStep", "nextStep")]:
    expect["edges"][e] = len(rows(f)[1])

expect["nodes"]["TOTAL(实体+概念)"] = (
    sum(v for k, v in expect["nodes"].items() if k != "TOTAL(实体+概念)"))
expect["edges"]["TOTAL"] = sum(expect["edges"].values())

print(json.dumps(expect, ensure_ascii=False, indent=2))
