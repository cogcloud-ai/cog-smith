#!/usr/bin/env python3
"""CLI entry point (cog-smith machinery, generic — edit task_logic.py, not
this). Same Cog as the web API, second interface.

    pixi run ask -- --bundle examples/sample-bundle.json
    pixi run ask -- --bundle big.json --timeout 600   # one-call deadline override
    pixi run ask -- --check          # health probe
    pixi run ask -- --check --deep   # proves the key works + identity echo
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_core  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="ask")
    ap.add_argument("--bundle", help="path to an input bundle (JSON)")
    ap.add_argument("--check", action="store_true", help="health probe only")
    ap.add_argument("--deep", action="store_true",
                    help="with --check: end-to-end model ping incl. identity")
    ap.add_argument("--raw", action="store_true", help="print the raw model text")
    ap.add_argument("--timeout", type=int, metavar="N",
                    help=f"override the binding's caller deadline for THIS call, "
                         f"in seconds ({cog_core.cog_binding.REQUEST_TIMEOUT_MIN}–"
                         f"{cog_core.cog_binding.REQUEST_TIMEOUT_MAX}); the "
                         f"binding says {cog_core.REQUEST_TIMEOUT_S}s")
    args = ap.parse_args()

    if args.timeout is not None:
        bad = cog_core.cog_binding.timeout_problems(args.timeout)
        if bad:
            ap.error("; ".join(bad))

    if args.check:
        ok, detail = cog_core.health(deep=args.deep)
        print(("OK " if ok else "DOWN ") + detail)
        return 0 if ok else 1

    if not args.bundle:
        ap.error("--bundle is required (or use --check)")
    bundle = json.loads(Path(args.bundle).read_text())
    result = cog_core.invoke(bundle, timeout=args.timeout)

    if args.raw:
        print(result.get("raw") or "")
        return 0

    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        return 1
    if result.get("problems"):
        print(f"\n{len(result['problems'])} problem(s) — ok with problems; "
              f"a gate (human or reviewer) decides.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
