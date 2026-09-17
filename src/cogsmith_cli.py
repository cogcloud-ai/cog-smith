#!/usr/bin/env python3
"""cog-smith — create, validate, and describe Cogs.

    pixi run new -- --dir ../cog-meeting-highlights [--id ...] [--yes]
    pixi run check -- ../cog-meeting-highlights [--tests]
    pixi run card -- ../cog-meeting-highlights [--json]
    pixi run migrate -- ../cog-meeting-highlights [--to pixi|yaml] [--dry-run]

`new` walks the builder questions interactively (Travis's list: what are
you, what model class, what do you prohibit…), or takes everything as flags
with --yes for scripted creating. The created Cog is immediately runnable:
resolve -> check --deep -> ask.

`--envelope` on new/check/card emits an envelope-v1 result instead of
human-readable text, so the Builder Op (see output/builder-op-note.md)
can consume smith at the Op seam like any other Cog. cog-smith is a
deterministic tooling Cog with no model dependency, so its envelopes
carry `binding: null`; checker findings travel in `problems`
(ok-with-problems: the run succeeded, a Gate decides about the findings).

`--manifest pixi|yaml` on new/generate-descriptors picks where the profile
manifest is written: `[tool.cog]` in pixi.toml (the default — one file that
Nebi already reads, per the cog-execution ADR D9) or a standalone cog.yaml.
`smith check` and `smith card` read either. `migrate` converts an existing
package between the two (default: to pixi) and re-syncs its machinery.

`new --from-request request.json` creates from a COG REQUEST — one JSON
document carrying the builder answers plus optionally the drafted
context files (system.md, schemas, worked example, sample bundle,
fixture, COG.md). This is the published seam a drafting cog targets
(builder-op note, build item 3): the drafting cog authors the request;
smith validates and creates it atomically, overlays included, then checks
the result. See examples/cog-request.json. Overlays never touch src/ —
task_logic.py and tests remain the starter's and still need the
BUILDING_COGS Step 2 rewrite when the drafted schemas diverge from it.
"""
import argparse
import json
import sys
import time

import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core   # noqa: E402
import smith_check  # noqa: E402
import smith_card   # noqa: E402
import smith_manifest  # noqa: E402
import smith_migrate  # noqa: E402
import smith_models  # noqa: E402

SMITH_ROOT = Path(__file__).resolve().parent.parent


def _self_identity():
    """This Cog's own id/version, from its manifest (either format)."""
    try:
        m, _, _ = smith_manifest.load(SMITH_ROOT)
        return {"id": m.get("id"), "version": m.get("version")}
    except (OSError, ValueError, smith_manifest.ManifestError):  # pragma: no cover
        return {"id": "openteams/cog-smith", "version": None}


def envelope(task, ok, payload=None, problems=None, error=None, started=None):
    """Envelope v1 for a deterministic tooling Cog: no model dependency,
    so binding is null; raw (verbatim model text) is null."""
    latency = round(time.monotonic() - started, 3) if started else None
    return {
        "envelope": 1,
        "cog": _self_identity(),
        "task": task,
        "ok": bool(ok),
        "error": error,
        "payload": payload,
        "raw": None,
        "problems": problems or [],
        "binding": None,
        "timing": {"latency_s": latency},
    }


def _problems(findings):
    """Checker findings -> envelope problems. `layer` rides along as an
    extra key (envelope consumers must ignore unknown fields)."""
    return [{"check": f["check"], "detail": f["detail"],
             "severity": f["level"], "layer": f["layer"]} for f in findings]


def _emit(env):
    print(json.dumps(env, indent=2))


def _prompt(label, default, explain=None):
    if explain:
        print(f"  # {explain}")
    val = input(f"  {label} [{default}]: ").strip()
    return val or default


COG_REQUEST_KEYS = {"cog_request", "dir", "name", "id", "summary", "owner",
                     "license", "publisher", "port", "produces", "model_cog",
                     "prohibits", "cog_md", "context", "examples", "evals",
                     "manifest"}


def _load_cog_request(path):
    req = json.loads(Path(path).read_text())
    if not isinstance(req, dict) or req.get("cog_request") != 1:
        raise smith_core.CreateError(
            "a create request is a JSON object with \"cog_request\": 1")
    unknown = sorted(set(req) - COG_REQUEST_KEYS)
    if unknown:
        raise smith_core.CreateError(f"unknown cog-request keys: {unknown}")
    if req.get("manifest") not in (None, *smith_core.MANIFEST_FORMATS):
        raise smith_core.CreateError(
            f"manifest must be one of {list(smith_core.MANIFEST_FORMATS)}, "
            f"got {req.get('manifest')!r}")
    return req


def _request_overrides_overlays(req):
    """Create request -> (token overrides, file overlays). Values the request
    omits fall back to smith defaults, exactly like omitted flags."""
    overrides = {
        "COG_ID": req.get("id"), "SUMMARY": req.get("summary"),
        "OWNER": req.get("owner"), "LICENSE": req.get("license"),
        "PUBLISHER": req.get("publisher"),
        "PORT": None if req.get("port") is None else str(req["port"]),
        "PRODUCES": req.get("produces"),
    }
    mc = req.get("model_cog") or {}
    if not isinstance(mc, dict):
        raise smith_core.CreateError("model_cog must be an object {id, source}")
    overrides["MODEL_COG_ID"] = mc.get("id")
    overrides["MODEL_COG_SOURCE"] = mc.get("source")
    if req.get("prohibits") is not None:
        if (not isinstance(req["prohibits"], list)
                or not all(isinstance(p, str) for p in req["prohibits"])):
            raise smith_core.CreateError("prohibits must be a list of strings")
        overrides["PROHIBITS_YAML"] = "\n".join(
            f"  - {p}" for p in req["prohibits"])

    overlays = {}

    def _json_overlay(rel, val, what):
        if val is None:
            return
        if not isinstance(val, dict):
            raise smith_core.CreateError(f"{what} must be a JSON object")
        overlays[rel] = json.dumps(val, indent=2) + "\n"

    ctx = req.get("context") or {}
    if ctx.get("system_md") is not None:
        overlays["context/system.md"] = str(ctx["system_md"])
    _json_overlay("context/input-schema.json", ctx.get("input_schema"),
                  "context.input_schema")
    _json_overlay("context/output-schema.json", ctx.get("output_schema"),
                  "context.output_schema")
    _json_overlay("context/output-example.json", ctx.get("output_example"),
                  "context.output_example")
    ex = req.get("examples") or {}
    _json_overlay("examples/sample-bundle.json", ex.get("sample_bundle"),
                  "examples.sample_bundle")
    ev = req.get("evals") or {}
    if ev.get("smoke_fixture") is not None:
        overlays["evals/smoke.fixture.yaml"] = str(ev["smoke_fixture"])
    if req.get("cog_md") is not None:
        overlays["COG.md"] = str(req["cog_md"])
    return overrides, overlays


def cmd_new(args):
    started = time.monotonic()
    req, req_overrides, overlays = {}, {}, None
    if args.from_request:
        req = _load_cog_request(args.from_request)
        req_overrides, overlays = _request_overrides_overlays(req)

    dir_arg = args.dir or req.get("dir")
    if not dir_arg:
        raise smith_core.CreateError(
            "destination required: pass --dir or set \"dir\" in the request")
    dest = Path(dir_arg)
    name = args.name or req.get("name") or dest.name
    flag_overrides = {
        "COG_ID": args.id, "SUMMARY": args.summary, "OWNER": args.owner,
        "LICENSE": args.license, "PUBLISHER": args.publisher,
        "PORT": args.port, "PRODUCES": args.produces,
        "MODEL_COG_ID": args.model_cog, "MODEL_COG_SOURCE": args.model_source,
    }
    if args.prohibit:
        flag_overrides["PROHIBITS_YAML"] = "\n".join(
            f"  - {p}" for p in args.prohibit)
    # request supplies values; explicit flags override the request
    overrides = {k: v for k, v in req_overrides.items() if v is not None}
    overrides.update({k: v for k, v in flag_overrides.items() if v is not None})
    tokens = smith_core.default_tokens(name, **overrides)
    # request supplies the manifest format; the explicit flag overrides it
    manifest_format = (args.manifest or req.get("manifest")
                       or smith_core.DEFAULT_MANIFEST_FORMAT)

    scripted = args.yes or bool(args.from_request)
    if not scripted and not sys.stdin.isatty():
        detail = ("stdin is not a terminal — pass --yes for non-interactive "
                  "creating (defaults + flags are used as-is)")
        if args.envelope:
            _emit(envelope("new", False,
                           error={"code": "invalid-input", "detail": detail},
                           problems=[{"check": "invalid-input",
                                      "detail": detail, "severity": "error"}],
                           started=started))
        else:
            print(f"error: {detail}", file=sys.stderr)
        return 2
    if not scripted and sys.stdin.isatty():
        print(f"Creating {tokens['COG_ID']} at {dest} — enter to accept defaults:")
        tokens["COG_ID"] = _prompt(
            "cog id", tokens["COG_ID"],
            "unique package identity, org/name — lowercase letters, digits, "
            "single hyphens; the name half should match the directory")
        tokens["SUMMARY"] = _prompt(
            "one-sentence summary", tokens["SUMMARY"],
            "shown on the catalog card and in the manifest; replace the "
            "created default before publishing")
        tokens["OWNER"] = _prompt(
            "owner (email)", tokens["OWNER"],
            "accountable contact recorded in the manifest")
        tokens["LICENSE"] = _prompt(
            "license", tokens["LICENSE"],
            "SPDX license id for the package, e.g. BSD-3-Clause")
        tokens["PORT"] = _prompt(
            "web-api port", tokens["PORT"],
            "loopback port the HTTP entry point listens on (pixi run serve)")
        tokens["PRODUCES"] = _prompt(
            "io.produces value", tokens["PRODUCES"],
            "lowercase token naming what this Cog produces, e.g. highlights "
            "— shown in the card's io line")
        tokens["MODEL_COG_ID"] = _prompt(
            "default model cog", tokens["MODEL_COG_ID"],
            "default satisfier for the model-endpoint requirement; a hosting "
            "environment may substitute another (pixi run resolve)")
        tokens["MODEL_COG_SOURCE"] = _prompt(
            "its source path", tokens["MODEL_COG_SOURCE"],
            "where resolve finds that default satisfier, relative to the "
            "created Cog")
        raw = _prompt(
            "prohibits (comma-separated)",
            "send_external_message, modify_source_data",
            "actions this Cog must never take — lowercase tokens; they "
            "become the manifest's prohibits list")
        tokens["PROHIBITS_YAML"] = "\n".join(
            f"  - {p.strip()}" for p in raw.split(",") if p.strip())
        tokens["SUMMARY_ONELINE"] = " ".join(tokens["SUMMARY"].split())[:160]

    result = smith_core.create(dest, tokens, overlays=overlays,
                               manifest_format=manifest_format)
    findings = smith_check.check(result["dest"])
    if args.envelope:
        errors = [f for f in findings if f["level"] == "error"]
        _emit(envelope("new", True, payload={
            "dest": result["dest"],
            "cog_id": tokens["COG_ID"],
            "files": len(result["files"]),
            "manifest": result["manifest"],
            "machinery": result["machinery"],
            "from_request": bool(args.from_request),
            "overlays": sorted(overlays) if overlays else [],
            "check": {"errors": len(errors),
                      "warnings": len(findings) - len(errors)},
        }, problems=_problems(findings), started=started))
        return 1 if errors else 0

    print(f"created {tokens['COG_ID']} -> {result['dest']}")
    print(f"  {len(result['files'])} files; manifest: {result['manifest']}; "
          f"machinery: {', '.join(result['machinery'])}")
    print("  next: edit context/system.md + src/task_logic.py, then:")
    print("        pixi install && pixi run resolve && pixi run test")
    return smith_check.report(findings)


def cmd_generate_descriptors(args):
    result = smith_models.generate_from_config(args.config, args.out_dir,
                                               manifest_format=args.manifest)
    for name in result["created"]:
        print(f"created {name} -> {result['out_dir']}/{name}")
    for name in result["skipped"]:
        print(f"skipped {name} (already exists)")
    rc = 0
    for name in result["created"]:
        findings = smith_check.check(Path(result["out_dir"]) / name)
        errors = [f for f in findings if f["level"] == "error"]
        if errors:
            rc = 1
            for f in errors:
                print(f"  [ERROR] {name}: {f['check']}: {f['detail']}")
        else:
            print(f"  {name}: PASS")
    return rc


def cmd_check(args):
    started = time.monotonic()
    findings = smith_check.check(args.path, run_tests=args.tests)
    if args.envelope:
        errors = [f for f in findings if f["level"] == "error"]
        _emit(envelope("check", True, payload={
            "path": str(Path(args.path).resolve()),
            "pass": not errors,
            "errors": len(errors),
            "warnings": len(findings) - len(errors),
        }, problems=_problems(findings), started=started))
        return 1 if errors else 0
    return smith_check.report(findings)


def cmd_card(args):
    started = time.monotonic()
    c = smith_card.card(args.path)
    if args.envelope:
        _emit(envelope("card", True, payload=c, started=started))
        return 0
    print(json.dumps(c, indent=2) if args.json else smith_card.render_text(c))
    return 0


def cmd_migrate(args):
    started = time.monotonic()
    p = smith_migrate.plan(args.path, to=args.to, machinery=not args.no_machinery)
    changed = bool(p["writes"] or p["copies"] or p["removes"])
    checked = changed and not args.dry_run
    if checked:
        smith_migrate.apply(p)
    findings = smith_check.check(p["root"]) if checked else []
    if args.envelope:
        errors = [f for f in findings if f["level"] == "error"]
        _emit(envelope("migrate", True, payload={
            "path": p["root"], "from": p["format"], "to": p["to"],
            "dry_run": bool(args.dry_run), "applied": changed and not args.dry_run,
            "writes": sorted(p["writes"]), "synced": [rel for _, rel in p["copies"]],
            "removed": p["removes"], "dropped": p["dropped"],
            "leftover": p["leftover"], "notes": p["notes"],
            "check": ({"errors": len(errors),
                       "warnings": len(findings) - len(errors)}
                      if checked else None),
        }, problems=_problems(findings), started=started))
        return 1 if errors else 0
    smith_migrate.describe(p)
    if args.dry_run:
        print("\ndry run — nothing written")
        return 0
    if not changed:
        print("\nnothing to do")
        return 0
    print()
    return smith_check.report(findings)


def _cli_error(args, detail):
    """Expected-failure exit: envelope when asked, stderr otherwise."""
    if getattr(args, "envelope", False):
        _emit(envelope(getattr(args, "cmd", None) or "?", False,
                       error={"code": "invalid-input", "detail": detail},
                       problems=[{"check": "invalid-input", "detail": detail,
                                  "severity": "error"}]))
    else:
        print(f"error: {detail}", file=sys.stderr)
    return 2


def main():
    ap = argparse.ArgumentParser(prog="smith", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create a new Cog from the template")
    p.add_argument("--dir", help="destination directory (or the request's "
                                 "\"dir\"; a given flag wins)")
    p.add_argument("--from-request", dest="from_request", metavar="REQ.json",
                   help="create from a cog-request JSON document (the "
                        "drafting-cog seam; implies non-interactive; "
                        "explicit flags override request values)")
    p.add_argument("--name", help="cog short name (default: dir basename)")
    p.add_argument("--id", help="full id, e.g. openteams/cog-foo")
    p.add_argument("--summary")
    p.add_argument("--owner")
    p.add_argument("--license")
    p.add_argument("--publisher")
    p.add_argument("--port")
    p.add_argument("--produces")
    p.add_argument("--model-cog", dest="model_cog")
    p.add_argument("--model-source", dest="model_source")
    p.add_argument("--prohibit", action="append",
                   help="prohibited action (repeatable)")
    p.add_argument("--manifest", choices=smith_core.MANIFEST_FORMATS,
                   help="where the profile manifest is written: pixi "
                        "([tool.cog] in pixi.toml, the default) or yaml "
                        "(a standalone cog.yaml); overrides the request's "
                        "\"manifest\"")
    p.add_argument("--yes", action="store_true", help="non-interactive")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (use with --yes)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("generate-descriptors",
                       help="create deployment-descriptor model cogs from a "
                            "model catalog config")
    p.add_argument("--config", required=True,
                   help="model catalog YAML (see examples/model-catalog.yaml)")
    p.add_argument("--out-dir", required=True,
                   help="directory to create the descriptor cogs into")
    p.add_argument("--manifest", choices=smith_core.MANIFEST_FORMATS,
                   help="manifest format for every generated descriptor: "
                        "pixi (default) or yaml; overrides the catalog's "
                        "top-level `manifest:`")
    p.set_defaults(fn=cmd_generate_descriptors)

    p = sub.add_parser("check", help="validate a Cog package")
    p.add_argument("path")
    p.add_argument("--tests", action="store_true",
                   help="also run the Cog's own test suite")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (findings in problems)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("migrate",
                       help="convert a Cog's manifest between formats "
                            "(default: cog.yaml -> [tool.cog] in pixi.toml) "
                            "and re-sync its cog-smith machinery")
    p.add_argument("path")
    p.add_argument("--to", choices=smith_core.MANIFEST_FORMATS, default="pixi",
                   help="target manifest format (default: pixi)")
    p.add_argument("--dry-run", action="store_true",
                   help="report the plan; write nothing")
    p.add_argument("--no-machinery", action="store_true",
                   help="convert the manifest only; leave src/ as it is")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("card", help="render a Cog's catalog card")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (card as payload)")
    p.set_defaults(fn=cmd_card)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except (smith_core.CreateError, smith_models.ModelConfigError,
            smith_migrate.MigrateError, smith_manifest.ManifestError) as e:
        return _cli_error(args, str(e))
    except FileNotFoundError as e:
        return _cli_error(args, f"not found: {e.filename or e}")
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as e:
        return _cli_error(args, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
