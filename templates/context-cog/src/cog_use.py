#!/usr/bin/env python3
"""Point this Cog at a model. Writes model.json (the binding record) next to the Cog.

    pixi run use local
    pixi run use summary-lora
    pixi run use collab --endpoint http://YOUR-COLLAB-HOST/v1 --insecure-http
    pixi run use openrouter --model qwen/qwen3.5-35b-a3b
    pixi run use --show

The binding persists across shells — an installed Cog must not depend on how you
happened to launch your terminal. API keys are never written here: a preset
records the NAME of the environment variable holding the key (a credential
reference, not a credential), so model.json stays safe to share.

`use` writes the SAME binding-record shape as `resolve`. A remote deployment has
no manifest to pin against, so the record is marked pinned only when you supply
--revision (a deployment/model revision you trust); otherwise it is explicitly
unpinned rather than silently unversioned.

Contract checks enforced here (the Cog's own rules, not system Guards), before anything is written:
  - locality: the manifest's requires[].locality constraint must admit the preset
  - transport: plain HTTP to a non-loopback host is refused without --insecure-http
Byte-identical across the Cogs that vendor it; `smith check` enforces it
by hash.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_binding  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

PRESETS = {
    "local": {
        "endpoint": "http://127.0.0.1:8080/v1",
        "model": "qwen2.5-3b-instruct-q4_k_m",
        "api_key_env": None,
        # llama-server constrains decoding to the schema: malformed output becomes
        # impossible rather than merely discouraged.
        "response_format": "json_schema",
        "locality": "local",
        "deployment": "cog-demo/cog-qwen3b (loopback)",
        "note": "the cog-demo cog-qwen3b model Cog on loopback",
    },
    "summary-lora": {
        "endpoint": "http://127.0.0.1:8081/v1",
        "model": "qwen2.5-3b-instruct-summary-sft-lora",
        "api_key_env": None,
        "response_format": "json_schema",
        "locality": "local",
        "deployment": "cog-demo/cog-qwen3b-summary-lora (loopback)",
        "note": "the summarization LoRA — specialization vs base is the interesting comparison",
    },
    "collab": {
        # Replace with the real Collab-hosted endpoint via --endpoint; this
        # placeholder deliberately fails rather than silently answering from the
        # wrong model.
        "endpoint": "http://collab-host.invalid/v1",
        "model": "Qwen/Qwen3.5-35B-A3B-GPTQ-Int4",
        "api_key_env": "COLLAB_API_KEY",
        "response_format": "json_object",
        "locality": "customer-vpc",
        "deployment": "collab-hosted qwen3.5-35b",
        "note": "the Collab-hosted Qwen3.5-35B; pass --endpoint (and --insecure-http if plain HTTP on a trusted LAN)",
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-latest",
        "api_key_env": "OPENROUTER_API_KEY",
        "response_format": "json_object",
        "locality": "cloud",
        "deployment": "openrouter",
        "note": "provider/model slugs; see openrouter.ai/models",
    },
    "ollama": {
        "endpoint": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:3b",
        "api_key_env": None,
        "response_format": "json_object",
        "locality": "local",
        "deployment": "local ollama",
        "note": "an already-running Ollama, if you have one",
    },
}


def main():
    ap = argparse.ArgumentParser(prog="use", description="point this Cog at a model")
    ap.add_argument("preset", nargs="?", choices=sorted(PRESETS), help="a named preset")
    ap.add_argument("--endpoint", help="OpenAI-compatible base URL")
    ap.add_argument("--model", help="model id or slug")
    ap.add_argument("--api-key-env", help="name of the env var holding the key")
    ap.add_argument("--response-format", choices=["json_object", "json_schema", "none"],
                    help="json_schema constrains decoding (llama-server); none omits the param")
    ap.add_argument("--revision", help="deployment/model revision you trust; marks the binding pinned")
    ap.add_argument("--timeout", type=int, metavar="N",
                    help=f"caller deadline for one model call, in seconds "
                         f"({cog_binding.REQUEST_TIMEOUT_MIN}–"
                         f"{cog_binding.REQUEST_TIMEOUT_MAX}; default "
                         f"{cog_binding.REQUEST_TIMEOUT_DEFAULT}) — a big input "
                         f"to a remote model needs a bigger one")
    ap.add_argument("--locality", choices=sorted(cog_binding.LOCALITIES),
                    help="override the preset's locality classification")
    ap.add_argument("--insecure-http", action="store_true",
                    help="allow plain HTTP to a non-loopback host (trusted private network only)")
    ap.add_argument("--show", action="store_true", help="print the current binding and exit")
    args = ap.parse_args()

    if args.show or not (args.preset or args.endpoint):
        record, key, source = cog_binding.load_record(ROOT)
        print(f"endpoint : {record['endpoint']}")
        print(f"model    : {record['model']}")
        print(f"format   : {record.get('response_format') or 'none'}")
        print(f"locality : {record.get('locality')}")
        print(f"timeout  : {cog_binding.request_timeout(record)}s")
        print(f"pinned   : {record.get('pinned')}")
        print(f"key      : {'set' if key else 'NOT SET'}")
        print(f"source   : {source}")
        sat = record.get("satisfier") or {}
        served = record.get("served_model") or {}
        if sat.get("cog"):
            print(f"satisfier: {sat['cog']} {sat.get('version', '')} via {sat.get('interface', '?')}")
        elif sat.get("deployment"):
            print(f"deployment: {sat['deployment']}"
                  + (f" @ {served['revision']}" if served.get("revision")
                     else " (served model unpinned)"))
        if not args.show:
            print(f"\npresets  : {', '.join(sorted(PRESETS))}")
            print("usage    : pixi run use <preset> [--endpoint <url>] [--model <slug>]")
        return

    cfg = dict(PRESETS.get(args.preset,
                           {"endpoint": None, "model": None, "api_key_env": None,
                            "locality": "cloud", "deployment": "custom"}))
    note = cfg.pop("note", None)
    for key_, val in (("endpoint", args.endpoint), ("model", args.model),
                      ("api_key_env", args.api_key_env), ("locality", args.locality)):
        if val:
            cfg[key_] = val
    if args.response_format:
        cfg["response_format"] = None if args.response_format == "none" else args.response_format

    if not cfg.get("endpoint"):
        ap.error("--endpoint is required when no preset is given")
    if ".invalid" in cfg["endpoint"]:
        print("the collab preset needs the real endpoint once:")
        print("  pixi run use collab --endpoint http://YOUR-COLLAB-HOST/v1")
        sys.exit(1)

    # --- contract checks, before anything is written -------------------------------
    manifest = cog_binding.load_manifest(ROOT)
    constraint = cog_binding.declared_locality_constraint(manifest)
    if not cog_binding.locality_allowed(constraint, cfg["locality"]):
        sys.exit(f"locality check: this Cog's manifest requires locality "
                 f"{constraint!r}; the requested binding is {cfg['locality']!r}. "
                 f"Refusing to write the binding record.")

    ok, reason = cog_binding.endpoint_policy(cfg["endpoint"], args.insecure_http)
    if not ok:
        sys.exit(f"transport check: {reason}")

    timeout = (cog_binding.REQUEST_TIMEOUT_DEFAULT if args.timeout is None
               else args.timeout)
    timeout_bad = cog_binding.timeout_problems(timeout)
    if timeout_bad:
        sys.exit("timeout check: " + "; ".join(timeout_bad))

    record = {
        "record": cog_binding.RECORD_SCHEMA,
        "capability": "model-endpoint/openai-compatible",
        "endpoint": cfg["endpoint"],
        "model": cfg["model"],
        "api_key_env": cfg.get("api_key_env"),
        "response_format": cfg.get("response_format"),
        "locality": cfg["locality"],
        "request_timeout_s": timeout,
        "pinned": bool(args.revision),
        "satisfier": {
            "source": f"use-preset:{args.preset}" if args.preset else "use-custom",
            "deployment": cfg.get("deployment", "custom"),
        },
        "served_model": {
            "revision": args.revision,
            "weights_sha256": None,
            "verified": False,
        },
    }
    if args.insecure_http:
        record["insecure_http"] = True

    cog_binding.write_record(ROOT, record)

    print("wrote model.json")
    print(f"  endpoint : {cfg['endpoint']}  ({reason})")
    print(f"  model    : {cfg['model']}")
    print(f"  format   : {cfg.get('response_format') or 'none'}")
    print(f"  locality : {cfg['locality']} (manifest constraint: {constraint})")
    print(f"  timeout  : {timeout}s (caller deadline for one model call)")
    print(f"  pinned   : {record['pinned']} (served model)"
          + ("" if record["pinned"] else "  — pass --revision to pin the served model"))
    if note:
        print(f"  note     : {note}")

    env = cfg.get("api_key_env")
    if env:
        have = bool(os.environ.get(env))
        print(f"  key      : ${env} — {'set' if have else 'NOT SET in this shell'}")
        if not have:
            print(f"\n  export {env}=... before running, then: pixi run check --deep")
            sys.exit(1)
    print("\nnext: pixi run check --deep")


if __name__ == "__main__":
    main()
