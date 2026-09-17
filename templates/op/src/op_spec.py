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
#: A `foreach` loop variable is a name: it becomes a mapping-path root.
LOOP_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
#: Every `$`-prefixed name the closed vocabulary knows. A `$` key that is not
#: one of these is an unknown operator WHEREVER it appears — sibling keys do
#: not turn `$join` into ordinary data (contract §2).
KNOWN_DOLLAR_KEYS = frozenset({"$from", "$default", "$path", "$run_dir",
                               "$stem", "$literal"})
PATH_ROOTS = ("inputs", "steps", "run", "request")

# A Cog's conventional lifecycle tasks, for steps whose interface declares no
# audience. Kept here (not imported from cog-smith) because this module is
# vendored into every Op package and must run without cog-smith installed.
LIFECYCLE_TASKS = {"resolve", "use", "check", "eval", "test", "bundle", "serve"}


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


def dollar_keys(expr):
    """The `$`-prefixed keys of a mapping-expression object."""
    return sorted(k for k in expr if isinstance(k, str) and k.startswith("$"))


def unknown_dollar_keys(expr):
    """The `$`-prefixed keys that name no operator in the closed vocabulary."""
    return sorted(k for k in dollar_keys(expr) if k not in KNOWN_DOLLAR_KEYS)


def operator_keys(expr):
    """The operator key set of a mapping-expression object, or None when the
    object is an ordinary one to walk recursively.

    Only an EXACT recognized key set is an operator (contract §2). Any other
    object is walked — `{"$from": "literal text", "label": "x"}` and
    `{"$from": "inputs.note", "$stem": "x"}` are both ordinary data — EXCEPT
    that a `$`-prefixed key naming no operator at all (`{"$join": [...]}`,
    with or without siblings) is an unknown operator and is always refused.
    """
    keys = frozenset(expr)
    if keys in OPERATORS:
        return keys
    unknown = unknown_dollar_keys(expr)
    if unknown:
        raise OpSpecError(
            f"mapping expression uses unknown operator(s) {unknown}; the "
            f"mapping vocabulary is closed ({OPERATOR_NAMES}).")
    return None


def run_dir_path(subpath, run_dir):
    """`<run dir>/<subpath>`, created — refusing anything that would land
    outside the run. `$run_dir` makes a directory INSIDE this run, so an
    absolute operand, a `..` escape, and a symlink out of the run are all
    refused BEFORE anything is created."""
    if subpath is None:
        subpath = ""
    if not isinstance(subpath, str):
        raise OpSpecError(f"$run_dir takes a relative subpath string, got "
                          f"{subpath!r}.")
    candidate = Path(subpath)
    base = Path(run_dir).resolve()
    if candidate.is_absolute():
        raise OpSpecError(f"$run_dir subpath {subpath!r} is absolute; "
                          f"$run_dir names a directory inside this run.")
    target = (base / candidate).resolve()
    if target != base and base not in target.parents:
        raise OpSpecError(f"$run_dir subpath {subpath!r} resolves outside the "
                          f"run directory; $run_dir names a directory inside "
                          f"this run.")
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def evaluate(expr, ctx):
    """Resolve a mapping expression against a run context
    {inputs, steps, run, request, <loop variables>}."""
    if isinstance(expr, dict):
        keys = operator_keys(expr)
        if keys is not None:
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
                return run_dir_path(value, ctx["run"]["dir"])
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
            if (keys in OPERATORS and "$from" in keys
                    and str(node["$from"]).split(".")[0] == "steps"):
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
        if keys in OPERATORS:
            if "$literal" in keys:
                return
            if "$from" in keys:
                _path_problems(expr["$from"], where, input_names, step_ids,
                               dep_ids, loop_vars, problems)
                if "$default" in keys:
                    _expr_problems(expr["$default"], where, input_names,
                                   step_ids, dep_ids, loop_vars, problems)
                return
            if "$run_dir" in keys and isinstance(expr["$run_dir"], str):
                subpath = expr["$run_dir"]
                if (Path(subpath).is_absolute()
                        or ".." in Path(subpath).parts):
                    problems.append(
                        f"{where} asks $run_dir for {subpath!r}; $run_dir "
                        f"names a directory inside this run, so the subpath "
                        f"is relative and never leaves the run directory.")
                    return
            for key in ("$path", "$run_dir", "$stem"):
                if key in keys:
                    _expr_problems(expr[key], where, input_names, step_ids,
                                   dep_ids, loop_vars, problems)
            return
        unknown = unknown_dollar_keys(expr)
        if unknown:
            problems.append(
                f"{where} uses unknown mapping operator(s) {unknown}; the "
                f"mapping vocabulary is closed ({OPERATOR_NAMES}).")
            return
        for key, value in expr.items():
            if not isinstance(key, str):
                problems.append(f"{where} has key {key!r}, which is not a "
                                f"string; a request document's keys are "
                                f"strings.")
                continue
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


def _has_id(step):
    """True when a step is an object with a string id — checked before the id
    is hashed, matched, or used in a message."""
    return isinstance(step, dict) and isinstance(step.get("id"), str) \
        and bool(step["id"])


def _deps(step):
    """A step's declared depends_on, as a list (malformed values are reported
    by `validate`, which runs before any ordering)."""
    declared = step.get("depends_on")
    return list(declared) if isinstance(declared, list) else []


def _order(steps, problems):
    """Topological order, ties broken by spec order. Records a cycle as a
    problem and returns the steps unordered.

    ONE step at a time: the earliest ready step in spec order runs next, so a
    step that becomes ready mid-batch still runs before a later independent
    step (contract §3, "ties break by spec order")."""
    ids = [s.get("id") for s in steps]
    pending = list(steps)
    done, ordered = set(), []
    while pending:
        index = next((i for i, s in enumerate(pending)
                      if all(d in done for d in _deps(s))), None)
        if index is None:
            stuck = sorted(str(s.get("id")) for s in pending)
            problems.append(f"the Op spec's steps form a dependency cycle "
                            f"among {stuck}; depends_on must be acyclic.")
            return list(steps)
        step = pending.pop(index)
        ordered.append(step)
        done.add(step.get("id"))
    assert len(ordered) == len(ids)
    return ordered


def _transitive(steps):
    direct = {s.get("id"): _deps(s) for s in steps}
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


def schema_problems(schema, name):
    """Why a declared input `schema:` is not a usable JSON Schema. Checked at
    LOAD, so a bad schema is a named problem rather than a surprise
    jsonschema error at request time."""
    if _SchemaValidator is None:                       # pragma: no cover
        return []
    try:
        _SchemaValidator.check_schema(schema)
    except Exception as exc:                           # jsonschema.SchemaError
        detail = " ".join(str(exc).split())[:160]
        return [f"Op input {name!r} declares a schema that is not valid JSON "
                f"Schema: {detail}."]
    return []


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
    for key in sorted(str(k) for k in set(doc) - TOP_KEYS - set(REFUSED_TOP)):
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
        if not isinstance(declared, dict) or not isinstance(
                declared.get("name"), str) or not declared["name"]:
            problems.append(f"Op input #{index} must be an object with a "
                            f"name (a string).")
            continue
        for key in sorted(str(k) for k in set(declared) - INPUT_KEYS):
            problems.append(f"unknown key {key!r} on Op input "
                            f"{declared['name']!r}; the input vocabulary is "
                            f"closed.")
        if declared["name"] in input_names:
            problems.append(f"Op input {declared['name']!r} is declared twice.")
        input_names.add(declared["name"])
        if "schema" in declared:
            problems.extend(schema_problems(declared["schema"],
                                            declared["name"]))

    track = doc.get("track")
    if track is not None:
        if not isinstance(track, dict):
            problems.append("the Op spec's track must be an object with a "
                            "records list.")
        else:
            for key in sorted(str(k) for k in set(track) - TRACK_KEYS):
                problems.append(f"unknown key {key!r} under track:; the Track "
                                f"vocabulary is closed.")

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("the Op spec must declare a non-empty steps list.")
        raise OpSpecError(problems)

    step_ids = set()
    for index, step in enumerate(steps):
        if not _has_id(step):
            problems.append(f"Op step #{index} must be an object with an id "
                            f"(a string).")
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
        if not _has_id(step):
            continue
        sid = step["id"]
        for key, phase in REFUSED_STEP.items():
            if key in step:
                problems.append(f"Op step {sid!r} declares a {key}: step, "
                                f"which the runner subset does not carry — "
                                f"{phase} adds it.")
        for key in sorted(str(k) for k in set(step) - STEP_KEYS
                          - set(REFUSED_STEP)):
            problems.append(f"unknown key {key!r} on Op step {sid!r}; the step "
                            f"vocabulary is closed.")
        declared_deps = step.get("depends_on")
        if declared_deps is not None and not isinstance(declared_deps, list):
            problems.append(f"Op step {sid!r} declares depends_on "
                            f"{declared_deps!r}; depends_on is a list of step "
                            f"ids.")
            declared_deps = []
        for dep in declared_deps or []:
            if not isinstance(dep, str):
                problems.append(f"Op step {sid!r} depends on {dep!r}, which is "
                                f"not a step id.")
                continue
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
            for key in sorted(str(k) for k in set(cog) - COG_KEYS):
                problems.append(f"unknown key {key!r} under Op step {sid!r}'s "
                                f"cog:; the step vocabulary is closed.")
            for field in ("id", "source", "task"):
                if not cog.get(field):
                    problems.append(f"Op step {sid!r} is missing cog.{field}.")
            # A Cog's identity, source and task are STRINGS: a list-valued
            # source is an invalid spec at load, not a path-construction
            # crash when the step is about to run.
            for field in ("id", "version", "source", "task"):
                if field in cog and cog[field] is not None \
                        and not isinstance(cog[field], str):
                    problems.append(f"Op step {sid!r} declares cog.{field} "
                                    f"{cog[field]!r}; cog.{field} is a "
                                    f"string.")

        gate = step.get("gate")
        if gate is not None:
            if not isinstance(gate, dict):
                problems.append(f"Op step {sid!r}'s gate must be an object "
                                f"with a policy.")
            else:
                for key in sorted(str(k) for k in set(gate) - GATE_KEYS):
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
                for key in sorted(str(k) for k in set(foreach) - FOREACH_KEYS):
                    problems.append(f"unknown key {key!r} under Op step "
                                    f"{sid!r}'s foreach:; the step vocabulary "
                                    f"is closed.")
                if "items" not in foreach:
                    problems.append(f"Op step {sid!r}'s foreach declares no "
                                    f"items expression.")
                loop_var = foreach.get("as")
                if not loop_var:
                    problems.append(f"Op step {sid!r}'s foreach declares no "
                                    f"loop variable (as:).")
                elif not (isinstance(loop_var, str)
                          and LOOP_VAR_RE.match(loop_var)):
                    # Checked before it is ever hashed or used as a context
                    # key: a list-valued `as` used to raise a bare TypeError.
                    problems.append(f"Op step {sid!r} declares foreach.as "
                                    f"{loop_var!r}; a loop variable is a name "
                                    f"(letters, digits and underscores, not "
                                    f"starting with a digit).")
                else:
                    loop_vars.add(loop_var)

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
        defaults applied, declared schemas validated, unknown keys refused.

        ABSENCE is not a value. An optional input the request omits and whose
        declaration carries no `default` key is simply not in the built
        inputs: a `$from` on it takes its `$default` or is the named "not
        available in this run" error. Only SUPPLIED values and DECLARED
        defaults (including `default: null`) are schema-validated — a
        declaration may say `type: string` without forcing every request to
        carry the input."""
        if not isinstance(request, dict):
            raise OpSpecError("an Op request is a JSON object of declared "
                              "inputs.")
        declared = {i["name"]: i for i in self.inputs}
        problems, values = [], {}
        for key in sorted(str(k) for k in set(request) - set(declared)):
            problems.append(f"the Op request carries undeclared input "
                            f"{key!r}; declare it in op.yaml or remove it.")
        for name, spec in declared.items():
            # Presence, not truthiness: an explicit null is a supplied value,
            # a declared `default: null` satisfies an omitted input, and a
            # default never replaces a value the request actually carries.
            if name in request:
                values[name] = request[name]
            elif "default" in spec:
                values[name] = spec["default"]
            elif spec.get("required", True):
                problems.append(f"the Op request is missing required input "
                                f"{name!r}.")
                continue
            else:
                continue         # absent, not null: nothing to validate
            if "schema" in spec and _SchemaValidator is not None:
                try:
                    validator = _SchemaValidator(spec["schema"])
                    errors = list(validator.iter_errors(values[name]))[:3]
                except Exception as exc:               # jsonschema complaints
                    detail = " ".join(str(exc).split())[:160]
                    problems.append(f"Op input {name!r} declares a schema this "
                                    f"runner cannot apply: {detail}.")
                    continue
                for error in errors:
                    where = ".".join(str(x) for x in error.absolute_path) or "$"
                    problems.append(f"Op input {name!r} violates its declared "
                                    f"schema at {where}: {error.message}.")
        if problems:
            raise OpSpecError(problems)
        return values

    def example_request(self):
        """A starting request document: the declared defaults. An optional
        input with no declared default is OMITTED — writing null there would
        supply a value the author never declared."""
        return {i["name"]: i.get("default") for i in self.inputs
                if "default" in i or i.get("required", True)}


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


# ------------------------------------------------- the Cogs a step names --
#
# Shared by the runner (which checks EVERY step before invoking any of them)
# and by `smith op check`. It lives here, in the vendored machinery, so an Op
# package validates its own declarations without cog-smith installed: a Cog's
# profile manifest is read straight from `pixi.toml [tool.cog]` or `cog.yaml`.

def read_cog_manifest(source):
    """(manifest, problem) for the Cog package at SOURCE. Exactly one profile
    manifest is expected: `[tool.cog]` in pixi.toml, or cog.yaml."""
    source = Path(source)
    pixi, standalone = source / "pixi.toml", source / "cog.yaml"
    doc = None
    if pixi.exists():
        try:
            import tomllib
            with open(pixi, "rb") as handle:
                doc = tomllib.load(handle)
        except Exception as exc:
            return None, f"pixi.toml is not readable ({exc})"
    in_pixi = bool(doc and isinstance(doc.get("tool"), dict)
                   and isinstance(doc["tool"].get("cog"), dict))
    if in_pixi and standalone.exists():
        return None, ("both pixi.toml [tool.cog] and cog.yaml are present — a "
                      "package carries exactly one profile manifest")
    if in_pixi:
        manifest = dict(doc["tool"]["cog"])
        workspace = doc.get("workspace") or doc.get("project") or {}
        for key, source_key in (("version", "version"),
                                ("summary", "description")):
            if key not in manifest and workspace.get(source_key) is not None:
                manifest[key] = workspace[source_key]
        return manifest, None
    if standalone.exists():
        try:
            manifest = yaml.safe_load(standalone.read_text())
        except yaml.YAMLError as exc:
            return None, f"cog.yaml is not readable ({exc})"
        if not isinstance(manifest, dict):
            return None, "cog.yaml is not a manifest mapping"
        return manifest, None
    return None, ("it carries no profile manifest: neither pixi.toml with a "
                  "[tool.cog] table nor cog.yaml")


def cog_step_findings(step, source_dir):
    """[(level, detail)] for one Cog step's declaration, checked against the
    Cog at SOURCE_DIR: it must be that Cog, and the task must be one of its
    declared USAGE interfaces. A source that is simply absent is a warning —
    the declaration could not be verified on this machine."""
    cog = step.get("cog") or {}
    sid = step.get("id")
    source = Path(source_dir)
    if not source.is_dir():
        return [("warn", f"Op step {sid!r} names cog source "
                         f"{cog.get('source')!r}, which is not present here — "
                         f"the task declaration could not be verified on this "
                         f"machine.")]
    manifest, problem = read_cog_manifest(source)
    if manifest is None:
        return [("error", f"Op step {sid!r} names {cog.get('source')!r}, whose "
                          f"manifest is unreadable: {problem}.")]
    findings = []
    if cog.get("id") and manifest.get("id") != cog["id"]:
        findings.append(("error", f"Op step {sid!r} declares cog id "
                                  f"{cog['id']!r} but {cog.get('source')!r} "
                                  f"identifies as {manifest.get('id')!r}."))
    if cog.get("version") and manifest.get("version") != cog["version"]:
        findings.append(("warn", f"Op step {sid!r} declares version "
                                 f"{cog['version']!r} but "
                                 f"{cog.get('source')!r} is at "
                                 f"{manifest.get('version')!r}."))
    interfaces = [i for i in manifest.get("interfaces") or []
                  if isinstance(i, dict) and i.get("task") == cog.get("task")]
    if not interfaces:
        findings.append(("error", f"Op step {sid!r} names task "
                                  f"{cog.get('task')!r}, which "
                                  f"{manifest.get('id')!r} does not declare as "
                                  f"an interface."))
        return findings
    audiences = {i.get("audience")
                 or ("lifecycle" if i.get("task") in LIFECYCLE_TASKS
                     else "usage")
                 for i in interfaces}
    if "usage" not in audiences:
        findings.append(("error", f"Op step {sid!r} names task "
                                  f"{cog.get('task')!r}, which "
                                  f"{manifest.get('id')!r} declares for the "
                                  f"{sorted(audiences)[0]} audience — Op steps "
                                  f"use a Cog's usage interfaces."))
    return findings


def declaration_problems(spec, package_root):
    """Every ERROR a step's Cog declaration carries, across the whole spec —
    what the runner refuses before it invokes anything."""
    problems = []
    for step in spec.ordered:
        source = (Path(package_root)
                  / str((step.get("cog") or {}).get("source"))).resolve()
        problems.extend(detail for level, detail
                        in cog_step_findings(step, source) if level == "error")
    return problems
