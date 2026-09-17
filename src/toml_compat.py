"""tomllib compatibility shim.

Python 3.11+ ships tomllib; older interpreters (like a 3.10 VM the workbench
may be inspected from) don't, and installing a parser contradicts the
zero-extra-deps rule. The fallback below reads exactly the subset these Cogs'
pixi.toml files actually use — `[section]` headers, comments, `key = "string"`
pairs, and single-line string arrays. It is deliberately LENIENT everywhere
except [tasks]: a value it can't read elsewhere is skipped (we never consume
it), but an unreadable value inside [tasks] raises, because silently dropping
a task would make an operation vanish from the UI without explanation.

A `[tool.cog]` profile manifest (nested tables, arrays of tables, inline
tables) is beyond the fallback on purpose: it raises rather than returning a
half-read manifest, since a manifest with silently missing declarations is
worse than no manifest. Reading `[tool.cog]` needs a real TOML parser
(Python 3.11+).

Same portability class as the jsonschema Draft202012→Draft7 fallback in the
forge Cogs (SPEC-NOTES §14): prefer the real library, degrade honestly.
"""
import re

try:
    import tomllib as _tomllib
except ImportError:  # Python < 3.11
    _tomllib = None

_SECTION = re.compile(r"^\[([^\]]+)\]\s*(?:#.*)?$")
_PAIR = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+)$")
_DQ = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
_SQ = re.compile(r"^'([^']*)'\s*(?:#.*)?$")


def _string_value(raw):
    """Parse a TOML basic or literal string; None if raw isn't one."""
    m = _DQ.match(raw)
    if m:
        # TOML basic-string escapes; our files are ASCII command strings.
        return m.group(1).encode("ascii", "backslashreplace").decode("unicode_escape")
    m = _SQ.match(raw)
    if m:
        return m.group(1)
    return None


def _fallback_parse(text):
    doc = {}
    table = doc
    in_tasks = False
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SECTION.match(line)
        if m:
            name = m.group(1).strip()
            if name.startswith("[") or name == "tool.cog" or name.startswith("tool.cog."):
                raise ValueError(
                    f"pixi.toml line {lineno}: [{name}] — the fallback TOML "
                    f"parser (no tomllib on this interpreter) cannot read a "
                    f"[tool.cog] manifest; use Python 3.11+")
            table = doc.setdefault(name, {})
            in_tasks = name == "tasks"
            continue
        m = _PAIR.match(line)
        if not m:
            if in_tasks:
                raise ValueError(f"pixi.toml line {lineno}: fallback TOML "
                                 f"parser can't read this [tasks] line: {line!r}")
            continue  # lenient outside [tasks]
        key, raw = m.group(1), m.group(2).strip()
        val = _string_value(raw)
        if val is None:
            if in_tasks:
                raise ValueError(f"pixi.toml line {lineno}: task {key!r} is not "
                                 f"a plain string — the fallback parser (no "
                                 f"tomllib on this interpreter) only supports "
                                 f"string task values")
            continue  # e.g. arrays in [workspace]; we never consume them
        table[key] = val
    return doc


def load(fobj):
    """Drop-in for tomllib.load (binary file object)."""
    if _tomllib is not None:
        return _tomllib.load(fobj)
    return _fallback_parse(fobj.read().decode("utf-8"))
