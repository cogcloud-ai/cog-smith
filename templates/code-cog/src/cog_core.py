"""Shared core for a CODE Cog's entry points (cog-smith machinery, generic).

A code Cog (`kind: code`, decided 2026-09-17) is a Cog with no model in the
loop: the same seam as a context Cog — a declared input schema, a declared
output schema, envelope v1 with structured `problems`, contract checks run
in-package — with the model half removed. Everything here is generic:
per-cog work lives in `task_logic.py`, which this machinery calls as

    run(bundle, grant, journal) -> (payload, problems)

`smith check` enforces this file and `cog_cli.py` by hash, exactly as it
enforces the context-cog machinery. Fixes happen in cog-smith's template and
roll out by re-copying (MACHINERY.md).

**Authority, honestly stated.** This process runs as its owner, with the
owner's ambient credentials. A grant is not an enforced sandbox: it is a
document the Op runner issues and THIS CODE checks before it reaches outside
the run. The Cog refuses to act without a valid grant, and reports every
attempted operation. Nothing here may be described as an enforced restricted
environment (phase 2 contract §0).
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_logic   # noqa: E402  (the ONLY per-cog module in src/)

try:
    import jsonschema
    _SchemaValidator = (getattr(jsonschema, "Draft202012Validator", None)
                        or getattr(jsonschema, "Draft7Validator", None))
except ImportError:                                    # pragma: no cover
    jsonschema = None
    _SchemaValidator = None

#: The code-cog machinery lineage (cog-smith MACHINERY.md). Reported in
#: every envelope's `binding`, so a saved result names the code that made it.
MACHINERY_VERSION = "0.1.0"

GRANT_SCHEMA = "openteams/op-grant [0.1]"

ROOT = Path(__file__).resolve().parent.parent


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------- the manifest --

def load_manifest(root=None):
    """The profile manifest: `[tool.cog]` in pixi.toml (default) or cog.yaml.
    Exactly one — a package whose two manifests could disagree is refused."""
    root = Path(root or ROOT)
    pixi, standalone = root / "pixi.toml", root / "cog.yaml"
    doc = None
    if pixi.exists():
        try:
            import tomllib
        except ModuleNotFoundError as exc:              # pragma: no cover
            raise RuntimeError(
                "reading a [tool.cog] manifest needs Python 3.11+ (tomllib); "
                "this Cog declares python >=3.11") from exc
        with open(pixi, "rb") as handle:
            doc = tomllib.load(handle)
    in_pixi = bool(doc and isinstance(doc.get("tool"), dict)
                   and isinstance(doc["tool"].get("cog"), dict))
    if in_pixi and standalone.exists():
        raise ValueError(f"{root} carries both pixi.toml [tool.cog] and "
                         f"cog.yaml — a package has exactly one manifest")
    if in_pixi:
        manifest = dict(doc["tool"]["cog"])
        workspace = doc.get("workspace") or doc.get("project") or {}
        for key, source in (("version", "version"), ("summary", "description")):
            if key not in manifest and workspace.get(source) is not None:
                manifest[key] = workspace[source]
        return manifest
    if standalone.exists():
        import yaml
        manifest = yaml.safe_load(standalone.read_text())
        if not isinstance(manifest, dict):
            raise ValueError(f"{standalone} is not a manifest mapping")
        return manifest
    raise FileNotFoundError(f"{root} carries no profile manifest "
                            f"(pixi.toml [tool.cog] or cog.yaml)")


MANIFEST = load_manifest()
SELF_ID = {"id": MANIFEST.get("id"), "version": MANIFEST.get("version")}
#: What this Cog touches outside the run. DECLARED, never inferred.
REACHES = MANIFEST.get("reaches") or []

_context = MANIFEST.get("context") or {}


def _schema(key):
    rel = _context.get(key)
    if not rel:
        return None
    path = ROOT / rel
    return json.loads(path.read_text()) if path.exists() else None


INPUT_SCHEMA = _schema("input_schema")
OUTPUT_SCHEMA = _schema("output_schema")

#: The default usage task — the entry point `pixi run <task>` names.
DEFAULT_TASK = "run"


def problem(check, detail, severity="error"):
    return {"check": check, "detail": str(detail), "severity": severity}


def task_logic_sha256():
    """The hash of the package-owned module — the code that did the work."""
    return hashlib.sha256(
        (ROOT / "src" / "task_logic.py").read_bytes()).hexdigest()


# ----------------------------------------------------------- the journal --

class Journal:
    """An append-only JSONL record of what this Cog did outside the run.

    One JSON object per line, flushed and fsynced per line, so a crash after
    an external effect leaves either the line or nothing — never a torn
    record the next run would misread. The runner creates the file and
    passes `--journal`; the Cog reads it FIRST on every invocation, so a
    change whose outcome is already recorded is skipped and a change left
    `applying` is reconciled before anything is attempted again.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry):
        """Write one entry (a dict), stamping `at` when it carries none."""
        if not isinstance(entry, dict):
            raise TypeError("a journal entry is a JSON object")
        entry = dict(entry)
        entry.setdefault("at", utc_now())
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def read(self):
        """Every entry, in order. A trailing torn line (a crash mid-write)
        is dropped rather than raising: the record before it is still true."""
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def phases(self):
        """change_id -> the LAST phase recorded for it."""
        out = {}
        for entry in self.read():
            if entry.get("change_id") is not None:
                out[entry["change_id"]] = entry.get("phase")
        return out

    def last(self, change_id):
        """The last entry for one change, or None."""
        found = None
        for entry in self.read():
            if entry.get("change_id") == change_id:
                found = entry
        return found


# ------------------------------------------------------------- the grant --
#
# A grant is issued by the Op's Gate, written by the runner, and read here.
# It carries no credentials, and no bundle, payload, decision or request can
# widen it: this module only ever READS it.

def load_grant(path):
    """The grant document at PATH. Raises ValueError when it is not JSON."""
    try:
        return json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"grant {Path(path).name} is not JSON: {exc}") from exc


def _expired(grant, now=None):
    expires = ((grant.get("valid") or {}).get("expires_at"))
    if not expires:
        return True, "the grant declares no expiry"
    try:
        deadline = datetime.fromisoformat(str(expires))
    except ValueError:
        return True, f"the grant's expires_at {expires!r} is not a timestamp"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment > deadline:
        return True, f"the grant expired at {expires}"
    return False, None


def check_grant(grant, run_id=None, now=None, cog_id=None):
    """(code, detail) when this grant may not be used, else (None, None).

    Checked before any external call: a grant for another run, another Cog,
    or a moment that has passed is refused by THIS Cog."""
    cog_id = cog_id or SELF_ID["id"]
    if not isinstance(grant, dict):
        return "grant-invalid", "the grant is not a JSON object"
    if grant.get("schema") != GRANT_SCHEMA:
        return "grant-invalid", (f"the grant declares schema "
                                 f"{grant.get('schema')!r}, not "
                                 f"{GRANT_SCHEMA!r}")
    if not isinstance(grant.get("operations"), list):
        return "grant-invalid", "the grant declares no operations list"
    valid = grant.get("valid")
    if not isinstance(valid, dict):
        return "grant-invalid", "the grant declares no validity conditions"
    expired, detail = _expired(grant, now)
    if expired:
        return "grant-expired", detail
    bound = valid.get("run_id") or grant.get("run_id")
    if run_id is not None and str(bound) != str(run_id):
        return "grant-wrong-run", (f"the grant is bound to run {bound!r}, not "
                                   f"to this run ({run_id!r})")
    recipient = ((grant.get("recipient") or {}).get("cog") or {}).get("id")
    if recipient != cog_id:
        return "grant-wrong-recipient", (f"the grant names {recipient!r} as "
                                         f"its recipient, not {cog_id!r}")
    return None, None


def operations(grant, resource=None, action=None):
    """The grant's operations, optionally filtered by resource and action."""
    out = []
    for op in (grant or {}).get("operations") or []:
        if not isinstance(op, dict):
            continue
        if resource is not None and op.get("resource") != resource:
            continue
        if action is not None and op.get("action") != action:
            continue
        out.append(op)
    return out


def read_allowed(grant, target, resource="github"):
    """(ok, detail) for reading TARGET (e.g. a repository)."""
    for op in operations(grant, resource, "read"):
        if target in (op.get("repositories") or []):
            return True, None
    return False, (f"{resource} read of {target!r} is not in this grant")


def approved_change(grant, change_id, resource="github"):
    """The grant's entry for CHANGE_ID, or None. The grant carries exactly
    the changes a human approved: a change that is not in it is denied, and
    that is the whole check — never a trim of what was requested."""
    for op in operations(grant, resource, "write"):
        for change in op.get("changes") or []:
            if isinstance(change, dict) and change.get("change_id") == change_id:
                return change
    return None


def write_allowed(grant, change_id, content_sha256=None, resource="github"):
    """(ok, detail) for writing CHANGE_ID, optionally against the current
    content hash of its target (staleness: the world moved since approval)."""
    change = approved_change(grant, change_id, resource)
    if change is None:
        return False, (f"change {change_id!r} is not in this grant; it was "
                       f"never approved")
    if (content_sha256 is not None
            and change.get("content_sha256") not in (None, content_sha256)):
        return False, (f"change {change_id!r} was approved against content "
                       f"{change.get('content_sha256')!r}, but the target is "
                       f"now {content_sha256!r}")
    return True, None


def use(operation, resource, target, outcome, detail=None):
    """One entry for a payload's `authority_use` list: what was attempted,
    against what, and how it ended (authorized | denied | failed)."""
    return {"operation": operation, "resource": resource, "target": target,
            "outcome": outcome, "detail": detail}


# ------------------------------------------------------------ validation --

def _schema_problems(value, schema, check):
    problems = []
    if schema is None or _SchemaValidator is None:      # pragma: no cover
        return problems
    validator = _SchemaValidator(schema)
    for err in sorted(validator.iter_errors(value),
                      key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "$"
        problems.append(problem(check, f"{where}: {err.message}"))
    return problems


def validate_input(bundle):
    """The declared input schema, then the package's own input checks."""
    if not isinstance(bundle, dict):
        return [problem("input", "input is not an object")]
    problems = _schema_problems(bundle, INPUT_SCHEMA, "input")
    checker = getattr(task_logic, "check_input", None)
    if checker:
        problems.extend(checker(bundle) or [])
    return problems


def validate_output(payload, bundle):
    """The declared output schema, then the package's contract checks."""
    if not isinstance(payload, dict):
        return [problem("schema", "the task returned a payload that is not "
                                  "an object")]
    problems = _schema_problems(payload, OUTPUT_SCHEMA, "schema")
    checker = getattr(task_logic, "check_output", None)
    if checker:
        problems.extend(checker(payload, bundle) or [])
    return problems


# ------------------------------------------------------------------ health --

def health():
    """(ok, detail). A code Cog has no model dependency; `check` reports what
    it reaches and whether its declared shapes are loadable."""
    missing = [key for key in ("input_schema", "output_schema")
               if _context.get(key) and not (ROOT / _context[key]).exists()]
    if missing:
        return False, f"declared context files missing: {missing}"
    if INPUT_SCHEMA is None or OUTPUT_SCHEMA is None:
        return False, "the manifest declares no input/output schema"
    reaches = ", ".join(f"{r.get('resource')}:{'/'.join(r.get('actions') or [])}"
                        for r in REACHES) or "nothing outside the run"
    return True, (f"{SELF_ID['id']} (kind: code, machinery "
                  f"{MACHINERY_VERSION}) reaches {reaches}")


# ------------------------------------------------------------------ invoke --

def binding():
    """The binding identity of a code Cog: which code produced this result.
    `model` is ABSENT, not null — there is no model in the loop."""
    return {"kind": "code", "cog": dict(SELF_ID),
            "task_logic_sha256": task_logic_sha256(),
            "machinery": MACHINERY_VERSION}


def _envelope(task, ok, payload=None, problems=None, error=None, latency=None):
    return {
        "envelope": 1,
        "cog": dict(SELF_ID),
        "task": task,
        "ok": bool(ok),
        "error": error,
        "payload": payload,
        "raw": None,
        "problems": problems or [],
        "binding": binding(),
        "timing": {"latency_s": latency},
    }


def _fail(task, code, detail):
    return _envelope(task, False, error={"code": code, "detail": str(detail)},
                     problems=[problem(code, detail)])


def invoke(bundle, grant=None, journal=None, run_id=None, task=DEFAULT_TASK,
           now=None):
    """Run this Cog over one input bundle. Returns an envelope-v1 dict.

    `grant` is the grant document (a dict) or None; `journal` is a Journal or
    None. Fails CLOSED: a Cog that declares `reaches` and is handed no grant
    refuses before `run` is called, and an invalid grant is refused with the
    reason named."""
    import time
    started = time.monotonic()

    problems = validate_input(bundle)
    if problems:
        env = _fail(task, "invalid-input",
                    "; ".join(p["detail"] for p in problems[:5]))
        env["problems"] = problems
        return env

    if REACHES and grant is None:
        return _fail(task, "no-grant",
                     f"{SELF_ID['id']} declares reaches "
                     f"{[r.get('resource') for r in REACHES]} and was invoked "
                     f"with no grant; a code Cog does not reach outside the "
                     f"run without one")
    if grant is not None:
        code, detail = check_grant(grant, run_id=run_id, now=now)
        if code:
            return _fail(task, code, detail)

    try:
        result = task_logic.run(bundle, grant, journal)
    except Exception as exc:                            # the task's own bug
        return _fail(task, "task-failed", f"{type(exc).__name__}: {exc}")
    if (not isinstance(result, tuple) or len(result) != 2):
        return _fail(task, "task-failed",
                     "task_logic.run must return (payload, problems)")
    payload, task_problems = result
    problems = list(task_problems or []) + validate_output(payload, bundle)
    return _envelope(task, True, payload=payload, problems=problems,
                     latency=round(time.monotonic() - started, 3))
