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

A run validates every step's Cog declaration before invoking anything, and a
run that stops records the steps it never reached as `not-reached`, so the
Track always lists every step of the spec.
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


def envelope_problems(value):
    """Why VALUE is not an envelope v1 the Gate can decide about. Field TYPES
    are checked, not merely field presence: an `ok` that is the string
    "false", or `problems` that are bare strings, are malformed output, not a
    result — they become a controlled invocation failure with the output kept
    as evidence."""
    if not isinstance(value, dict):
        return ["the Cog's result is not a JSON object."]
    problems = []
    if value.get("envelope") != 1:
        problems.append(f"the Cog's result declares envelope "
                        f"{value.get('envelope')!r}, not envelope v1.")
    if not isinstance(value.get("ok"), bool):
        problems.append(f"the Cog's result declares ok {value.get('ok')!r}, "
                        f"which is not true or false.")
    listed = value.get("problems")
    if listed is not None and (not isinstance(listed, list)
                               or any(not isinstance(p, dict) for p in listed)):
        problems.append("the Cog's result declares problems that are not a "
                        "list of problem objects.")
    return problems


def failed_envelope(cog_dir, task, detail, raw=None, carried=None):
    """A synthetic ok:false envelope for an invocation that never produced a
    usable result. What the Cog did emit is kept in `raw` as evidence; when
    that output was a well-formed envelope, its identity, binding and
    problems are carried across rather than thrown away."""
    carried = carried if isinstance(carried, dict) else {}
    return {
        "envelope": 1,
        "cog": carried.get("cog") or {"id": f"unavailable:{Path(cog_dir).name}",
                                      "version": None},
        "task": task,
        "ok": False,
        "error": {"code": "invocation-failed", "detail": detail},
        "payload": None,
        "raw": raw,
        "problems": [p for p in carried.get("problems") or []
                     if isinstance(p, dict)],
        "binding": carried.get("binding"),
        "timing": {"latency_s": 0},
    }


def invoke_cog(cog_dir, task, request_path):
    """Run one declared Cog task and return its envelope.

    Anything short of a well-formed envelope from a process that exited 0 is
    an invocation failure with a synthetic ok:false envelope, so the Gate
    always has something to decide about: a command that could not be
    launched, a nonzero exit (even after printing an envelope — a process
    that dies at 139 has not succeeded), output with no envelope, and a
    malformed envelope all arrive the same way."""
    cog_dir = Path(cog_dir)
    for flag in REQUEST_FLAGS:
        command = [
            "pixi", "run", "--manifest-path", str(cog_dir / "pixi.toml"),
            task, "--", flag, str(request_path),
        ]
        try:
            completed = subprocess.run(command, text=True, capture_output=True)
        except OSError as exc:
            return failed_envelope(
                cog_dir, task,
                f"could not launch {command[0]!r} for task {task!r}: {exc}")
        if not _rejected_flag(completed, flag):
            break
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    try:
        envelope = parse_envelope(stdout)
    except ValueError as exc:
        detail = stderr or stdout.strip() or str(exc)
        return failed_envelope(cog_dir, task, detail, raw=stdout)
    malformed = envelope_problems(envelope)
    if malformed:
        return failed_envelope(cog_dir, task, " ".join(malformed), raw=envelope)
    if completed.returncode != 0:
        detail = (f"the Cog command for task {task!r} exited "
                  f"{completed.returncode}")
        if stderr:
            detail += f": {stderr[-400:]}"
        return failed_envelope(cog_dir, task, detail, raw=envelope,
                               carried=envelope)
    return envelope


def gate_envelope(envelope):
    """The Gate: a three-state decision over one envelope, with reasons."""
    reasons = []
    if not isinstance(envelope, dict) or envelope.get("envelope") != 1:
        return {
            "policy": op_spec.GATE_POLICY, "status": "fail",
            "reasons": ["Cog result is not envelope v1."],
            "decided_at": op_track.utc_now(), "guards": [],
        }
    if envelope.get("ok") is not True:
        error = envelope.get("error") or {}
        error = error if isinstance(error, dict) else {}
        reasons.append(
            f"Cog invocation failed: {error.get('code', 'unknown')}: "
            f"{error.get('detail', '')}".rstrip())
    listed = envelope.get("problems") or []
    if not isinstance(listed, list) or any(not isinstance(p, dict)
                                           for p in listed):
        reasons.append("the Cog reported problems that are not problem "
                       "objects.")
        listed = [p for p in (listed if isinstance(listed, list) else [])
                  if isinstance(p, dict)]
    error_problems = [p for p in listed if p.get("severity") == "error"]
    reasons.extend(str(p.get("detail") or p.get("check")
                       or "contract check failed") for p in error_problems)
    if reasons:
        status = "fail"
    elif listed:
        status = "pass-with-problems"
        reasons = [str(p.get("detail") or p.get("check")) for p in listed]
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

    # Every step's Cog declaration is checked BEFORE any step is invoked: a
    # spec naming a task the Cog does not declare for the usage audience is
    # an invalid spec, not a mid-run surprise. (This is a declaration check;
    # authority over what a Cog may then do is a separate question — see the
    # contract's §0 amendment.)
    declaration = op_spec.declaration_problems(spec, package_root)
    if declaration:
        raise op_spec.OpSpecError(declaration)

    dependents = spec.dependents()
    statuses, blocked = {}, set()
    for position, step in enumerate(spec.ordered):
        sid = step["id"]
        if sid in blocked:
            track["steps"].append(op_track.step_record(step, "blocked"))
            statuses[sid] = "blocked"
            # A blocked step has no result: its payload is null in the output
            # context, so a mapping over it resolves rather than exploding.
            context["steps"][sid] = {"payload": None, "envelope": None}
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
            # The run stops here: every step it never reached is recorded as
            # `not-reached`, so a Track always lists every step of the spec.
            for later in spec.ordered[position + 1:]:
                track["steps"].append(
                    op_track.step_record(later, "not-reached"))
            track["status"] = "failed"
            track["ended_at"] = op_track.utc_now()
            track_path = op_track.save(track, run_dir)
            return 1, {"ok": False, "status": "failed", "failed_step": sid,
                       "run_dir": str(run_dir), "track": track_path}
        if status == "skipped":
            blocked |= dependents.get(sid, set())
            if step.get("foreach") is None:
                # The step failed its Gate; its payload is not evidence. A
                # foreach step keeps its aggregate (null per failed element).
                payload = None
        context["steps"][sid] = {"payload": payload,
                                 "envelope": envelope_for_context}

    problematic = any(s["status"] in ("passed-with-problems", "skipped",
                                      "blocked") for s in track["steps"])
    track["status"] = "completed-with-problems" if problematic else "completed"
    track["ended_at"] = op_track.utc_now()
    if spec.outputs:
        try:
            track["outputs"] = op_spec.evaluate(spec.outputs, context)
        except op_spec.OpSpecError:
            track["outputs"] = None      # the Track stays readable and final
            op_track.save(track, run_dir)
            raise
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
