# -*- coding: utf-8 -*-
"""只读探针：OpenSPG SearchClient.search_text / search_vector（entity linking 依赖的路径）"""
import json
from knext.search.client import SearchClient

sc = SearchClient(host_addr="http://127.0.0.1:8887", project_id=1)
for kw in ["申领居住证", "居住证", "营业执照"]:
    try:
        res = sc.search_text(query_string=kw, label_constraints=["GovAffair.Affair"], topk=5)
        print("TEXT", kw, "->", json.dumps(res, ensure_ascii=False)[:600])
    except Exception as e:
        print("TEXT", kw, "ERROR:", repr(e)[:300])
try:
    res = sc.search_text(query_string="申领居住证", label_constraints=None, topk=5)
    print("TEXT nolabel ->", json.dumps(res, ensure_ascii=False)[:600])
except Exception as e:
    print("TEXT nolabel ERROR:", repr(e)[:300])
