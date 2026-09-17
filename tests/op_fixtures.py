"""Shared fixtures for the Op machinery tests (not a test module).

Puts the Op machinery masters on sys.path — the same files an Op package
carries verbatim — so cog-smith tests the thing it ships.
"""
import copy
import json
import sys
from pathlib import Path

SMITH_ROOT = Path(__file__).resolve().parents[1]
OP_SRC = SMITH_ROOT / "templates" / "op" / "src"
if str(OP_SRC) not in sys.path:
    sys.path.insert(0, str(OP_SRC))

import op_runner  # noqa: E402
import op_spec    # noqa: E402
import op_track   # noqa: E402

SCHEMA = op_spec.SCHEMA_STRING


def spec_doc(steps=None, inputs=None, **extra):
    doc = {
        "schema": SCHEMA,
        "id": "openteams/op-test",
        "version": "0.1.0",
        "name": "Test Op",
        "description": "A two-step linear Op used by the machinery tests.",
        "inputs": inputs if inputs is not None else [
            {"name": "note", "description": "a note", "required": True},
        ],
        "steps": steps or [cog_step("first")],
    }
    doc.update(extra)
    return copy.deepcopy(doc)


def cog_step(sid, task="ask", depends_on=None, **extra):
    step = {
        "id": sid,
        "name": f"Step {sid}",
        "cog": {"id": f"openteams/cog-{sid}", "version": "0.1.0",
                "source": f"../cog-{sid}", "task": task},
        "input": {"note": {"$from": "inputs.note"}},
        "expected_outcome": "something useful",
        "gate": {"policy": op_spec.GATE_POLICY, "guards": []},
    }
    if depends_on:
        step["depends_on"] = list(depends_on)
    step.update(extra)
    return step


def envelope(ok=True, payload=None, problems=None, cog_id="openteams/cog-x",
             task="ask"):
    return {
        "envelope": 1,
        "cog": {"id": cog_id, "version": "0.1.0"},
        "task": task,
        "ok": ok,
        "error": None if ok else {"code": "model-unavailable",
                                  "detail": "no endpoint"},
        "payload": payload,
        "raw": None,
        "problems": problems or [],
        "binding": {"model": "test-model"},
        "timing": {"latency_s": 0.0},
    }


def write_package(root, doc):
    """A minimal Op package on disk: op.yaml is all the runner needs."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "op.yaml").write_text(json.dumps(doc, indent=2))
    return root


def write_request(path, value):
    Path(path).write_text(json.dumps(value, indent=2))
    return Path(path)


class FakeCog:
    """Stands in for `invoke_cog`: records every invocation and answers from
    a per-task (or per-request) script of envelopes."""

    def __init__(self, answers, watcher=None):
        self.answers = answers
        self.calls = []
        self.watcher = watcher

    def __call__(self, cog_dir, task, request_path):
        request = json.loads(Path(request_path).read_text())
        self.calls.append({"cog_dir": str(cog_dir), "task": task,
                           "request": request,
                           "request_path": str(request_path)})
        if self.watcher:
            self.watcher(self, request_path)
        answer = self.answers[task]
        if callable(answer):
            return answer(len(self.calls), request)
        if isinstance(answer, list):
            index = sum(1 for c in self.calls if c["task"] == task) - 1
            return answer[min(index, len(answer) - 1)]
        return answer
