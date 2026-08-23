#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 Neo4j HTTP transactional endpoint 执行 cypher（无需装驱动）。

用法:
  python3 kg/pilot/neo4j_query.py "MATCH (n) RETURN count(n)"
  python3 kg/pilot/neo4j_query.py -f queries.cypher   # 逐语句执行
"""
import base64
import json
import sys
import urllib.request

URL = "http://127.0.0.1:7474/db/govaffair/tx/commit"
AUTH = base64.b64encode(b"neo4j:neo4j@openspg").decode()


def run(cypher):
    body = json.dumps({"statements": [{"statement": cypher}]}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + AUTH,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        return {"error": out["errors"]}
    res = out["results"][0]
    cols = res["columns"]
    return [dict(zip(cols, row["row"])) for row in res["data"]]


if __name__ == "__main__":
    if sys.argv[1] == "-f":
        stmts = [s.strip() for s in open(sys.argv[2]).read().split(";") if s.strip()]
    else:
        stmts = [sys.argv[1]]
    for s in stmts:
        print("##", s[:120])
        print(json.dumps(run(s), ensure_ascii=False, indent=2))
