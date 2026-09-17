#!/usr/bin/env python3
"""Resolve this Cog's declared dependency on another Cog.

    pixi run resolve                 # bind to the declared (local) satisfier
    pixi run resolve --start         # also start it
    pixi run resolve --dry-run       # report only, write nothing

    # bind an ALTERNATE declared satisfier — e.g. the Collab deployment
    # descriptor, whose address is an installation fact:
    pixi run resolve -- --satisfier ../cog-collab-qwen35b \
        --endpoint http://YOUR-COLLAB-HOST/v1 [--insecure-http]

The Cog declares a capability it needs; `satisfied_by` names the default
satisfier and `also_satisfied_by` lists known alternates. Resolution checks the
candidate actually declares it *provides* that capability, that its version
satisfies the constraint, and that its locality is admitted by the manifest —
then writes the binding record.

Address handling follows the starting-point doc's three levels: the manifest is
class-declared; the satisfier's id/version/model identity is the pin; the
concrete endpoint is address-bound at install. A satisfier whose default
interface declares `address: install-time` (a deployment descriptor) REQUIRES
--endpoint; one that carries a package endpoint REFUSES --endpoint — endpoint
replacement is not an override mechanism (that is `use`, which records the
route as unpinned). Pin dimensions are reported separately: the satisfier
block pins the PACKAGE (id/version); `served_model` records whether the model
actually served is pinned (a weights digest for a local Cog, a declared
revision for a deployment); top-level `pinned` is true only when the SERVED
MODEL is pinned.

Response-format negotiation: `model-endpoint/openai-compatible` does NOT imply
schema-constrained decoding — that is a llama.cpp extension, not part of the
capability. The resolver grants json_schema only when the satisfier's declared
runtime is known to support it, and falls back to json_object otherwise.
Byte-identical across the cog-forge Cogs; tools/check_copies.py enforces it.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_binding  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Runtimes known to honor response_format: json_schema (decoder constraint).
SCHEMA_CAPABLE_RUNTIMES = {"llama.cpp", "llama-server"}


def load_cog(path):
    """(manifest, error) for the Cog at path — either manifest format
    (pixi.toml [tool.cog] or cog.yaml; see cog_binding.load_manifest)."""
    try:
        return cog_binding.load_manifest(path), None
    except FileNotFoundError:
        return None, f"no manifest (pixi.toml [tool.cog] or cog.yaml) at {path}"
    except (ValueError, yaml.YAMLError) as e:
        return None, f"{path}: unreadable manifest: {e}"


def served_model_pin(model):
    """(served_model_block, pinned) for a satisfier's model declaration.

    Three shapes:
      - simple weights:   model.weights.sha256           -> digest pin
      - deployment:       model.revision                 -> revision pin
      - composed/derived: model.base.sha256 + model.specialization.sha256 +
                          effective_id (the cog-demo LoRA shape) -> an
                          effective digest over every load-bearing lineage
                          component. Lineage is recorded, not flattened away.

    Digest discipline (v4 P0-1, hardened per v5): a pin claim needs
    digest-shaped evidence, validated with the SAME predicate the record
    validator uses (cog_binding.is_digest). An ABSENT digest is a legitimate
    unpinned state; a PRESENT-BUT-MALFORMED digest is invalid package identity
    and fails resolution closed — a manifest that declares a digest that is
    not a digest is broken, not optionally unpinned. The specialization
    revision is lineage METADATA — it never becomes the top-level served
    revision.

    Returns (served_block, pinned, error). error is a string when the
    declaration itself is invalid.
    """
    model = model or {}
    base = model.get("base") or {}
    spec = model.get("specialization") or {}

    if base or spec:
        runtime = model.get("runtime")
        if isinstance(runtime, dict):
            runtime_id = (f"{runtime.get('name')}@{runtime.get('version')}"
                          f"#{runtime.get('adapter_mode')}")
        else:
            runtime_id = str(runtime) if runtime else None
        lineage = {
            "effective_id": model.get("effective_id"),
            "base_cog": (base.get("cog")),
            "base_sha256": base.get("sha256"),
            "specialization_type": spec.get("type"),
            "specialization_revision": spec.get("revision"),
            "specialization_source_sha256": spec.get("source_sha256"),
            "specialization_sha256": spec.get("sha256"),
            "runtime": runtime_id,
        }
        for label, value in (("base.sha256", base.get("sha256")),
                             ("specialization.sha256", spec.get("sha256")),
                             ("specialization.source_sha256", spec.get("source_sha256"))):
            if value is not None and not cog_binding.is_digest(value):
                return None, False, (f"model.{label} is present but is not a "
                                     f"sha256 digest ({str(value)[:40]!r}) — "
                                     f"invalid package identity")

        # COMPLETE-composition semantics (v6 P1-1, option 1): "effective" means
        # the full lineage the starting-point doc trials — a digest at every
        # link AND an identified composition. Hashing null for a missing
        # component would be deterministic bytes, not reproducible identity, so
        # ANY missing link leaves the composed model honestly unpinned (absence
        # is legitimate; only malformed presence fails closed, above).
        def _named(v):
            return bool(str(v or "").strip())
        runtime_complete = (isinstance(runtime, dict)
                            and all(_named(runtime.get(k))
                                    for k in ("name", "version", "adapter_mode")))
        required = {
            "effective_id": _named(model.get("effective_id")),
            "base.sha256": cog_binding.is_digest(base.get("sha256")),
            "specialization.type": _named(spec.get("type")),
            "specialization.revision": _named(spec.get("revision")),
            "specialization.source_sha256": cog_binding.is_digest(spec.get("source_sha256")),
            "specialization.sha256": cog_binding.is_digest(spec.get("sha256")),
            "runtime (name+version+adapter_mode)": runtime_complete,
        }
        missing = [name for name, ok in required.items() if not ok]
        pinned = not missing
        effective = None
        if pinned:
            material = json.dumps(lineage, sort_keys=True)
            effective = hashlib.sha256(material.encode()).hexdigest()
        served = {
            "revision": None,          # composed identity lives in the digest
            "weights_sha256": None,
            "effective_sha256": effective,
            "lineage": lineage,
            "verified": False,
        }
        if missing:
            served["lineage_missing"] = missing
        return served, pinned, None

    weights_sha = (model.get("weights") or {}).get("sha256")
    if weights_sha is not None and not cog_binding.is_digest(weights_sha):
        return None, False, (f"model.weights.sha256 is present but is not a "
                             f"sha256 digest ({str(weights_sha)[:40]!r}) — "
                             f"invalid package identity")
    revision = model.get("revision")
    served = {"revision": revision if revision else None,
              "weights_sha256": weights_sha,
              "verified": False}
    return served, bool(weights_sha or revision), None


def _declared_runtime(dep):
    model = dep.get("model") or {}
    runtime = model.get("runtime")
    if isinstance(runtime, dict):
        runtime = runtime.get("name")
    return str(runtime or "").strip()


def candidate_specs(req):
    """All declared satisfier specs for a requirement: default + alternates."""
    specs = []
    if req.get("satisfied_by"):
        specs.append(req["satisfied_by"])
    specs.extend(req.get("also_satisfied_by") or [])
    return specs


def pick_spec(req, satisfier_arg, allow_undeclared=False):
    """Choose which declared spec to resolve. --satisfier matches by source
    path or cog id (suffix). An undeclared path is refused unless
    --allow-undeclared makes the trust status explicit — the binding is then
    recorded as UNDECLARED (satisfier.declared: false) while pin evidence is
    reported independently and honestly."""
    specs = candidate_specs(req)
    if not satisfier_arg:
        return (specs[0], None) if specs else (None, "no satisfied_by declared")
    for spec in specs:
        if spec.get("source") and Path(spec["source"]).name == Path(satisfier_arg).name:
            return spec, None
        if spec.get("cog") and str(spec["cog"]).endswith(Path(satisfier_arg).name):
            return spec, None
    if not allow_undeclared:
        declared = [s.get("cog") or s.get("source") for s in specs]
        return None, (f"{satisfier_arg!r} is not a declared satisfier "
                      f"(declared: {declared}) — declare it in the manifest, or pass "
                      f"--allow-undeclared to bind it as an explicitly undeclared "
                      f"satisfier (pin evidence is reported independently)")
    return {"source": satisfier_arg, "_undeclared": True}, None


def resolve_one(req, locality_constraint, args):
    """Check one requirement. Returns (ok, detail, record_or_None)."""
    capability = req.get("capability")
    spec, err = pick_spec(req, args.satisfier, getattr(args, "allow_undeclared", False))
    if err:
        return False, f"{capability}: {err}", None
    source = spec.get("source")
    if not source:
        return False, (f"{capability}: no source declared — bind a remote satisfier "
                       f"with `pixi run use` instead"), None

    dep_root = (ROOT / source).resolve()
    dep, err = load_cog(dep_root)
    if err:
        return False, f"{capability}: {err}", None

    provides = dep.get("provides") or []
    if capability not in provides:
        return False, (f"{capability}: {dep.get('id')} does not provide it "
                       f"(provides: {provides or 'nothing'})"), None

    want_id = spec.get("cog")
    if want_id and dep.get("id") != want_id:
        return False, f"{capability}: expected {want_id}, found {dep.get('id')}", None

    constraint = spec.get("version")
    if constraint:
        try:
            version_ok = cog_binding.satisfies(dep.get("version", "0"), constraint)
        except ValueError as e:
            return False, f"{capability}: invalid version/constraint — {e}", None
        if not version_ok:
            return False, (f"{capability}: {dep.get('id')} is {dep.get('version')}, "
                           f"needs {constraint}"), None

    default = next((i for i in dep.get("interfaces") or [] if i.get("default")), None)
    if not default:
        return False, f"{capability}: {dep.get('id')} declares no default interface", None

    # --- address: package-carried, or install-time ------------------------
    # --endpoint is ONLY the completion of an install-time descriptor. For a
    # package-carried endpoint it would separate the address from the identity,
    # locality, pin, and runtime capability that belong to it (re-review v2
    # P0-1) — the ad hoc override path is `use`, which records honestly.
    install_time = default.get("address") == "install-time"
    if args.endpoint and not install_time:
        return False, (f"{capability}: {dep.get('id')} carries its own endpoint — "
                       f"--endpoint is not an override mechanism; use "
                       f"`pixi run use --endpoint ...` for an ad hoc route"), None
    if install_time and not args.endpoint:
        return False, (f"{capability}: {dep.get('id')} is a deployment descriptor "
                       f"(address: install-time) — pass --endpoint"), None
    endpoint = args.endpoint or default.get("endpoint")
    if not endpoint:
        return False, f"{capability}: {dep.get('id')} declares no default endpoint", None

    locality = dep.get("locality", "local")
    if not cog_binding.locality_allowed(locality_constraint, locality):
        return False, (f"{capability}: satisfier locality {locality!r} violates the "
                       f"manifest constraint {locality_constraint!r}"), None

    ok, reason = cog_binding.endpoint_policy(endpoint, args.insecure_http)
    if not ok:
        return False, f"{capability}: {reason}", None

    runtime = _declared_runtime(dep)
    response_format = ("json_schema" if runtime in SCHEMA_CAPABLE_RUNTIMES
                       else "json_object")

    model = dep.get("model") or {}
    # Pin dimensions are distinct facts (re-review v2 P0-2): the PACKAGE pin is
    # the satisfier id/version; the SERVED-MODEL pin needs a weights digest, a
    # declared deployment revision, or a fully-digested lineage (v3 P0-1).
    # Whether the satisfier was DECLARED by the requiring manifest is a third,
    # independent fact (v4 P0-1) — trust status never contradicts pin evidence.
    served_block, served_pinned, pin_error = served_model_pin(model)
    if pin_error:
        return False, f"{capability}: {dep.get('id')} — {pin_error}", None
    undeclared = spec.get("_undeclared", False)

    record = {
        "record": cog_binding.RECORD_SCHEMA,
        "capability": capability,
        "endpoint": endpoint,
        "model": (default.get("served_model_id") or model.get("name") or "local"),
        "api_key_env": default.get("api_key_env"),
        "response_format": response_format,
        "locality": locality,
        "pinned": served_pinned,
        "satisfier": {
            "source": "resolve" + ("+undeclared" if undeclared else ""),
            "declared": not undeclared,
            "declared_source": None if undeclared else source,
            "cog": dep.get("id"),
            "version": dep.get("version"),
            "interface": default.get("name"),
            "path": str(dep_root),
            "runtime": runtime or None,
            "address_bound": "install-time" if install_time else "package",
        },
        "served_model": served_block,
    }
    if args.insecure_http:
        record["insecure_http"] = True

    # Dry-run/write parity (v4 P0-1): a resolver result is only ok if the
    # record it produced passes the SAME validator the write path enforces.
    record_problems = cog_binding.validate_record(record)
    if record_problems:
        return False, (f"{capability}: produced an invalid binding record — "
                       + "; ".join(record_problems)), None

    if served_pinned:
        pin_note = "served model pinned"
    elif (served_block or {}).get("lineage_missing"):
        pin_note = ("served model UNPINNED — incomplete lineage (missing: "
                    + ", ".join(served_block["lineage_missing"]) + ")")
    else:
        pin_note = ("served model UNPINNED — no weights digest, deployment "
                    "revision, or complete lineage")
    if undeclared:
        pin_note += "; satisfier UNDECLARED by the requiring manifest"
    return True, (f"{capability} <- {dep.get('id')} {dep.get('version')} "
                  f"via {default.get('name')} at {endpoint} "
                  f"[locality={locality}, response_format={response_format}, {pin_note}"
                  + (", address: install-time" if install_time else "") + "]"), record


def main():
    ap = argparse.ArgumentParser(prog="resolve")
    ap.add_argument("--satisfier", help="path (or cog-id suffix) of a declared alternate satisfier")
    ap.add_argument("--endpoint", help="concrete address for an install-time satisfier")
    ap.add_argument("--insecure-http", action="store_true",
                    help="allow plain HTTP to a non-loopback host (trusted private network only)")
    ap.add_argument("--allow-undeclared", action="store_true",
                    help="permit a satisfier the manifest does not declare — the "
                         "binding records satisfier.declared: false; pin evidence "
                         "is reported independently")
    ap.add_argument("--start", action="store_true", help="start the dependency after resolving")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    me, err = load_cog(ROOT)
    if err:
        sys.exit(err)
    locality_constraint = cog_binding.declared_locality_constraint(me)

    reqs = [r for r in (me.get("requires") or []) if isinstance(r, dict)]
    if not reqs:
        sys.exit("this Cog declares no structured requirements")

    print(f"{me.get('id')} {me.get('version')} — resolving {len(reqs)} requirement(s)\n")

    record = None
    failed = False
    for req in reqs:
        ok, detail, r = resolve_one(req, locality_constraint, args)
        print(f"  [{'ok' if ok else 'FAIL'}] {detail}")
        if not ok:
            failed = True
        elif record is None:
            record = r

    if failed or record is None:
        sys.exit("\nresolution failed")

    if args.dry_run:
        print("\n(dry run — model.json not written)")
        return

    cog_binding.write_record(ROOT, record)
    sat = record["satisfier"]
    served = record.get("served_model") or {}
    pin = f"{sat['cog']} {sat['version']}"
    if served.get("effective_sha256"):
        served_note = (f"served model pinned (composed lineage, effective "
                       f"{served['effective_sha256'][:12]}…)")
    elif served.get("revision"):
        served_note = f"served model pinned @ {served['revision']}"
    elif served.get("weights_sha256"):
        served_note = f"served model pinned (weights sha256 {served['weights_sha256'][:12]}…)"
    else:
        served_note = "served model UNPINNED — declare a deployment revision to pin it"
    print(f"\nwrote model.json — package pin {pin}; {served_note}")

    env = record.get("api_key_env")
    if env:
        import os
        print(f"  key: ${env} — {'set' if os.environ.get(env) else 'NOT SET in this shell'}")

    dep_path = sat["path"]
    if sat.get("address_bound") == "install-time":
        print("\nnext: pixi run check --deep")
    elif args.start:
        print(f"\nstarting {sat['cog']} …")
        print(f"  pixi run --manifest-path {dep_path}/pixi.toml serve")
        try:
            subprocess.Popen(
                ["pixi", "run", "--manifest-path", f"{dep_path}/pixi.toml", "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print("  started in the background; give it a moment, then: pixi run check --deep")
        except FileNotFoundError:
            print("  pixi not on PATH — start it yourself in another terminal")
    else:
        print(f"\nstart the dependency with:\n"
              f"  pixi run --manifest-path {dep_path}/pixi.toml serve\n"
              f"then:  pixi run check --deep")


if __name__ == "__main__":
    main()
