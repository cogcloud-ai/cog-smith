"""`smith op new` / `smith op check` — Op packages, in the same shape as Cogs.

An Op package is `op.yaml` (the executable spec), `pixi.toml` (the `op` and
`test` tasks), the Op machinery in `src/` (never edited in the package —
enforced here by hash, exactly as `smith check` enforces Cog machinery),
a generated `tests/test_op.py`, `examples/request.json`, `README.md`, and a
gitignored `runs/`. There is no per-Op Python: if an Op needs code, the
answer is a Cog step.

`op new --from-spec` validates the resolved spec first (the refusals live in
the machinery's `op_spec.validate`, so the package and the builder refuse the
same constructs by the same names) and then writes the package. Unlike
`smith new`, the destination may already exist: a design record can become
its own package. Any file the creation would overwrite refuses by filename.

`op check` validates the spec, the machinery hashes, the tasks, and each
step's declaration against the Cog it names: the source must carry that Cog,
and the task must be one of its declared USAGE interfaces. A source
directory that is simply absent (the package is being checked on a machine
without the Cogs) is a warning, not an error.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core      # noqa: E402
import smith_manifest  # noqa: E402

TEMPLATE = smith_core.TEMPLATES / "op"
sys.path.insert(0, str(TEMPLATE / "src"))
import op_spec         # noqa: E402

# The Op machinery lineage (MACHINERY.md). Bumped here when a master
# changes; `op check` reports it so a package's drift has a version to
# name.
MACHINERY_VERSION = "0.4.4"
MACHINERY = ("op_runner.py", "op_spec.py", "op_track.py")
OP_TASKS = ("op", "test")
PLACEHOLDERS = {"object": {}, "array": [], "integer": 0, "number": 0,
                "boolean": False, "string": "REPLACE_ME"}

#: The check name spec problems carry. An invalid SPEC is a different kind of
#: failure from a package finding: the CLI exits 2 for it, exactly as `op new`
#: and the runner do (contract §2).
INVALID_SPEC = "opspec-invalid"


class OpCreateError(Exception):
    pass


def machinery_hashes():
    return {name: hashlib.sha256((TEMPLATE / "src" / name).read_bytes()).hexdigest()
            for name in MACHINERY}


# --------------------------------------------------------------- create --

def _steps_markdown(spec):
    lines = []
    for index, step in enumerate(spec.ordered, start=1):
        cog = step.get("cog") or {}
        name = step.get("name") or step["id"]
        loop = " (once per element)" if step.get("foreach") else ""
        lines.append(f"{index}. **{step['id']}** — {name}{loop}: "
                     f"`{cog.get('id')}` task `{cog.get('task')}`")
    return "\n".join(lines)


def _tokens(spec, dest):
    summary = " ".join(str(spec.description or spec.name or spec.id).split())
    return {
        "OP_NAME": dest.name,
        "OP_ID": spec.id,
        "OP_VERSION": spec.version,
        "OP_TITLE": spec.name or dest.name,
        "OP_SUMMARY": summary,
        "OP_SUMMARY_TOML": json.dumps(summary),
        "OP_STEPS_MD": _steps_markdown(spec),
    }


def example_request(spec):
    """A starting request: declared defaults, and a type-shaped placeholder
    only where a REQUIRED input declares none.

    Presence, not truthiness: an input that declares `default: null` gets
    null. An OPTIONAL input with no declared default is omitted entirely —
    writing null there supplies a value the author never declared, and a
    declared `schema: {type: string}` would then reject the starter request."""
    out = {}
    for declared in spec.inputs:
        if "default" in declared:
            out[declared["name"]] = declared["default"]
            continue
        if not declared.get("required", True):
            continue                 # absent, not null
        schema = declared.get("schema") or {}
        kind = schema.get("type")
        if isinstance(kind, list):  # e.g. ["string", "null"]: first concrete type
            kind = next((k for k in kind if k != "null"), None)
        out[declared["name"]] = PLACEHOLDERS.get(kind, "REPLACE_ME")
    return out


def plan(spec_path, dest):
    """{relative path: text or None} for every file `op new` would write.
    None means 'copy the template file verbatim'."""
    spec = op_spec.load(spec_path)
    dest = Path(dest)
    tokens = _tokens(spec, dest)
    files = {}
    for name in MACHINERY:
        files[f"src/{name}"] = (TEMPLATE / "src" / name).read_text()
    files[".gitignore"] = (TEMPLATE / ".gitignore").read_text()
    files["pixi.toml"] = smith_core.render(
        (TEMPLATE / "pixi.toml.tmpl").read_text(), tokens)
    files["README.md"] = smith_core.render(
        (TEMPLATE / "README.md.tmpl").read_text(), tokens)
    files["tests/test_op.py"] = smith_core.render(
        (TEMPLATE / "tests" / "test_op.py.tmpl").read_text(), tokens)
    files["op.yaml"] = yaml.safe_dump(spec.doc, sort_keys=False,
                                      default_flow_style=False, width=88)
    files["examples/request.json"] = json.dumps(
        example_request(spec), indent=2) + "\n"
    for rel, text in files.items():
        if rel.endswith((".yaml", ".toml", ".md", ".json")) and \
                smith_core.TOKEN_RE.search(text):
            raise OpCreateError(f"unrendered tokens remain in {rel}.")
    return spec, files


def create(spec_path, dest):
    """Write the Op package. The destination may exist; any file that would
    be overwritten refuses by filename."""
    dest = Path(dest).resolve()
    spec, files = plan(spec_path, dest)
    collisions = sorted(rel for rel in files if (dest / rel).exists())
    if collisions:
        raise OpCreateError(
            f"{dest} already carries {collisions} — `op new` writes into an "
            f"existing directory only when none of its files are there.")
    written = []
    try:
        for rel in sorted(files):
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(files[rel])
            written.append(rel)
    except OSError:
        for rel in written:
            (dest / rel).unlink(missing_ok=True)
        raise
    return {"dest": str(dest), "files": written, "op_id": spec.id,
            "steps": [s["id"] for s in spec.ordered]}


# ---------------------------------------------------------------- check --

def check(root, run_tests=False):
    """Findings about an Op package, in the smith_check shape
    (level / layer / check / detail)."""
    root = Path(root).resolve()
    findings = []

    def err(layer, name, detail):
        findings.append({"level": "error", "layer": layer, "check": name,
                         "detail": detail})

    def warn(layer, name, detail):
        findings.append({"level": "warn", "layer": layer, "check": name,
                         "detail": detail})

    spec_file = root / "op.yaml"
    if not spec_file.exists():
        err("core", "opspec", f"{root} carries no op.yaml — an Op package is "
                              f"its spec plus the shared Op machinery.")
        return findings
    try:
        spec = op_spec.load(spec_file)
    except op_spec.OpSpecError as exc:
        for problem in exc.problems:
            err("core", INVALID_SPEC, problem)
        return findings

    # ---------------------------------------------------- runtime layer --
    for name in MACHINERY:
        path = root / "src" / name
        want = machinery_hashes()[name]
        if not path.exists():
            err("runtime", "machinery",
                f"src/{name} missing (cog-smith Op machinery).")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            err("runtime", "machinery",
                f"src/{name} differs from the cog-smith master — Op "
                f"machinery is shared; behaviour belongs in op.yaml or in a "
                f"Cog step.")
    src_dir = root / "src"
    extras = sorted(p.name for p in src_dir.glob("*.py")) if src_dir.is_dir() else []
    for extra in extras:
        if extra not in MACHINERY:
            err("runtime", "machinery",
                f"src/{extra} is not Op machinery — an Op carries no per-Op "
                f"Python; make it a Cog step.")

    try:
        pixi = smith_manifest.read_pixi(root)
    except ValueError as exc:
        pixi = None
        err("runtime", "tasks", f"pixi.toml is not readable: {exc}.")
    if pixi is None:
        err("runtime", "tasks", "pixi.toml missing — an Op declares its `op` "
                                "and `test` tasks there.")
    else:
        tasks = pixi.get("tasks") or {}
        for task in OP_TASKS:
            if task not in tasks:
                err("runtime", "tasks",
                    f"pixi.toml declares no {task!r} task.")
        if smith_manifest.has_pixi_manifest(pixi):
            err("runtime", "tasks",
                "pixi.toml carries a [tool.cog] manifest — this package is "
                "an Op, not a Cog; Ops declare op.yaml.")
    if not (root / "tests" / "test_op.py").exists():
        warn("runtime", "tests", "no tests/test_op.py — the generated suite "
                                 "is the package's own regression net.")
    if not (root / "examples" / "request.json").exists():
        warn("runtime", "examples", "no examples/request.json — a runnable "
                                    "example request is how an Op is tried.")

    # ---------------------------------------------------- profile layer --
    # The same declaration check the runner makes before it invokes anything
    # (op_spec.cog_step_findings), so the builder and the package refuse the
    # same steps for the same reasons.
    for step in spec.steps:
        source = root / str((step.get("cog") or {}).get("source"))
        for level, detail in op_spec.cog_step_findings(step, source):
            (err if level == "error" else warn)("profile", "steps", detail)

    if run_tests and not any(f["level"] == "error" for f in findings):
        result = subprocess.run([sys.executable, "-m", "unittest", "discover",
                                 "-s", "tests"], cwd=str(root),
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            err("runtime", "tests", (result.stderr or result.stdout)[-800:])
    return findings


def invalid_spec(findings):
    """True when op.yaml itself is invalid — the CLI exits 2, not 1."""
    return any(f["check"] == INVALID_SPEC for f in findings)
