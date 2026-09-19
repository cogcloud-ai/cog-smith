"""Shared core for this Cog's interfaces (cog-smith machinery, generic).

Derived from cog-forge @7fe8aca (engineering-gates PASS). Deltas from the
forge original are deliberate and documented in cog-smith's
MACHINERY.md: (1) all task-specific logic lives in task_logic.py — this file
is byte-identical across created Cogs and copy-sync-checked by `smith check`;
(2) input validation runs against the manifest-declared input schema;
(3) results are emitted in envelope v1 (see ENVELOPE.md in cog-smith):
fixed `payload` key, structured problems, ok-may-carry-problems.

Both the CLI and the web API call invoke() — the interface is how you reach
the Cog, not what it is.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_binding  # noqa: E402
import task_logic   # noqa: E402  (the ONLY per-cog module in src/)

try:
    import jsonschema
    _SchemaValidator = (getattr(jsonschema, "Draft202012Validator", None)
                        or getattr(jsonschema, "Draft7Validator", None))
except ImportError:                                    # pragma: no cover
    jsonschema = None
    _SchemaValidator = None

ROOT = Path(__file__).resolve().parent.parent

MANIFEST = cog_binding.load_manifest(ROOT)
SELF_ID = {"id": MANIFEST.get("id"), "version": MANIFEST.get("version")}

RECORD, API_KEY, BINDING_SOURCE = cog_binding.load_record(ROOT)
ENDPOINT = RECORD["endpoint"]
MODEL = RECORD["model"]
RESPONSE_FORMAT = RECORD.get("response_format")
# The caller's deadline comes from the binding, not from this file (see
# cog_binding.REQUEST_TIMEOUT_*). `cog_cli --timeout` overrides it for one call.
REQUEST_TIMEOUT_S = cog_binding.request_timeout(RECORD)

OUTPUT_SCHEMA = json.loads((ROOT / "context" / "output-schema.json").read_text())
_declared_input = (MANIFEST.get("context") or {}).get("input_schema")
INPUT_SCHEMA = (json.loads((ROOT / _declared_input).read_text())
                if _declared_input and (ROOT / _declared_input).exists() else None)

# The key whose presence marks a well-formed payload during salvage — the
# first required property of the output schema.
_MARKER = ((OUTPUT_SCHEMA.get("required") or ["abstained"])[0])


def problem(check, detail, severity="error"):
    return {"check": check, "detail": str(detail), "severity": severity}


def load_context():
    """System prompt: instructions + a fully WORKED EXAMPLE (small models copy
    examples reliably and interpret schemas poorly — cog-demo finding 3).
    output-schema.json remains the normative contract."""
    system = (ROOT / "context" / "system.md").read_text()
    example = (ROOT / "context" / "output-example.json").read_text()
    return (
        system.rstrip()
        + "\n\nReturn a single JSON object and nothing else — no prose, no code"
        " fence, no schema. The following is a complete worked example for a"
        " DIFFERENT input; produce the same shape with values from the input"
        " you are given:\n\n"
        + example.rstrip()
        + "\n\nUse \"abstained\": true with empty results if the input does not"
        " support the task."
    )


# ------------------------------------------------------------- input side --

def validate_input(bundle):
    """Preflight against the manifest-declared input schema, plus any
    task-specific checks. Returns a list of problem dicts."""
    problems = []
    if not isinstance(bundle, dict):
        return [problem("input", "input is not an object")]
    if INPUT_SCHEMA is not None and _SchemaValidator is not None:
        validator = _SchemaValidator(INPUT_SCHEMA)
        for err in sorted(validator.iter_errors(bundle),
                          key=lambda e: list(e.absolute_path)):
            where = ".".join(str(p) for p in err.absolute_path) or "$"
            problems.append(problem("input", f"{where}: {err.message}"))
    problems.extend(task_logic.check_input(bundle))
    return problems


# ------------------------------------------------------------ output side --

def extract_json(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and _MARKER not in parsed:
        for key in ("values", "value", "data", "result", "output", "response"):
            inner = parsed.get(key)
            if isinstance(inner, dict) and _MARKER in inner:
                inner["_unwrapped_from"] = key
                return inner
    return parsed


def models_match(echoed, requested, aliases=()):
    """Strict identity: exact normalized equality, or an exact match against
    the binding's declared model_aliases (forge v3 P1-2 — no substrings)."""
    norm = lambda s: str(s or "").strip().lower()
    a = norm(echoed)
    if not a:
        return False
    accepted = {norm(requested)} | {norm(x) for x in (aliases or ())}
    accepted.discard("")
    return a in accepted


def squash(s):
    """Whitespace-normalize. QUOTE RULE: a quote is verbatim up to whitespace;
    everything else is byte-exact (forge rule, documented in system.md)."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


def verbatim_quote_check(entry, sources, quote_field="evidence_quote",
                         ids_field=None, label=None):
    """Reusable grounding contract check: entry[quote_field] must be a verbatim
    (whitespace-normalized) span of a cited source text. `sources` maps
    source-id -> text. Returns a list of problem dicts."""
    out = []
    label = label or entry.get("title") or "?"
    quote = squash(entry.get(quote_field))
    cited = entry.get(ids_field) if ids_field else list(sources)
    cited = cited or []
    unknown = [i for i in cited if i not in sources]
    if unknown:
        out.append(problem("citation", f"{label}: cites unknown sources {unknown}"))
    if ids_field and not cited:
        out.append(problem("citation", f"{label}: no sources cited"))
    if not quote:
        out.append(problem("grounding", f"{label}: no {quote_field}"))
    elif not any(quote in squash(sources[i]) for i in cited if i in sources):
        out.append(problem("grounding",
                           f"{label}: {quote_field} is not a verbatim span of "
                           f"any cited source"))
    return out


def validate_output(parsed, bundle):
    """Normative JSON Schema first, then the Cog's semantic checks."""
    problems = []
    if parsed is None:
        return [problem("schema", "response did not parse as JSON")]
    if _SchemaValidator is not None:
        clean = {k: v for k, v in parsed.items() if not k.startswith("_")}
        validator = _SchemaValidator(OUTPUT_SCHEMA)
        for err in sorted(validator.iter_errors(clean),
                          key=lambda e: list(e.absolute_path)):
            where = ".".join(str(p) for p in err.absolute_path) or "$"
            problems.append(problem("schema", f"{where}: {err.message}"))
    elif _MARKER not in parsed:
        problems.append(problem("schema", f"missing '{_MARKER}'"))
    problems.extend(task_logic.check_output(parsed, bundle))
    return problems


# ------------------------------------------------------------------ health --

def health(timeout=3, deep=False):
    """Is the model dependency reachable? Returns (ok, detail)."""
    base = ENDPOINT.rstrip("/")
    if deep:
        body = {"model": MODEL, "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}]}
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        if API_KEY:
            req.add_header("Authorization", f"Bearer {API_KEY}")
        try:
            with urllib.request.urlopen(req, timeout=max(timeout, 30)) as resp:
                payload = json.loads(resp.read().decode())
            echoed = payload.get("model")
            if not echoed:
                return True, (f"{base} answered (model identity UNVERIFIED — "
                              f"provider echoed no model id)")
            if not models_match(echoed, MODEL, RECORD.get("model_aliases")):
                return False, (f"{base} answered, but as {echoed!r} — requested "
                               f"{MODEL!r}. Model identity mismatch; refusing "
                               f"to call this healthy.")
            return True, f"{base} answered as {echoed!r} (identity matches)"
        except urllib.error.HTTPError as e:
            return False, f"{base} returned HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
        except Exception as e:
            return False, f"{base} unreachable: {e!r}"
    for probe in (base.rsplit("/v1", 1)[0] + "/health", base + "/models"):
        try:
            req = urllib.request.Request(probe)
            if API_KEY:
                req.add_header("Authorization", f"Bearer {API_KEY}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True, probe
        except Exception:
            continue
    return False, f"no model endpoint at {ENDPOINT}"


# ------------------------------------------------------------------ invoke --

def _binding_report(payload_model=None, unwrapped=None, model_identity=None):
    """The complete binding identity, copied into every result — a saved
    result must identify the run without reading installation state."""
    record_copy = {k: v for k, v in RECORD.items()
                   if k != "record" and not k.startswith("_")}
    if isinstance(record_copy.get("satisfier"), dict):
        record_copy["satisfier"] = {k: v for k, v in record_copy["satisfier"].items()
                                    if k != "path"}
    return {
        "cog": dict(SELF_ID),
        "record": record_copy,
        "record_schema": RECORD.get("record", cog_binding.RECORD_SCHEMA),
        "violations": RECORD.get("_violations") or [],
        "model_echoed": payload_model,
        "model_identity": model_identity,
        "resolved_from": BINDING_SOURCE,
        "unwrapped": unwrapped,
    }


def _envelope(task, ok, payload=None, raw=None, problems=None, error=None,
              binding=None, latency=None):
    return {
        "envelope": 1,
        "cog": dict(SELF_ID),
        "task": task,
        "ok": bool(ok),
        "error": error,
        "payload": payload,
        "raw": raw,
        "problems": problems or [],
        "binding": binding or _binding_report(),
        "timing": {"latency_s": latency},
    }


def _fail(task, code, detail, binding=None):
    return _envelope(task, False, error={"code": code, "detail": detail},
                     problems=[problem(code, detail)], binding=binding)


def invoke(bundle, timeout=None, task="ask"):
    """Run this Cog against an input bundle. Returns an envelope-v1 dict.

    `timeout` is the caller's deadline for the model call, in seconds. None
    means "what the binding says" (`request_timeout_s`, default 180); an
    explicit value is a one-call override and is bounds-checked like any other.
    """
    if timeout is None:
        timeout = REQUEST_TIMEOUT_S
    else:
        bad = cog_binding.timeout_problems(timeout)
        if bad:
            return _fail(task, "invalid-timeout", "; ".join(bad))
    violations = RECORD.get("_violations")
    if violations:
        # Fail closed BEFORE anything can leave the machine.
        return _fail(task, "binding-invalid", violations)

    input_problems = validate_input(bundle)
    if input_problems:
        env = _fail(task, "invalid-input",
                    "; ".join(p["detail"] for p in input_problems[:5]))
        env["problems"] = input_problems
        return env

    ok, detail = health()
    if not ok:
        return _fail(task, "model-unavailable", detail)

    rendered = task_logic.render_input(bundle)
    if RECORD.get("locality") != "local":
        print(f"note: transmitting input (~{len(rendered)} chars) to "
              f"{ENDPOINT} [locality={RECORD.get('locality')}]", file=sys.stderr)

    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": load_context()},
            {"role": "user", "content": rendered},
        ],
    }
    if RESPONSE_FORMAT == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif RESPONSE_FORMAT == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "cog_output", "strict": True,
                            "schema": OUTPUT_SCHEMA},
        }
    req = urllib.request.Request(
        ENDPOINT.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            json.JSONDecodeError) as e:
        return _fail(task, "model-call-failed", repr(e))

    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _fail(task, "model-response-malformed",
                     f"unexpected provider payload shape: {str(payload)[:300]}",
                     binding=_binding_report(payload.get("model")
                                             if isinstance(payload, dict) else None))

    parsed = extract_json(text)
    if parsed is None:
        # F3 (review 2026-08-22): content that exists but is not parseable
        # JSON is a malformed upstream response — documented error code and
        # a 5xx at the HTTP layer, never a bare 200. Raw text retained.
        env = _fail(task, "model-response-malformed",
                    "model content did not parse as JSON",
                    binding=_binding_report(payload.get("model")))
        env["raw"] = text
        env["timing"]["latency_s"] = round(time.monotonic() - started, 3)
        return env

    # F4: the salvage marker is transport metadata, not domain payload —
    # emitted payloads must validate against the output schema EXACTLY as
    # emitted. The unwrapping fact travels in binding.unwrapped only.
    unwrapped = parsed.pop("_unwrapped_from", None)

    problems = validate_output(parsed, bundle)
    echoed = payload.get("model")
    if not echoed:
        identity = "unverified"
    elif models_match(echoed, MODEL, RECORD.get("model_aliases")):
        identity = "verified"
    else:
        identity = "mismatch"
        problems.append(problem(
            "identity",
            f"endpoint answered as {echoed!r}, requested {MODEL!r} — model "
            f"identity mismatch"))
    return _envelope(
        task, True, payload=parsed, raw=text, problems=problems,
        binding=_binding_report(payload.get("model"), unwrapped, identity),
        latency=round(time.monotonic() - started, 3))
