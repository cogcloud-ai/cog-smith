"""`smith card` — render a Cog's catalog card: what CogCloud, the Op
builder's picker, and any spec-aware client would show. Derived entirely
from the package's own declarations (the seam's fourth item)."""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smith_core       # noqa: E402
import toml_compat      # noqa: E402


def card(root):
    root = Path(root).resolve()
    m = yaml.safe_load((root / "cog.yaml").read_text()) or {}
    pixi = root / "pixi.toml"
    tasks = {}
    if pixi.exists():
        with open(pixi, "rb") as f:
            tasks = (toml_compat.load(f).get("tasks")) or {}

    interfaces = m.get("interfaces") or []
    iface_tasks = {i.get("task") for i in interfaces if i.get("task")}
    usage = sorted(t for t in iface_tasks if t not in smith_core.LIFECYCLE_TASKS)
    # serve is lifecycle (starting the service); the endpoint it serves is usage
    lifecycle = sorted(t for t in tasks if t in smith_core.LIFECYCLE_TASKS)

    reqs = []
    for r in m.get("requires") or []:
        if isinstance(r, dict):
            sats = [r.get("satisfied_by") or {}] + (r.get("also_satisfied_by") or [])
            reqs.append({"capability": r.get("capability"),
                         "locality": r.get("locality", "any"),
                         "satisfiers": [s.get("cog") for s in sats if s.get("cog")]})

    ctx = m.get("context") or {}
    return {
        "id": m.get("id"),
        "version": m.get("version"),
        "kind": m.get("kind"),
        "summary": " ".join(str(m.get("summary", "")).split()),
        "owner": m.get("owner"),
        "license": m.get("license"),
        "io": m.get("io"),
        "entry_points": [{"name": i.get("name"), "kind": i.get("kind"),
                          "endpoint": i.get("endpoint"),
                          "default": bool(i.get("default"))} for i in interfaces],
        "ops": {"usage": usage, "lifecycle": lifecycle},
        "requires": reqs,
        "prohibits": m.get("prohibits") or [],
        "input_contract": bool(ctx.get("input_schema")),
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
    lines.append(f"  input contract: {'declared' if c['input_contract'] else 'NONE'}"
                 f" · envelope v{c['envelope'] or '?'}"
                 f" · fixtures: {len(c['fixtures'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    c = card(sys.argv[1] if len(sys.argv) > 1 else ".")
    if "--json" in sys.argv:
        print(json.dumps(c, indent=2))
    else:
        print(render_text(c))
