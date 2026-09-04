#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线程化 OpenAI 兼容 mock 服务（kg/deploy/mock_llm.py 的多线程变体）。

用途：Builder 的 kagVectorizerAsyncTask 以 10-20 并发调用 /v1/embeddings；
原版用单线程 HTTPServer 串行处理，成为向量化瓶颈。本变体仅把 HTTPServer
换成 ThreadingHTTPServer，行为与原版一致（固定 1024 维零向量 / 固定回复）。

启动：python3 kg/import/mock_llm_threaded.py [port]
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMB_DIM = 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 降低日志噪音
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._reply({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})

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
            inputs = req.get("input") or []
            if isinstance(inputs, (str, dict)):
                inputs = [inputs]
            self._reply({
                "object": "list",
                "model": model,
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.0] * EMB_DIM}
                    for i in range(len(inputs))
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })
        else:
            self._reply({"detail": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
    print(f"openai-mock (threaded) listening on 0.0.0.0:{port} (emb_dim={EMB_DIM})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
