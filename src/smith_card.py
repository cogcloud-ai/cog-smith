"""`smith card` — render a Cog's catalog card: what CogCloud, the Op
builder's picker, and any spec-aware client would show. Derived entirely
from the package's own declarations (the seam's fourth item)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core       # noqa: E402
import smith_manifest   # noqa: E402


def card(root):
    root = Path(root).resolve()
    m, fmt, mp = smith_manifest.load(root)      # either manifest format
    pixi_doc = smith_manifest.read_pixi(root)
    tasks = (pixi_doc or {}).get("tasks") or {}

    interfaces = m.get("interfaces") or []
    # F7 (internal review): audience is contextual, not lexical — a
    # DECLARED audience wins; the lifecycle-name set is only the fallback.
    usage, lifecycle, inferred = set(), set(), False
    for i in interfaces:
        t_ = i.get("task")
        if not t_:
            continue
        if i.get("endpoint"):
            # An endpoint-bearing interface: the ENDPOINT is the usage
            # surface; its task starts the service, which is lifecycle.
            lifecycle.add(t_)
            continue
        aud = i.get("audience")
        if aud == "usage":
            usage.add(t_)
        elif aud == "lifecycle":
            lifecycle.add(t_)
        elif t_ in smith_core.LIFECYCLE_TASKS:
            lifecycle.add(t_); inferred = True
        else:
            usage.add(t_); inferred = True
    for t_ in tasks:
        if t_ not in usage and t_ not in lifecycle:
            (lifecycle if t_ in smith_core.LIFECYCLE_TASKS else usage).add(t_)
            inferred = True
    usage, lifecycle = sorted(usage), sorted(lifecycle)

    reqs = []
    for r in m.get("requires") or []:
        if isinstance(r, dict):
            sats = [r.get("satisfied_by") or {}] + (r.get("also_satisfied_by") or [])
            reqs.append({"capability": r.get("capability"),
                         "locality": r.get("locality", "any"),
                         "satisfiers": [s.get("cog") for s in sats if s.get("cog")]})

    ctx = m.get("context") or {}
    model = m.get("model") or {}
    return {
        "card": 1,
        "manifest": mp.name,
        "audience_inferred": inferred,
        "provides": m.get("provides") or [],
        "locality": m.get("locality"),
        "model": ({"name": model.get("name"),
                   "quantization": model.get("quantization"),
                   "runtime": model.get("runtime"),
                   "revision": model.get("revision"),
                   "served_model_id": next(
                       (i.get("served_model_id") for i in interfaces
                        if i.get("default")), None),
                   "address": next(
                       (i.get("address") or i.get("endpoint") for i in interfaces
                        if i.get("default")), None)}
                  if m.get("kind") == "model" else None),
        "id": m.get("id"),
        "version": m.get("version"),
        "kind": m.get("kind"),
        "summary": " ".join(str(m.get("summary", "")).split()),
        "owner": m.get("owner"),
        "license": m.get("license"),
        "io": m.get("io"),
        "entry_points": [{"name": i.get("name"), "kind": i.get("kind"),
                          "task": i.get("task"),
                          "audience": i.get("audience"),
                          "endpoint": i.get("endpoint"),
                          "default": bool(i.get("default"))} for i in interfaces],
        "ops": {"usage": usage, "lifecycle": lifecycle},
        "requires": reqs,
        "prohibits": m.get("prohibits") or [],
        "input_contract": ctx.get("input_schema"),
        "output_contract": ctx.get("output_schema"),
        "envelope": 1 if (root / "src" / "cog_core.py").exists() else None,
        "fixtures": (m.get("evaluation") or {}).get("fixtures") or [],
    }


def render_text(c):
    lines = [
        f"{c['id']}  ({c['kind']} · v{c['version']})",
        f"  {c['summary']}",
        f"  owner {c['owner']} · license {c['license']}",
        "",
        "  entry points:",
    ]
    for e in c["entry_points"]:
        star = " *default" if e["default"] else ""
        ep = f"  {e['endpoint']}" if e.get("endpoint") else ""
        lines.append(f"    {e['name']:10} [{e['kind']}]{ep}{star}")
    lines.append(f"  ops: usage {c['ops']['usage'] or '—'} · "
                 f"lifecycle {c['ops']['lifecycle'] or '—'}")
    if c["io"]:
        lines.append(f"  io: {c['io'].get('accepts')} -> {c['io'].get('produces')}")
    for r in c["requires"]:
        lines.append(f"  requires: {r['capability']} (locality {r['locality']}) "
                     f"<- {', '.join(r['satisfiers']) or 'undeclared'}")
    if c["prohibits"]:
        lines.append(f"  prohibits: {', '.join(c['prohibits'])}")
    if c.get("model"):
        mdl = c["model"]
        lines.append(f"  provides: {', '.join(c['provides'])} · "
                     f"locality {c['locality']}")
        lines.append(f"  model: {mdl['name']}"
                     + (f" ({mdl['quantization']})" if mdl.get("quantization")
                        not in (None, "null") else "")
                     + (f" · runtime {mdl['runtime']}" if mdl.get("runtime")
                        not in (None, "null") else ""))
        lines.append(f"  served_model_id: {mdl['served_model_id']} · "
                     f"address: {mdl['address']}"
                     + (f" · revision {mdl['revision']}"
                        if mdl.get("revision") not in (None, "null") else
                        " · revision unpinned"))
    else:
        lines.append(f"  input contract: "
                     f"{c['input_contract'] or 'NONE'}"
                     f" · envelope v{c['envelope'] or '?'}"
                     f" · fixtures: {len(c['fixtures'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    c = card(sys.argv[1] if len(sys.argv) > 1 else ".")
    if "--json" in sys.argv:
        print(json.dumps(c, indent=2))
    else:
        print(render_text(c))
