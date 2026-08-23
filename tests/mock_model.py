#!/usr/bin/env python3
"""Minimal OpenAI-compatible mock for live-testing created Cogs without a
model. Echoes the requested model id (identity verifies) and returns the
content of MOCK_BODY_FILE (default: a grounded answer for the template's
sample bundle). Not part of any Cog — a test harness for the workshop.

    python tests/mock_model.py --port 8080
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_BODY = {
    "abstained": False,
    "abstain_reason": None,
    "entries": [
        {"title": "Delivery deadline moved to Friday",
         "detail": "The vendor slip pushed delivery from Wednesday to Friday; the client will be notified.",
         "item_ids": ["item-standup"],
         "evidence_quote": "we agreed to push the delivery deadline to Friday"},
        {"title": "Second reviewer now required for schema changes",
         "detail": "Following the outage traced to an unreviewed migration, schema changes require a second reviewer.",
         "item_ids": ["item-retro"],
         "evidence_quote": "schema changes need a second reviewer from now on"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "mock"}]})
        if self.path.endswith("/health"):
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not-found"})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "not-found"})
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length).decode())
        body_file = os.environ.get("MOCK_BODY_FILE")
        content = (open(body_file).read() if body_file
                   else json.dumps(DEFAULT_BODY))
        self._send(200, {
            "model": req.get("model", "mock"),     # echo -> identity verifies
            "choices": [{"message": {"role": "assistant", "content": content}}],
        })

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"mock model on http://127.0.0.1:{args.port}/v1")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
