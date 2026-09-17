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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


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
        "steps": [],
    }


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
    }
    record.update(fields)
    return record


def save(track, run_dir):
    """Rewrite track.json; returns its absolute path."""
    path = Path(run_dir) / "track.json"
    write_json(path, track)
    return str(path.resolve())
