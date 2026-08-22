"""cog-smith core: template rendering and minting.

A minted Cog = rendered templates (identity, context, examples, tests) +
verbatim machinery (everything in templates/context-cog/src/ except
task_logic.py, which the author owns). `smith check` verifies the machinery
copies against these masters — the same copy-sync discipline as cog-forge.
"""
import hashlib
import re
import shutil
from pathlib import Path

SMITH_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SMITH_ROOT / "templates"

# Files the author edits after minting (rendered or copied, never sync-checked)
AUTHOR_OWNED_SRC = {"task_logic.py"}

# Conventional lifecycle tasks (face the hosting environment, not the Op layer)
LIFECYCLE_TASKS = {"resolve", "use", "check", "eval", "test", "bundle", "serve"}

TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


class MintError(Exception):
    pass


def machinery_files(template="context-cog"):
    src = TEMPLATES / template / "src"
    return sorted(p for p in src.glob("*.py") if p.name not in AUTHOR_OWNED_SRC)


def machinery_hashes(template="context-cog"):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in machinery_files(template)}


def render(text, tokens):
    def sub(m):
        key = m.group(1)
        if key not in tokens:
            raise MintError(f"template references undefined token {{{{{key}}}}}")
        return str(tokens[key])
    return TOKEN_RE.sub(sub, text)


def default_tokens(cog_name, **overrides):
    """Answer set for a mint. cog_name is the directory / short name
    (e.g. 'cog-meeting-highlights')."""
    short = cog_name[4:] if cog_name.startswith("cog-") else cog_name
    tokens = {
        "COG_NAME": cog_name,
        "COG_ID": f"openteams/{cog_name}",
        "TITLE": short.replace("-", " ").title(),
        "SUMMARY": "Produces grounded, cited highlights from supplied items. "
                   "(Minted default — replace.)",
        "OWNER": "trent@openteams.com",
        "LICENSE": "BSD-3-Clause",
        "PUBLISHER": "OpenTeams",
        "PORT": "8093",
        "PRODUCES": "highlights",
        "MODEL_COG_ID": "openteams/cog-qwen3b",
        "MODEL_COG_SOURCE": "../cog-demo/cog-qwen3b",
        "PROHIBITS_YAML": "  - send_external_message\n  - modify_source_data",
    }
    tokens.update({k: v for k, v in overrides.items() if v is not None})
    tokens.setdefault("SUMMARY_ONELINE",
                      " ".join(str(tokens["SUMMARY"]).split())[:160])
    return tokens


def mint(dest, tokens, template="context-cog"):
    """Create a new Cog package at dest from the template. Refuses to
    overwrite an existing directory."""
    dest = Path(dest).resolve()
    if dest.exists():
        raise MintError(f"{dest} already exists — refusing to overwrite")
    troot = TEMPLATES / template
    if not troot.is_dir():
        raise MintError(f"unknown template {template!r}")

    written = []
    for path in sorted(troot.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(troot)
        if rel.parts[0] == "src":
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)         # machinery + task_logic: verbatim
        elif path.suffix == ".tmpl":
            out = dest / rel.with_name(rel.name[:-len(".tmpl")])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render(path.read_text(), tokens))
        else:
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)
        written.append(str(out.relative_to(dest)))

    leftover = []
    for f in written:
        p = dest / f
        if p.suffix in (".yaml", ".yml", ".toml", ".md", ".json"):
            if TOKEN_RE.search(p.read_text()):
                leftover.append(f)
    if leftover:
        raise MintError(f"unrendered tokens remain in: {leftover}")
    return {"dest": str(dest), "files": written,
            "machinery": sorted(machinery_hashes().keys())}
