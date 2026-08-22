#!/usr/bin/env python3
"""Web-API entry point (cog-smith machinery, generic — do not edit; see
task_logic.py for your Cog's logic).

    pixi run serve
    curl -s localhost:$PORT/ask -d @examples/sample-bundle.json | jq
    (the port comes from the manifest's declared http-json endpoint)

Stdlib http.server: loopback only, single-threaded, no auth — a
demonstration of an entry point, not a deployment. Responses are envelope
v1 (ENVELOPE.md in cog-smith). Status mapping: caller faults 4xx (400
malformed JSON, 413 oversize, 422 invalid input), upstream faults 5xx
(502 model call failed/malformed, 503 unavailable or binding-invalid).
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_core  # noqa: E402

ERROR_STATUS = {
    "binding-invalid": 503,
    "invalid-input": 422,
    "model-unavailable": 503,
    "model-call-failed": 502,
    "model-response-malformed": 502,
}


def _declared():
    m = cog_core.MANIFEST
    ep = next((i.get("endpoint") for i in m.get("interfaces") or []
               if i.get("kind") == "http-json"), None)
    port = 8090
    path = "/ask"
    if ep:
        import re
        mm = re.match(r"^https?://[^:/]+:(\d+)(/.*)?$", ep)
        if mm:
            port = int(mm.group(1))
            path = mm.group(2) or "/ask"
    return ep, port, path


DECLARED_ENDPOINT, DEFAULT_PORT, ASK_PATH = _declared()
PORT = int(os.environ.get("COG_API_PORT", str(DEFAULT_PORT)))
MAX_BODY = 1 << 20


def cog_metadata():
    """Derived from the manifest — hard-coding drifted in forge review. The
    actually-bound port is authoritative for the address."""
    m = cog_core.MANIFEST
    return {
        "id": m.get("id"), "version": m.get("version"), "kind": m.get("kind"),
        "io": m.get("io"),
        "requires": [r.get("capability") for r in m.get("requires") or []
                     if isinstance(r, dict)],
        "prohibits": m.get("prohibits"),
        "serving_at": f"http://127.0.0.1:{PORT}{ASK_PATH}",
        "declared_endpoint": DECLARED_ENDPOINT,
        "model_endpoint": cog_core.ENDPOINT,
        "envelope": 1,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path.rstrip("/") in ("/health", ""):
                ok, detail = cog_core.health()
                return self._send(200 if ok else 503, {"ok": ok, "detail": detail})
            if self.path.rstrip("/") == "/cog":
                return self._send(200, cog_metadata())
            self._send(404, {"error": "not-found"})
        except Exception as e:                                # pragma: no cover
            self._send(500, {"error": "internal", "detail": repr(e)})

    def do_POST(self):
        try:
            if self.path.rstrip("/") != ASK_PATH.rstrip("/"):
                return self._send(404, {"error": "not-found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad-content-length"})
            if length <= 0:
                return self._send(400, {"error": "empty-body"})
            if length > MAX_BODY:
                return self._send(413, {"error": "body-too-large", "max_bytes": MAX_BODY})
            try:
                bundle = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except json.JSONDecodeError as e:
                return self._send(400, {"error": "invalid-json", "detail": str(e)})
            result = cog_core.invoke(bundle)
            code = ERROR_STATUS.get((result.get("error") or {}).get("code"), 200)
            self._send(code, result)
        except Exception as e:                                # pragma: no cover
            self._send(500, {"error": "internal", "detail": repr(e)})

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))


if __name__ == "__main__":
    print(f"{cog_core.SELF_ID['id']} web-api on http://127.0.0.1:{PORT}")
    if PORT != DEFAULT_PORT:
        print("  note: COG_API_PORT overrides the manifest's declared endpoint; "
              "the bound address above is authoritative")
    print(f"  model dependency: {cog_core.ENDPOINT}")
    print(f"  POST {ASK_PATH}   GET /health   GET /cog")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
