#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务问答生成器
==============
DeepSeek 调用 + 政务问答 prompt 模板。
约束：仅依据检索上下文作答，禁止编造；引用来源。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from openai import OpenAI

LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"

SYSTEM_PROMPT = """你是政务事项问答助手，服务于政务大模型系统。规则：
1. 仅依据下方【知识库检索结果】回答，禁止使用检索结果之外的信息，禁止编造。
2. 检索结果不足以回答时，明确回答"根据现有知识库信息，暂时无法回答该问题"，并可说明缺什么信息。
3. 涉及材料、条件、流程、时限、法律依据等事实时，保持与检索结果一致的表述，不要增删要件。
4. 回答末尾用"【来源】"列出依据的事项名称、法规名称（含文号）与条款号。
5. 用简体中文，分点作答，简洁准确。"""

USER_TEMPLATE = """【知识库检索结果】
{context}

【问题】
{question}"""

REWRITE_SYSTEM = """你是问题改写器。给定多轮对话历史与用户的最新追问，把追问改写成一个\n**可独立检索的完整问题**：
1. 把"那/它/这个/那里"等指代替换为历史中讨论的具体事项名称。
2. 若追问本身已是完整独立问题，原样返回，不要画蛇添足。
3. 只输出改写后的问题文本，不要任何解释、引号或前后缀。
4. 保持用户原意，不扩张问题范围。"""

REWRITE_USER = """【对话历史】
{history}

【最新追问】
{question}"""


def _load_dotenv() -> None:
    """把项目根 .env 的 KEY=VALUE 注入 os.environ（已存在的环境变量不覆盖）。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


def load_api_key() -> str:
    """优先环境变量 DEEPSEEK_API_KEY（含项目根 .env，自动加载），
    否则回退 kag_config.yaml（仓库内为 ${DEEPSEEK_API_KEY} 占位符，不视为有效 key，
    仅当本地为 knext 临时填过真实 key 时命中）。"""
    _load_dotenv()
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key and not key.startswith("${"):
        return key
    cfg = Path(__file__).resolve().parent.parent / "kg" / "pilot" / "GovAffair" / "kag_config.yaml"
    if cfg.exists():
        m = re.search(r"api_key:\s*(sk-[A-Za-z0-9]+)", cfg.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise RuntimeError(
        "未找到 DeepSeek api_key：请在项目根 .env 写入 DEEPSEEK_API_KEY=sk-xxx"
        "（参考 .env.example），或 export DEEPSEEK_API_KEY"
    )


class GovGenerator:
    def __init__(self, base_url: str = LLM_BASE_URL, model: str = LLM_MODEL,
                 api_key: str | None = None):
        self.model = model
        self.client = OpenAI(
            api_key=api_key or load_api_key(),
            base_url=base_url,
            timeout=120,
        )

    def rewrite(self, question: str, history: list[dict]) -> str:
        """把带指代的追问改写为独立完整问题。
        history: [{'role': 'user'|'assistant', 'content': str}, ...]
        无历史或改写失败时返回原问题。"""
        if not history:
            return question
        lines = []
        for h in history[-8:]:
            role = "用户" if h.get("role") == "user" else "助手"
            content = (h.get("content") or "").replace("\n", " ")[:300]
            lines.append(f"{role}：{content}")
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": REWRITE_SYSTEM},
                    {"role": "user", "content": REWRITE_USER.format(
                        history="\n".join(lines), question=question)},
                ],
                max_tokens=512,
                temperature=0,
                stream=False,
                timeout=45,
            )
            rewritten = (resp.choices[0].message.content or "").strip().strip('"\'“”')
            # 合理性校验：空/过长/含解释性前缀时降级原问题
            if not rewritten or len(rewritten) > 200 or "改写" in rewritten[:6]:
                return question
            return rewritten
        except Exception:
            return question

    def generate(self, question: str, context: str,
                 history: list[dict] | None = None,
                 max_tokens: int = 4096, temperature: float = 0.3) -> dict:
        """返回 {'answer': str, 'usage': dict}。max_tokens 给足：
        deepseek-v4-flash 为推理型模型，reasoning 会消耗 token 额度。
        history 用于让回答语气连贯（事实仍以本次检索 context 为准）。"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in (history or [])[-6:]:
            role = h.get("role")
            content = (h.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content[:1500]})
        messages.append({"role": "user", "content": USER_TEMPLATE.format(
            context=context, question=question)})
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        choice = resp.choices[0]
        return {
            "answer": choice.message.content or "",
            "finish_reason": choice.finish_reason,
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else None,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else None,
            },
        }


if __name__ == "__main__":
    g = GovGenerator()
    r = g.generate("申领居住证需要提交哪些材料？",
                   "【事项 1】申领居住证\n所需材料：身份证原件及复印件")
    print(r["answer"])
    print("---", r["usage"])
