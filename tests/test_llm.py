"""GatewayLLM against a local fake OpenAI-style server: retry, timeout and budget. No real model is called."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from analysis import ThreadVerdicts, classify_threads_with_model
from guards import Budget, BudgetExceeded
from llm import GatewayLLM
from models import Item

STATE = {"calls": 0, "fail_first": 1}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        STATE["calls"] += 1
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if STATE["calls"] <= STATE["fail_first"]:
            self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        user = json.loads(body["messages"][-1]["content"])
        ids = [t["thread_id"] for t in user.get("threads", [])]
        out = {"verdicts": [{"thread_id": i, "label": "problem_solved", "summary": "fixed it", "user_role": "built"} for i in ids]}
        msg = {"role": "assistant", "content": json.dumps(out)}
        payload = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = json.dumps(payload).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def thread(i):
    return Item(id=f"slack:C1:{i}", source="slack", type="thread", date="2026-05-01T00:00:00Z", title="t", text=f"Meera Joshi: fixed {i}", meta={"mine": True})


def test_retry_then_success_charges_every_attempt(server):
    STATE.update(calls=0, fail_first=1)
    llm = GatewayLLM(server, "tok", timeout=5, retries=1, backoff=0.01)
    b = Budget(max_calls=5, max_seconds=60)
    v, notes = asyncio.run(classify_threads_with_model(llm, b, [thread(1), thread(2)], {"Meera Joshi"}))
    assert set(v) == {"slack:C1:1", "slack:C1:2"} and STATE["calls"] == 2 and b.calls == 2, (STATE, b.calls, notes)


def test_retry_cannot_exceed_budget(server):
    STATE.update(calls=0, fail_first=99)
    llm = GatewayLLM(server, "tok", timeout=5, retries=3, backoff=0.01)
    b = Budget(max_calls=2, max_seconds=60)
    v, notes = asyncio.run(classify_threads_with_model(llm, b, [thread(1)], {"Meera Joshi"}))
    assert v == {} and b.calls == 2 and STATE["calls"] == 2 and notes
