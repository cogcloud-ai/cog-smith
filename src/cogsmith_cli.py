#!/usr/bin/env python3
"""cog-smith — mint, validate, and describe Cogs.

    pixi run new -- --dir ../cog-meeting-highlights [--id ...] [--yes]
    pixi run check -- ../cog-meeting-highlights [--tests]
    pixi run card -- ../cog-meeting-highlights [--json]

`new` walks the builder questions interactively (Travis's list: what are
you, what model class, what do you prohibit…), or takes everything as flags
with --yes for scripted minting. The minted Cog is immediately runnable:
resolve -> check --deep -> ask.

`--envelope` on new/check/card emits an envelope-v1 result instead of
human-readable text, so the Builder Op (see output/builder-op-note.md)
can consume smith at the Op seam like any other Cog. cog-smith is a
deterministic tooling Cog with no model dependency, so its envelopes
carry `binding: null`; checker findings travel in `problems`
(ok-with-problems: the run succeeded, a Gate decides about the findings).
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
import smith_models  # noqa: E402

SMITH_ROOT = Path(__file__).resolve().parent.parent


def _self_identity():
    """This Cog's own id/version, from its manifest."""
    try:
        m = yaml.safe_load((SMITH_ROOT / "cog.yaml").read_text()) or {}
        return {"id": m.get("id"), "version": m.get("version")}
    except OSError:                                    # pragma: no cover
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


def cmd_new(args):
    started = time.monotonic()
    dest = Path(args.dir)
    name = args.name or dest.name
    overrides = {
        "COG_ID": args.id, "SUMMARY": args.summary, "OWNER": args.owner,
        "LICENSE": args.license, "PUBLISHER": args.publisher,
        "PORT": args.port, "PRODUCES": args.produces,
        "MODEL_COG_ID": args.model_cog, "MODEL_COG_SOURCE": args.model_source,
    }
    if args.prohibit:
        overrides["PROHIBITS_YAML"] = "\n".join(f"  - {p}" for p in args.prohibit)
    tokens = smith_core.default_tokens(name, **overrides)

    if not args.yes and not sys.stdin.isatty():
        detail = ("stdin is not a terminal — pass --yes for non-interactive "
                  "minting (defaults + flags are used as-is)")
        if args.envelope:
            _emit(envelope("new", False,
                           error={"code": "invalid-input", "detail": detail},
                           problems=[{"check": "invalid-input",
                                      "detail": detail, "severity": "error"}],
                           started=started))
        else:
            print(f"error: {detail}", file=sys.stderr)
        return 2
    if not args.yes and sys.stdin.isatty():
        print(f"Minting {tokens['COG_ID']} at {dest} — enter to accept defaults:")
        tokens["COG_ID"] = _prompt(
            "cog id", tokens["COG_ID"],
            "unique package identity, org/name — lowercase letters, digits, "
            "single hyphens; the name half should match the directory")
        tokens["SUMMARY"] = _prompt(
            "one-sentence summary", tokens["SUMMARY"],
            "shown on the catalog card and in the manifest; replace the "
            "minted default before publishing")
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
            "minted Cog")
        raw = _prompt(
            "prohibits (comma-separated)",
            "send_external_message, modify_source_data",
            "actions this Cog must never take — lowercase tokens; they "
            "become the manifest's prohibits list")
        tokens["PROHIBITS_YAML"] = "\n".join(
            f"  - {p.strip()}" for p in raw.split(",") if p.strip())
        tokens["SUMMARY_ONELINE"] = " ".join(tokens["SUMMARY"].split())[:160]

    result = smith_core.mint(dest, tokens)
    findings = smith_check.check(result["dest"])
    if args.envelope:
        errors = [f for f in findings if f["level"] == "error"]
        _emit(envelope("new", True, payload={
            "dest": result["dest"],
            "cog_id": tokens["COG_ID"],
            "files": len(result["files"]),
            "machinery": result["machinery"],
            "check": {"errors": len(errors),
                      "warnings": len(findings) - len(errors)},
        }, problems=_problems(findings), started=started))
        return 1 if errors else 0

    print(f"minted {tokens['COG_ID']} -> {result['dest']}")
    print(f"  {len(result['files'])} files; machinery: {', '.join(result['machinery'])}")
    print("  next: edit context/system.md + src/task_logic.py, then:")
    print("        pixi install && pixi run resolve && pixi run test")
    return smith_check.report(findings)


def cmd_mint_models(args):
    result = smith_models.mint_from_config(args.config, args.out_dir)
    for name in result["minted"]:
        print(f"minted {name} -> {result['out_dir']}/{name}")
    for name in result["skipped"]:
        print(f"skipped {name} (already exists)")
    rc = 0
    for name in result["minted"]:
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

    p = sub.add_parser("new", help="mint a new Cog from the template")
    p.add_argument("--dir", required=True, help="destination directory")
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
    p.add_argument("--yes", action="store_true", help="non-interactive")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (use with --yes)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("mint-model-cog",
                       help="mint deployment-descriptor model cogs from a "
                            "model catalog config")
    p.add_argument("--config", required=True,
                   help="model catalog YAML (see examples/model-catalog.yaml)")
    p.add_argument("--out-dir", required=True,
                   help="directory to mint the descriptor cogs into")
    p.set_defaults(fn=cmd_mint_models)

    p = sub.add_parser("check", help="validate a Cog package")
    p.add_argument("path")
    p.add_argument("--tests", action="store_true",
                   help="also run the Cog's own test suite")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (findings in problems)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("card", help="render a Cog's catalog card")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.add_argument("--envelope", action="store_true",
                   help="emit an envelope-v1 result (card as payload)")
    p.set_defaults(fn=cmd_card)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except (smith_core.MintError, smith_models.ModelConfigError) as e:
        return _cli_error(args, str(e))
    except FileNotFoundError as e:
        return _cli_error(args, f"not found: {e.filename or e}")
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as e:
        return _cli_error(args, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
