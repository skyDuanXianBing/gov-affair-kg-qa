#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小 OpenAI 兼容 mock 服务（仅用于通过 knext project create/update 的连通性校验）。

提供：
  POST /v1/chat/completions  → 固定文本响应
  POST /v1/embeddings        → 固定 1024 维零向量

注意：本服务不产出任何真实语义结果。试点纯结构建图不消费 LLM；
正式问答/抽取前必须在 kag_config.yaml 中换成真实模型端点。
"""

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

EMB_DIM = 1024


class Handler(BaseHTTPRequestHandler):
    def _reply(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            req = {}
        model = req.get("model", "mock-model")

        if self.path.rstrip("/").endswith("/chat/completions"):
            self._reply({
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "mock-ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        elif self.path.rstrip("/").endswith("/embeddings"):
            inputs = req.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            self._reply({
                "object": "list",
                "data": [{"object": "embedding", "index": i,
                          "embedding": [0.0] * EMB_DIM}
                         for i, _ in enumerate(inputs)],
                "model": model,
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })
        elif self.path.rstrip("/").endswith("/models") or self.path == "/v1/models":
            self._reply({"object": "list",
                         "data": [{"id": model, "object": "model"}]})
        else:
            self.send_error(404, f"unknown path: {self.path}")

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._reply({"object": "list",
                         "data": [{"id": "mock-model", "object": "model"}]})
        else:
            self._reply({"status": "ok", "service": "openai-mock"})

    def log_message(self, fmt, *args):
        with open("mock_llm.log", "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
    print(f"openai-mock listening on 127.0.0.1:{port} (emb_dim={EMB_DIM})")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
