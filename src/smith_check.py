"""`smith check` — validate a minted Cog package.

Deterministic, model-free. Checks manifest integrity, context/schema
agreement, machinery copy-sync against cog-smith's masters, interface/task
consistency, and (optionally) runs the Cog's own test suite.
Findings carry a level: error (check fails) or warn (surfaced, non-fatal).
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core       # noqa: E402
import toml_compat      # noqa: E402

try:
    import jsonschema
    _SchemaValidator = (getattr(jsonschema, "Draft202012Validator", None)
                        or getattr(jsonschema, "Draft7Validator", None))
except ImportError:                                    # pragma: no cover
    _SchemaValidator = None

REQUIRED_FIELDS = ("schema", "id", "version", "kind", "summary", "owner",
                   "license", "io", "interfaces")
SCHEMA_STRING = "openteams/cog-manifest [0.1]"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def check(root, run_tests=False):
    root = Path(root).resolve()
    findings = []

    def err(check_name, detail):
        findings.append({"level": "error", "check": check_name, "detail": detail})

    def warn(check_name, detail):
        findings.append({"level": "warn", "check": check_name, "detail": detail})

    # ---- manifest ---------------------------------------------------------
    mp = root / "cog.yaml"
    if not mp.exists():
        err("manifest", f"{root} has no cog.yaml — not a Cog package")
        return findings
    try:
        m = yaml.safe_load(mp.read_text()) or {}
    except yaml.YAMLError as e:
        err("manifest", f"cog.yaml is not valid YAML: {e}")
        return findings
    for f in REQUIRED_FIELDS:
        if f not in m:
            err("manifest", f"missing required field: {f}")
    if m.get("schema") != SCHEMA_STRING:
        err("manifest", f"schema must be {SCHEMA_STRING!r}, got {m.get('schema')!r}")

    # ---- COG.md frontmatter ----------------------------------------------
    cogmd = root / "COG.md"
    if not cogmd.exists():
        err("cogmd", "COG.md missing")
    else:
        fm = FRONTMATTER_RE.match(cogmd.read_text())
        if not fm:
            err("cogmd", "COG.md has no frontmatter")
        else:
            try:
                meta = yaml.safe_load(fm.group(1)) or {}
                if meta.get("manifest") != "cog.yaml":
                    err("cogmd", "frontmatter must declare manifest: cog.yaml")
                if meta.get("version") != m.get("version"):
                    err("cogmd", f"frontmatter version {meta.get('version')!r} != "
                                 f"manifest {m.get('version')!r}")
            except yaml.YAMLError as e:
                err("cogmd", f"frontmatter not valid YAML: {e}")

    # ---- declared context files ------------------------------------------
    ctx = m.get("context") or {}
    schemas = {}
    for key in ("instructions", "input_schema", "output_schema", "output_example"):
        rel = ctx.get(key)
        if not rel:
            (warn if key == "input_schema" else err)(
                "context", f"context.{key} not declared")
            continue
        p = root / rel
        if not p.exists():
            err("context", f"declared context.{key} missing: {rel}")
            continue
        if p.suffix == ".json":
            try:
                schemas[key] = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                err("context", f"{rel} is not valid JSON: {e}")

    if _SchemaValidator and "output_schema" in schemas and "output_example" in schemas:
        v = _SchemaValidator(schemas["output_schema"])
        errors = [f"{'.'.join(str(x) for x in e.absolute_path) or '$'}: {e.message}"
                  for e in v.iter_errors(schemas["output_example"])]
        for detail in errors[:5]:
            err("schema", f"output-example violates output-schema: {detail}")

    example = root / "examples" / "sample-bundle.json"
    if example.exists() and _SchemaValidator and "input_schema" in schemas:
        try:
            bundle = json.loads(example.read_text())
            v = _SchemaValidator(schemas["input_schema"])
            for e in list(v.iter_errors(bundle))[:5]:
                where = ".".join(str(x) for x in e.absolute_path) or "$"
                err("schema", f"sample-bundle violates input-schema: {where}: {e.message}")
        except json.JSONDecodeError as e:
            err("examples", f"sample-bundle.json invalid: {e}")
    elif not example.exists():
        warn("examples", "no examples/sample-bundle.json")

    # ---- machinery copy-sync ---------------------------------------------
    masters = smith_core.machinery_hashes()
    src = root / "src"
    for name, want in masters.items():
        p = src / name
        if not p.exists():
            err("machinery", f"src/{name} missing (cog-smith machinery)")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != want:
            err("machinery", f"src/{name} differs from cog-smith master — "
                             f"machinery is shared; per-cog logic belongs in "
                             f"task_logic.py")
    if not (src / "task_logic.py").exists():
        err("machinery", "src/task_logic.py missing (the author-owned module)")

    # ---- interfaces / tasks ----------------------------------------------
    pixi_path = root / "pixi.toml"
    tasks = {}
    if not pixi_path.exists():
        err("interfaces", "pixi.toml missing")
    else:
        with open(pixi_path, "rb") as f:
            tasks = (toml_compat.load(f).get("tasks")) or {}
    interfaces = m.get("interfaces") or []
    defaults = [i for i in interfaces if i.get("default")]
    if len(defaults) != 1:
        err("interfaces", f"exactly one default interface required, got {len(defaults)}")
    for i in interfaces:
        t = i.get("task")
        if t and tasks and t not in tasks:
            err("interfaces", f"interface {i.get('name')!r} names task {t!r} "
                              f"not present in pixi.toml")
        if i.get("kind") == "http-json":
            ep = i.get("endpoint") or ""
            if not re.match(r"^https?://127\.0\.0\.1:\d+/", ep):
                warn("interfaces", f"http-json endpoint {ep!r} is not a "
                                   f"loopback address with an explicit port")
    for t in ("resolve", "check", "test"):
        if tasks and t not in tasks:
            err("interfaces", f"lifecycle task {t!r} missing from pixi.toml")

    # ---- declarations -----------------------------------------------------
    if not m.get("prohibits"):
        warn("declarations", "no prohibits declared — is the evidence-tier "
                             "boundary really empty?")
    reqs = m.get("requires") or []
    if m.get("kind") == "context" and not reqs:
        warn("declarations", "context cog declares no requires — no model "
                             "dependency?")
    for fx in (m.get("evaluation") or {}).get("fixtures") or []:
        if not (root / fx).exists():
            err("evaluation", f"declared fixture missing: {fx}")

    # ---- optional: the Cog's own tests ------------------------------------
    if run_tests and not any(f["level"] == "error" for f in findings):
        r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                            "-s", "tests"], cwd=str(root),
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            err("tests", (r.stderr or r.stdout)[-800:])
    return findings


def report(findings, out=print):
    errors = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    for f in findings:
        out(f"[{f['level'].upper():5}] {f['check']}: {f['detail']}")
    out(f"\n{'FAIL' if errors else 'PASS'} — {len(errors)} error(s), "
        f"{len(warns)} warning(s)")
    return 1 if errors else 0
