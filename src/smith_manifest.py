"""Manifest location and loading for the two profile manifest formats.

The openteams/cog-manifest [0.1] profile can live in either of two files:

- ``pixi.toml`` under ``[tool.cog]`` — the DEFAULT. Per the cog-execution
  ADR (D9) the profile puts its declarations in the one file Nebi already
  reads, so ``version`` and the summary are stated once: ``[tool.cog]`` may
  omit ``version`` and ``summary`` and they are taken from ``[workspace]``
  (``version`` / ``description``).
- ``cog.yaml`` — the original standalone YAML manifest, still supported
  (``smith new --manifest yaml``).

COG.md's ``manifest:`` pointer names which one a package uses; readers
dispatch on that pointer (CogSpec core), and this module is the one place
that dispatch happens for smith itself. The created Cogs' machinery
(``cog_binding.load_manifest``) applies the same rules independently —
machinery is copied verbatim and cannot import smith.
"""
from pathlib import Path

import yaml

import toml_compat

FORMATS = ("pixi", "yaml")
DEFAULT_FORMAT = "pixi"
FILES = {"pixi": "pixi.toml", "yaml": "cog.yaml"}
TOOL_TABLE = "cog"


class ManifestError(Exception):
    pass


def read_pixi(root):
    """The parsed pixi.toml document, or None if the file is absent."""
    p = Path(root) / "pixi.toml"
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return toml_compat.load(f)


def has_pixi_manifest(doc):
    return bool(doc and isinstance(doc.get("tool"), dict)
                and isinstance(doc["tool"].get(TOOL_TABLE), dict))


def from_pixi(doc):
    """[tool.cog] with the stated-once fallbacks applied."""
    m = dict(doc["tool"][TOOL_TABLE])
    ws = doc.get("workspace") or doc.get("project") or {}
    if "version" not in m and ws.get("version") is not None:
        m["version"] = ws["version"]
    if "summary" not in m and ws.get("description") is not None:
        m["summary"] = ws["description"]
    return m


def locate(root):
    """Which manifest file(s) a package carries.

    Returns (format, path) or (None, None). Raises ManifestError when both
    formats are present — a package must have exactly one profile manifest,
    otherwise two readers could disagree about what the Cog is."""
    root = Path(root)
    found = []
    doc = read_pixi(root)
    if has_pixi_manifest(doc):
        found.append(("pixi", root / "pixi.toml"))
    if (root / "cog.yaml").exists():
        found.append(("yaml", root / "cog.yaml"))
    if len(found) > 1:
        raise ManifestError(
            "both pixi.toml [tool.cog] and cog.yaml are present — a package "
            "carries exactly one profile manifest; remove one")
    return found[0] if found else (None, None)


def load(root):
    """(manifest dict, format, path). Raises ManifestError (none / both /
    unparseable) so callers report one kind of problem."""
    fmt, path = locate(root)
    if fmt is None:
        raise ManifestError(
            f"{Path(root)} has no profile manifest: neither pixi.toml with a "
            f"[tool.cog] table nor cog.yaml")
    if fmt == "pixi":
        try:
            return from_pixi(read_pixi(root)), fmt, path
        except ValueError as e:
            raise ManifestError(f"pixi.toml is not readable: {e}") from e
    try:
        m = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"cog.yaml is not valid YAML: {e}") from e
    if not isinstance(m, dict):
        raise ManifestError("cog.yaml must be a mapping")
    return m, fmt, path
