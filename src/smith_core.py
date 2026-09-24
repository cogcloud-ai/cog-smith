"""cog-smith core: cog-request validation, format-aware rendering, and
atomic creation.

Internal-review hardening (F2): builder answers are TYPED and VALIDATED
before anything touches disk; values that land in structured formats are
inserted through format-aware serialization tokens (``*_TOML``, ``*_YAML``
variants produced here with json/yaml serializers), never raw; creating is
ATOMIC — rendered into a staging sibling, validated for unrendered tokens,
then renamed into place; any failure removes the staging directory and the
destination is never half-created.

A created Cog = rendered templates (identity, context, examples, tests) +
verbatim machinery (everything in templates/<t>/src/ except task_logic.py,
which the author owns). `smith check` verifies machinery against these
masters — the same copy-sync discipline as the forge lineage it came from.

Manifest format (cog-execution ADR D9): the profile manifest is written into
``pixi.toml`` under ``[tool.cog]`` by default, or into a standalone
``cog.yaml`` (``manifest_format="yaml"``). Each template carries both
renderings: ``cog.yaml.tmpl`` (emitted only for yaml) and
``_partials/manifest.toml.tmpl`` (rendered into the ``{{MANIFEST_TOML}}``
token of ``pixi.toml.tmpl`` only for pixi). ``_partials/`` is never emitted
as files.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path

SMITH_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SMITH_ROOT / "templates"

# Files the author edits after creating (rendered or copied, never sync-checked)
AUTHOR_OWNED_SRC = {"task_logic.py"}

# Conventional lifecycle tasks — the FALLBACK classification when an
# interface declares no explicit `audience:` (F7: audience is contextual;
# declaration beats inference).
LIFECYCLE_TASKS = {"resolve", "use", "check", "eval", "test", "bundle", "serve"}

TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

#: Cog kinds smith creates, and the template master for each. `context` is
#: a Cog whose work a model does; `code` is a Cog whose work code does
#: (kind: code, decided 2026-09-17) — the same seam with the model half
#: removed.
KIND_TEMPLATES = {"context": "context-cog", "code": "code-cog"}
COG_KINDS = tuple(KIND_TEMPLATES)
DEFAULT_KIND = "context"

MANIFEST_FORMATS = ("pixi", "yaml")
DEFAULT_MANIFEST_FORMAT = "pixi"
MANIFEST_FILES = {"pixi": "pixi.toml", "yaml": "cog.yaml"}
PARTIALS_DIR = "_partials"
MANIFEST_PARTIAL = "manifest.toml.tmpl"
YAML_MANIFEST_TEMPLATE = "cog.yaml.tmpl"

# CogSpec core name grammar: lowercase alphanumerics, single hyphens,
# starts with a letter, no consecutive hyphens, no underscores.
COG_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
COG_ID_RE = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9]*(-[a-z0-9]+)*$")
TOKEN_WORD_RE = re.compile(r"^[a-z][a-z0-9_]*$")     # io values, prohibits


class CreateError(Exception):
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
            raise CreateError(f"template references undefined token {{{{{key}}}}}")
        return str(tokens[key])
    return TOKEN_RE.sub(sub, text)


def _one_line(s):
    return " ".join(str(s).split())


def validate_request(tokens, template="context-cog"):
    """Validate the typed create request BEFORE creating anything (F2).
    Raises CreateError with every problem, not just the first.

    A code Cog answers fewer questions: it has no model dependency to name
    and no served endpoint, so MODEL_COG_* and PORT are not asked about."""
    problems = []
    model_backed = template != "code-cog"

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

    identity = ["SUMMARY", "OWNER", "LICENSE", "PUBLISHER", "TITLE"]
    if model_backed:
        identity += ["MODEL_COG_ID", "MODEL_COG_SOURCE"]
    for key in identity:
        v = str(tokens.get(key, ""))
        if not v.strip():
            problems.append(f"{key} must be non-empty")
        if "\n" in v:
            problems.append(f"{key} must be a single line")

    port = str(tokens.get("PORT", ""))
    if model_backed and (not port.isdigit() or not (1024 <= int(port) <= 65535)):
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
        raise CreateError("invalid create request:\n  - " + "\n  - ".join(problems))


def _prohibits_list(tokens):
    """The prohibited actions as a list, from the request's YAML-list token."""
    items = []
    for line in str(tokens.get("PROHIBITS_YAML", "")).splitlines():
        item = line.strip().lstrip("- ").strip()
        if item:
            items.append(item)
    return items


def _serialization_tokens(tokens, template="context-cog"):
    """Derived tokens for structured-format insertion points (F2):
    values are produced by serializers, not raw substitution."""
    out = dict(tokens)
    summary = _one_line(tokens.get("SUMMARY", ""))
    out.setdefault("SUMMARY_ONELINE", summary[:160])
    # TOML basic strings (json string escaping is valid TOML). The workspace
    # description IS the profile summary when the manifest lives in
    # pixi.toml (stated once — ADR D9), so it carries the full sentence.
    out["SUMMARY_TOML"] = json.dumps(summary)
    out["PROHIBITS_TOML"] = json.dumps(_prohibits_list(tokens))
    for key in ("COG_ID", "OWNER", "LICENSE", "MODEL_COG_ID",
                "MODEL_COG_SOURCE", "PRODUCES"):
        if key in out:
            out[f"{key}_TOML"] = json.dumps(str(out[key]))
    # YAML double-quoted scalars for frontmatter lines:
    out["DESCRIPTION_YAML"] = json.dumps(
        f"Code Cog. {summary} No model in the loop; what it reaches outside "
        f"the run is declared in the manifest."
        if template == "code-cog" else
        f"Context Cog. {summary} Depends on a Cog providing an "
        f"OpenAI-compatible model endpoint.")
    out["SUMMARY"] = summary          # single-line; safe inside block scalars
    return out


def _manifest_tokens(tokens, troot, manifest_format):
    """MANIFEST_FILE (what COG.md points at) and MANIFEST_TOML (the rendered
    [tool.cog] block, empty for the yaml format)."""
    if manifest_format not in MANIFEST_FORMATS:
        raise CreateError(f"unknown manifest format {manifest_format!r}; "
                          f"choose one of {list(MANIFEST_FORMATS)}")
    out = dict(tokens)
    out["MANIFEST_FILE"] = MANIFEST_FILES[manifest_format]
    partial = troot / PARTIALS_DIR / MANIFEST_PARTIAL
    if manifest_format == "pixi":
        if not partial.exists():
            raise CreateError(f"template {troot.name!r} has no "
                              f"{PARTIALS_DIR}/{MANIFEST_PARTIAL}; it cannot "
                              f"write a pixi.toml manifest")
        out["MANIFEST_TOML"] = render(partial.read_text(), out)
    else:
        out["MANIFEST_TOML"] = ""
    return out


def default_tokens(cog_name, **overrides):
    """Answer set for a package creation. cog_name is the directory / short name
    (e.g. 'cog-my-worker')."""
    short = cog_name[4:] if cog_name.startswith("cog-") else cog_name
    tokens = {
        "COG_NAME": cog_name,
        "COG_ID": f"openteams/{cog_name}",
        "TITLE": short.replace("-", " ").title(),
        "SUMMARY": "Produces grounded, cited highlights from supplied items. "
                   "(Starter default — replace.)",
        "OWNER": "trent@openteams.com",
        "LICENSE": "Apache-2.0",
        "PUBLISHER": "OpenTeams",
        "PORT": "8093",
        "PRODUCES": "highlights",
        "MODEL_COG_ID": "openteams/cog-qwen3b",
        "MODEL_COG_SOURCE": "../cog-demo/cog-qwen3b",
        "PROHIBITS_YAML": "  - send_external_message\n  - modify_source_data",
    }
    tokens.update({k: v for k, v in overrides.items() if v is not None})
    return tokens


def create(dest, tokens, template="context-cog", validate=True, overlays=None,
           manifest_format=DEFAULT_MANIFEST_FORMAT):
    """Create a new Cog package at dest, atomically. The destination never
    exists half-built: rendering happens in a staging sibling which is
    renamed into place only after every file rendered cleanly.

    manifest_format: "pixi" (default) writes the profile manifest into
    pixi.toml under [tool.cog] and emits no cog.yaml; "yaml" writes the
    standalone cog.yaml and a plain pixi.toml. COG.md's `manifest:` pointer
    names whichever was written.

    overlays: optional {relative-path: text} written into the staging
    directory AFTER template rendering and the leftover-token check, so a
    drafting cog's authored context files replace the starter's inside the
    same atomic creation (the builder-op seam). Overlay content is written
    verbatim — it is not token-rendered and not token-checked, because
    drafted content may legitimately contain braces."""
    dest = Path(dest).resolve()
    if dest.exists():
        raise CreateError(f"{dest} already exists — refusing to overwrite")
    troot = TEMPLATES / template
    if not troot.is_dir():
        raise CreateError(f"unknown template {template!r}")

    if validate and template in KIND_TEMPLATES.values():
        validate_request(tokens, template)
    tokens = _serialization_tokens(tokens, template)
    tokens = _manifest_tokens(tokens, troot, manifest_format)

    staging = dest.parent / f".smith-{dest.name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    written = []
    try:
        for path in sorted(troot.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(troot)
            if rel.parts[0] == PARTIALS_DIR:
                continue                       # rendered into tokens, never emitted
            if rel.name == YAML_MANIFEST_TEMPLATE and manifest_format != "yaml":
                continue
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
            raise CreateError(f"unrendered tokens remain in: {leftover}")

        staging_root = staging.resolve()
        for rel, text in (overlays or {}).items():
            out = (staging / rel)
            if staging_root not in out.resolve().parents:
                raise CreateError(f"overlay path escapes the package: {rel!r}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(str(text))
            if str(rel) not in written:
                written.append(str(rel))

        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"dest": str(dest), "files": written,
            "manifest": MANIFEST_FILES[manifest_format],
            "machinery": sorted(machinery_hashes(template).keys())}
