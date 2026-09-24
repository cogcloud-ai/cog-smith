#!/usr/bin/env python3
"""(cog-smith machinery: derived from an earlier internal package;
envelope-v1 deltas marked.)
Run this Cog's declared evaluation fixtures against the bound model.

    pixi run eval                  # every fixture declared in the manifest
    pixi run eval -- --fixture evals/smoke.fixture.yaml
    pixi run eval -- --report      # also write evals/last-report.json
    pixi run eval -- --baseline    # retain a named report under evals/reports/

A fixture pairs an input bundle with mechanical expectations: parse, shape
(normative JSON Schema via the validator), grounding (citations exist, quotes
verbatim-up-to-whitespace), expected classification / category constraints
where the evidence is discriminating, and forbidden canary tokens.

Deterministic contract tests that need NO model live in tests/ (`pixi run
test`). This runner is the manifest's `evaluation.fixtures` block made
executable against a live binding; --baseline retains the report (with the full
binding identity and a content hash) as comparison evidence across routes.
The judged half of evaluation belongs to the independent-reviewer Cog.
Byte-identical across the Cogs that vendor it; `smith check` enforces it
by hash.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cog_binding  # noqa: E402
import cog_core     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _binding_key(binding):
    """The identity facts that must be COMMON across a run for a single
    top-level binding summary to be honest — a canonical dump of the WHOLE
    record (endpoint, model, pin state, satisfier, served_model, locality,
    response format, credential reference), not a hand-picked subset (v6
    hardening: a subset can hide a mid-run change in any omitted field)."""
    rec = (binding or {}).get("record") or {}
    return json.dumps(rec, sort_keys=True, default=str)


def aggregate_run(results):
    """One eligibility object for the whole run, derived from EVERY fixture
    result (v5 P0-1 — the endpoint can answer differently on any call, so a
    policy applied to the first binding can hide a later mismatch).

    Returns (eligibility, consistent, identities).
    """
    bindings = [r.get("binding") for r in results if r.get("binding")]
    identities = [(b or {}).get("model_identity") for b in bindings]
    keys = {_binding_key(b) for b in bindings}
    consistent = len(keys) <= 1
    first = bindings[0] if bindings else {}
    rec = (first or {}).get("record") or {}
    sat = rec.get("satisfier") or {}
    eligibility = {
        # every result must carry a binding, or a fixture silently vanishes
        # from provenance accounting (v6 hardening)
        "all_results_carry_bindings": len(bindings) == len(results),
        "evaluation_passed": all(r.get("passed") for r in results),
        "all_response_identities_verified": bool(identities)
                                            and all(i == "verified" for i in identities),
        "any_identity_mismatch": any(i == "mismatch" for i in identities),
        "binding_pinned": bool(rec.get("pinned")),
        "satisfier_declared": sat.get("declared") is True,
        "bindings_consistent": consistent,
        "waivers": [],
    }
    eligibility["provenance_eligible"] = (
        eligibility["all_results_carry_bindings"]
        and eligibility["evaluation_passed"]
        and eligibility["all_response_identities_verified"]
        and not eligibility["any_identity_mismatch"]
        and eligibility["binding_pinned"]
        and eligibility["satisfier_declared"]
        and consistent)
    return eligibility, consistent, identities


def baseline_decision(results, allow_unverified=False):
    """(retain_ok, suffix, reason, eligibility) — may this run be RETAINED?

    Retention rules (v4 P1-2, corrected for multi-fixture runs in v5 P0-1):
      - ANY mismatched response identity: never retained, waiver or not;
      - inconsistent bindings across fixtures: never retained — the run has no
        single identity to name;
      - any missing/unverified identity: retained only with the explicit
        waiver, which suffixes the name and is recorded INSIDE the hashed
        report;
      - everything else is retained, with eligibility dimensions stated
        explicitly — an unpinned or undeclared route is legitimate comparison
        evidence, it just can't claim provenance.
    """
    eligibility, consistent, identities = aggregate_run(results)
    if not identities:
        return False, "", "no fixture produced a binding — nothing to retain", eligibility
    if not eligibility["all_results_carry_bindings"]:
        return False, "", ("a fixture result carries no binding — provenance "
                           "accounting is incomplete; nothing retained"), eligibility
    if eligibility["any_identity_mismatch"]:
        return False, "", ("model identity MISMATCH on at least one fixture "
                           f"(identities: {identities}) — a run that answered from "
                           "a different model than the binding is never a baseline"), eligibility
    if not consistent:
        return False, "", ("fixtures ran against differing bindings — the run has "
                           "no single identity to retain"), eligibility
    if not eligibility["all_response_identities_verified"]:
        if not allow_unverified:
            return False, "", (f"response identities are {identities} — the provider "
                               "did not prove which model answered every fixture. Pass "
                               "--allow-unverified-identity to retain anyway; the report "
                               "will be marked ineligible for provenance claims"), eligibility
        eligibility["waivers"].append("allow-unverified-identity")
        eligibility["provenance_eligible"] = False
        return True, "+unverified", None, eligibility
    return True, "", None, eligibility


def served_revision_label(binding):
    """The revision component of a baseline name, read from the served-model
    pin fields (v3 P1-1) — a declared revision, else a digest-derived identity,
    else honestly 'unpinned'. Never the generic word 'pinned'."""
    rec = (binding or {}).get("record") or {}
    served = rec.get("served_model") or {}
    if served.get("revision"):
        return str(served["revision"])
    for field in ("weights_sha256", "effective_sha256"):
        if served.get(field):
            return str(served[field])[:12]
    return "unpinned"


def context_hashes(fixtures):
    """Identify exactly what was evaluated: carried context, schema, example,
    fixtures, and input bundles. Cog id/version alone is not enough identity
    pre-release — content changes without the version changing."""
    hashes = {}
    paths = [ROOT / "context" / "system.md",
             ROOT / "context" / "output-schema.json",
             ROOT / "context" / "output-example.json",
             # the evaluator itself is part of evaluated identity — scoring
             # code can change without the Cog version changing
             ROOT / "src" / "cog_eval.py",
             ROOT / "src" / "cog_core.py",
             ROOT / "src" / "cog_binding.py"]
    for fx_path in fixtures:
        paths.append(Path(fx_path))
        fx = yaml.safe_load(Path(fx_path).read_text())
        if fx.get("bundle"):
            paths.append(ROOT / fx["bundle"])
    for p in paths:
        try:
            rel = str(p.resolve().relative_to(ROOT.resolve()))
        except ValueError:
            rel = p.name
        hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def run_fixture(path):
    fx = yaml.safe_load(Path(path).read_text())
    expect = fx.get("expect") or {}
    bundle = json.loads((ROOT / fx["bundle"]).read_text())

    result = cog_core.invoke(bundle)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if expect.get("error") is False:
        check("no transport error", not result.get("error"),
              str((result.get("error") or {}).get("detail", "")))
    if result.get("error"):
        return {"fixture": fx.get("name", Path(path).stem), "checks": checks,
                "passed": False, "binding": result.get("binding"),
                "latency_s": None, "parsed": None}

    parsed = result.get("payload")   # envelope v1 (cog-smith machinery delta)
    raw = result.get("raw", "")

    if expect.get("parsed", True):
        check("response parsed as JSON", parsed is not None)

    want_abstain = expect.get("abstained", "any")
    if want_abstain != "any" and parsed is not None:
        check(f"abstained == {want_abstain}",
              bool(parsed.get("abstained")) == bool(want_abstain),
              f"abstain_reason={parsed.get('abstain_reason')!r}")

    # `fields` asserts the STRUCTURED RESULT (machinery 0.4.2).
    # Outside the not-abstained block on purpose: an abstention is a result
    # with fields too. A fixture that can only forbid a token asserts what the
    # Cog must not SAY, never what it must DECIDE — a response that assigned
    # P2 while citing some other grounded passage passed the review-priority
    # fixture, and a correct `unrated` answer that explained why the
    # reviewer's marker was ineligible FAILED it.
    for key, want in (expect.get("fields") or {}).items():
        got = (parsed or {}).get(key)
        check(f"{key} == {want!r}", got == want, f"got {got!r}")

    if parsed is not None and not parsed.get("abstained"):
        for key in expect.get("required_keys") or []:
            check(f"has '{key}'", key in parsed)
        n = expect.get("min_entries")
        items = parsed.get("entries") or parsed.get("findings") or []
        if n is not None:
            check(f"at least {n} entr(ies)", len(items) >= n, f"got {len(items)}")

        want_cls = expect.get("classification")
        if want_cls:
            got = parsed.get("classification")
            check(f"classification == {want_cls}", got == want_cls, f"got {got!r}")

        cats = {e.get("category") for e in items if isinstance(e, dict)}
        for cat in expect.get("categories_include") or []:
            check(f"category '{cat}' present", cat in cats, f"got {sorted(c for c in cats if c)}")
        for cat in expect.get("categories_exclude") or []:
            check(f"category '{cat}' absent", cat not in cats, f"got {sorted(c for c in cats if c)}")

    if expect.get("grounded"):
        probs = result.get("problems") or []
        details = [p.get("detail", str(p)) if isinstance(p, dict) else str(p)
                   for p in probs]
        check("grounded + schema-valid", not probs, "; ".join(details[:5]))

    for token in expect.get("forbid_tokens") or []:
        check(f"canary '{token}' absent",
              token not in raw and token not in json.dumps(parsed or {}),
              "CANARY LEAKED — the Cog followed instructions embedded in its input")

    return {
        "fixture": fx.get("name", Path(path).stem),
        "checks": checks,
        "passed": all(c["ok"] for c in checks),
        "binding": result.get("binding"),
        "latency_s": (result.get("timing") or {}).get("latency_s"),
        "parsed": parsed,
    }


def main():
    ap = argparse.ArgumentParser(prog="eval")
    ap.add_argument("--fixture", action="append", help="run only this fixture (repeatable)")
    ap.add_argument("--report", action="store_true", help="write evals/last-report.json")
    ap.add_argument("--baseline", action="store_true",
                    help="retain a named report under evals/reports/ as comparison evidence")
    ap.add_argument("--force", action="store_true",
                    help="allow --baseline to overwrite an existing report")
    ap.add_argument("--allow-unverified-identity", action="store_true",
                    help="retain a baseline even when the provider did not echo "
                         "the bound model; the report is marked ineligible for "
                         "provenance claims")
    args = ap.parse_args()

    if args.fixture:
        fixtures = [Path(f) for f in args.fixture]
    else:
        manifest = cog_binding.load_manifest(ROOT)
        declared = ((manifest.get("evaluation") or {}).get("fixtures")) or []
        fixtures = [ROOT / f for f in declared]
    if not fixtures:
        sys.exit("no fixtures declared and none given")

    results, all_ok = [], True
    for path in fixtures:
        r = run_fixture(path)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        lat = f"  ({r['latency_s']}s)" if r.get("latency_s") else ""
        print(f"[{mark}] {r['fixture']}{lat}")
        for c in r["checks"]:
            print(f"    {'ok  ' if c['ok'] else 'FAIL'} {c['check']}"
                  + (f"  — {c['detail']}" if c["detail"] and not c["ok"] else ""))
        all_ok &= r["passed"]

    binding = next((r["binding"] for r in results if r.get("binding")), None)
    if binding:
        rec = binding.get("record", {})
        print(f"\nbinding: {binding.get('model_echoed') or rec.get('model')} via "
              f"{rec.get('endpoint')} (from {binding.get('resolved_from')}, "
              f"pinned={rec.get('pinned')})")

    # The retention decision is made BEFORE the report is hashed, so the
    # eligibility object — waivers included — is covered by the integrity hash
    # (v5 P0-1). The top-level binding is only a convenience summary; the
    # eligibility object says whether it was actually common to every fixture.
    retain_ok, suffix, refuse_reason, eligibility = baseline_decision(
        results, getattr(args, "allow_unverified_identity", False))

    ctx_hashes = context_hashes(fixtures)
    ctx_identity = hashlib.sha256(
        json.dumps(ctx_hashes, sort_keys=True).encode()).hexdigest()
    report = {"results": results, "binding": binding,
              "eligibility": eligibility,
              "evaluated_content": ctx_hashes}
    body = json.dumps(report, indent=2, sort_keys=True)
    report_wrapped = {"sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "context_sha256": ctx_identity,
                      "report": report}

    if args.report:
        out = ROOT / "evals" / "last-report.json"
        out.write_text(json.dumps(report_wrapped, indent=2) + "\n")
        print(f"wrote {out.relative_to(ROOT)}")

    if args.baseline:
        if not retain_ok:
            sys.exit(f"refusing to retain baseline: {refuse_reason}")
        rec = (binding or {}).get("record") or {}
        model = (binding or {}).get("model_echoed") or rec.get("model") or "unknown"
        cogv = ((binding or {}).get("cog") or {}).get("version", "0")
        revision = served_revision_label(binding)
        ctx8 = report_wrapped["context_sha256"][:8]

        def slugify(s):
            return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-").lower()

        outdir = ROOT / "evals" / "reports"
        outdir.mkdir(exist_ok=True)
        out = outdir / (f"v{slugify(cogv)}+{slugify(model)}@{slugify(revision)}"
                        f"{suffix}+{ctx8}.json")
        if out.exists() and not args.force:
            sys.exit(f"refusing to overwrite baseline {out.relative_to(ROOT)} — "
                     f"use --force to replace it deliberately")
        out.write_text(json.dumps(report_wrapped, indent=2) + "\n")
        print(f"retained baseline {out.relative_to(ROOT)} — commit this deliberately")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
