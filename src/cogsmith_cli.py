#!/usr/bin/env python3
"""cog-smith — mint, validate, and describe Cogs.

    pixi run new -- --dir ../cog-meeting-highlights [--id ...] [--yes]
    pixi run check -- ../cog-meeting-highlights [--tests]
    pixi run card -- ../cog-meeting-highlights [--json]

`new` walks the builder questions interactively (Travis's list: what are
you, what model class, what do you prohibit…), or takes everything as flags
with --yes for scripted minting. The minted Cog is immediately runnable:
resolve -> check --deep -> ask.
"""
import argparse
import json
import sys

import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core   # noqa: E402
import smith_check  # noqa: E402
import smith_card   # noqa: E402
import smith_models  # noqa: E402


def _prompt(label, default):
    val = input(f"  {label} [{default}]: ").strip()
    return val or default


def cmd_new(args):
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
        print("error: stdin is not a terminal — pass --yes for "
              "non-interactive minting (defaults + flags are used as-is)",
              file=sys.stderr)
        return 2
    if not args.yes and sys.stdin.isatty():
        print(f"Minting {tokens['COG_ID']} at {dest} — enter to accept defaults:")
        tokens["COG_ID"] = _prompt("cog id", tokens["COG_ID"])
        tokens["SUMMARY"] = _prompt("one-sentence summary", tokens["SUMMARY"])
        tokens["OWNER"] = _prompt("owner (email)", tokens["OWNER"])
        tokens["LICENSE"] = _prompt("license", tokens["LICENSE"])
        tokens["PORT"] = _prompt("web-api port", tokens["PORT"])
        tokens["PRODUCES"] = _prompt("io.produces value", tokens["PRODUCES"])
        tokens["MODEL_COG_ID"] = _prompt("default model cog", tokens["MODEL_COG_ID"])
        tokens["MODEL_COG_SOURCE"] = _prompt("its source path", tokens["MODEL_COG_SOURCE"])
        raw = _prompt("prohibits (comma-separated)",
                      "send_external_message, modify_source_data")
        tokens["PROHIBITS_YAML"] = "\n".join(
            f"  - {p.strip()}" for p in raw.split(",") if p.strip())
        tokens["SUMMARY_ONELINE"] = " ".join(tokens["SUMMARY"].split())[:160]

    result = smith_core.mint(dest, tokens)
    print(f"minted {tokens['COG_ID']} -> {result['dest']}")
    print(f"  {len(result['files'])} files; machinery: {', '.join(result['machinery'])}")
    print("  next: edit context/system.md + src/task_logic.py, then:")
    print("        pixi install && pixi run resolve && pixi run test")

    findings = smith_check.check(result["dest"])
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
    return smith_check.report(smith_check.check(args.path, run_tests=args.tests))


def cmd_card(args):
    c = smith_card.card(args.path)
    print(json.dumps(c, indent=2) if args.json else smith_card.render_text(c))
    return 0


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
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("card", help="render a Cog's catalog card")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_card)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except (smith_core.MintError, smith_models.ModelConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: not found: {e.filename or e}", file=sys.stderr)
        return 2
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
