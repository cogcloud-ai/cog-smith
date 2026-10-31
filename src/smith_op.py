"""`smith op new` / `smith op check` — Op packages, in the same shape as Cogs.

An Op package is `OP.md` (the Op Spec entry file, naming `op.yaml` as its
manifest), `op.yaml` (the executable spec), `pixi.toml` (the `op` and
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
without the Cogs) is a warning, not an error. A missing or invalid `OP.md`
is a warning too, so a package created before `op new` wrote one keeps
passing, as is an OP.md `name` other than the last segment of op.yaml's
`id`; an OP.md `version` other than op.yaml's is an error.

Both `openteams/op-manifest [0.2]` and `[0.1]` specs are accepted (Op
machinery 0.11.0). `op new` writes the spec in the profile it declares — it
never rewrites a `[0.1]` spec, whose changes name `repository` where `[0.2]`
reads `target` — and `op check` warns on a `[0.1]` spec, naming what moving
to `[0.2]` takes. For a `[0.2]` spec, a stated Cog version other than the
Cog's is an error.
"""
import hashlib
import json
import re
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
MACHINERY_VERSION = "0.11.0"
MACHINERY = ("op_runner.py", "op_spec.py", "op_track.py", "op_cycle.py")
OP_TASKS = ("op", "test", "cycle")
PLACEHOLDERS = {"object": {}, "array": [], "integer": 0, "number": 0,
                "boolean": False, "string": "REPLACE_ME"}

#: The Op Spec core entry file (Op Spec v0.1, "File format").
OP_MD = "OP.md"
#: `type` values an Op Spec v0.1 entry file may declare.
OP_TYPE_RE = re.compile(r"^op( \[0\.1\])?$")
#: The core naming rules (shared with the Cog Spec): 1-64 lowercase ASCII
#: letters, digits and hyphens, no leading, trailing or doubled hyphen.
OP_MD_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
OP_MD_NAME_MAX = 64
OP_MD_DESCRIPTION_MAX = 1024
OP_MD_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)",
                                  re.S)
OP_MD_REQUIRED = ("type", "name", "description", "manifest", "manifest_schema")
#: Package metadata OP.md states when the caller states none — the same
#: defaults `smith new` gives a Cog's COG.md.
_COG_DEFAULTS = smith_core.default_tokens("op")
DEFAULT_LICENSE = _COG_DEFAULTS["LICENSE"]
DEFAULT_PUBLISHER = _COG_DEFAULTS["PUBLISHER"]

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
        name = op_spec.step_title(step) or step["id"]
        loop = " (once per element)" if step.get("foreach") else ""
        lines.append(f"{index}. **{step['id']}** — {name}{loop}: "
                     f"`{cog.get('id')}` task `{cog.get('task')}`")
    return "\n".join(lines)


def _gates_markdown(spec):
    """Who decides what, from each step's Gate."""
    lines = []
    for step in spec.ordered:
        if op_spec.gate_policy(step) != op_spec.HUMAN_GATE_POLICY:
            continue
        if op_spec.gate_decides(step) == op_spec.DECIDES_ARTIFACT:
            kind = (op_spec.gate_artifact(step) or {}).get("kind")
            what = (f"the `{kind}` artifact" if isinstance(kind, str)
                    else "the artifact")
            lines.append(f"- **{step['id']}** — a person accepts or rejects "
                         f"{what} it produced, as a whole, bound to its "
                         f"digests. A rejection ends the run.")
        else:
            lines.append(f"- **{step['id']}** — a person approves, rejects, "
                         f"edits or defers each change it proposes. Only "
                         f"approved changes can authorize a write.")
    if not lines:
        return ("None. Every step is accepted by the envelope Gate "
                f"(`{op_spec.GATE_POLICY}`); no person is asked.")
    return ("The run pauses at these steps, after their result has passed the "
            "envelope Gate, and nothing after them runs until a person "
            "decides.\n\n" + "\n".join(lines))


def _authority_markdown(spec):
    """What each step requires outside the run, and where it comes from."""
    lines = []
    for step in spec.ordered:
        for requirement in op_spec.requirements(step):
            if not isinstance(requirement, dict):
                continue
            resource = requirement.get("resource")
            action = requirement.get("action")
            if action == "write":
                source = (f"authorized only by the human decision at "
                          f"`{op_spec.decision_step(requirement)}`")
            else:
                source = "from the run owner's admission"
            lines.append(f"- **{step['id']}** requires `{resource}` "
                         f"`{action}`, {source}.")
    if not lines:
        return "None. No step reaches outside the run."
    return ("Loading this Op grants nothing. The runner issues each grant "
            "immediately before the step that requires it.\n\n"
            + "\n".join(lines))


def _outputs_markdown(spec):
    names = [str(name) for name in spec.outputs]
    if not names:
        return ("No declared `outputs`; the run's Track records every step's "
                "request, result and Gate decision.")
    listed = ", ".join(f"`{name}`" for name in names)
    return (f"The run's `outputs`: {listed}. The Track records every step's "
            f"request, result and Gate decision beside them.")


def id_name(op_id):
    """The last path segment of an Op's manifest `id` — the `name` its OP.md
    states (`openteams/op-learning-shortlist` -> `op-learning-shortlist`)."""
    return str(op_id).rsplit("/", 1)[-1]


def valid_core_name(name):
    """True when NAME follows the core naming rules: 1-64 lowercase ASCII
    letters, digits and hyphens, no leading, trailing or doubled hyphen."""
    return (isinstance(name, str) and 0 < len(name) <= OP_MD_NAME_MAX
            and bool(OP_MD_NAME_RE.match(name)))


def op_md_name(spec):
    """OP.md's `name`: the last segment of the manifest `id`. A segment that
    is not a valid core name is refused, never rewritten into one — the
    name is how the Op is recognized, so it is stated, not guessed."""
    name = id_name(spec.id)
    if not valid_core_name(name):
        raise OpCreateError(
            f"op.yaml id {spec.id!r} ends in {name!r}, which is not a valid "
            f"OP.md name; OP.md's name is the id's last segment, and a name "
            f"is 1-64 lowercase letters, digits and hyphens, with no leading, "
            f"trailing or doubled hyphen. Rename the id, e.g. "
            f"'example.org/op-my-workflow'.")
    return name


def _one_line_value(value, what):
    text = " ".join(str(value).split())
    if not text:
        raise OpCreateError(f"{what} must be non-empty.")
    return text


def _tokens(spec, dest, license=None, publisher=None):
    summary = " ".join(str(spec.description or spec.title or spec.id).split())
    description = summary
    if len(description) > OP_MD_DESCRIPTION_MAX:
        description = description[:OP_MD_DESCRIPTION_MAX - 3].rstrip() + "..."
    return {
        "OP_NAME": dest.name,
        "OP_ID": spec.id,
        "OP_VERSION": spec.version,
        "OP_TITLE": spec.title or dest.name,
        "OP_SUMMARY": summary,
        "OP_SUMMARY_TOML": json.dumps(summary),
        "OP_STEPS_MD": _steps_markdown(spec),
        # OP.md (Op Spec v0.1): YAML scalars written as JSON strings, which
        # YAML reads as double-quoted scalars — so a colon or a quote in a
        # description can never break the frontmatter.
        "OP_MD_NAME": op_md_name(spec),
        "OP_DESCRIPTION_YAML": json.dumps(description),
        "OP_VERSION_YAML": json.dumps(str(spec.version)),
        "OP_LICENSE_YAML": json.dumps(_one_line_value(
            license or DEFAULT_LICENSE, "license")),
        "OP_PUBLISHER_YAML": json.dumps(_one_line_value(
            publisher or DEFAULT_PUBLISHER, "publisher")),
        # The profile op.yaml declares: OP.md names what actually runs.
        "OP_SCHEMA_YAML": spec.schema,
        "OP_GATES_MD": _gates_markdown(spec),
        "OP_AUTHORITY_MD": _authority_markdown(spec),
        "OP_OUTPUTS_MD": _outputs_markdown(spec),
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
        # A JSON Schema may be a BOOLEAN (`true` accepts anything): only a
        # dictionary schema has a `type` to shape the placeholder from.
        schema = declared.get("schema")
        kind = schema.get("type") if isinstance(schema, dict) else None
        if isinstance(kind, list):  # e.g. ["string", "null"]: first concrete type
            kind = next((k for k in kind if k != "null"), None)
        out[declared["name"]] = PLACEHOLDERS.get(kind, "REPLACE_ME")
    return out


def example_admission(spec):
    """Empty-scope placeholders never grant authority to real targets."""
    pairs = sorted({(requirement['resource'], requirement['action'])
                    for step in spec.ordered for requirement in op_spec.requirements(step)})
    return {'schema': 'openteams/op-authority [0.1]',
            'operations': [{'resource': resource, 'action': action, 'targets': []}
                           for resource, action in pairs]}


def plan(spec_path, dest, license=None, publisher=None):
    """{relative path: text or None} for every file `op new` would write.
    None means 'copy the template file verbatim'."""
    spec = op_spec.load(spec_path)
    dest = Path(dest)
    tokens = _tokens(spec, dest, license=license, publisher=publisher)
    files = {}
    files[OP_MD] = smith_core.render((TEMPLATE / "OP.md.tmpl").read_text(),
                                     tokens)
    for name in MACHINERY:
        files[f"src/{name}"] = (TEMPLATE / "src" / name).read_text()
    files[".gitignore"] = (TEMPLATE / ".gitignore").read_text()
    for name in ("LICENSE.smith", "NOTICE.smith", "LICENSING.md"):
        files[name] = (TEMPLATE / name).read_text()
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
    if any(op_spec.requirements(step) for step in spec.ordered):
        files["examples/admission.json"] = json.dumps(example_admission(spec), indent=2) + "\n"
    for rel, text in files.items():
        if rel.endswith((".yaml", ".toml", ".md", ".json")) and \
                smith_core.TOKEN_RE.search(text):
            raise OpCreateError(f"unrendered tokens remain in {rel}.")
    return spec, files


def create(spec_path, dest, license=None, publisher=None):
    """Write the Op package. The destination may exist; any file that would
    be overwritten refuses by filename — except an `OP.md` already there,
    which is the author's (an Op Spec draft is an OP.md written before its
    manifest) and is kept, never replaced; `op check` then reports what it
    still lacks."""
    dest = Path(dest).resolve()
    spec, files = plan(spec_path, dest, license=license, publisher=publisher)
    kept = []
    if (dest / OP_MD).exists() or (dest / OP_MD).is_symlink():
        files.pop(OP_MD)
        kept.append(OP_MD)
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
    return {"dest": str(dest), "files": written, "kept": kept,
            "op_id": spec.id, "steps": [s["id"] for s in spec.ordered]}


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
    for level, detail in op_md_findings(root, spec):
        (err if level == "error" else warn)("core", "opmd", detail)
    if spec.profile == op_spec.PROFILE_0_1:
        warn("profile", "opspec-profile", migration_hint(spec))
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
        for level, detail in op_spec.cog_step_findings(step, source,
                                                       spec.profile):
            (err if level == "error" else warn)("profile", "steps", detail)

    if run_tests and not any(f["level"] == "error" for f in findings):
        result = subprocess.run([sys.executable, "-m", "unittest", "discover",
                                 "-s", "tests"], cwd=str(root),
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            err("runtime", "tests", (result.stderr or result.stdout)[-800:])
    return findings


def migration_hint(spec):
    """The warning a `[0.1]` spec carries: it runs unchanged, and this is what
    declaring `openteams/op-manifest [0.2]` takes. Never an error."""
    return (f"op.yaml declares {op_spec.SCHEMA_STRING_0_1!r}, which still "
            f"runs; to move to {op_spec.SCHEMA_STRING_0_2!r}, rename `name` "
            f"to `title` (top level and on every step, including cycle "
            f"preparation steps), state every `cog.version`, write "
            f"`targets` for `repositories`, and have every proposed change "
            f"name what it changes as `target` — a write target the "
            f"admission lists. [0.2] also refuses operator objects with "
            f"other keys, `$path` values beginning with `~`, and actions "
            f"other than read and write, and at run time requires each "
            f"named Cog present at its stated version and every envelope to "
            f"name the step's Cog. Update OP.md's manifest_schema with it.")


def op_md_findings(root, spec):
    """[(level, detail)] for ROOT's `OP.md` against the loaded op.yaml SPEC.

    Every entry-file problem `op_md_problems` names is a WARNING — a missing
    OP.md included, so a package created before `op new` wrote one keeps
    passing — and so is a `name` that is not the last segment of op.yaml's
    `id`. A `version` that is not a string, or differs from op.yaml's, is an
    ERROR: OP.md would then describe a different revision of the Op than the
    one that runs."""
    findings = [("warn", detail) for detail in op_md_problems(root)]
    meta = op_md_frontmatter(root)
    if not isinstance(meta, dict):
        return findings
    expected = id_name(spec.id)
    name = meta.get("name")
    if "name" in meta and name != expected:
        findings.append(("warn", f"{OP_MD} declares name {name!r}, but "
                                 f"op.yaml's id {spec.id!r} names "
                                 f"{expected!r}; OP.md's name should be the "
                                 f"id's last segment."))
    schema = meta.get("manifest_schema")
    if schema in op_spec.SCHEMA_STRINGS and schema != spec.schema:
        findings.append(("warn", f"{OP_MD} declares manifest_schema "
                                 f"{schema!r}, but op.yaml declares "
                                 f"{spec.schema!r}; the entry file names the "
                                 f"profile that runs."))
    # Compared as written, never through str(): coercing both sides made an
    # unquoted `version: null` agree with an op.yaml version "None", and
    # `false` with "False". A stated version is a string, and only an equal
    # string agrees (`spec.version` is op.yaml's version, as the runner
    # records it).
    version = meta.get("version")
    if "version" in meta and not isinstance(version, str):
        findings.append(("error", f"{OP_MD} declares version {version!r}, "
                                  f"which is not a string, so it cannot "
                                  f"agree with op.yaml's version "
                                  f"{spec.version!r}; the entry file "
                                  f"describes the revision that runs."))
    elif "version" in meta and version != spec.version:
        findings.append(("error", f"{OP_MD} declares version {version!r}, "
                                  f"but op.yaml is version "
                                  f"{spec.version!r}; the entry file "
                                  f"describes the revision that runs."))
    return findings


def op_md_frontmatter(root):
    """OP.md's frontmatter mapping, or None when there is none to read."""
    path = Path(root) / OP_MD
    try:
        text = path.read_bytes().decode("utf-8")
        match = OP_MD_FRONTMATTER_RE.match(text)
        return yaml.safe_load(match.group(1)) if match else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def op_md_problems(root):
    """Why ROOT's `OP.md` is missing or is not a valid Op Spec v0.1 entry
    file naming this package's manifest — each a WARNING in `op check`.

    The core validation of Op Spec v0.1: valid UTF-8 opening with YAML
    frontmatter; `type`, `name` and `description` valid; `manifest` a
    relative path to an existing file inside the package after symlinks are
    followed, and `manifest_schema` a non-empty string. Then what this
    runner can say about the pointer: it runs `op.yaml` under
    `openteams/op-manifest [0.1]` or `[0.2]`. Agreement with op.yaml's `id`,
    `version` and `schema` is `op_md_findings`'s."""
    root = Path(root)
    path = root / OP_MD
    if not path.is_file():
        return [f"no {OP_MD} — the Op Spec entry file names the package and "
                f"its manifest; `op new` writes one."]
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return [f"{OP_MD} is not valid UTF-8."]
    match = OP_MD_FRONTMATTER_RE.match(text)
    if not match:
        return [f"{OP_MD} does not begin with YAML frontmatter between "
                f"'---' lines."]
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [f"{OP_MD} frontmatter is not valid YAML: "
                f"{' '.join(str(exc).split())[:160]}."]
    if not isinstance(meta, dict):
        return [f"{OP_MD} frontmatter is not a mapping."]
    problems = []
    for field in OP_MD_REQUIRED:
        if field not in meta:
            problems.append(f"{OP_MD} frontmatter declares no {field!r}.")
    nested = sorted(str(k) for k, v in meta.items()
                    if isinstance(v, (dict, list)) or not isinstance(k, str))
    if nested:
        problems.append(f"{OP_MD} frontmatter is a flat mapping of scalars; "
                        f"{nested} are not.")
    kind = meta.get("type")
    if "type" in meta and not (isinstance(kind, str) and OP_TYPE_RE.match(kind)):
        problems.append(f"{OP_MD} declares type {kind!r}; an Op Spec v0.1 "
                        f"entry file declares 'op' or 'op [0.1]'.")
    name = meta.get("name")
    if "name" in meta and not valid_core_name(name):
        problems.append(f"{OP_MD} declares name {name!r}; a name is 1-64 "
                        f"lowercase letters, digits and single hyphens, not "
                        f"starting or ending with a hyphen.")
    description = meta.get("description")
    if "description" in meta and not (isinstance(description, str)
                                      and description.strip()
                                      and len(description)
                                      <= OP_MD_DESCRIPTION_MAX):
        problems.append(f"{OP_MD} declares a description that is not a "
                        f"non-empty string of at most "
                        f"{OP_MD_DESCRIPTION_MAX} characters.")
    if "version" in meta and not isinstance(meta["version"], str):
        problems.append(f"{OP_MD} declares version {meta['version']!r} "
                        f"unquoted; quote it, so 1.0 stays the string "
                        f"'1.0'.")
    schema = meta.get("manifest_schema")
    if "manifest_schema" in meta:
        if not isinstance(schema, str) or not schema.strip():
            problems.append(f"{OP_MD} declares manifest_schema {schema!r}; "
                            f"it names the execution profile as a "
                            f"non-empty string.")
        elif schema not in op_spec.SCHEMA_STRINGS:
            problems.append(f"{OP_MD} declares manifest_schema {schema!r}; "
                            f"this package's runner implements "
                            f"{list(op_spec.SCHEMA_STRINGS)}.")
    manifest = meta.get("manifest")
    if "manifest" in meta:
        problems.extend(_manifest_pointer_problems(root, manifest))
    return problems


def _manifest_pointer_problems(root, manifest):
    if not isinstance(manifest, str) or not manifest:
        return [f"{OP_MD} declares manifest {manifest!r}; it is a relative "
                f"path to the Op's manifest."]
    pointer = Path(manifest)
    if pointer.is_absolute() or ".." in pointer.parts:
        return [f"{OP_MD} declares manifest {manifest!r}; the manifest path "
                f"is relative and stays inside the Op directory."]
    base = root.resolve()
    try:
        target = (root / pointer).resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        # A pointer no filesystem can name — an embedded NUL (ValueError), or
        # a link loop (RuntimeError from Python 3.12's resolve) — is a
        # problem with OP.md, so the promised warning, never an exception
        # that aborts `op check`.
        return [f"{OP_MD} declares manifest {manifest!r}, which is not a "
                f"path this machine can name ({type(exc).__name__}: {exc})."]
    if target != base and base not in target.parents:
        return [f"{OP_MD} declares manifest {manifest!r}, which resolves "
                f"outside the Op directory."]
    if not target.is_file():
        return [f"{OP_MD} declares manifest {manifest!r}, which is not a "
                f"file in the Op directory."]
    if target != (base / "op.yaml").resolve():
        return [f"{OP_MD} declares manifest {manifest!r}, but this package's "
                f"runner runs op.yaml."]
    return []


def invalid_spec(findings):
    """True when op.yaml itself is invalid — the CLI exits 2, not 1."""
    return any(f["check"] == INVALID_SPEC for f in findings)
