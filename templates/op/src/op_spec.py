"""Op spec: load, validate, refuse by name, and evaluate mapping expressions.

Machinery master (cog-smith `templates/op/src/`). An Op package never edits
this file — `smith op check` enforces it by hash, exactly as `smith check`
enforces Cog machinery. If an Op needs behaviour this module does not have,
the answer is a Cog step, never a script in the Op.

The spec is `openteams/op-manifest [0.1]`: identity, declared `inputs`, a
list of Cog `steps` with mapping expressions for their requests, optional
`outputs`, and the `track` records. The vocabulary is CLOSED — anything the
runner subset does not carry is refused BY NAME at load, with the phase that
adds it, so an author never discovers a missing construct mid-run.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

try:                                                   # pragma: no cover
    import jsonschema
    _SchemaValidator = (getattr(jsonschema, "Draft202012Validator", None)
                        or getattr(jsonschema, "Draft7Validator", None))
except ImportError:                                    # pragma: no cover
    _SchemaValidator = None

SCHEMA_STRING = "openteams/op-manifest [0.1]"
GATE_POLICY = "envelope-ok-no-error-problems"
STEP_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ON_FAIL = ("stop", "skip", "retry-once")

TOP_KEYS = {"schema", "id", "version", "name", "description", "inputs",
            "steps", "outputs", "track"}
STEP_KEYS = {"id", "name", "depends_on", "cog", "foreach", "input",
             "expected_outcome", "gate", "on_fail"}
COG_KEYS = {"id", "version", "source", "task"}
INPUT_KEYS = {"name", "description", "required", "default", "schema"}
FOREACH_KEYS = {"items", "as"}
GATE_KEYS = {"policy", "guards"}
TRACK_KEYS = {"records"}

# Refused by name, with the phase that adds the construct.
REFUSED_TOP = {"state": "phase 4"}
REFUSED_STEP = {"tool": "phase 3", "human": "phase 3"}

# Mapping-expression operators: an object whose key set is EXACTLY one of
# these is an operator; every other object is walked; scalars are literals.
OPERATORS = (
    frozenset({"$from"}),
    frozenset({"$from", "$default"}),
    frozenset({"$path"}),
    frozenset({"$run_dir"}),
    frozenset({"$stem"}),
    frozenset({"$literal"}),
)
OPERATOR_NAMES = "$from, $from/$default, $path, $run_dir, $stem, $literal"
PATH_ROOTS = ("inputs", "steps", "run", "request")


class OpSpecError(Exception):
    """One or more one-sentence problems with a spec, request, or mapping."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


# ---------------------------------------------------------------- lookup --

def lookup(path, ctx):
    """(found, value) for a mapping path. Dotted keys index objects,
    integers index arrays."""
    segments = str(path).split(".")
    root = segments[0]
    if root in PATH_ROOTS or root in ctx:
        current = ctx.get(root)
    else:
        return False, None
    for segment in segments[1:]:
        if isinstance(current, dict):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, list):
            try:
                index = int(segment)
            except ValueError:
                return False, None
            if not -len(current) <= index < len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def evaluate(expr, ctx):
    """Resolve a mapping expression against a run context
    {inputs, steps, run, request, <loop variables>}."""
    if isinstance(expr, dict):
        keys = frozenset(expr)
        if any(k.startswith("$") for k in keys):
            if keys not in OPERATORS:
                raise OpSpecError(
                    f"mapping expression {sorted(keys)} is not a known "
                    f"operator; the mapping vocabulary is closed "
                    f"({OPERATOR_NAMES}).")
            if "$literal" in keys:
                return expr["$literal"]
            if "$from" in keys:
                found, value = lookup(expr["$from"], ctx)
                if found and value is not None:
                    return value
                if "$default" in keys:
                    return evaluate(expr["$default"], ctx)
                if not found:
                    raise OpSpecError(
                        f"mapping path {expr['$from']!r} is not available in "
                        f"this run and the expression declares no $default.")
                return None
            if "$path" in keys:
                value = evaluate(expr["$path"], ctx)
                if value is None:
                    return None
                path = Path(str(value)).expanduser()
                if not path.is_absolute():
                    path = Path(ctx["request"]["dir"]) / path
                return str(path.resolve())
            if "$run_dir" in keys:
                value = evaluate(expr["$run_dir"], ctx)
                path = (Path(ctx["run"]["dir"]) / str(value or "")).resolve()
                path.mkdir(parents=True, exist_ok=True)
                return str(path)
            if "$stem" in keys:
                value = evaluate(expr["$stem"], ctx)
                return None if value is None else Path(str(value)).stem
        return {k: evaluate(v, ctx) for k, v in expr.items()}
    if isinstance(expr, list):
        return [evaluate(item, ctx) for item in expr]
    return expr


def reads_steps(expr):
    """True when an expression reads any earlier step's result — the runner
    uses this to list a step's request as null in a dry run."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            keys = frozenset(node)
            if keys in OPERATORS and "$literal" in keys:
                return
            if "$from" in keys and str(node["$from"]).split(".")[0] == "steps":
                found.append(True)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(expr)
    return bool(found)


# ------------------------------------------------------------ validation --

def _expr_problems(expr, where, input_names, step_ids, dep_ids, loop_vars,
                   problems):
    if isinstance(expr, dict):
        keys = frozenset(expr)
        if any(k.startswith("$") for k in keys):
            if keys not in OPERATORS:
                problems.append(
                    f"{where} uses unknown mapping operator(s) "
                    f"{sorted(k for k in keys if k.startswith('$'))}; the "
                    f"mapping vocabulary is closed ({OPERATOR_NAMES}).")
                return
            if "$literal" in keys:
                return
            if "$from" in keys:
                _path_problems(expr["$from"], where, input_names, step_ids,
                               dep_ids, loop_vars, problems)
                if "$default" in keys:
                    _expr_problems(expr["$default"], where, input_names,
                                   step_ids, dep_ids, loop_vars, problems)
                return
            for key in ("$path", "$run_dir", "$stem"):
                if key in keys:
                    _expr_problems(expr[key], where, input_names, step_ids,
                                   dep_ids, loop_vars, problems)
            return
        for key, value in expr.items():
            _expr_problems(value, f"{where}.{key}", input_names, step_ids,
                           dep_ids, loop_vars, problems)
    elif isinstance(expr, list):
        for index, item in enumerate(expr):
            _expr_problems(item, f"{where}[{index}]", input_names, step_ids,
                           dep_ids, loop_vars, problems)


def _path_problems(path, where, input_names, step_ids, dep_ids, loop_vars,
                   problems):
    if not isinstance(path, str) or not path:
        problems.append(f"{where}: $from takes a dotted path string, got "
                        f"{path!r}.")
        return
    segments = path.split(".")
    root = segments[0]
    if root == "inputs":
        if len(segments) < 2 or segments[1] not in input_names:
            problems.append(f"{where} reads input {'.'.join(segments[1:2]) or '?'!r}, "
                            f"which the Op spec does not declare.")
    elif root == "steps":
        if len(segments) < 3 or segments[2] not in ("payload", "envelope"):
            problems.append(f"{where} reads {path!r}; a step path is "
                            f"steps.<id>.payload or steps.<id>.envelope.")
            return
        target = segments[1]
        if target not in step_ids:
            problems.append(f"{where} reads step {target!r}, which the Op "
                            f"spec does not declare.")
        elif target not in dep_ids:
            problems.append(f"{where} reads {path!r} without depending on "
                            f"{target!r}; add it to that step's depends_on.")
    elif root == "run":
        if len(segments) < 2 or segments[1] not in ("dir", "id"):
            problems.append(f"{where} reads {path!r}; the run paths are "
                            f"run.dir and run.id.")
    elif root == "request":
        if len(segments) < 2 or segments[1] != "dir":
            problems.append(f"{where} reads {path!r}; the only request path "
                            f"is request.dir.")
    elif root not in loop_vars:
        problems.append(f"{where} reads {path!r}, whose root is neither a "
                        f"declared input, a step, run/request, nor this "
                        f"step's foreach variable.")


def _order(steps, problems):
    """Topological order, ties broken by spec order. Records a cycle as a
    problem and returns the steps unordered."""
    ids = [s.get("id") for s in steps]
    pending = list(steps)
    done, ordered = set(), []
    while pending:
        ready = [s for s in pending
                 if all(d in done for d in (s.get("depends_on") or []))]
        if not ready:
            stuck = sorted(str(s.get("id")) for s in pending)
            problems.append(f"the Op spec's steps form a dependency cycle "
                            f"among {stuck}; depends_on must be acyclic.")
            return list(steps)
        for step in ready:
            ordered.append(step)
            done.add(step.get("id"))
            pending.remove(step)
    assert len(ordered) == len(ids)
    return ordered


def _transitive(steps):
    direct = {s.get("id"): list(s.get("depends_on") or []) for s in steps}
    out = {}

    def collect(sid, seen):
        result = set()
        for dep in direct.get(sid) or []:
            if dep in seen:
                continue
            seen.add(dep)
            result.add(dep)
            result |= collect(dep, seen)
        return result

    for sid in direct:
        out[sid] = collect(sid, set())
    return out


def validate(doc):
    """Every problem with a spec document, as one-sentence strings."""
    problems = []
    if not isinstance(doc, dict):
        raise OpSpecError("an Op spec is a mapping with schema, id, version, "
                          "and steps.")

    for key, phase in REFUSED_TOP.items():
        if key in doc:
            problems.append(f"the Op spec declares {key}:, which the runner "
                            f"subset does not carry — {phase} adds it.")
    for key in sorted(set(doc) - TOP_KEYS - set(REFUSED_TOP)):
        problems.append(f"unknown top-level Op spec key {key!r}; the Op spec "
                        f"vocabulary is closed.")
    if doc.get("schema") != SCHEMA_STRING:
        problems.append(f"the Op spec schema must be {SCHEMA_STRING!r}, got "
                        f"{doc.get('schema')!r}.")
    for field in ("id", "version", "name"):
        if not doc.get(field):
            problems.append(f"the Op spec is missing required field {field!r}.")

    inputs = doc.get("inputs") or []
    input_names = set()
    if not isinstance(inputs, list):
        problems.append("the Op spec's inputs must be an ordered list of "
                        "input declarations.")
        inputs = []
    for index, declared in enumerate(inputs):
        if not isinstance(declared, dict) or not declared.get("name"):
            problems.append(f"Op input #{index} must be an object with a name.")
            continue
        for key in sorted(set(declared) - INPUT_KEYS):
            problems.append(f"unknown key {key!r} on Op input "
                            f"{declared['name']!r}; the input vocabulary is "
                            f"closed.")
        if declared["name"] in input_names:
            problems.append(f"Op input {declared['name']!r} is declared twice.")
        input_names.add(declared["name"])

    track = doc.get("track")
    if track is not None:
        if not isinstance(track, dict):
            problems.append("the Op spec's track must be an object with a "
                            "records list.")
        else:
            for key in sorted(set(track) - TRACK_KEYS):
                problems.append(f"unknown key {key!r} under track:; the Track "
                                f"vocabulary is closed.")

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("the Op spec must declare a non-empty steps list.")
        raise OpSpecError(problems)

    step_ids = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("id"):
            problems.append(f"Op step #{index} must be an object with an id.")
            continue
        sid = step["id"]
        if sid in step_ids:
            problems.append(f"Op step id {sid!r} is declared twice; step ids "
                            f"are unique.")
        step_ids.add(sid)
        if not STEP_ID_RE.match(str(sid)):
            problems.append(f"Op step id {sid!r} must be lowercase "
                            f"alphanumerics and single hyphens.")

    for step in steps:
        if not isinstance(step, dict) or not step.get("id"):
            continue
        sid = step["id"]
        for key, phase in REFUSED_STEP.items():
            if key in step:
                problems.append(f"Op step {sid!r} declares a {key}: step, "
                                f"which the runner subset does not carry — "
                                f"{phase} adds it.")
        for key in sorted(set(step) - STEP_KEYS - set(REFUSED_STEP)):
            problems.append(f"unknown key {key!r} on Op step {sid!r}; the step "
                            f"vocabulary is closed.")
        for dep in step.get("depends_on") or []:
            if dep not in step_ids:
                problems.append(f"Op step {sid!r} depends on {dep!r}, which "
                                f"the Op spec does not declare.")
            if dep == sid:
                problems.append(f"Op step {sid!r} depends on itself.")

        cog = step.get("cog")
        if not isinstance(cog, dict):
            if "tool" not in step and "human" not in step:
                problems.append(f"Op step {sid!r} declares no cog:; a Cog step "
                                f"is the only step kind in the runner subset.")
        else:
            for key in sorted(set(cog) - COG_KEYS):
                problems.append(f"unknown key {key!r} under Op step {sid!r}'s "
                                f"cog:; the step vocabulary is closed.")
            for field in ("id", "source", "task"):
                if not cog.get(field):
                    problems.append(f"Op step {sid!r} is missing cog.{field}.")

        gate = step.get("gate")
        if gate is not None:
            if not isinstance(gate, dict):
                problems.append(f"Op step {sid!r}'s gate must be an object "
                                f"with a policy.")
            else:
                for key in sorted(set(gate) - GATE_KEYS):
                    problems.append(f"unknown key {key!r} under Op step "
                                    f"{sid!r}'s gate:; the Gate vocabulary is "
                                    f"closed.")
                policy = gate.get("policy", GATE_POLICY)
                if policy == "human":
                    problems.append(f"Op step {sid!r} declares gate.policy: "
                                    f"human, which the runner subset does not "
                                    f"carry — phase 3 adds it.")
                elif policy != GATE_POLICY:
                    problems.append(f"Op step {sid!r} declares gate.policy "
                                    f"{policy!r}; {GATE_POLICY!r} is the only "
                                    f"policy in the runner subset.")
                if gate.get("guards"):
                    problems.append(f"Op step {sid!r} declares gate.guards, "
                                    f"which are not in the runner subset; "
                                    f"Guards are system-side verifiers a "
                                    f"hosting environment supplies.")

        on_fail = step.get("on_fail", "stop")
        if on_fail not in ON_FAIL:
            problems.append(f"Op step {sid!r} declares on_fail {on_fail!r}; "
                            f"choose one of {list(ON_FAIL)}.")

        foreach = step.get("foreach")
        loop_vars = set()
        if foreach is not None:
            if not isinstance(foreach, dict):
                problems.append(f"Op step {sid!r}'s foreach must be an object "
                                f"with items and as.")
            else:
                for key in sorted(set(foreach) - FOREACH_KEYS):
                    problems.append(f"unknown key {key!r} under Op step "
                                    f"{sid!r}'s foreach:; the step vocabulary "
                                    f"is closed.")
                if "items" not in foreach:
                    problems.append(f"Op step {sid!r}'s foreach declares no "
                                    f"items expression.")
                if not foreach.get("as"):
                    problems.append(f"Op step {sid!r}'s foreach declares no "
                                    f"loop variable (as:).")
                else:
                    loop_vars.add(foreach["as"])

    if problems:
        raise OpSpecError(problems)

    ordered = _order(steps, problems)
    if problems:
        raise OpSpecError(problems)
    deps = _transitive(steps)

    for step in steps:
        sid = step["id"]
        dep_ids = deps.get(sid, set())
        loop_vars = {(step.get("foreach") or {}).get("as")} - {None}
        if step.get("foreach") is not None:
            _expr_problems((step["foreach"] or {}).get("items"),
                           f"Op step {sid!r} foreach.items", input_names,
                           step_ids, dep_ids, set(), problems)
        _expr_problems(step.get("input") or {}, f"Op step {sid!r} input",
                       input_names, step_ids, dep_ids, loop_vars, problems)

    outputs = doc.get("outputs") or {}
    if not isinstance(outputs, dict):
        problems.append("the Op spec's outputs must be an object of mapping "
                        "expressions.")
    else:
        _expr_problems(outputs, "the Op spec's outputs", input_names, step_ids,
                       step_ids, set(), problems)

    if problems:
        raise OpSpecError(problems)
    return ordered


class OpSpec:
    """A validated Op spec and the order its steps run in."""

    def __init__(self, doc, path=None):
        self.doc = doc
        self.path = Path(path).resolve() if path else None
        self.ordered = validate(doc)
        self.id = doc["id"]
        self.version = str(doc.get("version", ""))
        self.name = doc.get("name")
        self.description = doc.get("description")
        self.inputs = doc.get("inputs") or []
        self.steps = doc["steps"]
        self.outputs = doc.get("outputs") or {}
        self.track = doc.get("track") or {}

    def step(self, sid):
        for step in self.steps:
            if step["id"] == sid:
                return step
        raise OpSpecError(f"the Op spec declares no step {sid!r}.")

    def dependents(self):
        """step id -> the ids that transitively depend on it."""
        deps = _transitive(self.steps)
        out = {s["id"]: set() for s in self.steps}
        for sid, upstream in deps.items():
            for up in upstream:
                out.setdefault(up, set()).add(sid)
        return out

    def sha256(self):
        if not self.path or not self.path.exists():
            return None
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def build_inputs(self, request):
        """Declared input values for a request document: required present,
        defaults applied, declared schemas validated, unknown keys refused."""
        if not isinstance(request, dict):
            raise OpSpecError("an Op request is a JSON object of declared "
                              "inputs.")
        declared = {i["name"]: i for i in self.inputs}
        problems, values = [], {}
        for key in sorted(set(request) - set(declared)):
            problems.append(f"the Op request carries undeclared input "
                            f"{key!r}; declare it in op.yaml or remove it.")
        for name, spec in declared.items():
            if request.get(name) is not None:
                values[name] = request[name]
            elif spec.get("default") is not None:
                values[name] = spec["default"]
            elif spec.get("required", True):
                problems.append(f"the Op request is missing required input "
                                f"{name!r}.")
                continue
            else:
                values[name] = spec.get("default")
            schema = spec.get("schema")
            if schema and _SchemaValidator and values.get(name) is not None:
                validator = _SchemaValidator(schema)
                for error in list(validator.iter_errors(values[name]))[:3]:
                    where = ".".join(str(x) for x in error.absolute_path) or "$"
                    problems.append(f"Op input {name!r} violates its declared "
                                    f"schema at {where}: {error.message}.")
        if problems:
            raise OpSpecError(problems)
        return values

    def example_request(self):
        """A starting request document: declared defaults, nulls elsewhere."""
        return {i["name"]: i.get("default") for i in self.inputs}


def load_document(path):
    """The spec document from JSON or YAML, unvalidated."""
    path = Path(path)
    text = path.read_text()
    try:
        if path.suffix == ".json":
            return json.loads(text)
        return yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise OpSpecError(f"{path.name} is not readable as "
                          f"{'JSON' if path.suffix == '.json' else 'YAML'}: "
                          f"{exc}") from exc


def load(path):
    """Load and validate an Op spec file."""
    return OpSpec(load_document(path), path=path)
