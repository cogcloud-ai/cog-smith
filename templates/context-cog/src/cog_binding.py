"""Shared binding-record machinery. Byte-identical across the cog-forge Cogs;
tools/check_copies.py enforces the sync.

One binding-record shape for BOTH paths (`resolve` and `use`), and ONE
fail-closed normalization for everything that can influence the binding —
saved records AND environment overrides. The re-review found that COG_MODEL_*
overrides bypassed the locality check, the transport policy, and pin
provenance; every load now flows through normalize(), and a record that
violates policy carries `_violations`, which invoke() treats as fatal before
any evidence can leave the machine.

model.json IS the binding record. It is installation state (gitignored), and it
is copied verbatim into every invocation result.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

RECORD_SCHEMA = "openteams/binding-record [0.1]"

DEFAULTS = {
    "record": RECORD_SCHEMA,
    "capability": "model-endpoint/openai-compatible",
    "endpoint": "http://127.0.0.1:8080/v1",
    "model": "local",
    "api_key_env": None,
    "response_format": "json_object",
    "locality": "local",
    "pinned": False,
    "satisfier": {"source": "default",
                  "note": "built-in default; run resolve or use to bind explicitly"},
    "served_model": {"revision": None, "weights_sha256": None, "verified": False},
}

LOCALITIES = ("local", "customer-vpc", "cloud")
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ---------------------------------------------------------------- manifest --
#
# The profile manifest (openteams/cog-manifest [0.1]) lives in ONE of:
#   - pixi.toml under [tool.cog]  (default; cog-execution ADR D9) — `version`
#     and `summary` may be omitted there and fall back to [workspace]
#     version / description, so each is stated once;
#   - cog.yaml                     (the standalone YAML form).
# Same rules as cog-smith's smith_manifest.py — kept in step by hand because
# machinery is copied verbatim into every Cog and cannot import smith.

MANIFEST_TOOL_TABLE = "cog"


def _read_pixi_manifest(root):
    p = Path(root) / "pixi.toml"
    if not p.exists():
        return None
    try:
        import tomllib
    except ImportError as e:                          # Python < 3.11
        raise RuntimeError("reading a [tool.cog] manifest in pixi.toml needs "
                           "Python 3.11+ (tomllib)") from e
    with open(p, "rb") as f:
        doc = tomllib.load(f)
    tool = doc.get("tool")
    if not (isinstance(tool, dict) and isinstance(tool.get(MANIFEST_TOOL_TABLE), dict)):
        return None
    m = dict(tool[MANIFEST_TOOL_TABLE])
    ws = doc.get("workspace") or doc.get("project") or {}
    if "version" not in m and ws.get("version") is not None:
        m["version"] = ws["version"]
    if "summary" not in m and ws.get("description") is not None:
        m["summary"] = ws["description"]
    return m


def manifest_path(root):
    """The file this Cog's manifest lives in (pixi.toml or cog.yaml), or None."""
    root = Path(root)
    if _read_pixi_manifest(root) is not None:
        return root / "pixi.toml"
    if (root / "cog.yaml").exists():
        return root / "cog.yaml"
    return None


def load_manifest(root):
    """This Cog's own manifest: identity and declared requirements.
    Raises FileNotFoundError when the package carries neither form, and
    ValueError when it carries both (readers could disagree about the Cog)."""
    root = Path(root)
    pixi = _read_pixi_manifest(root)
    yaml_path = root / "cog.yaml"
    if pixi is not None and yaml_path.exists():
        raise ValueError(f"{root}: both pixi.toml [tool.cog] and cog.yaml are "
                         f"present — a package carries exactly one manifest")
    if pixi is not None:
        return pixi
    if yaml_path.exists():
        return yaml.safe_load(yaml_path.read_text()) or {}
    raise FileNotFoundError(f"{root}: no manifest — neither pixi.toml with a "
                            f"[tool.cog] table nor cog.yaml")


def declared_locality_constraint(manifest):
    """The locality constraint on the model-endpoint requirement; 'any' if unstated."""
    for req in manifest.get("requires") or []:
        if isinstance(req, dict) and str(req.get("capability", "")).startswith("model-endpoint/"):
            return req.get("locality", "any")
    return "any"


def locality_allowed(constraint, locality):
    if constraint in (None, "any"):
        return True
    return constraint == locality


# ---------------------------------------------------------- endpoint policy --

def endpoint_policy(endpoint, insecure_http=False):
    """(ok, reason). Non-loopback plain HTTP is refused unless explicitly waived
    — evidence bundles are repository content and must not transit cleartext by
    accident. Credential-bearing URLs are refused unconditionally: the record
    stores a credential REFERENCE (env var name), never a credential."""
    try:
        parts = urlsplit(endpoint)
        _ = parts.port          # invalid ports raise here, not at urlsplit
    except ValueError as e:
        return False, f"unparseable endpoint: {e}"
    if parts.scheme in ("http", "https") and not parts.hostname:
        return False, "endpoint has no host"
    if parts.username or parts.password:
        return False, ("credential-bearing URL refused — put the key in an "
                       "environment variable and reference it via api_key_env")
    if parts.scheme == "https":
        return True, "https"
    if parts.scheme != "http":
        return False, f"unsupported scheme {parts.scheme!r}"
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK:
        return True, "http loopback"
    if insecure_http:
        return True, "http non-loopback, explicitly waived (insecure_http)"
    return False, (f"plain HTTP to non-loopback host {host!r} refused — use https, "
                   f"or pass --insecure-http for a trusted private network")


def _is_loopback(endpoint):
    try:
        return (urlsplit(endpoint).hostname or "").lower() in _LOOPBACK
    except ValueError:
        return False


def sanitize_url(url):
    """Strip credentials from a URL (git remotes can embed tokens)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


# ------------------------------------------------------------- record load --

# Identity-critical fields a written record must carry with the right types.
# Once model.json exists, these are NEVER filled in from built-in defaults.
_RECORD_FIELDS = {
    "capability": str,
    "endpoint": str,
    "model": str,
    "locality": str,
    "pinned": bool,
    "satisfier": dict,
    # v3 P0-2: the served-model block is required — `pinned` is meaningless
    # without the evidence structure it summarizes.
    "served_model": dict,
}
_OPTIONAL_FIELDS = {
    "record": str,
    "api_key_env": (str, type(None)),
    "response_format": (str, type(None)),
    "insecure_http": bool,
    "model_aliases": list,
}
_SERVED_FIELDS = {
    "revision": (str, type(None)),
    "weights_sha256": (str, type(None)),
    "effective_sha256": (str, type(None)),
    "lineage": dict,
    "lineage_missing": list,
    "verified": bool,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_digest(value):
    """True iff value is a lowercase 64-char sha256 hex digest. The ONE digest
    predicate — resolution and record validation must agree on what counts as
    pin evidence (v4 P0-1: hashing an unvalidated claim does not convert it
    into artifact evidence)."""
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


_is_digest = is_digest


def served_pin_evidence(served):
    """True iff the served_model block carries real pin evidence."""
    if not isinstance(served, dict):
        return False
    return bool(served.get("revision")
                or _is_digest(served.get("weights_sha256"))
                or _is_digest(served.get("effective_sha256")))


def validate_record(record):
    """Structural + semantic validation of a binding record. Returns problems.
    Used identically on LOAD and WRITE (v3 P0-2) — no accepted record can claim
    a served-model pin without carrying the evidence for it."""
    problems = []
    if not isinstance(record, dict):
        return [f"binding record must be an object, got {type(record).__name__}"]
    for field, types in _RECORD_FIELDS.items():
        if field not in record:
            problems.append(f"missing required field {field!r} — "
                            f"incomplete record; re-run resolve/use")
        elif not isinstance(record[field], types):
            problems.append(f"field {field!r} has wrong type "
                            f"{type(record[field]).__name__}")
    for field, types in _OPTIONAL_FIELDS.items():
        if field in record and not isinstance(record[field], types):
            problems.append(f"field {field!r} has wrong type "
                            f"{type(record[field]).__name__}")
    sat = record.get("satisfier")
    if isinstance(sat, dict) and "declared" in sat and not isinstance(sat["declared"], bool):
        problems.append(f"satisfier.declared has wrong type "
                        f"{type(sat['declared']).__name__}")
    served = record.get("served_model")
    if isinstance(served, dict):
        for field, types in _SERVED_FIELDS.items():
            if field in served and not isinstance(served[field], types):
                problems.append(f"served_model.{field} has wrong type "
                                f"{type(served[field]).__name__}")
        for field in ("weights_sha256", "effective_sha256"):
            v = served.get(field)
            if isinstance(v, str) and not _is_digest(v):
                problems.append(f"served_model.{field} is not a sha256 digest")
    if problems:
        return problems
    if record.get("record", RECORD_SCHEMA) != RECORD_SCHEMA:
        problems.append(f"unknown record schema {record.get('record')!r} "
                        f"(expected {RECORD_SCHEMA!r})")
    rf = record.get("response_format")
    if rf not in (None, "json_object", "json_schema"):
        problems.append(f"unknown response_format {rf!r}")
    if record.get("locality") not in LOCALITIES:
        problems.append(f"unknown locality {record.get('locality')!r}")
    # the invariant itself: pinned <=> evidence
    if bool(record.get("pinned")) != served_pin_evidence(record.get("served_model")):
        if record.get("pinned"):
            problems.append("pinned is true but served_model carries no revision "
                            "or valid digest — a pin claim needs pin evidence")
        else:
            problems.append("pinned is false but served_model carries pin "
                            "evidence — contradictory provenance")
    return problems


def _load_record_file(config):
    """Parse and validate model.json via the shared record validator. Returns
    (record, problems). Any problem means the record is unusable — the caller
    records violations and does NOT merge partial content over defaults."""
    try:
        loaded = json.loads(config.read_text())
    except json.JSONDecodeError as e:
        return None, [f"model.json is not valid JSON ({e}) — re-run resolve/use"]
    if not isinstance(loaded, dict):
        return None, [f"model.json must be an object, got "
                      f"{type(loaded).__name__} — re-run resolve/use"]
    problems = [f"model.json: {p}" for p in validate_record(loaded)]
    return (None, problems) if problems else (loaded, [])


def load_record(root, environ=None):
    """Load and NORMALIZE the binding record. Returns (record, key, source).

    Precedence, lowest to highest:
        1. built-in defaults      (the sibling model Cog on loopback)
        2. model.json             (the binding record; written by resolve/use)
        3. COG_MODEL_* env vars   (explicit one-off override)

    Fail-closed invariants, enforced here for EVERY path:
      - a malformed model.json is a violation, never a silent fallback to a
        different model (cog-demo finding 1: silent model drift);
      - an endpoint override invalidates the pin and satisfier, recomputes
        locality (loopback -> local, else COG_MODEL_LOCALITY or cloud), and
        downgrades a json_schema decoder guarantee it can no longer promise;
      - a model override invalidates the pin and marks the satisfier overridden;
      - transport policy and the manifest locality constraint are re-checked on
        the final record, whatever produced it.
    Violations are recorded in record["_violations"]; callers that transmit
    evidence must treat a non-empty list as fatal.
    """
    env = os.environ if environ is None else environ
    root = Path(root)
    config = root / "model.json"

    record = dict(DEFAULTS)
    source = "default"
    violations = []
    overrides = []

    if config.exists():
        loaded, load_problems = _load_record_file(config)
        if load_problems:
            # Corrupt or structurally invalid installation state must not
            # silently become "answer from the default model" (re-review v2
            # P1-1: {} / [] / partial objects inherited defaults before).
            violations.extend(load_problems)
            source = "model.json (malformed)"
        else:
            record.update(loaded)
            source = "model.json"

    if env.get("COG_MODEL_ENDPOINT"):
        record["endpoint"] = env["COG_MODEL_ENDPOINT"]
        overrides.append("endpoint")
        source = "env"
        # An address change voids the pin, the satisfier, the served-model
        # identity, and the locality attribution of whatever it replaced —
        # leaving the old revision/digest behind would be contradictory
        # provenance (v3 P0-2).
        record["pinned"] = False
        record["served_model"] = {"revision": None, "weights_sha256": None,
                                  "verified": False}
        if env.get("COG_MODEL_LOCALITY"):
            record["locality"] = env["COG_MODEL_LOCALITY"]
        else:
            record["locality"] = "local" if _is_loopback(record["endpoint"]) else "cloud"
        if record.get("response_format") == "json_schema":
            # the decoder-constraint guarantee belonged to the replaced endpoint
            record["response_format"] = "json_object"
            overrides.append("response_format:json_schema->json_object")
        record["insecure_http"] = env.get("COG_MODEL_INSECURE_HTTP", "") == "1"

    if env.get("COG_MODEL_NAME"):
        record["model"] = env["COG_MODEL_NAME"]
        overrides.append("model")
        record["pinned"] = False
        record["served_model"] = {"revision": None, "weights_sha256": None,
                                  "verified": False}
        if source != "env":
            source = "env"

    if overrides:
        record["satisfier"] = {"source": "env-override", "overrides": overrides}

    # --- validation of the FINAL record, whatever produced it --------------
    if record.get("locality") not in LOCALITIES:
        violations.append(f"unknown locality {record.get('locality')!r}")

    ok, reason = endpoint_policy(record["endpoint"], record.get("insecure_http", False))
    if not ok:
        violations.append(f"transport: {reason}"
                          + (" (set COG_MODEL_INSECURE_HTTP=1 for a trusted "
                             "private network)" if overrides else ""))

    try:
        constraint = declared_locality_constraint(load_manifest(root))
    except FileNotFoundError:
        constraint = "any"
    if not locality_allowed(constraint, record.get("locality")):
        violations.append(f"locality: manifest requires {constraint!r}, "
                          f"binding is {record.get('locality')!r}")

    if violations:
        record["_violations"] = violations

    key = env.get("COG_MODEL_API_KEY", "")
    if not key and record.get("api_key_env"):
        key = env.get(record["api_key_env"], "")

    return record, key, source


# ------------------------------------------------------------ record write --

def write_record(root, record):
    """Validate via the SAME validator as load, then write model.json."""
    record = {k: v for k, v in record.items() if not k.startswith("_")}
    record["record"] = RECORD_SCHEMA
    problems = validate_record(record)
    if problems:
        raise ValueError("refusing to write binding record: " + "; ".join(problems))
    ok, reason = endpoint_policy(record["endpoint"], record.get("insecure_http", False))
    if not ok:
        raise ValueError(f"transport: {reason}")
    path = Path(root) / "model.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


# ------------------------------------------------------------------ semver --

_PRE_ORDER = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2, "c": 2}


def parse_version(v):
    """(release_tuple, prerelease_key). Handles 1.2.3, 1.2.3-rc.1, 1.2.3a1.
    Raises ValueError on an unparseable version — a release resolver must fail
    invalid identity explicitly, never coerce it to 0.0.0 (v3 review)."""
    raw = v
    v = str(v).strip().split("+", 1)[0]
    m = re.match(r"^v?(\d+(?:\.\d+)*)(?:[-.]?(.*))?$", v)
    if not m:
        raise ValueError(f"invalid version {raw!r}")
    release = tuple(int(x) for x in m.group(1).split("."))
    release = (release + (0, 0, 0))[:3]
    pre = (m.group(2) or "").strip().lower() or None
    if pre is None:
        return release, None
    pm = re.match(r"^(alpha|beta|rc|a|b|c)[-.]?(\d*)", pre)
    if pm:
        return release, (_PRE_ORDER[pm.group(1)], int(pm.group(2) or 0))
    return release, (3, 0)   # unknown prerelease tag: after rc, before release


def _cmp(a, b):
    ra, pa = parse_version(a)
    rb, pb = parse_version(b)
    if ra != rb:
        return -1 if ra < rb else 1
    # a prerelease sorts BEFORE its release
    if pa is None and pb is None:
        return 0
    if pa is None:
        return 1
    if pb is None:
        return -1
    return -1 if pa < pb else (0 if pa == pb else 1)


def satisfies(actual, constraint):
    """Comma-separated clauses, each >=, <=, ==, !=, >, <, or bare (>=).
    All clauses must hold."""
    for clause in str(constraint).split(","):
        clause = clause.strip()
        if not clause:
            continue
        op = ">="
        for candidate in (">=", "<=", "==", "!=", ">", "<"):
            if clause.startswith(candidate):
                op, clause = candidate, clause[len(candidate):].strip()
                break
        c = _cmp(actual, clause)
        ok = {">=": c >= 0, "<=": c <= 0, "==": c == 0,
              "!=": c != 0, ">": c > 0, "<": c < 0}[op]
        if not ok:
            return False
    return True
