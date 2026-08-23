# -*- coding: utf-8 -*-
"""
任务B 临时验证材料（2026-08-09 quality_assurance）：
GovAffair 项目 schema 感知的 LF 静态规划 prompt。
背景：default_lf_static_planning 的 few-shot 全部是通用类型（公众人物/企业...），
实测规划器会产出 Entity[居住证]/p:申领材料 这类与 schema 不符的逻辑形式，
导致 exact_one_hop_select 无法把谓词映射为图关系标签、召回为空。
参照 kag/examples/supplychain/solver/prompt/logic_form_plan.py，仅覆写 few-shot cases，
引导规划器使用本 schema 的中文类型名（政务事项/申请材料/办理环节/实施主体/法律依据）
与关系中文名（申请材料/办理环节/实施主体/引用法条）。
"""
import logging

from kag.interface import PromptABC
from kag.solver.prompt import RetrieverLFStaticPlanningPrompt

logger = logging.getLogger(__name__)


@PromptABC.register("govaffair_lf_plan")
class GovAffairLogicFormPlanPrompt(RetrieverLFStaticPlanningPrompt):
    default_case_zh = [
        {
            "query": "申领居住证需要提交哪些材料？",
            "answer": "首先需要查询政务事项'申领居住证'所需的申请材料\n```\nStep1:查询申领居住证的申请材料\nAction1:Retrieval(s=s1:政务事项[`申领居住证`], p=p1:申请材料, o=o1:申请材料)\n```\n根据检索结果输出材料清单\n```\nStep2:输出o1\nAction2:output(o1)\n```",
        },
        {
            "query": "食品经营许可的办理流程是什么？",
            "answer": "首先需要查询政务事项'食品经营许可'的办理环节\n```\nStep1:查询食品经营许可的办理环节\nAction1:Retrieval(s=s1:政务事项[`食品经营许可`], p=p1:办理环节, o=o1:办理环节)\n```\n根据检索结果输出办理流程\n```\nStep2:输出o1\nAction2:output(o1)\n```",
        },
        {
            "query": "公司设立登记由哪个部门负责实施？",
            "answer": "首先需要查询政务事项'公司设立登记'的实施主体\n```\nStep1:查询公司设立登记的实施主体\nAction1:Retrieval(s=s1:政务事项[`公司设立登记`], p=p1:实施主体, o=o1:实施主体)\n```\n根据检索结果输出实施部门\n```\nStep2:输出o1\nAction2:output(o1)\n```",
        },
        {
            "query": "特种设备使用登记的法律依据是什么？",
            "answer": "首先需要查询政务事项'特种设备使用登记'引用的法条\n```\nStep1:查询特种设备使用登记引用的法条\nAction1:Retrieval(s=s1:政务事项[`特种设备使用登记`], p=p1:引用法条, o=o1:法条引用)\n```\n根据检索结果输出法律依据\n```\nStep2:输出o1\nAction2:output(o1)\n```",
        },
    ]

    default_case_en = default_case_zh
