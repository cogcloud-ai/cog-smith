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

    def __call__(self, cog_dir, task, request_path, **seam):
        request = json.loads(Path(request_path).read_text())
        self.calls.append({"cog_dir": str(cog_dir), "task": task,
                           "request": request,
                           "request_path": str(request_path),
                           "seam": seam})
        if self.watcher:
            self.watcher(self, request_path)
        answer = self.answers[task]
        if callable(answer):
            return answer(len(self.calls), request)
        if isinstance(answer, list):
            index = sum(1 for c in self.calls if c["task"] == task) - 1
            return answer[min(index, len(answer) - 1)]
        return answer


# --------------------------------------------------- authority fixtures --
#
# Phase 3: a step declares what it requires, the runner issues the grant.
# These build the three documents and the minimal Cog packages a
# declaration/authority check reads.

AUTHORITY_SCHEMA = "openteams/op-authority [0.1]"
DECISION_SCHEMA = "openteams/op-decision [0.1]"
REPO = "example-org/example-repo"

COG_MANIFEST = """[workspace]
name = "cog-{name}"
version = "0.1.0"
description = "A fixture Cog."

[tasks]
{task} = "python src/cog_cli.py"
check = "python src/cog_cli.py --check"
test = "python -m unittest discover -s tests"

[tool.cog]
schema = "openteams/cog-manifest [0.1]"
id = "openteams/cog-{name}"
kind = "{kind}"
owner = "trent@openteams.com"
license = "BSD-3-Clause"
requires = []
reaches = {reaches}

[[tool.cog.interfaces]]
name = "cli"
kind = "command"
task = "{task}"
audience = "usage"
default = true
"""


def write_cog(parent, name, kind="code", reaches=(), task="ask"):
    """A minimal Cog package: enough manifest for the declaration and
    authority checks (identity, kind, reaches, one usage interface)."""
    root = Path(parent) / f"cog-{name}"
    root.mkdir(parents=True, exist_ok=True)
    rendered = "[" + ", ".join(
        '{ resource = "%s", actions = [%s] }'
        % (entry["resource"], ", ".join(f'"{a}"' for a in entry["actions"]))
        for entry in reaches) + "]"
    (root / "pixi.toml").write_text(COG_MANIFEST.format(
        name=name, kind=kind, reaches=rendered, task=task))
    return root


def authority_doc(read=(REPO,), write=(REPO,)):
    operations = []
    if read is not None:
        operations.append({"resource": "github", "action": "read",
                           "repositories": list(read)})
    if write is not None:
        operations.append({"resource": "github", "action": "write",
                           "repositories": list(write)})
    return {"schema": AUTHORITY_SCHEMA, "operations": operations}


def change(change_id, repository=REPO, kind="label", summary="add a label",
           target_sha256="1" * 64, content_sha256=None):
    """A proposed change, with BOTH hashes (contract §9): `content_sha256`
    is the change object's OWN hash — stated by the proposing Cog, checked
    at the pause and never repaired (contract §9c) — and `target_sha256` is
    the target item's content hash as the Op read it.

    `content_sha256` is computed here, exactly as a proposing Cog computes
    it; pass one explicitly to build a proposal that misstates its hash."""
    body = {"change_id": change_id, "kind": kind,
            "target": f"{repository}#1", "repository": repository,
            "summary": summary}
    return dict(body,
                content_sha256=(content_sha256 if content_sha256 is not None
                                else op_runner.change_content_sha256(body)),
                target_sha256=target_sha256)


def decision_doc(run_id, step, payload_sha256, verdicts):
    """verdicts: {change_id: "approve" | "reject" | edited-change-dict}."""
    decisions = []
    for cid, verdict in verdicts.items():
        if isinstance(verdict, dict):
            decisions.append({"change_id": cid, "verdict": "edit",
                              "change": verdict})
        elif verdict == "reject":
            decisions.append({"change_id": cid, "verdict": "reject",
                              "reason": "not this week"})
        else:
            decisions.append({"change_id": cid, "verdict": "approve"})
    return {"schema": DECISION_SCHEMA, "run_id": run_id, "step": step,
            "payload_sha256": payload_sha256, "decided_by": "trent",
            "decided_at": op_track.utc_now(), "decisions": decisions}
