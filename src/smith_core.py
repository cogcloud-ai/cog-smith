"""cog-smith core: mint-request validation, format-aware rendering, and
atomic minting.

Review 2026-08-22 hardening (F2): builder answers are TYPED and VALIDATED
before anything touches disk; values that land in structured formats are
inserted through format-aware serialization tokens (``*_TOML``, ``*_YAML``
variants produced here with json/yaml serializers), never raw; minting is
ATOMIC — rendered into a staging sibling, validated for unrendered tokens,
then renamed into place; any failure removes the staging directory and the
destination is never half-created.

A minted Cog = rendered templates (identity, context, examples, tests) +
verbatim machinery (everything in templates/<t>/src/ except task_logic.py,
which the author owns). `smith check` verifies machinery against these
masters — the same copy-sync discipline as cog-forge.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path

SMITH_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SMITH_ROOT / "templates"

# Files the author edits after minting (rendered or copied, never sync-checked)
AUTHOR_OWNED_SRC = {"task_logic.py"}

# Conventional lifecycle tasks — the FALLBACK classification when an
# interface declares no explicit `audience:` (F7: audience is contextual;
# declaration beats inference).
LIFECYCLE_TASKS = {"resolve", "use", "check", "eval", "test", "bundle", "serve"}

TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# CogSpec core name grammar: lowercase alphanumerics, single hyphens,
# starts with a letter, no consecutive hyphens, no underscores.
COG_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
COG_ID_RE = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9]*(-[a-z0-9]+)*$")
TOKEN_WORD_RE = re.compile(r"^[a-z][a-z0-9_]*$")     # io values, prohibits


class MintError(Exception):
    pass


def machinery_files(template="context-cog"):
    src = TEMPLATES / template / "src"
    if not src.is_dir():
        return []
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


def _one_line(s):
    return " ".join(str(s).split())


def validate_request(tokens):
    """Validate the typed mint request BEFORE creating anything (F2).
    Raises MintError with every problem, not just the first."""
    problems = []

    name = str(tokens.get("COG_NAME", ""))
    if not COG_NAME_RE.match(name):
        problems.append(
            f"cog name {name!r} violates the name grammar (lowercase "
            f"alphanumerics and single hyphens, e.g. cog-my-worker)")
    cog_id = str(tokens.get("COG_ID", ""))
    if not COG_ID_RE.match(cog_id):
        problems.append(f"cog id {cog_id!r} must be org/name in the same grammar")
    elif not cog_id.endswith("/" + name):
        problems.append(f"cog id {cog_id!r} does not end with the cog name {name!r}")

    for key in ("SUMMARY", "OWNER", "LICENSE", "PUBLISHER", "TITLE",
                "MODEL_COG_ID", "MODEL_COG_SOURCE"):
        v = str(tokens.get(key, ""))
        if not v.strip():
            problems.append(f"{key} must be non-empty")
        if "\n" in v:
            problems.append(f"{key} must be a single line")

    port = str(tokens.get("PORT", ""))
    if not port.isdigit() or not (1024 <= int(port) <= 65535):
        problems.append(f"PORT must be an integer in 1024..65535, got {port!r}")

    produces = str(tokens.get("PRODUCES", ""))
    if not TOKEN_WORD_RE.match(produces):
        problems.append(f"PRODUCES must be a lowercase token "
                        f"(e.g. highlights), got {produces!r}")

    for line in str(tokens.get("PROHIBITS_YAML", "")).splitlines():
        item = line.strip().lstrip("- ").strip()
        if item and not TOKEN_WORD_RE.match(item):
            problems.append(f"prohibited action {item!r} must be a lowercase "
                            f"token (letters, digits, underscores)")

    if problems:
        raise MintError("invalid mint request:\n  - " + "\n  - ".join(problems))


def _serialization_tokens(tokens):
    """Derived tokens for structured-format insertion points (F2):
    values are produced by serializers, not raw substitution."""
    out = dict(tokens)
    summary = _one_line(tokens.get("SUMMARY", ""))
    out.setdefault("SUMMARY_ONELINE", summary[:160])
    # TOML basic string (json string escaping is valid TOML):
    out["SUMMARY_TOML"] = json.dumps(f"Context Cog: {out['SUMMARY_ONELINE']}")
    # YAML double-quoted scalars for frontmatter lines:
    out["DESCRIPTION_YAML"] = json.dumps(
        f"Context Cog. {summary} Depends on a Cog providing an "
        f"OpenAI-compatible model endpoint.")
    out["SUMMARY"] = summary          # single-line; safe inside block scalars
    return out


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
    return tokens


def mint(dest, tokens, template="context-cog", validate=True):
    """Create a new Cog package at dest, atomically. The destination never
    exists half-built: rendering happens in a staging sibling which is
    renamed into place only after every file rendered cleanly."""
    dest = Path(dest).resolve()
    if dest.exists():
        raise MintError(f"{dest} already exists — refusing to overwrite")
    troot = TEMPLATES / template
    if not troot.is_dir():
        raise MintError(f"unknown template {template!r}")

    if validate and template == "context-cog":
        validate_request(tokens)
    tokens = _serialization_tokens(tokens)

    staging = dest.parent / f".mint-{dest.name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    written = []
    try:
        for path in sorted(troot.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(troot)
            if rel.parts[0] == "src":
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)   # machinery + task_logic: verbatim
            elif path.suffix == ".tmpl":
                out = staging / rel.with_name(rel.name[:-len(".tmpl")])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render(path.read_text(), tokens))
            else:
                out = staging / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)
            written.append(str(out.relative_to(staging)))

        leftover = []
        for f in written:
            p = staging / f
            if p.suffix in (".yaml", ".yml", ".toml", ".md", ".json"):
                if TOKEN_RE.search(p.read_text()):
                    leftover.append(f)
        if leftover:
            raise MintError(f"unrendered tokens remain in: {leftover}")

        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"dest": str(dest), "files": written,
            "machinery": sorted(machinery_hashes(template).keys())}
