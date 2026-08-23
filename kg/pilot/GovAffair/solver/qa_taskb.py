# -*- coding: utf-8 -*-
"""
任务B 临时验证脚本（2026-08-09 quality_assurance）：
对 GovAffair v0.1 试点图谱跑自然语言问答端到端验证。
- 从本地 kag_config.yaml 的 kag_solver_pipeline 段构建 solver 管道（AffairQA 示例同款入口）
- TraceLogReporter 采集召回痕迹；对 OpenAIClient(maas) 的 __call__/acall 打点计数真实 LLM 调用
- 结果写 solver/data/qa_taskb_result.json；运行日志由外层 tee 落盘
"""
import asyncio
import json
import logging
import os
import sys
import time

from kag.common.conf import KAG_CONFIG
from kag.common.registry import import_modules_from_path
from kag.interface import SolverPipelineABC
from kag.solver.reporter.trace_log_reporter import TraceLogReporter
from kag.common.llm.openai_client import OpenAIClient

logger = logging.getLogger(__name__)

# ---- 真实 LLM 调用计数（含重试）----
LLM_STATS = {"calls": 0, "per_question": {}}
_orig_call = OpenAIClient.__call__
_orig_acall = OpenAIClient.acall
_CURRENT_Q = {"name": "init"}


def _counted_call(self, prompt="", **kwargs):
    LLM_STATS["calls"] += 1
    LLM_STATS["per_question"].setdefault(_CURRENT_Q["name"], 0)
    LLM_STATS["per_question"][_CURRENT_Q["name"]] += 1
    return _orig_call(self, prompt, **kwargs)


async def _counted_acall(self, prompt="", **kwargs):
    LLM_STATS["calls"] += 1
    LLM_STATS["per_question"].setdefault(_CURRENT_Q["name"], 0)
    LLM_STATS["per_question"][_CURRENT_Q["name"]] += 1
    return await _orig_acall(self, prompt, **kwargs)


OpenAIClient.__call__ = _counted_call
OpenAIClient.acall = _counted_acall


def summarize_trace(info_dict):
    """从 TraceLogReporter 报告里提取召回证据摘要（节点/边/chunk 计数与样例）"""
    out = {"retrieval_lines": [], "generator_refs": []}
    text = json.dumps(info_dict, ensure_ascii=False)
    out["trace_chars"] = len(text)
    # 报告行按 (tag, name) 索引，尽量收集 retrieved spo 与 reference 信息
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and ("retriever" in k or "kag_merger" in k):
                    out["retrieval_lines"].append({k: v if isinstance(v, (str, int, float)) else str(v)[:800]})
                if isinstance(k, str) and ("reference" in k or "chunk" in k):
                    out["generator_refs"].append({k: v if isinstance(v, (str, int, float)) else str(v)[:800]})
                walk(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)
    walk(info_dict)
    return out


async def qa_one(pipeline, question):
    reporter = TraceLogReporter()
    t0 = time.time()
    answer = await pipeline.ainvoke(question, reporter=reporter)
    cost = time.time() - t0
    info, status = reporter.generate_report_data()
    info_dict = info.to_dict() if hasattr(info, "to_dict") else str(info)
    return {
        "question": question,
        "answer": answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False),
        "cost_sec": round(cost, 1),
        "trace_summary": summarize_trace(info_dict),
        "trace_file": None,
    }


def main():
    questions = [
        "申领居住证需要提交哪些材料？",
        "食品经营许可的办理流程是什么？",
        "申领居住证由哪个部门负责实施办理？",
    ]
    max_q = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    questions = questions[:max_q]

    import_modules_from_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt"))
    cfg = KAG_CONFIG.all_config
    assert "kag_solver_pipeline" in cfg, "kag_solver_pipeline missing in kag_config.yaml"
    pipeline = SolverPipelineABC.from_config(cfg["kag_solver_pipeline"])

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for i, q in enumerate(questions, 1):
        _CURRENT_Q["name"] = f"q{i}"
        print(f"\n{'='*30}\n[Q{i}] {q}\n{'='*30}", flush=True)
        try:
            r = asyncio.run(qa_one(pipeline, q))
        except Exception as e:
            import traceback
            r = {"question": q, "error": repr(e), "traceback": traceback.format_exc()}
        results.append(r)
        print(f"\n[Q{i}] ANSWER: {str(r.get('answer', r.get('error')))[:1200]}\n", flush=True)
        # 每题后立即落盘（中断也保留证据）
        with open(os.path.join(out_dir, "qa_taskb_result.json"), "w", encoding="utf-8") as f:
            json.dump({"results": results, "llm_stats": LLM_STATS}, f, ensure_ascii=False, indent=2)

    print(f"\nTOTAL REAL LLM CALLS: {LLM_STATS['calls']}  detail: {LLM_STATS['per_question']}")


if __name__ == "__main__":
    main()
