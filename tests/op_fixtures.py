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
SCHEMA_0_2 = op_spec.SCHEMA_STRING_0_2


def resolve_like_python_3_12(name):
    """A patch making `Path.resolve` raise RuntimeError for a path through
    a SYMLINK called NAME, as Python 3.12 does when that link is a loop.

    Python 3.13 and later return such a path unresolved instead, so a test
    of the RuntimeError handling would pass vacuously under the Pixi
    environment's newer Python; this makes it exercise the handling on
    every version."""
    from unittest import mock
    real = Path.resolve

    def resolve(self, strict=False):
        for here in (self, *self.parents):
            if here.name == name and here.is_symlink():
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return real(self, strict=strict)
    return mock.patch.object(Path, "resolve", resolve)


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


def as_0_2(doc):
    """DOC as an `openteams/op-manifest [0.2]` document: the schema string,
    and `title` for `name` on the Op and every step, including the steps a
    cycle transition prepares."""
    doc = copy.deepcopy(doc)
    doc["schema"] = SCHEMA_0_2
    steps = list(doc.get("steps") or [])
    for transition in ((doc.get("cycle") or {}).get("transitions")
                       or {}).values():
        steps.extend(transition.get("prepare") or [])
    for item in [doc] + steps:
        if isinstance(item, dict) and "name" in item:
            item["title"] = item.pop("name")
    return doc


def spec_doc_0_2(steps=None, inputs=None, **extra):
    """`spec_doc`, as a `[0.2]` document."""
    return as_0_2(spec_doc(steps, inputs, **extra))


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


def envelope_1_violations(value, where="$"):
    """Every way VALUE breaks the structural rules of envelope v1, as the Op
    Spec's envelope-1 JSON Schema states them — expressed here directly, so
    cog-smith's tests vendor nothing from the spec: required `envelope`
    (the integer 1), `ok` (a boolean) and `problems`; `cog` with a non-empty
    string id and a string-or-null version; `task` a string; `error` null or
    `{code, detail, evidence?}`; `raw` text or null; `binding`, `timing` and
    `provider_result` objects or null; and `ok` false beside an error object.
    """
    found = []
    if not isinstance(value, dict):
        return [f"{where} is not an object"]
    for key in ("envelope", "ok", "problems"):
        if key not in value:
            found.append(f"{where}.{key} is required")
    if "envelope" in value and (type(value["envelope"]) is not int
                                or value["envelope"] != 1):
        found.append(f"{where}.envelope is not the integer 1")
    if "ok" in value and not isinstance(value["ok"], bool):
        found.append(f"{where}.ok is not a boolean")
    if "cog" in value:
        cog = value["cog"]
        if not isinstance(cog, dict) or not isinstance(cog.get("id"), str) \
                or not cog["id"]:
            found.append(f"{where}.cog has no non-empty string id")
        elif "version" in cog and not isinstance(cog["version"], (str, type(None))):
            found.append(f"{where}.cog.version is not a string or null")
    if "task" in value and not isinstance(value["task"], str):
        found.append(f"{where}.task is not a string")
    if "raw" in value and not isinstance(value["raw"], (str, type(None))):
        found.append(f"{where}.raw is not a string or null")
    for key in ("binding", "timing", "provider_result"):
        if key in value and not isinstance(value[key], (dict, type(None))):
            found.append(f"{where}.{key} is not an object or null")
    timing = value.get("timing")
    if isinstance(timing, dict) and "latency_s" in timing:
        latency = timing["latency_s"]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) \
                or latency < 0:
            found.append(f"{where}.timing.latency_s is not a number >= 0")
    error = value.get("error")
    if error is not None:
        if not isinstance(error, dict):
            found.append(f"{where}.error is not null or an object")
        else:
            if not isinstance(error.get("code"), str) or not error["code"]:
                found.append(f"{where}.error.code is not a non-empty string")
            if not isinstance(error.get("detail"), str):
                found.append(f"{where}.error.detail is not a string")
            if "evidence" in error:
                found.extend(_evidence_violations(error["evidence"],
                                                  f"{where}.error.evidence"))
            if value.get("ok") is not False:
                found.append(f"{where} carries an error but ok is not false")
    problems = value.get("problems")
    if "problems" in value and not isinstance(problems, list):
        found.append(f"{where}.problems is not an array")
    for index, problem in enumerate(problems if isinstance(problems, list) else []):
        at = f"{where}.problems[{index}]"
        if not isinstance(problem, dict):
            found.append(f"{at} is not an object")
            continue
        if not isinstance(problem.get("check"), str) or not problem["check"]:
            found.append(f"{at}.check is not a non-empty string")
        if not isinstance(problem.get("detail"), str):
            found.append(f"{at}.detail is not a string")
        if problem.get("severity") not in ("error", "warning", "warn"):
            found.append(f"{at}.severity is not error, warning or warn")
    return found


def _evidence_violations(evidence, where):
    if not isinstance(evidence, dict):
        return [f"{where} is not an object"]
    found = []
    for key in ("command", "returncode", "stdout_tail", "stderr_tail"):
        if key not in evidence:
            found.append(f"{where}.{key} is required")
    command = evidence.get("command")
    if "command" in evidence and (not isinstance(command, list) or any(
            not isinstance(part, str) for part in command)):
        found.append(f"{where}.command is not an array of strings")
    code = evidence.get("returncode")
    if "returncode" in evidence and code is not None and (
            isinstance(code, bool) or not isinstance(code, int)):
        found.append(f"{where}.returncode is not an integer or null")
    for key in ("stdout_tail", "stderr_tail"):
        if key in evidence and (not isinstance(evidence[key], str)
                                or len(evidence[key]) > 2000):
            found.append(f"{where}.{key} is not a string of at most 2000 "
                         f"characters")
    previous = evidence.get("previous_attempts")
    if "previous_attempts" in evidence:
        if not isinstance(previous, list):
            found.append(f"{where}.previous_attempts is not an array")
        else:
            for index, attempt in enumerate(previous):
                found.extend(_evidence_violations(
                    attempt, f"{where}.previous_attempts[{index}]"))
    return found


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


def authority_doc(read=(REPO,), write=(REPO,), resource="github",
                  key="targets"):
    """An admission. KEY is `targets`, or `repositories` for a document
    written before the neutral name existed."""
    operations = []
    if read is not None:
        operations.append({"resource": resource, "action": "read",
                           key: list(read)})
    if write is not None:
        operations.append({"resource": resource, "action": "write",
                           key: list(write)})
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


def change_0_2(change_id, target=REPO, kind="label", summary="add a label",
               target_sha256="1" * 64, content_sha256=None):
    """A proposed change as a `[0.2]` Op reads it: `target` names what it
    changes — a write target the admission lists — and there is no
    `repository`."""
    body = {"change_id": change_id, "kind": kind, "target": target,
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
