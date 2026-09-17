#!/usr/bin/env python3
"""The Op runner: run the declared steps of this package's op.yaml.

    python src/op_runner.py --request examples/request.json [--dry-run]
                            [--runs-dir DIR]

Machinery master (cog-smith `templates/op/src/`) — never edited inside an Op
package. The runner is the Op layer: it sequences steps, builds each step's
request from the spec's mapping expressions, invokes each Cog ONLY through
the usage task that Cog declares, gates the envelope it gets back, and
writes the durable Track. It never imports a Cog's Python and never calls a
model itself.

Gate semantics are lifted from op-video-transcription/src/run_op.py: three
states (pass, pass-with-problems, fail) with the reasons listed. The Gate
decides; the Cog never decides its own acceptance.

Exit codes: 0 completed (or completed-with-problems, or a planned dry run),
1 failed, 2 an invalid spec or request. Stdout is one JSON object.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import op_spec    # noqa: E402
import op_track   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------- the Cog seam ----

def parse_envelope(stdout):
    """The last JSON object on stdout that looks like an envelope.

    A Cog may print progress before its result and may print the envelope
    pretty-printed over many lines (cog-smith's own context-cog machinery
    does), so this scans stdout for JSON objects rather than reading lines.
    """
    decoder = json.JSONDecoder()
    found = None
    index = stdout.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(stdout, index)
        except json.JSONDecodeError:
            index = stdout.find("{", index + 1)
            continue
        if isinstance(value, dict) and "envelope" in value:
            found = value
        index = stdout.find("{", max(end, index + 1))
    if found is None:
        raise ValueError("Cog command did not emit a JSON envelope")
    return found


#: Request-file flags the seam will try, in order. `--request` is the seam's
#: flag; `--bundle` is the flag cog-smith's own context-cog machinery gives a
#: created Cog, so an Op must be able to call one. A later flag is tried ONLY
#: when the Cog's CLI rejected the earlier one by name before doing any work
#: (argparse exits non-zero with "unrecognized arguments"), so nothing
#: effectful can run twice.
REQUEST_FLAGS = ("--request", "--bundle")


def _rejected_flag(completed, flag):
    """True when the Cog's CLI refused FLAG by name without running."""
    if completed.returncode == 0:
        return False
    text = f"{completed.stdout}\n{completed.stderr}"
    return "unrecognized arguments" in text and flag in text


def invoke_cog(cog_dir, task, request_path):
    """Run one declared Cog task and return its envelope. A Cog that emits
    no envelope yields a synthetic ok:false envelope (invocation-failed) so
    the Gate always has something to decide about."""
    cog_dir = Path(cog_dir)
    for flag in REQUEST_FLAGS:
        command = [
            "pixi", "run", "--manifest-path", str(cog_dir / "pixi.toml"),
            task, "--", flag, str(request_path),
        ]
        completed = subprocess.run(command, text=True, capture_output=True)
        if not _rejected_flag(completed, flag):
            break
    try:
        return parse_envelope(completed.stdout)
    except ValueError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip() or str(exc)
        return {
            "envelope": 1,
            "cog": {"id": f"unavailable:{cog_dir.name}", "version": None},
            "task": task,
            "ok": False,
            "error": {"code": "invocation-failed", "detail": detail},
            "payload": None,
            "raw": completed.stdout,
            "problems": [],
            "binding": None,
            "timing": {"latency_s": 0},
        }


def gate_envelope(envelope):
    """The Gate: a three-state decision over one envelope, with reasons."""
    reasons = []
    if envelope.get("envelope") != 1:
        reasons.append("Cog result is not envelope v1.")
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        reasons.append(
            f"Cog invocation failed: {error.get('code', 'unknown')}: "
            f"{error.get('detail', '')}".rstrip())
    error_problems = [p for p in envelope.get("problems") or []
                      if p.get("severity") == "error"]
    reasons.extend(str(p.get("detail") or p.get("check")
                       or "contract check failed") for p in error_problems)
    if reasons:
        status = "fail"
    elif envelope.get("problems"):
        status = "pass-with-problems"
        reasons = [str(p.get("detail") or p.get("check"))
                   for p in envelope["problems"]]
    else:
        status = "pass"
    return {
        "policy": op_spec.GATE_POLICY,
        "status": status,
        "reasons": reasons,
        "decided_at": op_track.utc_now(),
        "guards": [],
    }


def combine_gates(gates):
    """One Gate decision over a foreach step's per-element decisions."""
    statuses = [g["status"] for g in gates]
    if "fail" in statuses:
        status = "fail"
    elif "pass-with-problems" in statuses:
        status = "pass-with-problems"
    else:
        status = "pass"
    reasons = [f"element {i}: {reason}"
               for i, gate in enumerate(gates) for reason in gate["reasons"]]
    return {
        "policy": op_spec.GATE_POLICY,
        "status": status,
        "reasons": reasons,
        "decided_at": op_track.utc_now(),
        "guards": [],
    }


STEP_STATUS = {"pass": "passed", "pass-with-problems": "passed-with-problems",
               "fail": "failed"}


# ------------------------------------------------------------- the run ---

def _attempt(cog_dir, task, request_path, envelope_path, on_fail):
    """Invoke once, gate, and retry exactly once when the failure was a
    transport/model failure (ok:false) and the step asked for retry-once.
    An error-severity problem in an ok envelope is never retried."""
    envelope_path = Path(envelope_path)
    started = time.monotonic()
    envelope = invoke_cog(cog_dir, task, request_path)
    op_track.write_json(envelope_path, envelope)
    gate = gate_envelope(envelope)
    attempts = []
    if (gate["status"] == "fail" and on_fail == "retry-once"
            and not envelope.get("ok")):
        first = envelope_path.with_name(envelope_path.stem + ".attempt-1.json")
        op_track.write_json(first, envelope)
        envelope = invoke_cog(cog_dir, task, request_path)
        op_track.write_json(envelope_path, envelope)
        gate = gate_envelope(envelope)
        attempts = [str(first.resolve()), str(envelope_path.resolve())]
    return envelope, gate, attempts, round(time.monotonic() - started, 3)


def _plan(spec, track, run_dir, context):
    """A dry run: resolve the order and every request that depends only on
    the inputs; list the rest with request: null."""
    for step in spec.ordered:
        request_path = None
        expressions = [step.get("input") or {}]
        if step.get("foreach") is not None:
            expressions.append((step["foreach"] or {}).get("items"))
        if (step.get("foreach") is None
                and not any(op_spec.reads_steps(e) for e in expressions)):
            request = op_spec.evaluate(step.get("input") or {}, context)
            path = Path(run_dir) / "requests" / f"{step['id']}.json"
            op_track.write_json(path, request)
            request_path = str(path.resolve())
        track["steps"].append(
            op_track.step_record(step, "planned", request=request_path))
    track["status"] = "planned"
    track["ended_at"] = op_track.utc_now()
    track_path = op_track.save(track, run_dir)
    return 0, {"ok": True, "status": "planned",
               "run_dir": str(Path(run_dir).resolve()), "track": track_path}


def _run_foreach(spec, step, cog_dir, run_dir, context):
    """Run one step once per element; returns (record fields, payload list)."""
    foreach = step["foreach"]
    items = op_spec.evaluate(foreach["items"], context)
    if not isinstance(items, list):
        raise op_spec.OpSpecError(
            f"Op step {step['id']!r} declares foreach over a value that is "
            f"not a list.")
    task = step["cog"]["task"]
    on_fail = step.get("on_fail", "stop")
    elements, gates, payloads, envelopes = [], [], [], []
    elapsed = 0.0
    for index, item in enumerate(items):
        element_context = dict(context)
        element_context[foreach["as"]] = item
        request = op_spec.evaluate(step.get("input") or {}, element_context)
        request_path = Path(run_dir) / "requests" / step["id"] / f"{index}.json"
        op_track.write_json(request_path, request)
        envelope_path = Path(run_dir) / "envelopes" / step["id"] / f"{index}.json"
        envelope, gate, attempts, seconds = _attempt(
            cog_dir, task, request_path, envelope_path, on_fail)
        elapsed += seconds
        gates.append(gate)
        envelopes.append(envelope)
        payloads.append(None if gate["status"] == "fail"
                        else envelope.get("payload"))
        elements.append({
            "index": index,
            "request": str(request_path.resolve()),
            "envelope": str(envelope_path.resolve()),
            "gate": gate,
            "attempts": attempts,
            "problems": envelope.get("problems") or [],
        })
    gate = combine_gates(gates) if gates else combine_gates([])
    fields = {
        "request": str((Path(run_dir) / "requests" / step["id"]).resolve()),
        "envelope": str((Path(run_dir) / "envelopes" / step["id"]).resolve()),
        "binding": next((e.get("binding") for e in envelopes
                         if e.get("binding")), None),
        "problems": [p for e in envelopes for p in (e.get("problems") or [])],
        "gate": gate,
        "elapsed_s": round(elapsed, 3),
        "elements": elements,
    }
    if envelopes:
        fields["cog"] = envelopes[0].get("cog") or fields.get("cog")
    return fields, payloads, envelopes


def _run_single(step, cog_dir, run_dir, context):
    task = step["cog"]["task"]
    request = op_spec.evaluate(step.get("input") or {}, context)
    request_path = Path(run_dir) / "requests" / f"{step['id']}.json"
    op_track.write_json(request_path, request)
    envelope_path = Path(run_dir) / "envelopes" / f"{step['id']}.json"
    envelope, gate, attempts, seconds = _attempt(
        cog_dir, task, request_path, envelope_path,
        step.get("on_fail", "stop"))
    fields = {
        "cog": envelope.get("cog") or {"id": step["cog"].get("id"),
                                       "version": step["cog"].get("version")},
        "request": str(request_path.resolve()),
        "envelope": str(envelope_path.resolve()),
        "binding": envelope.get("binding"),
        "problems": envelope.get("problems") or [],
        "gate": gate,
        "elapsed_s": seconds,
        "attempts": attempts,
    }
    return fields, envelope


def run(package_root, request_path, dry_run=False, runs_dir=None):
    """Run this Op package's spec over one request. Returns (exit code,
    the JSON object the CLI prints)."""
    package_root = Path(package_root).resolve()
    spec = op_spec.load(package_root / "op.yaml")
    request_path = Path(request_path).resolve()
    request_doc = op_spec.load_document(request_path)
    values = spec.build_inputs(request_doc)

    run_id = op_track.new_run_id()
    run_dir = (Path(runs_dir).resolve() if runs_dir
               else package_root / "runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    input_request = run_dir / "input-request.json"
    op_track.write_json(input_request, request_doc)

    track = op_track.new_track(spec, run_id, input_request,
                               status="planned" if dry_run else "running")
    context = {
        "inputs": values,
        "steps": {},
        "run": {"dir": str(run_dir), "id": run_id},
        "request": {"dir": str(request_path.parent)},
    }
    op_track.save(track, run_dir)
    if dry_run:
        return _plan(spec, track, run_dir, context)

    dependents = spec.dependents()
    statuses, blocked = {}, set()
    for step in spec.ordered:
        sid = step["id"]
        if sid in blocked:
            track["steps"].append(op_track.step_record(step, "blocked"))
            statuses[sid] = "blocked"
            op_track.save(track, run_dir)
            continue
        cog_dir = (package_root / step["cog"]["source"]).resolve()
        if step.get("foreach") is not None:
            fields, payload, envelopes = _run_foreach(
                spec, step, cog_dir, run_dir, context)
            envelope_for_context = envelopes
        else:
            fields, envelope = _run_single(step, cog_dir, run_dir, context)
            payload = envelope.get("payload")
            envelope_for_context = envelope
        gate = fields["gate"]
        on_fail = step.get("on_fail", "stop")
        if gate["status"] == "fail" and on_fail == "skip":
            status = "skipped"
        else:
            status = STEP_STATUS[gate["status"]]
        track["steps"].append(op_track.step_record(step, status, **fields))
        statuses[sid] = status
        op_track.save(track, run_dir)

        if status == "failed":
            track["status"] = "failed"
            track["ended_at"] = op_track.utc_now()
            track_path = op_track.save(track, run_dir)
            return 1, {"ok": False, "status": "failed", "failed_step": sid,
                       "run_dir": str(run_dir), "track": track_path}
        if status == "skipped":
            blocked |= dependents.get(sid, set())
            continue
        context["steps"][sid] = {"payload": payload,
                                 "envelope": envelope_for_context}

    if spec.outputs:
        track["outputs"] = op_spec.evaluate(spec.outputs, context)
    problematic = any(s["status"] in ("passed-with-problems", "skipped",
                                      "blocked") for s in track["steps"])
    track["status"] = "completed-with-problems" if problematic else "completed"
    track["ended_at"] = op_track.utc_now()
    track_path = op_track.save(track, run_dir)
    output = {"ok": True, "status": track["status"],
              "run_dir": str(run_dir), "track": track_path}
    if spec.outputs:
        output["outputs"] = track["outputs"]
    return 0, output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run this Op package's spec.")
    parser.add_argument("--request", required=True,
                        help="the Op request document (JSON or YAML)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the plan and write a planned Track "
                             "without invoking any Cog")
    parser.add_argument("--runs-dir",
                        help="where run directories are written "
                             "(default: <package>/runs)")
    args = parser.parse_args(argv)
    try:
        code, output = run(ROOT, args.request, dry_run=args.dry_run,
                           runs_dir=args.runs_dir)
    except op_spec.OpSpecError as exc:
        print(json.dumps({"ok": False, "status": "invalid-input",
                          "problems": exc.problems}, indent=2))
        return 2
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "status": "invalid-input",
                          "problems": [f"{type(exc).__name__}: {exc}"]},
                         indent=2))
        return 2
    print(json.dumps(output, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
