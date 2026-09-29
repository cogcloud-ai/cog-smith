"""`smith migrate` — move a Cog's profile manifest between the two formats
and bring its shared machinery up to the current masters.

    pixi run migrate -- ../cog-x                 # cog.yaml -> [tool.cog] in pixi.toml
    pixi run migrate -- ../cog-x --to yaml       # the reverse
    pixi run migrate -- ../cog-x --dry-run       # report the plan, write nothing
    pixi run migrate -- ../cog-x --no-machinery  # manifest only

To pixi (the default, cog-execution ADR D9):
  - the cog.yaml declarations are written under ``[tool.cog]`` in pixi.toml,
    keeping their order; ``version`` and ``summary`` move to ``[workspace]``
    (version / description) so each is stated once. Existing pixi.toml
    content — comments, tasks, dependencies — is edited in place, never
    regenerated; any earlier ``[tool.cog]`` block is replaced.
  - keys whose value is ``null`` are dropped (TOML has no null; readers
    treat absent and null alike) and reported.
  - COG.md's ``manifest:`` pointer is updated; cog.yaml is removed.
To yaml: the inverse — ``[tool.cog]`` (with the stated-once fallbacks
applied) becomes cog.yaml, the block is removed from pixi.toml.

Machinery (packages carrying src/cog_core.py): every master in
templates/context-cog/src except task_logic.py is copied over the package's
copy when it differs, and the known manifest-reading lines in the created
tests/test_cog.py and the task_logic docstring are rewritten. Anything else
that still mentions cog.yaml (prose, per-cog code) is listed for the author
— migrate never edits author-owned logic.

Nothing is written until every new file content has been computed; the
result is then checked with `smith check` and the findings reported.
"""
import os
import re
import shutil
from pathlib import Path

import yaml

import smith_core
import smith_manifest

TOOL_HEADER_RE = re.compile(r"^\s*\[\[?\s*tool\.cog(\.[^\]]*)?\s*\]\]?\s*(#.*)?$")
ANY_HEADER_RE = re.compile(r"^\s*\[")
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MANIFEST_BANNER = [
    "",
    "# ---------------------------------------------------------------------------",
    "# Profile manifest: openteams/cog-manifest [0.1], hosted in pixi.toml under",
    "# [tool.cog] (cog-execution ADR D9). `version` and `summary` are stated once,",
    "# in [workspace] above (version / description); readers fall back to them.",
    "# ---------------------------------------------------------------------------",
]
# Template-origin lines that named cog.yaml; rewritten on every migrate call
# (idempotent). Author prose and per-cog code are never touched — they are
# listed under `review` instead.
TEMPLATE_REWRITES = {
    "pixi.toml": [
        ("# reads cog.yaml (resolution) and *.fixture.yaml (eval)",
         "# reads cog.yaml-format manifests and *.fixture.yaml (eval)"),
        ("# starting it is lifecycle (see the interface's audience note in cog.yaml)",
         "# starting it is lifecycle (see the interface's audience note in the manifest)"),
    ],
    "COG.md": [
        ("prohibitions in cog.yaml make that structural.)",
         "prohibitions in the manifest make that structural.)"),
        ("This Cog's identity lives in\n`cog.yaml`, `context/`, and `src/task_logic.py`.",
         "This Cog's identity lives in\nits manifest (`pixi.toml`), `context/`, and `src/task_logic.py`."),
        ("its manifest (`cog.yaml`)", "its manifest (`pixi.toml`)"),
    ],
}
TEST_MANIFEST_LINE = 'self.manifest = yaml.safe_load((ROOT / "cog.yaml").read_text())'
TEST_MANIFEST_NEW = ("# either format: [tool.cog] in pixi.toml, or cog.yaml\n"
                     "        self.manifest = cog_binding.load_manifest(ROOT)")
TASK_LOGIC_LINE = "  - cog.yaml (what you are, what you require, what you prohibit)"
TASK_LOGIC_NEW = ("  - the manifest — [tool.cog] in pixi.toml, or cog.yaml — (what you are,\n"
                  "    what you require, what you prohibit)")


class MigrateError(Exception):
    pass


# ------------------------------------------------------------ TOML writing
# A deliberately small writer for the manifest's shapes: scalars, lists of
# scalars, tables, arrays of tables, inline tables for small nested
# mappings. Not a general TOML emitter — it refuses what it cannot express
# faithfully rather than approximating.

def _key(k):
    k = str(k)
    return k if BARE_KEY_RE.match(k) else _string(k)


def _string(s):
    out = ['"']
    for ch in str(s):
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _is_scalar(v):
    return isinstance(v, (bool, int, float, str))


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise MigrateError(f"cannot write non-finite float {v!r} to TOML")
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    raise MigrateError(f"cannot write {type(v).__name__} {v!r} to TOML")


def _inlineable(d):
    """A mapping small enough to write as an inline table: scalars and
    lists of scalars only, nothing nested."""
    return isinstance(d, dict) and all(
        _is_scalar(v) or (isinstance(v, list) and all(_is_scalar(x) for x in v))
        for v in d.values())


def _value(v, path, dropped):
    """Inline rendering of a scalar, list, or inlineable mapping."""
    if _is_scalar(v):
        return _scalar(v)
    if isinstance(v, list):
        items = []
        for i, x in enumerate(v):
            if x is None:
                raise MigrateError(f"{path}[{i}] is null inside a list — TOML "
                                   f"lists cannot hold null; fix the manifest")
            if isinstance(x, dict):
                if not _inlineable(x):
                    raise MigrateError(f"{path}[{i}]: nested mapping inside a "
                                       f"list of mixed values cannot be written")
                items.append(_inline(x, f"{path}[{i}]", dropped))
            elif isinstance(x, list):
                items.append(_value(x, f"{path}[{i}]", dropped))
            else:
                items.append(_scalar(x))
        return "[" + ", ".join(items) + "]"
    if isinstance(v, dict):
        return _inline(v, path, dropped)
    raise MigrateError(f"{path}: cannot write {type(v).__name__} to TOML")


def _inline(d, path, dropped):
    parts = []
    for k, v in d.items():
        if v is None:
            dropped.append(f"{path}.{k}")
            continue
        parts.append(f"{_key(k)} = {_value(v, f'{path}.{k}', dropped)}")
    return "{ " + ", ".join(parts) + " }" if parts else "{}"


def _emit_table(lines, path, d, dropped, header=True):
    scalars, tables, arrays = [], [], []
    for k, v in d.items():
        p = f"{path}.{k}"
        if v is None:
            dropped.append(p)
        elif isinstance(v, dict):
            # top-level mappings (context, io, model, evaluation) read best
            # as their own tables; an empty mapping is just `{}`
            (scalars if not v else tables).append((k, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays.append((k, v))
        else:
            scalars.append((k, v))
    if header:
        lines.append(f"[{path}]")
    for k, v in scalars:
        lines.append(f"{_key(k)} = {_value(v, f'{path}.{k}', dropped)}")
    for k, v in tables:
        lines.append("")
        _emit_table(lines, f"{path}.{_key(k)}", v, dropped)
    for k, items in arrays:
        for i, item in enumerate(items):
            lines.append("")
            _emit_array_item(lines, f"{path}.{_key(k)}", item, dropped, f"{path}.{k}[{i}]")


def _emit_array_item(lines, path, d, dropped, where):
    lines.append(f"[[{path}]]")
    tables, arrays = [], []
    for k, v in d.items():
        p = f"{where}.{k}"
        if v is None:
            dropped.append(p)
        elif isinstance(v, dict) and not _inlineable(v):
            tables.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v) \
                and not all(_inlineable(x) for x in v):
            arrays.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_value(v, p, dropped)}")
    for k, v in tables:
        lines.append("")
        _emit_table(lines, f"{path}.{_key(k)}", v, dropped)
    for k, items in arrays:
        for i, item in enumerate(items):
            lines.append("")
            _emit_array_item(lines, f"{path}.{_key(k)}", item, dropped, f"{where}.{k}[{i}]")


def manifest_toml(manifest):
    """The ``[tool.cog]`` block for a manifest dict (without version /
    summary — those belong to [workspace]). Returns (text, dropped_keys)."""
    m = {k: v for k, v in manifest.items() if k not in ("version", "summary")}
    lines, dropped = [], []
    _emit_table(lines, "tool.cog", m, dropped)
    return "\n".join(lines) + "\n", dropped


# ------------------------------------------------------------ pixi.toml edits

def strip_tool_cog(text):
    """pixi.toml text without any [tool.cog*] sections (and the banner
    comment block immediately preceding the first one)."""
    lines = text.splitlines()
    keep, skipping, first_seen = [], False, False
    for line in lines:
        if TOOL_HEADER_RE.match(line):
            skipping = True
            if not first_seen:
                first_seen = True
                # drop the contiguous comment/blank run above the first header
                while keep and (keep[-1].strip() == "" or keep[-1].lstrip().startswith("#")):
                    keep.pop()
            continue
        if skipping and ANY_HEADER_RE.match(line):
            skipping = False
        if not skipping:
            keep.append(line)
    out = "\n".join(keep).rstrip("\n") + "\n"
    return out


def _set_workspace_field(text, field, value):
    """Set `field = "value"` inside [workspace] (or [project]), replacing an
    existing line or inserting after `name`."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"^\s*\[(workspace|project)\]\s*(#.*)?$", l)), None)
    if start is None:
        raise MigrateError("pixi.toml has no [workspace] table")
    end = next((i for i in range(start + 1, len(lines)) if ANY_HEADER_RE.match(lines[i])),
               len(lines))
    rendered = f"{field} = {_string(value)}"
    for i in range(start + 1, end):
        if re.match(rf"^\s*{field}\s*=", lines[i]):
            lines[i] = rendered
            break
    else:
        name_at = next((i for i in range(start + 1, end)
                        if re.match(r"^\s*name\s*=", lines[i])), start)
        lines.insert(name_at + 1, rendered)
    return "\n".join(lines) + "\n"


def _workspace(text):
    doc = smith_manifest.toml_compat.load(_Bytes(text))
    return doc.get("workspace") or doc.get("project") or {}


class _Bytes:
    def __init__(self, text):
        self._b = text.encode("utf-8")

    def read(self):
        return self._b


# ------------------------------------------------------------ the plan

def plan(root, to="pixi", machinery=True):
    """Compute every change without writing. Returns a dict:
    {"format": from, "to": to, "writes": {rel: text}, "removes": [rel],
     "copies": [(master_path, rel)], "notes": [...], "dropped": [...],
     "leftover": [rel...]}"""
    root = Path(root).resolve()
    if to not in smith_core.MANIFEST_FORMATS:
        raise MigrateError(f"unknown target format {to!r}")
    try:
        manifest, fmt, mpath = smith_manifest.load(root)
    except smith_manifest.ManifestError as e:
        raise MigrateError(str(e)) from e
    out = {"root": str(root), "format": fmt, "to": to, "writes": {},
           "removes": [], "copies": [], "notes": [], "dropped": [],
           "leftover": []}

    pixi_path = root / "pixi.toml"
    if fmt != to:
        if to == "pixi":
            if not pixi_path.exists():
                raise MigrateError("no pixi.toml to host [tool.cog] — the package "
                                   "is not a pixi workspace")
            text = strip_tool_cog(pixi_path.read_text())
            version = manifest.get("version")
            if version is None:
                raise MigrateError("cog.yaml has no version to move to [workspace]")
            ws = _workspace(text)
            if ws.get("version") not in (None, str(version)):
                out["notes"].append(
                    f"[workspace].version {ws.get('version')!r} replaced by the "
                    f"manifest version {version!r} (the Cog's declared identity)")
            text = _set_workspace_field(text, "version", str(version))
            summary = " ".join(str(manifest.get("summary") or "").split())
            if summary:
                if ws.get("description") not in (None, summary):
                    out["notes"].append(
                        f"[workspace].description replaced by the manifest "
                        f"summary (was {ws.get('description')!r})")
                text = _set_workspace_field(text, "description", summary)
            block, dropped = manifest_toml(manifest)
            out["dropped"] = dropped
            text = text.rstrip("\n") + "\n" + "\n".join(MANIFEST_BANNER) + "\n" + block
            out["writes"]["pixi.toml"] = text
            out["removes"].append("cog.yaml")
        else:
            m = dict(manifest)              # fallbacks already applied
            header = ("# Profile manifest: openteams/cog-manifest [0.1] "
                      "(standalone form; `smith migrate --to pixi` moves it "
                      "into pixi.toml under [tool.cog]).\n\n")
            out["writes"]["cog.yaml"] = header + yaml.safe_dump(
                m, sort_keys=False, allow_unicode=True, width=88)
            if pixi_path.exists():
                out["writes"]["pixi.toml"] = strip_tool_cog(pixi_path.read_text())
        cogmd = root / "COG.md"
        if cogmd.exists():
            text = cogmd.read_text()
            new = re.sub(r"^manifest:\s*.*$",
                         f"manifest: {smith_core.MANIFEST_FILES[to]}",
                         text, count=1, flags=re.M)
            if new != text:
                out["writes"]["COG.md"] = new
            elif "manifest:" not in text:
                out["notes"].append("COG.md has no `manifest:` pointer to update")
    else:
        out["notes"].append(f"manifest already in {to} format "
                            f"({mpath.name}); nothing to convert")

    # ---- template-origin wording ---------------------------------------
    if to == "pixi":
        for rel, pairs in TEMPLATE_REWRITES.items():
            path = root / rel
            text = out["writes"].get(rel)
            if text is None and path.exists():
                text = path.read_text()
            if text is None:
                continue
            new = text
            for old_, new_ in pairs:
                new = new.replace(old_, new_)
            if new != text:
                out["writes"][rel] = new

    # ---- machinery -----------------------------------------------------
    if machinery and (root / "src" / "cog_core.py").exists():
        for master in smith_core.machinery_files(smith_core.template_for(manifest)):
            target = root / "src" / master.name
            if not target.exists() or target.read_bytes() != master.read_bytes():
                out["copies"].append((str(master), f"src/{master.name}"))
        tl = root / "src" / "task_logic.py"
        if tl.exists() and TASK_LOGIC_LINE in tl.read_text():
            out["writes"]["src/task_logic.py"] = tl.read_text().replace(
                TASK_LOGIC_LINE, TASK_LOGIC_NEW, 1)
        tc = root / "tests" / "test_cog.py"
        if tc.exists():
            text = tc.read_text()
            if TEST_MANIFEST_LINE in text:
                new = text.replace(TEST_MANIFEST_LINE, TEST_MANIFEST_NEW, 1)
                if "import cog_binding" not in new:
                    new = new.replace("import cog_core     # noqa: E402",
                                      "import cog_binding  # noqa: E402\n"
                                      "import cog_core     # noqa: E402", 1)
                if "import cog_binding" not in new:
                    out["notes"].append("tests/test_cog.py: could not add the "
                                        "cog_binding import — add it by hand")
                out["writes"]["tests/test_cog.py"] = new

    # ---- what still says cog.yaml ---------------------------------------
    # Only after a real conversion, and discounting the wording migrate
    # itself introduces (which names both formats on purpose).
    introduced = (TEST_MANIFEST_NEW, TASK_LOGIC_NEW,
                  TEMPLATE_REWRITES["pixi.toml"][0][1])
    if to == "pixi" and fmt != to:
        pending = {rel: text for rel, text in out["writes"].items()}
        machinery_rels = {f"src/{m.name}" for m in
                          smith_core.machinery_files(smith_core.template_for(manifest))}
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in (".pixi", ".git", "__pycache__", "runs"))
            files.extend(Path(dirpath) / f for f in sorted(filenames))
        for p in files:
            if p.suffix not in (".py", ".md", ".toml", ".yaml", ".json"):
                continue
            rel = str(p.relative_to(root))
            if rel in out["removes"]:
                continue
            if rel in machinery_rels:
                continue                      # shared masters, not this Cog's text
            text = pending.get(rel)
            if text is None:
                try:
                    text = p.read_text()
                except UnicodeDecodeError:
                    continue
            for snippet in introduced:
                text = text.replace(snippet, "")
            if "cog.yaml" in text:
                out["leftover"].append(rel)
    return out


def apply(p):
    """Write a plan. Copies first (byte-identical masters), then rendered
    files, then removals."""
    root = Path(p["root"])
    for master, rel in p["copies"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(master, target)
    for rel, text in p["writes"].items():
        (root / rel).write_text(text)
    for rel in p["removes"]:
        target = root / rel
        if target.exists():
            target.unlink()


def describe(p, out=print):
    root = p["root"]
    out(f"{root}: {p['format']} -> {p['to']}")
    for rel in p["writes"]:
        out(f"  write   {rel}")
    for _, rel in p["copies"]:
        out(f"  sync    {rel}  (cog-smith machinery master)")
    for rel in p["removes"]:
        out(f"  remove  {rel}")
    for k in p["dropped"]:
        out(f"  drop    {k} (null — TOML has no null; readers treat absent the same)")
    for n in p["notes"]:
        out(f"  note    {n}")
    for rel in p["leftover"]:
        out(f"  review  {rel} still mentions cog.yaml (author-owned; not edited)")
