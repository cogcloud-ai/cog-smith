#!/usr/bin/env python3
"""LLMModel CRs -> model-catalog.yaml (the hub's model offering as data).

Reads llm-serving-pack `LLMModel` custom resources — files, directories,
kubectl output (`kubectl get llmmodels -o yaml`, List kind and multi-doc
both handled) — and emits the model-catalog YAML that cog-smith's
`generate-descriptors` consumes, one deployment-descriptor model cog per served
model:

    python scripts/llmmodel_catalog.py path/to/models/ --base-domain \\
        cluster.example.com --surface internal -o /tmp/catalog.yaml
    pixi run generate-descriptors -- --config /tmp/catalog.yaml --out-dir ../models

Surface selection (llm-serving-pack serves every model on one shared
hostname pair):

    internal      endpoint https://llm-internal.<base-domain>/v1 — the
                  in-cluster JWT path (default api_key_env NEBARI_LLM_JWT);
                  for cogs hosted on the hub
    external      endpoint https://llm.<base-domain>/v1 — the API-key path
                  (default api_key_env LLM_API_KEY); for external consumers
    install-time  no address baked in; resolution requires --endpoint —
                  the portable default when no --base-domain is known

Deliberately pack-neutral: stdlib + pyyaml only, no cog-smith imports, so
this script can live in cog-smith, nebari-nexus-pack, or a future cog pack
without change — the pack-composability decision stays open. Deterministic:
same input, same catalog. Credentials are never read or written; the
catalog carries environment-variable NAMES only.

Not covered (deliberately, v1): per-model external subdomains
(`endpoints.external.subdomain`) — the shared-hostname pair is the
canonical shape; quantization is left null unless stated by the CR (it is
not an LLMModel field today).
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

COG_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

SURFACES = ("internal", "external", "install-time")


class LLMModelError(Exception):
    pass


def _iter_docs(text, source):
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        raise LLMModelError(f"{source}: not valid YAML: {e}")
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "List":
            for item in doc.get("items") or []:
                if isinstance(item, dict):
                    yield item
        else:
            yield doc


def load_llmmodels(paths):
    """Collect LLMModel documents from files and directories (*.yaml/*.yml).
    Non-LLMModel documents are skipped silently — kubectl output and mixed
    example directories both contain other kinds."""
    models, seen_sources = [], 0
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = sorted(f for f in path.rglob("*")
                           if f.suffix in (".yaml", ".yml") and f.is_file())
        elif path.is_file():
            files = [path]
        elif str(p) == "-":
            seen_sources += 1
            models += [d for d in _iter_docs(sys.stdin.read(), "<stdin>")
                       if d.get("kind") == "LLMModel"]
            continue
        else:
            raise LLMModelError(f"no such file or directory: {p}")
        for f in files:
            seen_sources += 1
            models += [d for d in _iter_docs(f.read_text(), str(f))
                       if d.get("kind") == "LLMModel"]
    if not seen_sources:
        raise LLMModelError("no input files found")
    return models


def _cog_name(metadata_name):
    """k8s object name -> cog name. k8s names are lowercase RFC1123
    (letters, digits, hyphens, possibly dots); the cog- prefix guarantees
    the letter-initial rule, dots become hyphens."""
    base = re.sub(r"-{2,}", "-", str(metadata_name).lower().replace(".", "-"))
    name = f"cog-{base}".strip("-")
    if not COG_NAME_RE.match(name):
        raise LLMModelError(
            f"LLMModel name {metadata_name!r} cannot become a valid cog "
            f"name ({name!r}); rename it or edit the emitted catalog")
    return name


def catalog_entry(doc, surface, base_domain, api_key_env):
    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    model = spec.get("model") or {}
    model_name = model.get("name")
    if not meta.get("name") or not model_name:
        raise LLMModelError(
            f"LLMModel missing metadata.name or spec.model.name: "
            f"{meta.get('name')!r}")

    entry = {
        "cog_name": _cog_name(meta["name"]),
        "model_name": model_name,
        # llm-serving-pack dispatches on the model field == spec.model.name
        "served_model_id": model_name,
        "provider": (f"llm-serving-pack vLLM (llm-d), namespace "
                     f"{meta.get('namespace', '?')}"),
        "runtime": "vllm",
        "locality": "customer-vpc",
        "api_key_env": api_key_env,
        # LLMModel pins no served revision today: bindings are honest about
        # being unpinned rather than silently unversioned.
        "revision": None,
    }
    if surface == "internal":
        entry["endpoint"] = f"https://llm-internal.{base_domain}/v1"
    elif surface == "external":
        entry["endpoint"] = f"https://llm.{base_domain}/v1"
    else:
        entry["address"] = "install-time"
    return entry


def build_catalog(models, surface, base_domain, api_key_env, owner=None):
    if surface in ("internal", "external") and not base_domain:
        raise LLMModelError(
            f"--surface {surface} needs --base-domain (or use "
            f"--surface install-time)")
    entries, seen = [], set()
    for doc in models:
        entry = catalog_entry(doc, surface, base_domain, api_key_env)
        if entry["cog_name"] in seen:
            raise LLMModelError(
                f"duplicate cog name {entry['cog_name']!r} — two LLMModels "
                f"share a metadata.name across inputs")
        seen.add(entry["cog_name"])
        entries.append(entry)
    if not entries:
        raise LLMModelError("no LLMModel documents found in the inputs")
    catalog = {"defaults": {}, "models": entries}
    if owner:
        catalog["defaults"]["owner"] = owner
    if not catalog["defaults"]:
        del catalog["defaults"]
    return catalog


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="llmmodel-catalog",
        description="LLMModel CRs -> generate-descriptors catalog YAML")
    ap.add_argument("paths", nargs="+",
                    help="LLMModel YAML files/directories, or - for stdin "
                         "(kubectl get llmmodels -o yaml | ...)")
    ap.add_argument("--surface", choices=SURFACES, default="install-time",
                    help="which serving surface the descriptors point at "
                         "(default: install-time — portable, no address)")
    ap.add_argument("--base-domain",
                    help="cluster base domain, e.g. cluster.example.com "
                         "(required for --surface internal/external)")
    ap.add_argument("--api-key-env",
                    help="credential REFERENCE (env var name) the "
                         "descriptors carry; defaults per surface")
    ap.add_argument("--owner", help="defaults.owner for the catalog")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    api_key_env = args.api_key_env or {
        "internal": "NEBARI_LLM_JWT",
        "external": "LLM_API_KEY",
        "install-time": "MODEL_API_KEY",
    }[args.surface]

    try:
        models = load_llmmodels(args.paths)
        catalog = build_catalog(models, args.surface, args.base_domain,
                                api_key_env, owner=args.owner)
    except LLMModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    header = ("# Generated by scripts/llmmodel_catalog.py from LLMModel "
              "CRs.\n# Feed to: pixi run generate-descriptors -- --config "
              "<this file> --out-dir <dir>\n")
    text = header + yaml.safe_dump(catalog, sort_keys=False,
                                   default_flow_style=False)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(catalog['models'])} model(s))")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
