#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务问答 Web 服务
=================
FastAPI + 单页问答界面。

  启动：kg/venv/bin/python qa/server.py
  访问：http://127.0.0.1:8210/
  API ：POST /api/ask  {"question": "..."}
        GET  /api/health
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retriever import GovRetriever
from generator import GovGenerator

HOST = "127.0.0.1"
PORT = 8210

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("govqa")

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="政务事项智能问答", docs_url=None, redoc_url=None)

# 全局单例（neo4j driver / OpenAI client 均线程安全、内部连接池）
retriever = GovRetriever()
generator = GovGenerator()
log.info("retriever/generator 初始化完成")


class AskReq(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[dict] = Field(default_factory=list)  # [{role, content}, ...] 多轮上下文


@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    try:
        with retriever.driver.session(database=retriever.db) as s:
            c = s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        return {"status": "ok", "nodes": c}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "fail", "error": str(e)})


@app.post("/api/ask")
def ask_api(req: AskReq):
    q = req.question.strip()
    t0 = time.time()
    try:
        # 多轮：先改写追问为独立问题（无历史时原样返回）
        rewritten = generator.rewrite(q, req.history)
        t_rw = time.time()
        ret = retriever.retrieve(rewritten)
        t1 = time.time()
        context = ret.to_prompt_context()
        out = generator.generate(rewritten, context, history=req.history)
        t2 = time.time()
        log.info("Q=%r rewritten=%r seeds=%d rewrite=%dms retrieve=%dms generate=%dms",
                 q, rewritten, len(ret.seeds), int((t_rw - t0) * 1000),
                 int((t1 - t_rw) * 1000), int((t2 - t1) * 1000))
        return {
            "answer": out["answer"],
            "usage": out["usage"],
            "rewritten": rewritten,
            "seeds": [{"label": s.label, "name": s.name, "score": round(s.score, 4)}
                      for s in ret.seeds],
            "context": context,
            "rewrite_ms": int((t_rw - t0) * 1000),
            "retrieve_ms": int((t1 - t_rw) * 1000),
            "generate_ms": int((t2 - t1) * 1000),
        }
    except Exception as e:
        log.error("ask failed: %s\n%s", e, traceback.format_exc())
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    import uvicorn
    log.info("启动政务问答服务 http://%s:%d/", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
