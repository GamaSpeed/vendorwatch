# api.py — VendorWatch FastAPI Backend
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import asyncio
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from memory.findings_store import get_findings, load as load_store
from agents.orchestrator import get_summary, answer_question, run_full_analysis

app = FastAPI(title="VendorWatch", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# SERVE REACT FRONTEND
# ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    path = os.path.join(os.path.dirname(__file__), "frontend", "vendorwatch.html")
    with open(path, encoding="utf-8") as f:
        return f.read()

# ──────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────
@app.get("/findings")
async def get_all_findings():
    return get_findings()

@app.get("/summary")
async def get_kpi_summary():
    return json.loads(get_summary())

@app.post("/ask")
async def ask_agent(body: dict):
    question = body.get("question", "")
    if not question:
        return {"error": "question required"}
    try:
        return json.loads(answer_question(question))
    except Exception as e:
        return {"error": str(e)}

@app.post("/analyze")
async def trigger_analysis():
    try:
        result = run_full_analysis()
        return json.loads(result)
    except Exception as e:
        return {"error": str(e)}

@app.get("/watchdog/check")
async def watchdog_check():
    from agents.watchdog_agent import run_watchdog_cycle
    return run_watchdog_cycle()

# ──────────────────────────────────────────────
# SSE — AGENT ACTIVITY STREAM
# ──────────────────────────────────────────────
import logging
_sse_queue: list[str] = []

class SSEHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        if "[activity]" in msg:
            _sse_queue.append(msg)
            if len(_sse_queue) > 100:
                _sse_queue.pop(0)

sse_handler = SSEHandler()
sse_handler.setLevel(logging.INFO)
logging.getLogger("vendorwatch.orchestrator").addHandler(sse_handler)
logging.getLogger("vendorwatch.analyst").addHandler(sse_handler)
logging.getLogger("vendorwatch.narrator").addHandler(sse_handler)
logging.getLogger("vendorwatch.watchdog").addHandler(sse_handler)

async def event_generator() -> AsyncGenerator[str, None]:
    sent = 0
    while True:
        while sent < len(_sse_queue):
            yield f"data: {_sse_queue[sent]}\n\n"
            sent += 1
        await asyncio.sleep(0.5)

@app.get("/agent/activity")
async def agent_activity():
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# ──────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)