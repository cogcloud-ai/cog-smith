"""The durable Track (`openteams/op-track [0.1]`) an Op run leaves behind.

Machinery master (cog-smith `templates/op/src/`) — never edited inside an Op
package. The Track is the run's record: the input request, every step
request, every Cog envelope unchanged, the contract-check problems the Cogs
reported, the Gate decision for each step, the model bindings those
envelopes carried, and the output artifacts. It is rewritten after every
step, so a crash leaves a readable partial Track.

Shape lifted from op-video-transcription's track.json and extended with
step status, attempts, and foreach elements.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "openteams/op-track [0.1]"

DEFAULT_RECORDS = ["input_request", "step_requests", "cog_envelopes",
                   "contract_check_problems", "gate_decisions",
                   "model_bindings", "output_artifacts"]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_run_id():
    return (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid.uuid4().hex[:8])


def write_json(path, value):
    """Write one JSON document, atomically: a temporary sibling is written
    and flushed, then replaces the destination in one step. Rewriting the
    Track can therefore never destroy the previous readable one — an
    interrupted write leaves the earlier file exactly as it was."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def new_track(spec, run_id, input_request, status="running"):
    return {
        "schema": SCHEMA,
        "op": {"id": spec.id, "version": spec.version},
        "run_id": run_id,
        "status": status,
        "started_at": utc_now(),
        "ended_at": None,
        "input_request": str(Path(input_request).resolve()),
        "spec_sha256": spec.sha256(),
        "records": (spec.track or {}).get("records") or DEFAULT_RECORDS,
        # Authority (phase 3 §5): the admission this run was started with,
        # every grant it issued (scope and provenance, never a credential),
        # and every time a human resumed it.
        "authority": None,
        "grants": [],
        "resumes": [],
        "steps": [],
    }


#: Step statuses a Track carries. `not-reached` is a step the run never got
#: to because an earlier `on_fail: stop` ended it — recorded so a Track
#: always lists every step of the spec. `denied` is a step whose grant was
#: refused, so it was never invoked; `awaiting-decision` is a human-gated
#: step whose proposals are waiting for a person; `running` is a step a
#: crash interrupted, which a resume runs again.
STEP_STATUSES = ("passed", "passed-with-problems", "failed", "skipped",
                 "blocked", "planned", "not-reached", "denied", "running",
                 "awaiting-decision")

#: Run statuses. `paused` is a run waiting on a human Gate.
RUN_STATUSES = ("planned", "running", "paused", "completed",
                "completed-with-problems", "failed")


def step_record(step, status, **fields):
    """One step's record. Paths are absolute; unknown-to-this-run fields are
    present and null rather than absent, so a partial Track reads the same
    way as a complete one."""
    cog = step.get("cog") or {}
    record = {
        "id": step["id"],
        "status": status,
        "cog": {"id": cog.get("id"), "version": cog.get("version")},
        "task": cog.get("task"),
        "request": None,
        "envelope": None,
        "binding": None,
        "problems": [],
        "gate": None,
        "elapsed_s": None,
        "attempts": [],
        "elements": None,
        # Authority (phase 3 §5): the grant this step was issued, the
        # journal it recorded its external effects in, what it reported
        # attempting, and — for a human Gate — the decision it carries.
        "grant": None,
        "journal": None,
        "authority_use": None,
        "decision": None,
    }
    record.update(fields)
    return record


def save(track, run_dir):
    """Rewrite track.json; returns its absolute path."""
    path = Path(run_dir) / "track.json"
    write_json(path, track)
    return str(path.resolve())
