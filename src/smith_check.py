"""`smith check` — validate a Cog package, in explicit layers (review
2026-08-22, F1):

- **core**    — CogSpec-shaped basics: COG.md frontmatter (required fields,
                types), the name grammar, manifest reference agreement.
                (Not yet the full reference validator — see the note in
                AGENTS.md; findings are labeled so the layers never blur.)
- **profile** — openteams/cog-manifest [0.1] fields and semantics, incl.
                the manifest-file convention (a PROFILE rule, not core):
                `[tool.cog]` in pixi.toml (default, ADR D9) or cog.yaml —
                exactly one, and COG.md's `manifest:` pointer must agree.
- **runtime** — cog-smith machinery copy-sync, tasks, schemas, fixtures,
                and (optionally) the Cog's own test suite. Skipped, with a
                note, for packages that don't carry smith machinery
                (deployment descriptors; tooling Cogs like cog-smith).

Deterministic, model-free. Findings carry layer + level (error|warn).
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
import smith_manifest   # noqa: E402

try:
    import jsonschema
    _SchemaValidator = (getattr(jsonschema, "Draft202012Validator", None)
                        or getattr(jsonschema, "Draft7Validator", None))
except ImportError:                                    # pragma: no cover
    _SchemaValidator = None

PROFILE_FIELDS = ("schema", "id", "version", "kind", "summary", "owner",
                  "license", "io", "interfaces")
SCHEMA_STRING = "openteams/cog-manifest [0.1]"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
CORE_FRONTMATTER = ("type", "name", "description", "version", "manifest",
                    "manifest_schema")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
VALID_LOCALITY = {"local", "customer-vpc", "cloud"}
VALID_AUDIENCE = {"usage", "lifecycle"}


def check(root, run_tests=False):
    root = Path(root).resolve()
    findings = []

    def err(layer, check_name, detail):
        findings.append({"level": "error", "layer": layer,
                         "check": check_name, "detail": detail})

    def warn(layer, check_name, detail):
        findings.append({"level": "warn", "layer": layer,
                         "check": check_name, "detail": detail})

    # ======================================================= core layer ==
    cogmd = root / "COG.md"
    meta = {}
    if not cogmd.exists():
        err("core", "cogmd", "COG.md missing — a Cog is COG.md plus the "
                             "manifest it names")
    else:
        fm = FRONTMATTER_RE.match(cogmd.read_text())
        if not fm:
            err("core", "cogmd", "COG.md has no frontmatter")
        else:
            try:
                meta = yaml.safe_load(fm.group(1)) or {}
            except yaml.YAMLError as e:
                err("core", "cogmd", f"frontmatter not valid YAML: {e}")
                meta = {}
            for f in CORE_FRONTMATTER:
                if f not in meta:
                    err("core", "cogmd", f"frontmatter missing required "
                                         f"field: {f}")
            name = str(meta.get("name", ""))
            if name and not smith_core.COG_NAME_RE.match(name):
                err("core", "name-grammar",
                    f"name {name!r} violates the name grammar (lowercase "
                    f"alphanumerics, single hyphens)")
            if name and root.name != name:
                warn("core", "name-grammar",
                     f"directory {root.name!r} != declared name {name!r}")
            desc = meta.get("description")
            if desc is not None and not isinstance(desc, str):
                err("core", "cogmd", "description must be a string")

    # ==================================================== profile layer ==
    try:
        m, fmt, mp = smith_manifest.load(root)
    except smith_manifest.ManifestError as e:
        err("profile", "manifest", str(e))
        return findings
    if meta and meta.get("manifest") not in (None, mp.name):
        err("core", "cogmd", f"frontmatter names manifest "
                             f"{meta.get('manifest')!r}, but the profile "
                             f"manifest found is {mp.name}")
    pixi_doc = smith_manifest.read_pixi(root) if fmt == "pixi" else None
    if pixi_doc is not None:
        ws = pixi_doc.get("workspace") or pixi_doc.get("project") or {}
        tool = pixi_doc["tool"]["cog"]
        if "version" in tool and ws.get("version") not in (None, tool["version"]):
            err("profile", "manifest",
                f"[tool.cog].version {tool['version']!r} != [workspace].version "
                f"{ws.get('version')!r} — state the version once, in [workspace]")
        if not ws.get("version"):
            err("profile", "manifest",
                "[workspace].version is required (it is the manifest version)")

    required = tuple(f for f in PROFILE_FIELDS
                     if not (m.get("kind") == "model" and f == "io"))
    for f in required:                 # model cogs: tokens in, tokens out —
        if f not in m:                 # io is a context-cog declaration
            err("profile", "manifest", f"missing required field: {f}")
    if m.get("schema") != SCHEMA_STRING:
        err("profile", "manifest",
            f"schema must be {SCHEMA_STRING!r}, got {m.get('schema')!r}")
    short = str(m.get("id", "")).rsplit("/", 1)[-1]
    if short and not smith_core.COG_NAME_RE.match(short):
        err("profile", "name-grammar",
            f"manifest id name part {short!r} violates the name grammar")
    if meta and meta.get("version") != m.get("version"):
        err("profile", "manifest",
            f"frontmatter version {meta.get('version')!r} != manifest "
            f"{m.get('version')!r}")

    for i in m.get("interfaces") or []:
        aud = i.get("audience")
        if aud is not None and aud not in VALID_AUDIENCE:
            err("profile", "interfaces",
                f"interface {i.get('name')!r}: audience must be one of "
                f"{sorted(VALID_AUDIENCE)}, got {aud!r}")
    defaults = [i for i in m.get("interfaces") or [] if i.get("default")]
    if len(defaults) != 1:
        err("profile", "interfaces",
            f"exactly one default interface required, got {len(defaults)}")

    # ---- code cogs (decided 2026-09-17) ----------------------------------
    # kind: code is a model-free Cog: the Cog shape with no model in the
    # loop. The kind is declared, never inferred, and a code Cog that
    # declares a model requirement is contradicting itself.
    is_code = m.get("kind") == "code"
    if is_code and (m.get("requires") or []):
        err("profile", "declarations",
            "kind: code declares requires — a code Cog has no model in the "
            "loop; if it needs a model it is a context Cog")

    # `reaches` — what a Cog touches OUTSIDE the run. Declared, never
    # inferred; it is what a grant is checked against.
    # Today only code Cogs carry it; context Cogs get it when
    # tool-using Cogs arrive.
    reaches = m.get("reaches")
    if reaches is not None and not is_code:
        err("profile", "declarations",
            "reaches is declared on a Cog that is not kind: code — in phase "
            "3 only code Cogs declare what they reach outside the run")
    if reaches is not None and not isinstance(reaches, list):
        err("profile", "declarations",
            f"reaches must be a list of {{resource, actions}} entries, got "
            f"{type(reaches).__name__}")
    elif isinstance(reaches, list):
        for index, entry in enumerate(reaches):
            if not isinstance(entry, dict):
                err("profile", "declarations",
                    f"reaches[{index}] must be an object with resource and "
                    f"actions")
                continue
            if not isinstance(entry.get("resource"), str):
                err("profile", "declarations",
                    f"reaches[{index}].resource must be a string, got "
                    f"{entry.get('resource')!r}")
            actions = entry.get("actions")
            if not isinstance(actions, list) or not all(
                    isinstance(a, str) for a in actions):
                err("profile", "declarations",
                    f"reaches[{index}].actions must be a list of strings, got "
                    f"{actions!r}")

    # ---- model cogs without smith machinery ------------------------------
    # Classification is DECLARED, never inferred (the same F7 rule as
    # interface audience): descriptor rules apply only when the manifest
    # carries the F5 marker. A weight-carrying model cog (one with
    # model.weights.source + a serving task) is not a descriptor and must
    # not be judged by descriptor rules.
    if m.get("kind") == "model" and not (root / "src").exists():
        model = m.get("model") or {}
        if model.get("descriptor"):
            _check_model_descriptor(m, err, warn)
        elif not (model.get("weights") or {}).get("source"):
            warn("profile", "descriptor",
                 "kind: model with neither carried weights "
                 "(model.weights.source) nor the descriptor marker "
                 "(model.descriptor: true) — declare which this is")
        return findings

    # ==================================================== runtime layer ==
    if not (root / "src" / "cog_core.py").exists():
        warn("runtime", "machinery",
             "runtime layer skipped — package carries no cog-smith "
             "machinery (a tooling Cog or a foreign package); core and "
             "profile layers still apply")
        return findings

    ctx = m.get("context") or {}
    schemas = {}
    # A code Cog's "context" is its declared SHAPES: there is no model to
    # instruct, so instructions and the worked example are not required.
    declared_context = (("input_schema", "output_schema") if is_code else
                        ("instructions", "input_schema", "output_schema",
                         "output_example"))
    for key in ("instructions", "input_schema", "output_schema",
                "output_example"):
        rel = ctx.get(key)
        if not rel:
            if key not in declared_context:
                continue
            (warn if (key == "input_schema" and not is_code) else err)(
                "profile", "context", f"context.{key} not declared")
            continue
        p = root / rel
        if not p.exists():
            err("runtime", "context", f"declared context.{key} missing: {rel}")
            continue
        if p.suffix == ".json":
            try:
                schemas[key] = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                err("runtime", "context", f"{rel} is not valid JSON: {e}")

    if _SchemaValidator and "output_schema" in schemas and "output_example" in schemas:
        v = _SchemaValidator(schemas["output_schema"])
        errors = [f"{'.'.join(str(x) for x in e.absolute_path) or '$'}: {e.message}"
                  for e in v.iter_errors(schemas["output_example"])]
        for detail in errors[:5]:
            err("runtime", "schema",
                f"output-example violates output-schema: {detail}")

    example = root / "examples" / "sample-bundle.json"
    if example.exists() and _SchemaValidator and "input_schema" in schemas:
        try:
            bundle = json.loads(example.read_text())
            v = _SchemaValidator(schemas["input_schema"])
            for e in list(v.iter_errors(bundle))[:5]:
                where = ".".join(str(x) for x in e.absolute_path) or "$"
                err("runtime", "schema",
                    f"sample-bundle violates input-schema: {where}: {e.message}")
        except json.JSONDecodeError as e:
            err("runtime", "examples", f"sample-bundle.json invalid: {e}")
    elif not example.exists():
        warn("runtime", "examples", "no examples/sample-bundle.json")

    # Machinery is per KIND: a code Cog carries the code-cog masters
    # (cog_core + cog_cli, no model machinery); everything else carries the
    # context-cog masters. Same copy-sync discipline, two lineages.
    template = smith_core.KIND_TEMPLATES.get(m.get("kind"), "context-cog")
    masters = smith_core.machinery_hashes(template)
    src = root / "src"
    for name, want in masters.items():
        p = src / name
        if not p.exists():
            err("runtime", "machinery", f"src/{name} missing (cog-smith machinery)")
        elif hashlib.sha256(p.read_bytes()).hexdigest() != want:
            err("runtime", "machinery",
                f"src/{name} differs from cog-smith master — machinery is "
                f"shared; per-cog logic belongs in task_logic.py")
    if not (src / "task_logic.py").exists():
        err("runtime", "machinery", "src/task_logic.py missing (the "
                                    "author-owned module)")

    tasks = {}
    try:
        pixi_doc = pixi_doc if pixi_doc is not None else smith_manifest.read_pixi(root)
    except ValueError as e:
        pixi_doc = None
        err("runtime", "interfaces", f"pixi.toml unreadable: {e}")
    if pixi_doc is None:
        err("runtime", "interfaces", "pixi.toml missing")
    else:
        tasks = pixi_doc.get("tasks") or {}
    for i in m.get("interfaces") or []:
        t = i.get("task")
        if t and tasks and t not in tasks:
            err("runtime", "interfaces",
                f"interface {i.get('name')!r} names task {t!r} not present "
                f"in pixi.toml")
        if i.get("kind") == "http-json":
            ep = i.get("endpoint") or ""
            if not re.match(r"^https?://127\.0\.0\.1:\d+/", ep):
                warn("runtime", "interfaces",
                     f"http-json endpoint {ep!r} is not a loopback address "
                     f"with an explicit port")
    # `resolve` binds a model dependency; a code Cog has none to bind.
    for t in (("check", "test") if is_code else ("resolve", "check", "test")):
        if tasks and t not in tasks:
            err("runtime", "interfaces",
                f"lifecycle task {t!r} missing from pixi.toml")

    if not m.get("prohibits"):
        warn("profile", "declarations",
             "no prohibits declared — is the evidence-tier boundary really "
             "empty?")
    if m.get("kind") == "context" and not (m.get("requires") or []):
        warn("profile", "declarations",
             "context cog declares no requires — no model dependency?")
    for fx in (m.get("evaluation") or {}).get("fixtures") or []:
        if not (root / fx).exists():
            err("runtime", "evaluation", f"declared fixture missing: {fx}")

    if run_tests and not any(f["level"] == "error" for f in findings):
        r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                            "-s", "tests"], cwd=str(root),
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            err("runtime", "tests", (r.stderr or r.stdout)[-800:])
    return findings


def _check_model_descriptor(m, err, warn):
    """Deployment-descriptor model cog: no machinery, no context files —
    the whole contract is the manifest."""
    provides = m.get("provides") or []
    if "model-endpoint/openai-compatible" not in provides:
        err("profile", "descriptor",
            "must provide model-endpoint/openai-compatible")
    if m.get("locality") not in VALID_LOCALITY:
        err("profile", "descriptor",
            f"locality {m.get('locality')!r} not in {sorted(VALID_LOCALITY)}")
    model = m.get("model") or {}
    if not model.get("name"):
        err("profile", "descriptor",
            "model.name is required (the pinnable identity)")
    if not model.get("descriptor"):
        warn("profile", "descriptor",
             "model.descriptor: true marker absent — the profile marks "
             "deployment descriptors explicitly")

    defaults = [i for i in m.get("interfaces") or [] if i.get("default")]
    if len(defaults) != 1:
        return
    d = defaults[0]
    if d.get("kind") != "openai-compatible":
        err("profile", "descriptor",
            "default interface must be openai-compatible")
    if not d.get("served_model_id"):
        warn("profile", "descriptor",
             "no served_model_id — identity verification will fall back to "
             "model.name")
    ep, addr = d.get("endpoint"), d.get("address")
    if bool(ep) == bool(addr):
        err("profile", "descriptor",
            "declare exactly one of endpoint / address: install-time")
    if addr and addr != "install-time":
        err("profile", "descriptor",
            f"address must be 'install-time', got {addr!r}")
    if ep and not re.match(r"^(https://|http://127\.0\.0\.1[:/])", ep):
        err("profile", "descriptor",
            f"endpoint {ep!r} must be https or loopback http")
    key = d.get("api_key_env")
    if key and not ENV_NAME_RE.match(str(key)):
        err("profile", "descriptor",
            f"api_key_env {key!r} is not an env-var NAME — descriptors "
            f"carry references, never credentials")


def report(findings, out=print):
    errors = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    for f in findings:
        out(f"[{f['level'].upper():5}] {f.get('layer', '?'):7} "
            f"{f['check']}: {f['detail']}")
    out(f"\n{'FAIL' if errors else 'PASS'} — {len(errors)} error(s), "
        f"{len(warns)} warning(s)")
    return 1 if errors else 0
