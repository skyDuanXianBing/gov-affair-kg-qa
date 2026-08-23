# -*- coding: utf-8 -*-
"""冒烟脚本：用 GovAffair/kag_config.yaml 的 chat_llm 配置，经 KAG maas 客户端
对 DeepSeek 发一次真实 chat completion（中文问答，max_tokens≤50 省费）。
用法：cd kg/pilot && ../venv/bin/python smoke_deepseek_chat.py
"""
import os
import sys

# DeepSeek 为国内服务，必须直连，不走任何代理
for v in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(v, None)

from kag.common.conf import init_env, KAG_CONFIG  # noqa: E402
from kag.interface.common.llm_client import LLMClient  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GovAffair", "kag_config.yaml")


def main():
    init_env(CONFIG_PATH)
    chat_conf = KAG_CONFIG.all_config.get("chat_llm")
    assert chat_conf, "chat_llm section not found in config"
    print(f"[smoke] chat_llm type={chat_conf.get('type')} base_url={chat_conf.get('base_url')} model={chat_conf.get('model')}")
    client = LLMClient.from_config(chat_conf)
    question = "用一句中文回答：办理居住证一般需要什么材料？"
    answer = client(question, max_tokens=50)
    print(f"[smoke] Q: {question}")
    print(f"[smoke] A: {answer}")
    assert answer and any("一" <= ch <= "鿿" for ch in str(answer)), "empty or non-Chinese answer"
    print("[smoke] PASS")


if __name__ == "__main__":
    sys.exit(main())
