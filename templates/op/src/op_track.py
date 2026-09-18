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


def contained(path, base):
    """PATH, checked to resolve INSIDE base. The runner's own control files
    (the Track, grants, pending, decisions, journals) go through this: a
    symlinked parent, or a destination that is itself a symlink out of the
    run, is refused rather than followed (contract §9, review S1).

    This is application-level integrity, not host sandboxing: it keeps the
    runner from writing its own records somewhere else by accident or by a
    planted link — it does not confine the Cogs it invokes."""
    path, base = Path(path), Path(base).resolve()
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; the runner writes its own "
                         f"records, never through a link")
    resolved = path.parent.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"{path} resolves outside the run directory {base}; "
                         f"the runner's records stay inside the run")
    return path


def _fsync_dir(path):
    """Persist a directory ENTRY. `os.replace` is atomic, but the rename
    itself is only durable once the containing directory is synced — without
    this, a power loss can lose a file the process was told it had written."""
    try:
        fd = os.open(str(path), getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:                                    # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:                                    # pragma: no cover
        pass
    finally:
        os.close(fd)


def write_atomic(path, text, base=None):
    """Write TEXT to PATH atomically and durably: a temporary sibling is
    written, flushed and fsynced, then replaces the destination in one step,
    and the containing directory is fsynced so the rename survives a power
    loss. An interrupted write leaves the earlier file exactly as it was.

    `base`, when given, is the run directory the destination must resolve
    inside."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if base is not None:
        contained(path, base)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_json(path, value, base=None):
    """Write one JSON document the same way `write_atomic` writes text."""
    return write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False)
                        + "\n", base=base)


def new_track(spec, run_id, input_request, status="running"):
    return {
        "schema": SCHEMA,
        "op": {"id": spec.id, "version": spec.version},
        "run_id": run_id,
        "status": status,
        "started_at": utc_now(),
        "ended_at": None,
        "input_request": str(Path(input_request).resolve()),
        # Where the request was READ from: relative `$path` operands resolve
        # against it, so a resume must resolve them the same way the first
        # run did (review S4).
        "request_dir": None,
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
    write_json(path, track, base=run_dir)
    return str(path.resolve())
