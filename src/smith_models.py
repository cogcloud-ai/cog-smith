"""`smith mint-model-cog` — generate deployment-descriptor model Cogs from a
model catalog config. This is how a hosting environment's model offering
becomes ordinary, resolvable satisfiers (see output/model-cogs-hub-offering.md
in coglab): one descriptor cog per served model, minted from config, with
metering/audit attaching at the binding record.

Config shape (YAML):

    defaults:                 # optional, merged under every model
      owner: trent@openteams.com
      license: BSD-3-Clause
      publisher: OpenTeams
    models:
      - name: qwen35b-collab            # -> cog-<name> unless cog_name given
        model_name: Qwen/Qwen3.5-35B-A3B-GPTQ-Int4
        served_model_id: Qwen/Qwen3.5-35B-A3B-GPTQ-Int4
        provider: Collab-hosted vLLM
        locality: customer-vpc          # local | customer-vpc | cloud
        address: install-time           # OR endpoint: https://...
        api_key_env: COLLAB_API_KEY
        quantization: GPTQ-Int4         # optional
        runtime: vllm                   # optional
        revision: null                  # optional served-revision pin
        summary: ...                    # optional; generated if absent

Identity rule (one cog per served model, never a parameterized gateway cog):
pinning and identity verification are per served model — that property is
what makes Gates and Tracks trustworthy.
"""
import re
from pathlib import Path

import yaml

import smith_core

VALID_LOCALITY = {"local", "customer-vpc", "cloud"}
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class ModelConfigError(Exception):
    pass


def _entry_tokens(entry, defaults):
    e = {**defaults, **entry}
    name = e.get("cog_name") or (f"cog-{e['name']}" if e.get("name") else None)
    if not name:
        raise ModelConfigError("each model needs a `name` (or `cog_name`)")
    short = name.rsplit("/", 1)[-1]
    if not smith_core.COG_NAME_RE.match(short):
        raise ModelConfigError(
            f"{name}: cog name violates the name grammar (lowercase "
            f"alphanumerics, single hyphens)")
    model_name = e.get("model_name") or e.get("served_model_id")
    if not model_name:
        raise ModelConfigError(f"{name}: needs model_name or served_model_id")
    served = e.get("served_model_id") or model_name

    locality = e.get("locality", "cloud")
    if locality not in VALID_LOCALITY:
        raise ModelConfigError(f"{name}: locality {locality!r} not in "
                               f"{sorted(VALID_LOCALITY)}")

    api_key_env = e.get("api_key_env", "MODEL_API_KEY")
    if not ENV_NAME_RE.match(str(api_key_env)):
        raise ModelConfigError(
            f"{name}: api_key_env {api_key_env!r} does not look like an "
            f"environment-variable NAME. Descriptors carry credential "
            f"references, never credentials.")

    endpoint = e.get("endpoint")
    address = e.get("address")
    if endpoint and address:
        raise ModelConfigError(f"{name}: give endpoint OR address, not both")
    if endpoint:
        if not re.match(r"^(https://|http://127\.0\.0\.1[:/])", endpoint):
            raise ModelConfigError(
                f"{name}: endpoint must be https:// or loopback http "
                f"(non-loopback plaintext is refused at resolution anyway)")
        address_yaml = f"    endpoint: {endpoint}"
        resolve_hint = ""
    else:
        if address not in (None, "install-time"):
            raise ModelConfigError(f"{name}: address must be 'install-time'")
        address_yaml = ("    # The address is an installation fact — resolution"
                        " requires --endpoint.\n"
                        "    address: install-time")
        resolve_hint = " --endpoint <URL>"

    provider = e.get("provider", "externally provided")
    summary = e.get("summary") or (
        f"Deployment descriptor for {model_name} ({provider}), served over an "
        f"OpenAI-compatible API. Carries the pinnable identity of the "
        f"deployment; credentials and (where install-time) the address are "
        f"installation facts.")
    revision = e.get("revision")

    return name, {
        "COG_NAME": name,
        "COG_ID": f"openteams/{name}",
        "TITLE": name[4:].replace("-", " ").title() if name.startswith("cog-")
                 else name.replace("-", " ").title(),
        "SUMMARY": " ".join(summary.split()),
        "OWNER": e.get("owner", "trent@openteams.com"),
        "LICENSE": e.get("license", "BSD-3-Clause"),
        "PUBLISHER": e.get("publisher", "OpenTeams"),
        "LOCALITY": locality,
        "MODEL_NAME": model_name,
        "QUANTIZATION": e.get("quantization") or "null",
        "RUNTIME": e.get("runtime") or "null",
        "REVISION": "null" if revision in (None, "null", "") else str(revision),
        "SERVED_MODEL_ID": served,
        "API_KEY_ENV": api_key_env,
        "ADDRESS_YAML": address_yaml,
        "RESOLVE_HINT": resolve_hint,
        "PROVIDER_NOTE": provider,
    }


def mint_from_config(config_path, out_dir):
    """Mint one descriptor model cog per config entry. Returns a summary;
    refuses to overwrite existing directories (skips them with a note)."""
    cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    models = cfg.get("models")
    if not isinstance(models, list) or not models:
        raise ModelConfigError("config needs a non-empty `models:` list")
    defaults = cfg.get("defaults") or {}
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: validate EVERY entry and reject duplicates before any
    # write, so an invalid late entry can never leave a partial catalog
    # (review 2026-08-22, F8).
    plan, seen = [], set()
    for entry in models:
        name, tokens = _entry_tokens(entry, defaults)
        if name in seen:
            raise ModelConfigError(f"duplicate cog name in catalog: {name}")
        seen.add(name)
        plan.append((name, tokens))

    minted, skipped = [], []
    for name, tokens in plan:
        dest = out_dir / name
        if dest.exists():
            skipped.append(name)
            continue
        smith_core.mint(dest, tokens, template="model-descriptor-cog",
                        validate=False)
        minted.append(name)
    return {"minted": minted, "skipped": skipped, "out_dir": str(out_dir)}
