#!/usr/bin/env python3
"""The Op runner: run the declared steps of this package's op.yaml.

    python src/op_runner.py --request examples/request.json [--dry-run]
                            [--runs-dir DIR] [--authority FILE]
    python src/op_runner.py --resume RUN_DIR [--decision FILE]

Machinery master (cog-smith `templates/op/src/`) — never edited inside an Op
package. The runner is the Op layer: it sequences steps, builds each step's
request from the spec's mapping expressions, invokes each Cog ONLY through
the usage task that Cog declares, gates the envelope it gets back, and
writes the durable Track. It never imports a Cog's Python and never calls a
model itself.

Gate semantics are lifted from op-video-transcription/src/run_op.py: three
states (pass, pass-with-problems, fail) with the reasons listed. The Gate
decides; the Cog never decides its own acceptance.

Authority (phase 3): a step declares what it `requires`, the runner issues
the grant immediately before the invocation — from the run's admission
(`--authority`) for a read, and from a human Gate's decision for a write —
and passes `--grant`, `--run-id` and `--journal` to the Cog. The Cog checks
that grant itself before it reaches outside the run: this runner sequences
and records, it does not enforce a restricted environment (contract §0).

A step with `gate: {policy: human}` pauses the run once its Cog has passed
the ordinary envelope Gate: the proposed changes are written to
`pending/<step>.json` (and a readable `.md`), the Track goes `paused`, and
the process exits 3. `--resume RUN_DIR --decision FILE` applies the human's
answer and carries on; steps that already passed are never re-run.

One run, one process: `runs/<run_id>/run.lock` is locked with `flock` at the
start and on every resume, so two resumes of the same paused run cannot both
accept the decision and both write. The lock is the open DESCRIPTOR, held for
the process lifetime and inherited by every Cog this runner launches; a run
another process holds is refused by name (exit 2), and the kernel releases
the lock when the last holder exits — there is no pid to parse and no stale
lock to take over.

Durability order, so the Track on disk always says what was attempted before
anything external could happen: accept the decision → record the decision and
the resume, save → issue the grant, create the journal, record the step
`running`, save → invoke.

Exit codes: 0 completed (or completed-with-problems, or a planned dry run),
1 failed, 2 an invalid spec, request or authority document (and a run another
process holds), 3 paused for a human. Stdout is one JSON object.

A run validates every step's Cog declaration before it creates anything (so
a refused declaration leaves no run directory and no half-open Track), and a
run that stops records the steps it never reached as `not-reached`, so the
Track always lists every step of the spec.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import op_spec    # noqa: E402
import op_track   # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------- the Cog seam ----

#: How much of a failed process's output is kept as evidence.
EVIDENCE_TAIL = 2000


def _tail(text):
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL:]


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
#: created Cog, so an Op must be able to call one.
REQUEST_FLAGS = ("--request", "--bundle")

#: argparse's exit code for a command line it could not parse.
ARGPARSE_EXIT = 2


def _rejected_flag(completed, flag):
    """True when the Cog's CLI refused FLAG by name WITHOUT DOING ANY WORK,
    so trying the next flag cannot run an effectful Cog twice (contract §0).

    Three conditions, all required: argparse's own exit code (2), the
    diagnostic on STDERR (where argparse writes it) naming this exact flag,
    and no envelope anywhere on stdout. An exit-1 envelope whose error detail
    happens to quote "unrecognized arguments: --request" is a RESULT, not a
    rejection — the Cog already ran."""
    if completed.returncode != ARGPARSE_EXIT:
        return False
    if f"unrecognized arguments: {flag}" not in (completed.stderr or ""):
        return False
    try:
        parse_envelope(completed.stdout or "")
    except ValueError:
        return True
    return False                 # it produced a result: never re-invoke


def envelope_problems(value):
    """Why VALUE is not an envelope v1 the Gate can decide about. Field TYPES
    are checked, not merely field presence: an `ok` that is the string
    "false", or `problems` that are bare strings, are malformed output, not a
    result — they become a controlled invocation failure with the output kept
    as evidence."""
    if not isinstance(value, dict):
        return ["the Cog's result is not a JSON object."]
    problems = []
    version = value.get("envelope")
    if type(version) is not int or version != 1:
        # `type(x) is int`, not isinstance: True == 1 in Python, and
        # `{"envelope": true}` is malformed output, not envelope v1.
        problems.append(f"the Cog's result declares envelope "
                        f"{version!r}, not envelope v1.")
    if not isinstance(value.get("ok"), bool):
        problems.append(f"the Cog's result declares ok {value.get('ok')!r}, "
                        f"which is not true or false.")
    listed = value.get("problems")
    if not isinstance(listed, list) or any(not isinstance(p, dict)
                                           for p in listed):
        # `problems` is REQUIRED: missing or null is malformed, not an empty
        # list the Gate may assume.
        problems.append("the Cog's result declares problems that are not a "
                        "list of problem objects.")
    return problems


def failed_envelope(cog_dir, task, detail, raw=None, carried=None,
                    evidence=None):
    """A synthetic ok:false envelope for an invocation that never produced a
    usable result. What the Cog did emit is kept in `raw` as evidence; when
    that output was a well-formed envelope, its identity, binding and
    problems are carried across rather than thrown away.

    `error.evidence` is the ONE place process evidence lives, on EVERY
    synthetic failure: `{command, returncode, stdout_tail, stderr_tail}` and,
    when the seam tried the other request flag first, `previous_attempts`
    with the same fields (BUILDING_OPS, "When a Cog invocation fails")."""
    carried = carried if isinstance(carried, dict) else {}
    return {
        "envelope": 1,
        "cog": carried.get("cog") or {"id": f"unavailable:{Path(cog_dir).name}",
                                      "version": None},
        "task": task,
        "ok": False,
        "error": {"code": "invocation-failed", "detail": detail,
                  "evidence": evidence},
        "payload": None,
        "raw": raw,
        "problems": [p for p in carried.get("problems") or []
                     if isinstance(p, dict)],
        "binding": carried.get("binding"),
        "timing": {"latency_s": 0},
    }


def _evidence(command, returncode, stdout, stderr, previous):
    """One attempt's process evidence: what was run, how it exited, and the
    tail of each stream. Captured once, kept on every synthetic failure."""
    record = {
        "command": list(command),
        "returncode": returncode,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
    if previous:
        record["previous_attempts"] = list(previous)
    return record


def invoke_cog(cog_dir, task, request_path, grant_path=None, run_id=None,
               journal_path=None):
    """Run one declared Cog task and return its envelope.

    `grant_path`, `run_id` and `journal_path` are the invocation context a
    granted step gets (phase 3 §2): they are passed as `--grant`, `--run-id`
    and `--journal` BESIDE the request, never inside it, and only for a step
    the runner issued a grant to.

    Anything short of a well-formed envelope from a process that exited 0 is
    an invocation failure with a synthetic ok:false envelope, so the Gate
    always has something to decide about: a command that could not be
    launched, a nonzero exit (even after printing an envelope — a process
    that dies at 139 has not succeeded), output with no envelope, and a
    malformed envelope all arrive the same way.

    The run lock's descriptor is passed to the child (`pass_fds`): while a
    Cog is running, the run stays locked even if this runner dies (contract
    §9b)."""
    cog_dir = Path(cog_dir)
    inherited = tuple(fd for fd in (LOCK_FD,) if fd is not None)
    previous = []
    context = []
    if grant_path:
        context += ["--grant", str(grant_path)]
    if run_id:
        context += ["--run-id", str(run_id)]
    if journal_path:
        context += ["--journal", str(journal_path)]
    for index, flag in enumerate(REQUEST_FLAGS):
        command = [
            "pixi", "run", "--manifest-path", str(cog_dir / "pixi.toml"),
            task, "--", flag, str(request_path), *context,
        ]
        try:
            completed = subprocess.run(command, text=True, capture_output=True,
                                       pass_fds=inherited)
        except OSError as exc:
            return failed_envelope(
                cog_dir, task,
                f"could not launch {command[0]!r} for task {task!r}: {exc}",
                evidence=_evidence(command, None, "", "", previous))
        if (not _rejected_flag(completed, flag)
                or index == len(REQUEST_FLAGS) - 1):
            break
        # The CLI refused this flag before doing any work; the next flag is
        # tried, and this attempt stays as evidence.
        previous.append(_evidence(command, completed.returncode,
                                  completed.stdout, completed.stderr, []))
    stdout = completed.stdout or ""
    stderr = (completed.stderr or "").strip()
    evidence = _evidence(command, completed.returncode, stdout, stderr,
                         previous)
    try:
        envelope = parse_envelope(stdout)
    except ValueError as exc:
        detail = stderr or stdout.strip() or str(exc)
        return failed_envelope(cog_dir, task, detail, raw=stdout,
                               evidence=evidence)
    malformed = envelope_problems(envelope)
    if malformed:
        return failed_envelope(cog_dir, task, " ".join(malformed), raw=envelope,
                               evidence=evidence)
    if completed.returncode != 0:
        detail = (f"the Cog command for task {task!r} exited "
                  f"{completed.returncode}")
        if stderr:
            detail += f": {stderr[-400:]}"
        return failed_envelope(cog_dir, task, detail, raw=envelope,
                               carried=envelope, evidence=evidence)
    return envelope


def gate_envelope(envelope):
    """The Gate: a three-state decision over one envelope, with reasons."""
    reasons = []
    malformed = envelope_problems(envelope)
    if malformed:
        # The Gate decides about envelope v1 and nothing else: a result whose
        # required fields are the wrong TYPE never reaches the policy.
        return {
            "policy": op_spec.GATE_POLICY, "status": "fail",
            "reasons": ["Cog result is not envelope v1."] + malformed,
            "decided_at": op_track.utc_now(), "guards": [],
        }
    if envelope.get("ok") is not True:
        error = envelope.get("error") or {}
        error = error if isinstance(error, dict) else {}
        reasons.append(
            f"Cog invocation failed: {error.get('code', 'unknown')}: "
            f"{error.get('detail', '')}".rstrip())
    # `envelope_problems` above has already established that `problems` is a
    # list of objects: the policy reads it without re-checking its type.
    listed = envelope.get("problems") or []
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


# ----------------------------------------------- the authority documents --
#
# Three documents, all validated by their `schema` string BY NAME. None of
# them carries a credential, and none of them can be built by a mapping
# expression: authority is trusted invocation context, separate from the
# request document (phase 2 contract §0, phase 3 §2).

AUTHORITY_SCHEMA = "openteams/op-authority [0.1]"
GRANT_SCHEMA = "openteams/op-grant [0.1]"
PENDING_SCHEMA = "openteams/op-pending-decision [0.1]"
DECISION_SCHEMA = "openteams/op-decision [0.1]"
VERDICTS = ("approve", "reject", "edit")

#: The exit code of a run that paused for a human.
PAUSED_EXIT = 3


class Denied(Exception):
    """Why a step was denied its grant. The step is recorded `denied` and
    never invoked; `on_fail` then applies as it does for a failure."""


#: One run, one process (contract §9, §9b). Two resumes of the same paused
#: run would each accept the decision, issue the same grant, read the same
#: empty journal, and apply the same change twice: atomically replacing the
#: Track orders the WRITES, not the executions. The lock does.
LOCK_NAME = "run.lock"

#: The open descriptor of the lock this process holds, if any. It is passed
#: to every Cog subprocess (`pass_fds`), so the lock outlives a runner that
#: dies with a Cog still running: the kernel drops it only when the LAST
#: holder exits (contract §9b).
LOCK_FD = None


class RunLock:
    """An advisory `flock` on one run directory's `run.lock`, held for the
    process lifetime — taken BEFORE the Track is read, so two processes
    cannot even reach the same decision.

    The lock is the OPEN DESCRIPTOR, not the file's content: there is no pid
    to parse, nothing to take over, and nothing to unlink. A process that
    dies releases it because the kernel closes its descriptors; a process
    that is alive holds it because the kernel says so. The file's JSON (pid,
    time) is INFORMATIONAL — it says who to look for, and is never the
    thing consulted to decide (review finding 1).

    The lock FILE is a control file like the Track: contained in the run
    and opened `O_NOFOLLOW`, so it is never a link to something else
    (contract §9c).

    The descriptor is inherited by every Cog this runner launches, so a
    runner killed mid-invocation keeps the run locked until its Cog is
    finished too: the lock protects the whole execution, including an
    outstanding external effect."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / LOCK_NAME
        self.fd = None
        self.held = False

    def acquire(self):
        """Take the lock, then say who holds it.

        The lock file is a CONTROL FILE, opened like every other one: the
        path is contained in the run directory (no link out, no alias
        inside), and the open itself is `O_NOFOLLOW`, so `run.lock` pointing
        at `track.json` is refused rather than followed and truncated
        (contract §9c, review 3 finding 3). Containment is checked before
        the descriptor exists; `O_NOFOLLOW` closes the window between the
        check and the open.

        The metadata is written only AFTER the lock is held, and a failure
        while writing it releases the descriptor before re-raising: an
        embedding process that catches the exception is not left holding a
        lock it does not know about."""
        global LOCK_FD
        op_track.ensure_dir(self.path.parent)
        try:
            op_track.contained(self.path, self.run_dir)
        except ValueError as exc:
            raise op_spec.OpSpecError(
                f"the run lock {self.path} is not a real file inside the run "
                f"({exc}); the lock is a control file of the run and is "
                f"never opened through a link.")
        try:
            fd = os.open(str(self.path),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise op_spec.OpSpecError(
                    f"{self.path} is a symlink; the run lock is a control "
                    f"file of the run and is never opened through a link.")
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._holder(fd)
            os.close(fd)
            raise op_spec.OpSpecError(
                f"run {self.path.parent.name} is already running as pid "
                f"{holder.get('pid')} (since {holder.get('at')}); one run, "
                f"one process — wait for that process to finish.")
        self.fd, self.held, LOCK_FD = fd, True, fd
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": os.getpid(),
                                     "at": op_track.utc_now()}).encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:                            # pragma: no cover
                pass
        except BaseException:
            self.release()
            raise
        return self

    def _holder(self, fd):
        """Who the lock file SAYS is running. Informational only: a lock
        file that is empty, truncated or not JSON at all still locks."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            value = json.loads(os.read(fd, 4096).decode("utf-8", "replace"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def release(self):
        """Close the descriptor; the kernel releases the lock when the last
        holder — this process or a Cog that inherited it — is gone. The file
        stays: unlinking it would let a second process create a NEW file and
        lock that instead."""
        global LOCK_FD
        if self.fd is not None:
            if LOCK_FD == self.fd:
                LOCK_FD = None
            os.close(self.fd)
            self.fd, self.held = None, False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(value):
    """The hash of a JSON value, canonically serialized — the same bytes for
    the same document however it was written."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: A change carries TWO hashes, and they answer different questions
#: (contract §9). `content_sha256` is the hash of the change OBJECT — what
#: the human approved, recomputed on an edit, checked by the runner at
#: issuance. `target_sha256` is the content hash of the TARGET ITEM as the Op
#: read it — the staleness precondition the write Cog checks against a fresh
#: fetch, and which an edit never changes. Neither may be null.
CHANGE_HASHES = ("content_sha256", "target_sha256")


def change_content_sha256(change):
    """The content hash of a proposed change: everything about it EXCEPT the
    two hash fields. An edited change is re-hashed with this, and that is
    what the write grant carries as `content_sha256`."""
    return canonical_sha256({k: v for k, v in change.items()
                             if k not in CHANGE_HASHES})


#: A content hash is 64 hexadecimal characters. Checked, not assumed: a
#: grant that carried `target_sha256: "yes"` would authorize a write whose
#: staleness precondition no fetch can ever match — or, worse, one a Cog
#: comparing loosely would treat as satisfied (contract §9b).
#: `fullmatch`, never `match`: with `re.match`, `"<64 hex>\n"` passed —
#: `$` also matches before a trailing newline, so a digest with a newline
#: glued to it was accepted as a content hash (review 3, finding 1).
HEX64 = re.compile(r"[0-9a-f]{64}")


def hex64_problem(value, field, where):
    """Why VALUE is not a content hash, or None."""
    if not isinstance(value, str) or not value:
        return (f"{where} carries {field} {value!r}; a change carries both "
                f"hashes, and neither may be null")
    if not HEX64.fullmatch(value):
        return (f"{where} carries {field} {value!r}, which is not a sha256 "
                f"(64 hex characters)")
    return None


def _document(path, schema, what):
    doc = op_spec.load_document(path)
    if not isinstance(doc, dict):
        raise op_spec.OpSpecError(f"{Path(path).name} is not a {what} "
                                  f"document.")
    if doc.get("schema") != schema:
        raise op_spec.OpSpecError(
            f"{Path(path).name} declares schema {doc.get('schema')!r}; a "
            f"{what} is {schema!r}.")
    return doc


def load_authority(path):
    """The run's ADMISSION: the owner's authority for the whole run, in the
    same operation shape as a grant. `write` operations carry the
    repositories writes may EVER touch — never change ids, which only a
    human decision can name."""
    doc = _document(path, AUTHORITY_SCHEMA, "run admission")
    operations = doc.get("operations")
    problems = []
    if not isinstance(operations, list) or not operations:
        raise op_spec.OpSpecError(
            f"{Path(path).name} admits no operations; a run admission is a "
            f"list of {{resource, action, repositories}} operations.")
    for index, operation in enumerate(operations):
        where = f"the admission's operations[{index}]"
        if not isinstance(operation, dict):
            problems.append(f"{where} must be an object.")
            continue
        for field in ("resource", "action"):
            if not isinstance(operation.get(field), str) or not operation[field]:
                problems.append(f"{where} declares {field} "
                                f"{operation.get(field)!r}; it is a string.")
        repositories = operation.get("repositories")
        if not isinstance(repositories, list) or any(
                not isinstance(r, str) for r in repositories):
            problems.append(f"{where} declares repositories "
                            f"{repositories!r}; an admitted operation names "
                            f"the repositories it may touch.")
        if "changes" in operation:
            problems.append(f"{where} names changes; an admission admits "
                            f"repositories, and only a human decision names "
                            f"change ids.")
    if problems:
        raise op_spec.OpSpecError(problems)
    return doc


def admitted_repositories(authority, resource, action):
    """The repositories the run was admitted to touch for one operation."""
    out = set()
    for operation in (authority or {}).get("operations") or []:
        if (operation.get("resource") == resource
                and operation.get("action") == action):
            out |= {r for r in operation.get("repositories") or []}
    return out


def admission_problems(spec, authority):
    """Why this run may not start: a step requires authority the run was
    never admitted to have. Refused at LOAD, before anything is created."""
    problems = []
    for step in spec.ordered:
        for requirement in op_spec.requirements(step):
            if not isinstance(requirement, dict):
                continue
            resource = requirement.get("resource")
            action = requirement.get("action")
            if authority is None:
                problems.append(
                    f"step {step['id']!r} requires {resource} {action}; the "
                    f"run was admitted with none (pass --authority FILE).")
            elif not any(o.get("resource") == resource
                         and o.get("action") == action
                         for o in authority.get("operations") or []):
                problems.append(
                    f"step {step['id']!r} requires {resource} {action}, which "
                    f"this run's admission does not carry.")
    return problems


# ------------------------------------------------------------- the grant --

def grant_document(step, run_id, operations, provenance, ttl_minutes,
                   cog_version=None, index=0):
    expires = (datetime.now(timezone.utc)
               + timedelta(minutes=float(ttl_minutes)))
    sid = step["id"]
    return {
        "schema": GRANT_SCHEMA,
        "grant_id": f"{run_id}/{sid}/{index}",
        "run_id": run_id,
        "recipient": {"step": sid,
                      "cog": {"id": (step.get("cog") or {}).get("id"),
                              "version": cog_version
                              or (step.get("cog") or {}).get("version")}},
        "issued_at": op_track.utc_now(),
        "issued_by": provenance,
        "operations": operations,
        "valid": {"expires_at": expires.isoformat(), "run_id": run_id},
    }


def _change_id(value):
    """The change id of a requested entry, as a STRING, or None. A list- or
    object-valued id is not an id: it is refused by name rather than raising
    an unhashable-value error inside a set lookup (review S7)."""
    cid = value.get("change_id") if isinstance(value, dict) else value
    return cid if isinstance(cid, str) and cid else None


def granted_change(change):
    """What a grant carries about one approved change: its id, the
    repository it touches, and BOTH hashes. Every field is required and none
    may be null — a grant that fails open on a hash authorizes anything."""
    entry = {"change_id": change.get("change_id"),
             "repository": change.get("repository")}
    for field in CHANGE_HASHES:
        entry[field] = change.get(field)
    return entry


def _next_grant_index(run_dir, sid):
    """The next issuance number for this step. Every issuance gets its OWN
    id and file: a reissue after an interruption never overwrites the grant
    an earlier Track entry points at (review S6)."""
    directory = Path(run_dir) / "grants" / sid
    index = 0
    while (directory / f"{index}.json").exists():
        index += 1
    return index


def cog_version(step, package_root):
    """The recipient's version: the spec's when it states one, otherwise the
    version the Cog's own manifest declares — a grant names the recipient it
    has, never a null it could have read (review S6)."""
    declared = (step.get("cog") or {}).get("version")
    if declared:
        return declared
    source = (step.get("cog") or {}).get("source")
    if not package_root or not isinstance(source, str):
        return None
    manifest, _ = op_spec.read_cog_manifest(Path(package_root) / source)
    return (manifest or {}).get("version")


def issue_grant(step, context, authority, spec, run_id, run_dir, decisions,
                package_root=None):
    """The grant for a step that requires authority, written into the run.

    Raises `Denied` with the reason when the run's admission does not cover
    a read, or when a write asks for anything the human did not approve.
    Requesting more than was approved is a DENIAL, never a trim."""
    declared = op_spec.requirements(step)
    if not declared:
        return None, None
    sid = step["id"]
    operations, provenance = [], {"kind": "admission"}
    for requirement in declared:
        resource = requirement.get("resource")
        action = requirement.get("action")
        if action == "write":
            target = op_spec.decision_step(requirement)
            record = (decisions or {}).get(target)
            if not record:
                raise Denied(f"step {sid!r} requires a {resource} write "
                             f"authorized by step {target!r}, which produced "
                             f"no human decision")
            path = record.get("decision")
            if not path or not Path(path).exists():
                raise Denied(f"the decision record for step {target!r} is "
                             f"missing; no write grant can be issued")
            if sha256_file(path) != record.get("decision_sha256"):
                raise Denied(f"the decision record {Path(path).name} changed "
                             f"since the Track recorded it; no write grant "
                             f"can be issued")
            approved = {c["change_id"]: c
                        for c in record["value"].get("approved") or []
                        if isinstance(c, dict) and _change_id(c)}
            requested = op_spec.evaluate(requirement.get("changes"), context)
            if requested is not None and not isinstance(requested, list):
                raise Denied(f"step {sid!r} requires a write of {requested!r}, "
                             f"which is not a list of changes")
            for change in requested or []:
                cid = _change_id(change)
                if cid is None:
                    raise Denied(f"step {sid!r} requires a write for "
                                 f"{change!r}, which names no change id")
                if cid not in approved:
                    raise Denied(f"step {sid!r} requires a write for change "
                                 f"{cid!r}, which the human did not approve")
                if isinstance(change, dict) and change.get("content_sha256") \
                        != approved[cid].get("content_sha256"):
                    raise Denied(f"change {cid!r} was approved against other "
                                 f"content than the one requested")
            admitted = admitted_repositories(authority, resource, "write")
            for cid, change in approved.items():
                repository = change.get("repository")
                if not isinstance(repository, str) or repository not in admitted:
                    raise Denied(f"change {cid!r} targets repository "
                                 f"{repository!r}, which this run was not "
                                 f"admitted to write")
                for field in CHANGE_HASHES:
                    problem = hex64_problem(change.get(field), field,
                                            f"change {cid!r}")
                    if problem:
                        raise Denied(problem)
                # The runner recomputes what it is about to authorize: the
                # grant's `content_sha256` is the hash of THIS object, not a
                # digest copied along with it (contract §9b, finding 2).
                recomputed = change_content_sha256(change)
                if change["content_sha256"] != recomputed:
                    raise Denied(f"change {cid!r} carries content_sha256 "
                                 f"{change['content_sha256']!r}, but its "
                                 f"content hashes to {recomputed!r}; the "
                                 f"approved change is not the one recorded")
            # EXACTLY the approved list, never the requested one.
            operations.append({
                "resource": resource, "action": "write",
                "changes": [granted_change(c)
                            for c in record["value"].get("approved") or []],
            })
            provenance = {"kind": "gate", "step": target,
                          "decision": str(Path(path).resolve()),
                          "decision_sha256": record.get("decision_sha256")}
        else:
            requested = op_spec.evaluate(requirement.get("repositories"),
                                         context) or []
            if not isinstance(requested, list) or any(
                    not isinstance(r, str) for r in requested):
                raise Denied(f"step {sid!r} requires {resource} {action} of "
                             f"{requested!r}, which is not a list of "
                             f"repositories")
            admitted = admitted_repositories(authority, resource, action)
            outside = [r for r in requested if r not in admitted]
            if outside:
                raise Denied(f"step {sid!r} requires {resource} {action} of "
                             f"{outside}, which this run's admission does not "
                             f"cover")
            operations.append({"resource": resource, "action": action,
                               "repositories": list(requested)})
    index = _next_grant_index(run_dir, sid)
    grant = grant_document(step, run_id, operations, provenance,
                           spec.ttl_minutes,
                           cog_version=cog_version(step, package_root),
                           index=index)
    path = Path(run_dir) / "grants" / sid / f"{index}.json"
    op_track.write_json(path, grant, base=run_dir)
    return grant, path


def grant_record(grant, path):
    """What the Track keeps about a grant: its scope and its provenance —
    never a credential, because a grant carries none."""
    return {
        "grant_id": grant["grant_id"],
        "step": grant["recipient"]["step"],
        "path": str(Path(path).resolve()),
        "operations": [{"resource": o.get("resource"),
                        "action": o.get("action"),
                        "count": len(o.get("repositories")
                                     or o.get("changes") or [])}
                       for o in grant["operations"]],
        "issued_by": grant["issued_by"],
    }


# -------------------------------------------------------- the human gate --

def pending_changes(payload, sid):
    """The proposed changes in a human-gated step's payload.

    Change ids are unique STRINGS. Two proposals sharing an id would collapse
    into one entry the moment they were indexed, so one approval would
    silently authorize both: a duplicate refuses the pause (review S2).

    HASHES ARE NEVER REPAIRED (contract §9c). Every proposal states its own
    `content_sha256`, and here — at the pause, before a human ever sees it —
    that digest must EQUAL the canonical hash of the object it arrived on,
    and `target_sha256` must be 64 hex characters. A proposal carrying
    `"content_sha256": "placeholder"` used to pass the pause and be
    laundered into a valid digest at approval; now the pause is refused by
    name (review 3, finding 1)."""
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if not isinstance(changes, list) or any(
            not isinstance(c, dict) or not isinstance(c.get("change_id"), str)
            or not c["change_id"] for c in changes):
        raise op_spec.OpSpecError(
            f"Op step {sid!r} has a human Gate, so its payload must carry a "
            f"`changes` list of objects with a change_id (a string): that is "
            f"what the human decides about.")
    seen, duplicates, unhashed, misstated = set(), [], [], []
    for change in changes:
        cid = change["change_id"]
        if cid in seen and cid not in duplicates:
            duplicates.append(cid)
        seen.add(cid)
        value = change.get("content_sha256")
        if not isinstance(value, str) or not value:
            unhashed.append(cid)
        elif not HEX64.fullmatch(value):
            misstated.append((cid, "content_sha256", value,
                              "which is not a sha256 (64 hex characters)"))
        elif value != change_content_sha256(change):
            misstated.append((cid, "content_sha256", value,
                              f"but its content hashes to "
                              f"{change_content_sha256(change)!r}"))
        target = change.get("target_sha256")
        if not isinstance(target, str) or not HEX64.fullmatch(target):
            misstated.append((cid, "target_sha256", target,
                              "which is not a sha256 (64 hex characters); a "
                              "change states the content hash of the item it "
                              "modifies"))
    if unhashed:
        # A proposal with no content hash is not repaired into one: the hash
        # is what the human's approval is ABOUT, so a Cog that states none
        # has not produced something decidable (contract §9b, finding 2).
        raise op_spec.OpSpecError(
            f"Op step {sid!r} proposes change(s) {unhashed} with no "
            f"content_sha256; a proposed change states the hash of its own "
            f"content, and the runner never invents one for it.")
    if duplicates:
        raise op_spec.OpSpecError(
            f"Op step {sid!r} proposes change id(s) {duplicates} more than "
            f"once; a human decides about each change exactly once, so change "
            f"ids are unique.")
    if misstated:
        # Refused, never recomputed: the digest a proposal states is the
        # thing the approval is ABOUT, so a wrong one is a wrong proposal
        # (contract §9c).
        raise op_spec.OpSpecError(
            [f"Op step {sid!r} proposes change {cid!r} carrying {field} "
             f"{value!r}, {why}; hashes are checked at the pause and never "
             f"repaired."
             for cid, field, value, why in misstated])
    return changes


def render_pending(doc):
    """The human's copy: one line per change."""
    lines = [f"# Decision needed: {doc['step']}", "",
             f"Run: {doc['run_id']}", f"Asked: {doc['asked_at']}", "",
             f"Decide with: `{doc['decide_with']}`", "",
             "| change_id | kind | target | summary |",
             "|---|---|---|---|"]
    for change in pending_changes(doc["payload"], doc["step"]):
        lines.append("| {} | {} | {} | {} |".format(
            change.get("change_id"), change.get("kind", ""),
            change.get("target", ""),
            " ".join(str(change.get("summary", "")).split())))
    return "\n".join(lines) + "\n"


def write_pending(run_dir, run_id, sid, payload):
    """The pause: the pending document and its rendered twin."""
    changes = pending_changes(payload, sid)
    doc = {
        "schema": PENDING_SCHEMA,
        "run_id": run_id,
        "step": sid,
        "payload": payload,
        "payload_sha256": canonical_sha256(payload),
        "asked_at": op_track.utc_now(),
        "decide_with": (f"op run --resume {Path(run_dir).resolve()} "
                        f"--decision <file>"),
    }
    json_path = Path(run_dir) / "pending" / f"{sid}.json"
    md_path = Path(run_dir) / "pending" / f"{sid}.md"
    op_track.write_json(json_path, doc, base=run_dir)
    # The human's copy is written the same way as the JSON: a half-written
    # decision sheet is a half-read decision (review S9).
    op_track.write_atomic(md_path, render_pending(doc), base=run_dir)
    del changes
    return doc, json_path, md_path


def load_decision(path):
    return _document(path, DECISION_SCHEMA, "human decision")


def normalized_change(change):
    """An EDITED change with its `content_sha256` recomputed from its own
    content.

    Only an edit is re-hashed. An approval or a rejection carries the digest
    the proposal stated — checked at the pause against the object it arrived
    on, and checked again at issuance — because recomputing it here would
    turn any digest, however wrong, into a valid-looking one (contract §9c,
    review 3 finding 1)."""
    change = dict(change)
    change["content_sha256"] = change_content_sha256(change)
    return change


def edited_change_problems(edit, proposed, where):
    """Why an edited change is not the SAME KIND of change as the one it
    replaces. An edit may change content; it may not change what the change
    IS, the item it targets, or the state it was approved against (review
    S2, contract §9)."""
    problems = []
    unknown = sorted(str(k) for k in set(edit) - set(proposed))
    if unknown:
        problems.append(f"{where} adds key(s) {unknown} the proposed change "
                        f"does not carry; an edit edits the proposal.")
    for key, value in proposed.items():
        if key not in edit:
            problems.append(f"{where} drops {key!r}; an edited change carries "
                            f"the same fields as the one it replaces.")
        elif key != "content_sha256" and type(edit[key]) is not type(value):
            problems.append(f"{where} declares {key} {edit[key]!r}, which is "
                            f"not the type the proposed change declares.")
    if "target_sha256" in proposed and \
            edit.get("target_sha256") != proposed.get("target_sha256"):
        problems.append(f"{where} changes target_sha256; an edit changes what "
                        f"is written, never the target state it was approved "
                        f"against.")
    if "repository" in proposed and \
            edit.get("repository") != proposed.get("repository"):
        problems.append(f"{where} retargets the change to repository "
                        f"{edit.get('repository')!r}; an edit stays on the "
                        f"item that was proposed.")
    return problems


def apply_decision(pending, decision):
    """The decision, checked against what was actually proposed, as
    {approved, rejected, edited, history}.

    The pending PAYLOAD is re-hashed here and the decision is checked against
    that hash, never against the hash string the pending file carries beside
    it: otherwise editing the proposals and leaving the old hash in place
    would pass an old approval off as a decision about the new ones (review
    B5).

    Every proposed change gets exactly one decision; a decision about
    anything else is refused; an edited change is re-hashed from its edited
    content, and THAT hash is what a write grant will carry. A decision can
    only select among the proposed changes: it can never add one."""
    problems = []
    actual_sha256 = canonical_sha256(pending.get("payload"))
    if decision.get("run_id") != pending["run_id"]:
        problems.append(f"the decision is for run {decision.get('run_id')!r}, "
                        f"not for {pending['run_id']!r}.")
    if decision.get("step") != pending["step"]:
        problems.append(f"the decision is about step "
                        f"{decision.get('step')!r}, not about "
                        f"{pending['step']!r}.")
    if decision.get("payload_sha256") != actual_sha256:
        problems.append("the decision's payload_sha256 does not match the "
                        "pending payload; the human decided about something "
                        "else.")
    for field in ("decided_by", "decided_at"):
        # `strip()`: a whitespace-only identity names nobody, and a Track
        # that records one says nothing about who decided (review S2).
        if not isinstance(decision.get(field), str) or not decision[field].strip():
            problems.append(f"the decision declares {field} "
                            f"{decision.get(field)!r}; a decision record says "
                            f"who decided and when.")
    when = decision.get("decided_at")
    if isinstance(when, str) and when.strip():
        try:
            datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"the decision declares decided_at {when!r}, "
                            f"which is not a timestamp; a durable decision "
                            f"record says WHEN it was made.")
    if problems:
        raise op_spec.OpSpecError(problems)

    proposed = {c["change_id"]: c
                for c in pending_changes(pending["payload"], pending["step"])}
    decisions = decision.get("decisions")
    if not isinstance(decisions, list):
        raise op_spec.OpSpecError("the decision document declares no "
                                  "decisions list.")
    seen, approved, rejected, edited, history = set(), [], [], [], []
    for index, entry in enumerate(decisions):
        where = f"decisions[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be an object with a change_id and "
                            f"a verdict.")
            continue
        cid = entry.get("change_id")
        if not isinstance(cid, str) or cid not in proposed:
            problems.append(f"{where} decides about change {cid!r}, which "
                            f"step {pending['step']!r} never proposed.")
            continue
        if cid in seen:
            problems.append(f"{where} is a second decision about change "
                            f"{cid!r}; every change gets exactly one.")
            continue
        seen.add(cid)
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            problems.append(f"{where} declares verdict {verdict!r}; the "
                            f"verdicts are {list(VERDICTS)}.")
            continue
        if verdict == "approve":
            # The SUPPLIED digest, preserved: it is what the pause checked
            # and what the human approved (contract §9c).
            change = dict(proposed[cid])
            approved.append(change)
        elif verdict == "reject":
            change = dict(proposed[cid])
            rejected.append(cid)
        else:
            change = entry.get("change")
            if not isinstance(change, dict) or change.get("change_id") != cid:
                problems.append(f"{where} is an edit without the edited "
                                f"change (the same change_id).")
                continue
            edit_problems = edited_change_problems(change, proposed[cid],
                                                   f"{where}'s edited change")
            if edit_problems:
                problems.extend(edit_problems)
                continue
            change = normalized_change(change)
            approved.append(change)
            edited.append(cid)
        # The durable decision history: every proposal with its verdict and
        # the hashes that were current when it was decided.
        history.append({"change_id": cid, "verdict": verdict,
                        "content_sha256": change["content_sha256"],
                        "target_sha256": change.get("target_sha256"),
                        "reason": entry.get("reason")})
    missing = sorted(set(proposed) - seen)
    if missing:
        problems.append(f"the decision leaves {missing} undecided; every "
                        f"proposed change needs exactly one decision.")
    if problems:
        raise op_spec.OpSpecError(problems)
    return {"approved": approved, "rejected": rejected, "edited": edited,
            "history": history}


# ------------------------------------------------------------- the run ---

def _attempt(cog_dir, task, request_path, envelope_path, on_fail, seam=None):
    """Invoke once, gate, and retry exactly once when the failure was a
    transport/model failure (ok:false) and the step asked for retry-once.
    An error-severity problem in an ok envelope is never retried.

    `seam` carries the invocation context a granted step gets: the grant
    path, the run id, and the journal. It is EMPTY for a step with no
    authority, so an ordinary Cog is invoked exactly as before."""
    seam = seam or {}
    envelope_path = Path(envelope_path)
    started = time.monotonic()
    envelope = invoke_cog(cog_dir, task, request_path, **seam)
    op_track.write_json(envelope_path, envelope)
    gate = gate_envelope(envelope)
    attempts = []
    if (gate["status"] == "fail" and on_fail == "retry-once"
            and not envelope.get("ok")):
        first = envelope_path.with_name(envelope_path.stem + ".attempt-1.json")
        op_track.write_json(first, envelope)
        envelope = invoke_cog(cog_dir, task, request_path, **seam)
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


def _run_foreach(spec, step, cog_dir, run_dir, context, seam=None):
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
            cog_dir, task, request_path, envelope_path, on_fail, seam)
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


def _run_single(step, cog_dir, run_dir, context, seam=None):
    task = step["cog"]["task"]
    request = op_spec.evaluate(step.get("input") or {}, context)
    request_path = Path(run_dir) / "requests" / f"{step['id']}.json"
    op_track.write_json(request_path, request)
    envelope_path = Path(run_dir) / "envelopes" / f"{step['id']}.json"
    envelope, gate, attempts, seconds = _attempt(
        cog_dir, task, request_path, envelope_path,
        step.get("on_fail", "stop"), seam)
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


def _element_envelopes(record):
    """The envelopes of a foreach step, in element order. An EMPTY aggregate
    is a real result: `elements: []` restores as `[]`, never as an attempt to
    read the step's envelope DIRECTORY as a file (review S4)."""
    envelopes = []
    for element in record["elements"]:
        if (element.get("gate") or {}).get("status") == "fail":
            envelopes.append(None)
            continue
        envelopes.append(json.loads(Path(element["envelope"]).read_text()))
    return envelopes


def _results_of(record):
    """(payload, envelope) a finished step contributes to later mappings,
    read back from the envelope(s) the Track points at. Both are restored:
    a resumed run exposes `steps.X.envelope` exactly as the first run did."""
    if record["status"] in ("blocked", "denied", "not-reached", "planned",
                            "running", "failed"):
        return None, None
    if record.get("elements") is not None:
        envelopes = _element_envelopes(record)
        payloads = [None if e is None else e.get("payload") for e in envelopes]
        return payloads, envelopes
    if record["status"] == "skipped" or not record.get("envelope"):
        return None, None
    envelope = json.loads(Path(record["envelope"]).read_text())
    return envelope.get("payload"), envelope


def _payload_of(record):
    """The payload a finished step contributes to later mappings."""
    return _results_of(record)[0]


def _restore_context(track, context):
    """Rebuild the run context from a Track: every finished step's payload
    AND envelope, and the human decision a gated step carries."""
    decisions = {}
    for record in track.get("steps") or []:
        payload, envelope = _results_of(record)
        entry = {"payload": payload, "envelope": envelope}
        if record.get("decision"):
            entry["decision"] = record["decision"]["value"]
            decisions[record["id"]] = record["decision"]
        context["steps"][record["id"]] = entry
    return decisions


def _paused_output(run_dir, track, sid, pending_path, track_path):
    return PAUSED_EXIT, {
        "ok": False, "status": "paused", "run_dir": str(Path(run_dir).resolve()),
        "track": track_path, "step": sid,
        "pending": str(Path(pending_path).resolve()),
    }


def _execute(spec, track, context, run_dir, package_root, authority, run_id,
             done=None, decisions=None):
    """The step loop, shared by a fresh run and a resume.

    `done` holds the records of steps this run already finished — they are
    never re-run. A step recorded `running` (a crash mid-step) is absent
    from `done` and runs again."""
    done = done or {}
    decisions = dict(decisions or {})
    dependents = spec.dependents()
    blocked = set()
    track["steps"] = []

    for position, step in enumerate(spec.ordered):
        sid = step["id"]
        prior = done.get(sid)
        if prior is not None:
            track["steps"].append(prior)
            if prior["status"] == "awaiting-decision":
                # Still waiting on the human: the run pauses again, with the
                # pending document it already wrote.
                for later in spec.ordered[position + 1:]:
                    if later["id"] not in done:
                        track["steps"].append(
                            op_track.step_record(later, "not-reached"))
                track["status"] = "paused"
                track_path = op_track.save(track, run_dir)
                return _paused_output(
                    run_dir, track, sid,
                    Path(run_dir) / "pending" / f"{sid}.json", track_path)
            if prior["status"] in ("skipped", "denied"):
                blocked |= dependents.get(sid, set())
            continue

        if sid in blocked:
            track["steps"].append(op_track.step_record(step, "blocked"))
            context["steps"][sid] = {"payload": None, "envelope": None}
            op_track.save(track, run_dir)
            continue

        # ---- authority: the grant is issued HERE, immediately before the
        # invocation, and only after every step it depends on has passed.
        seam, grant, grant_path, denial = {}, None, None, None
        if op_spec.requirements(step):
            try:
                grant, grant_path = issue_grant(step, context, authority, spec,
                                                run_id, run_dir, decisions,
                                                package_root=package_root)
            except Denied as exc:
                denial = str(exc)
        if denial is not None:
            record = op_track.step_record(
                step, "denied",
                gate={"policy": "authority", "status": "denied",
                      "reasons": [denial], "decided_at": op_track.utc_now(),
                      "guards": []},
                grant=None)
            track["steps"].append(record)
            context["steps"][sid] = {"payload": None, "envelope": None}
            op_track.save(track, run_dir)
            if step.get("on_fail", "stop") == "stop":
                for later in spec.ordered[position + 1:]:
                    track["steps"].append(
                        op_track.step_record(later, "not-reached"))
                track["status"] = "failed"
                track["ended_at"] = op_track.utc_now()
                track_path = op_track.save(track, run_dir)
                return 1, {"ok": False, "status": "failed", "failed_step": sid,
                           "run_dir": str(run_dir), "track": track_path,
                           "denied": denial}
            blocked |= dependents.get(sid, set())
            continue

        journal_path = None
        if grant is not None:
            track.setdefault("grants", []).append(
                grant_record(grant, grant_path))
            journal_path = Path(run_dir) / "journal" / f"{sid}.jsonl"
            # Created DURABLY: the Track is about to name this journal as the
            # evidence for an external effect, so its directory entry has to
            # survive the same power loss the Track does (finding 5).
            op_track.touch_durable(journal_path, base=run_dir)
            seam = {"grant_path": str(Path(grant_path).resolve()),
                    "run_id": run_id,
                    "journal_path": str(journal_path.resolve())}

        # ---- durability: the Track says the step is RUNNING, with the grant
        # and journal it was given, BEFORE the Cog is launched. Nothing
        # external can happen that the run directory does not already
        # describe (contract §9, review B1).
        position_in_track = len(track["steps"])
        track["steps"].append(op_track.step_record(
            step, "running",
            grant=str(Path(grant_path).resolve()) if grant_path else None,
            journal=str(journal_path) if journal_path else None))
        op_track.save(track, run_dir)

        cog_dir = (package_root / step["cog"]["source"]).resolve()
        if step.get("foreach") is not None:
            fields, payload, envelopes = _run_foreach(
                spec, step, cog_dir, run_dir, context, seam)
            envelope_for_context = envelopes
            authority_use = None
        else:
            fields, envelope = _run_single(step, cog_dir, run_dir, context,
                                           seam)
            payload = envelope.get("payload")
            envelope_for_context = envelope
            authority_use = (payload or {}).get("authority_use") \
                if isinstance(payload, dict) else None
        fields["grant"] = str(Path(grant_path).resolve()) if grant_path else None
        fields["journal"] = str(journal_path) if journal_path else None
        fields["authority_use"] = authority_use
        gate = fields["gate"]
        on_fail = step.get("on_fail", "stop")

        # ---- the human Gate: only a PASSING envelope reaches the human.
        if (op_spec.gate_policy(step) == op_spec.HUMAN_GATE_POLICY
                and gate["status"] != "fail"):
            pending, pending_path, _ = write_pending(run_dir, run_id, sid,
                                                     payload)
            fields["gate"] = {"policy": op_spec.HUMAN_GATE_POLICY,
                              "status": "pending",
                              # What the ENVELOPE Gate decided before the
                              # human was asked. A human approving proposals
                              # does not erase the problems the Cog reported
                              # making them: the step keeps
                              # `passed-with-problems` (contract §9b,
                              # finding 6).
                              "envelope_status": gate["status"],
                              "asked_at": pending["asked_at"],
                              # The hash the TRACK remembers: a resume
                              # re-hashes the pending payload and compares it
                              # with this, not with the hash the pending file
                              # carries beside it (review B5).
                              "payload_sha256": pending["payload_sha256"],
                              "reasons": gate["reasons"],
                              "decided_at": None, "guards": []}
            track["steps"][position_in_track] = (
                op_track.step_record(step, "awaiting-decision", **fields))
            for later in spec.ordered[position + 1:]:
                track["steps"].append(op_track.step_record(later, "not-reached"))
            track["status"] = "paused"
            track_path = op_track.save(track, run_dir)
            return _paused_output(run_dir, track, sid, pending_path, track_path)

        if gate["status"] == "fail" and on_fail == "skip":
            status = "skipped"
        else:
            status = STEP_STATUS[gate["status"]]
        track["steps"][position_in_track] = op_track.step_record(
            step, status, **fields)
        op_track.save(track, run_dir)

        if status == "failed":
            for later in spec.ordered[position + 1:]:
                track["steps"].append(op_track.step_record(later, "not-reached"))
            track["status"] = "failed"
            track["ended_at"] = op_track.utc_now()
            track_path = op_track.save(track, run_dir)
            return 1, {"ok": False, "status": "failed", "failed_step": sid,
                       "run_dir": str(run_dir), "track": track_path}
        if status == "skipped":
            blocked |= dependents.get(sid, set())
            if step.get("foreach") is None:
                payload = None
        context["steps"][sid] = {"payload": payload,
                                 "envelope": envelope_for_context}

    problematic = any(s["status"] in ("passed-with-problems", "skipped",
                                      "blocked", "denied")
                      for s in track["steps"])
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


def run(package_root, request_path, dry_run=False, runs_dir=None,
        authority_path=None):
    """Run this Op package's spec over one request. Returns (exit code,
    the JSON object the CLI prints)."""
    package_root = Path(package_root).resolve()
    spec = op_spec.load(package_root / "op.yaml")
    request_path = Path(request_path).resolve()
    request_doc = op_spec.load_document(request_path)
    values = spec.build_inputs(request_doc)
    authority = load_authority(authority_path) if authority_path else None

    # Every step's Cog declaration is checked BEFORE anything is created: a
    # spec naming a task the Cog does not declare for the usage audience is
    # an invalid spec, so it leaves no run directory and no Track stuck at
    # `status: running`. The same is true of a step that requires authority
    # this run was never admitted to have. (These are DECLARATION and
    # ADMISSION checks; the code Cog checks its own grant before it reaches
    # outside the run — see the contract's §0 amendment.) A dry run is
    # portable and checks neither.
    if not dry_run:
        problems = (op_spec.declaration_problems(spec, package_root)
                    + admission_problems(spec, authority))
        if problems:
            raise op_spec.OpSpecError(problems)

    run_id = op_track.new_run_id()
    run_dir = (Path(runs_dir).resolve() if runs_dir
               else package_root / "runs") / run_id
    # Through `ensure_dir`, not `mkdir(parents=True)`: the run directory's
    # OWN entry is fsynced in its parent. Every control directory beneath it
    # is created durably, and a Track, grant or journal whose containing
    # directory did not survive a power loss is not durable either (contract
    # §9c, review 3 finding 2).
    op_track.ensure_dir(run_dir)
    input_request = run_dir / "input-request.json"
    op_track.write_json(input_request, request_doc, base=run_dir)

    track = op_track.new_track(spec, run_id, input_request,
                               status="planned" if dry_run else "running")
    track["authority"] = ({"path": str(Path(authority_path).resolve()),
                           "sha256": sha256_file(authority_path)}
                          if authority_path else None)
    track["request_dir"] = str(request_path.parent)
    context = {
        "inputs": values,
        "steps": {},
        "run": {"dir": str(run_dir), "id": run_id},
        "request": {"dir": str(request_path.parent)},
    }
    lock = RunLock(run_dir).acquire()
    try:
        op_track.save(track, run_dir)
        if dry_run:
            return _plan(spec, track, run_dir, context)
        return _execute(spec, track, context, run_dir, package_root, authority,
                        run_id)
    finally:
        lock.release()


def resume(package_root, run_dir, decision_path=None, authority_path=None):
    """Continue a paused or interrupted run: apply the human's decision to
    the step that asked for it, and carry on from the next step.

    Steps already `passed` are never re-run; a step left `running` by a
    crash runs again (a Cog with a journal reconciles first)."""
    package_root = Path(package_root).resolve()
    run_dir = Path(run_dir).resolve()
    track_path = run_dir / "track.json"
    if not track_path.exists():
        raise op_spec.OpSpecError(f"{run_dir} carries no track.json; there is "
                                  f"no run to resume there.")
    # The lock is taken BEFORE the Track is read: two resumes that both read
    # `awaiting-decision` would both accept the decision and both write
    # (review B3).
    lock = RunLock(run_dir).acquire()
    try:
        return _resume(package_root, run_dir, track_path, decision_path,
                       authority_path)
    finally:
        lock.release()


def _resume(package_root, run_dir, track_path, decision_path,
            authority_path):
    track = json.loads(track_path.read_text())
    spec = op_spec.load(package_root / "op.yaml")
    if spec.sha256() != track.get("spec_sha256"):
        raise op_spec.OpSpecError(
            "op.yaml has changed since this run started; a resume continues "
            "the run it was planned as, so it cannot adopt a new spec.")
    if track.get("status") == "planned":
        raise op_spec.OpSpecError(
            "this run is a dry run: it resolved a plan and invoked nothing, "
            "so there is nothing to resume — start a run with --request.")

    recorded = track.get("authority") or None
    if authority_path is None and recorded:
        authority_path = recorded["path"]
    authority = load_authority(authority_path) if authority_path else None
    if authority_path and recorded and \
            sha256_file(authority_path) != recorded["sha256"]:
        raise op_spec.OpSpecError(
            "the admission file has changed since this run started; a resume "
            "runs under the authority the run was admitted with.")

    # A resume is a RUN: the same load-time refusals apply. A Cog's manifest
    # may have changed while the run was paused, and a run that was never
    # admitted for what its remaining steps require must not reach them
    # (review S3).
    problems = (op_spec.declaration_problems(spec, package_root)
                + admission_problems(spec, authority))
    if problems:
        raise op_spec.OpSpecError(problems)

    request_doc = json.loads(Path(track["input_request"]).read_text())
    values = spec.build_inputs(request_doc)
    context = {
        "inputs": values,
        "steps": {},
        "run": {"dir": str(run_dir), "id": track["run_id"]},
        # Relative `$path` operands resolved against the ORIGINAL request
        # directory on the first run, and resolve against it again here
        # (review S4); older Tracks fall back to where the copy lives.
        "request": {"dir": track.get("request_dir")
                    or str(Path(track["input_request"]).parent)},
    }
    decisions = _restore_context(track, context)

    done = {r["id"]: r for r in track.get("steps") or []
            if r["status"] in ("passed", "passed-with-problems", "skipped",
                               "blocked", "denied", "awaiting-decision")}

    decision_copy = None
    if decision_path:
        decision = load_decision(decision_path)
        sid = decision.get("step")
        record = done.get(sid)
        if record is None or record["status"] != "awaiting-decision":
            raise op_spec.OpSpecError(
                f"step {sid!r} is not waiting for a decision in this run.")
        pending = json.loads(
            (run_dir / "pending" / f"{sid}.json").read_text())
        remembered = (record.get("gate") or {}).get("payload_sha256")
        if remembered and canonical_sha256(pending.get("payload")) != remembered:
            raise op_spec.OpSpecError(
                f"the pending payload for step {sid!r} is not the one this "
                f"run paused on; the proposals changed on disk since the "
                f"Track recorded them, so no decision about them can be "
                f"applied.")
        value = apply_decision(pending, decision)
        decision_copy = run_dir / "decisions" / f"{sid}.json"
        op_track.write_json(decision_copy, decision, base=run_dir)
        digest = sha256_file(decision_copy)
        # The human decided about the proposals; the ENVELOPE Gate's verdict
        # on the step that made them stands (contract §9b, finding 6).
        envelope_status = (record.get("gate") or {}).get("envelope_status")
        record["status"] = STEP_STATUS.get(envelope_status, "passed")
        record["gate"] = {"policy": op_spec.HUMAN_GATE_POLICY, "status": "pass",
                          "envelope_status": envelope_status,
                          "asked_at": (record.get("gate") or {}).get("asked_at"),
                          "payload_sha256": remembered,
                          "decided_at": op_track.utc_now(),
                          "decision": str(decision_copy.resolve()),
                          "decision_sha256": digest,
                          "reasons": (record.get("gate") or {}).get("reasons")
                          or [], "guards": []}
        record["decision"] = {"decision": str(decision_copy.resolve()),
                              "decision_sha256": digest, "value": value}
        decisions[sid] = record["decision"]
        payload, envelope = _results_of(record)
        context["steps"][sid] = {"payload": payload, "envelope": envelope,
                                 "decision": value}

    entry = {"at": op_track.utc_now(),
             "decision": str(decision_copy.resolve()) if decision_copy else None}
    track.setdefault("resumes", []).append(entry)
    track["status"] = "running"
    track["ended_at"] = None
    # Durability order (contract §9): the accepted decision and the resume are
    # on disk BEFORE any grant is issued or any Cog is invoked.
    op_track.save(track, run_dir)
    return _execute(spec, track, context, run_dir, package_root, authority,
                    track["run_id"], done=done, decisions=decisions)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run this Op package's spec.")
    parser.add_argument("--request",
                        help="the Op request document (JSON or YAML)")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the plan and write a planned Track "
                             "without invoking any Cog")
    parser.add_argument("--runs-dir",
                        help="where run directories are written "
                             "(default: <package>/runs)")
    parser.add_argument("--authority",
                        help="the run's admission (openteams/op-authority "
                             "[0.1]): the owner's authority for this run. "
                             "Without it, a step that requires authority is "
                             "refused before anything runs.")
    parser.add_argument("--resume", metavar="RUN_DIR",
                        help="continue a paused or interrupted run")
    parser.add_argument("--decision",
                        help="with --resume: the human decision "
                             "(openteams/op-decision [0.1]) for the step that "
                             "is waiting")
    args = parser.parse_args(argv)
    if bool(args.resume) == bool(args.request):
        parser.error("pass --request to start a run or --resume to continue "
                     "one, not both")
    if args.decision and not args.resume:
        parser.error("--decision applies to --resume")
    try:
        if args.resume:
            code, output = resume(ROOT, args.resume,
                                  decision_path=args.decision,
                                  authority_path=args.authority)
        else:
            code, output = run(ROOT, args.request, dry_run=args.dry_run,
                               runs_dir=args.runs_dir,
                               authority_path=args.authority)
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
