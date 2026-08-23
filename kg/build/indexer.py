#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
试点建图脚本：把 adapter 产出的 CSV 灌入 OpenSPG（纯结构建图，不走向量化）。

链结构（kg/design/kag_notes.md §3）：
  SafeCSVScanner >> SPGTypeMapping/RelationMapping >> KGWriter
  —— 不挂 BatchVectorizer，本阶段不需要向量索引生效。

运行前提：
  1. OpenSPG Server 已启动（默认 http://127.0.0.1:8887）；
  2. 当前目录在 KAG 项目目录内（向上能找到 kag_config.yaml，
     且 project.id 已写入——即 knext project create/restore 之后）；
  3. schema 已 knext schema commit；
  4. --data-dir 指向 adapter 输出目录。

用法：
  cd GovAffair && python <仓库>/kg/build/indexer.py --data-dir <仓库>/kg/pilot/csv
"""

import argparse
import os

import pandas as pd

from kag.builder.component import KGWriter, RelationMapping, SPGTypeMapping
from kag.builder.component.scanner.csv_scanner import CSVScanner
from kag.builder.default_chain import DefaultStructuredBuilderChain
from kag.builder.runner import BuilderChainRunner


class SafeCSVScanner(CSVScanner):
    """CSVScanner 变体：空单元格读作空字符串而不是 float NaN。

    原生 CSVScanner 用 pd.read_csv(dtype=str) 默认把空格解析成 NaN，
    而 SPGTypeMapping 只过滤 falsy/NaT（bool(NaN)=True），会把 NaN 写进图。
    """

    def load_data(self, input, **kwargs):
        input = self.download_data(input)
        data = pd.read_csv(input, dtype=str, delimiter=self.delimiter,
                           keep_default_na=False)
        return data.to_dict(orient="records")


# 节点导入顺序：概念 → 共享实体 → 弱实体 → 核心实体（先点后边）
NODE_TYPES = [
    "AffairType", "ServiceTarget", "ExerciseLevel", "ThemeCategory",
    "ImplementingOrg", "Material", "LegalBasis", "LegalCitation",
    "ProcessStep", "ResultDocument", "CrossRegionHandling",
    "Affair",
]

# (文件名, subject, predicate, object, [边子属性])
RELATIONS = [
    ("Affair_requireMaterial_Material", "Affair", "requireMaterial", "Material",
     ["seq", "copies", "submitForm", "materialType", "materialSource",
      "isRequired", "note"]),
    ("Affair_citeLegal_LegalCitation", "Affair", "citeLegal", "LegalCitation",
     []),
    ("LegalCitation_partOf_LegalBasis", "LegalCitation", "partOf", "LegalBasis",
     []),
    ("ProcessStep_nextStep_ProcessStep", "ProcessStep", "nextStep", "ProcessStep",
     []),
]


def run(scanner, mapping, path, num_chains=2, num_threads=8, vectorizer=None):
    chain = DefaultStructuredBuilderChain(mapping=mapping, writer=KGWriter(),
                                          vectorizer=vectorizer)
    BuilderChainRunner(scanner=scanner, chain=chain,
                       num_chains=num_chains,
                       num_threads_per_chain=num_threads).invoke(path)


def main():
    ap = argparse.ArgumentParser(description="政务事项建图（纯结构）")
    ap.add_argument("--data-dir", required=True, help="adapter CSV 输出目录")
    ap.add_argument("--only", default=None,
                    help="只导入指定文件基名（逗号分隔），用于断点续跑")
    ap.add_argument("--num-chains", type=int, default=2, help="并行链数")
    ap.add_argument("--num-threads", type=int, default=8, help="链内线程数")
    ap.add_argument("--vectorize", action="store_true",
                    help="挂载 BatchVectorizer（读 kag_config.yaml 的 chain_vectorizer 段）为节点 name 生成向量")
    args = ap.parse_args()

    vectorizer = None
    if args.vectorize:
        from kag.common.conf import KAG_CONFIG
        from kag.interface import VectorizerABC
        if "chain_vectorizer" not in KAG_CONFIG.all_config:
            raise SystemExit("[vectorize] kag_config.yaml 缺少 chain_vectorizer 段")
        vectorizer = VectorizerABC.from_config(
            KAG_CONFIG.all_config["chain_vectorizer"])
        print("[vectorize] BatchVectorizer enabled (bge-m3, 节点 name 向量)")

    only = set(args.only.split(",")) if args.only else None
    scanner = SafeCSVScanner()

    for spg_type in NODE_TYPES:
        path = os.path.join(args.data_dir, f"{spg_type}.csv")
        if only and spg_type not in only:
            continue
        if not os.path.exists(path):
            print(f"[skip] 文件不存在: {path}")
            continue
        print(f"[node] {spg_type} <- {path}")
        run(scanner, SPGTypeMapping(spg_type), path,
            args.num_chains, args.num_threads, vectorizer)

    for fname, s, p, o, sub_props in RELATIONS:
        path = os.path.join(args.data_dir, f"{fname}.csv")
        if only and fname not in only:
            continue
        if not os.path.exists(path):
            print(f"[skip] 文件不存在: {path}")
            continue
        mapping = (RelationMapping(s, p, o)
                   .add_src_id_mapping("srcId")
                   .add_dst_id_mapping("dstId"))
        for sp in sub_props:
            mapping = mapping.add_sub_property_mapping(sp, sp)
        print(f"[edge] {s}-[{p}]->{o} <- {path}")
        run(scanner, mapping, path, args.num_chains, args.num_threads)

    print("done.")


if __name__ == "__main__":
    main()
