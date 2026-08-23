# -*- coding: utf-8 -*-
"""只读探针：Neo4j 图结构 + OpenSPG SearchClient 文本检索（solver 将使用的同一路径）"""
import json
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", "neo4j@openspg"))
with driver.session(database="govaffair") as s:
    print("== labels ==")
    for r in s.run("CALL db.labels()"):
        print(r[0])
    print("== node count ==")
    print(s.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    print("== Affair samples matching 居住证/营业执照 ==")
    for kw in ["居住证", "营业执照"]:
        recs = s.run(
            "MATCH (n:`GovAffair.Affair`) WHERE n.name CONTAINS $kw RETURN n.id AS id, n.name AS name LIMIT 5",
            kw=kw,
        ).data()
        print(kw, "->", json.dumps(recs, ensure_ascii=False))
    print("== chunk-ish labels? ==")
    print(s.run("MATCH (n) WHERE any(l IN labels(n) WHERE l CONTAINS 'Chunk') RETURN count(n) AS c").single()["c"])
    print("== vector props present? ==")
    print(s.run("MATCH (n) WHERE n._name_vector IS NOT NULL RETURN count(n) AS c").single()["c"])
driver.close()
