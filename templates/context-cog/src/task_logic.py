"""THE per-cog module — the only file in src/ you edit.

Everything else in src/ is shared cog-smith machinery, byte-identical across
minted Cogs and verified by `smith check`. Your Cog's identity lives in:
  - cog.yaml (what you are, what you require, what you prohibit)
  - context/ (system.md instructions, input/output schemas, worked example)
  - this file (how input becomes a prompt; what makes output trustworthy)

The template default is a working toy task ("grounded highlights"): items go
in, cited highlights come out, every quote verified verbatim. Replace it.
"""


def check_input(bundle):
    """Task-specific input checks beyond the input schema.
    Return a list of cog_core.problem(...) dicts (import inside to avoid a
    cycle at module load)."""
    import cog_core
    problems = []
    seen = set()
    for i, item in enumerate(bundle.get("items") or []):
        iid = item.get("id")
        if iid in seen:
            problems.append(cog_core.problem("input", f"items[{i}]: duplicate id {iid}"))
        seen.add(iid)
    return problems


def render_input(bundle):
    """Input bundle -> the user message the model sees. Keep it plain text,
    include the same caveats a human would see."""
    lines = [f"Focus: {bundle.get('focus', 'general')}"]
    if bundle.get("notes"):
        lines.append(f"Input notes: {bundle['notes']}")
    lines += ["", "ITEMS"]
    for item in bundle.get("items") or []:
        lines += ["", f"--- item: {item.get('id')}", str(item.get("text", "")).rstrip()]
    return "\n".join(lines)


def check_output(parsed, bundle):
    """Task-specific semantic contract checks, run AFTER the output schema
    check. The forge rule: verifying a citation EXISTS is not verifying that
    it SUPPORTS the claim — hence the verbatim-quote check."""
    import cog_core
    problems = []
    if parsed.get("abstained") and parsed.get("entries"):
        problems.append(cog_core.problem(
            "schema", "abstained is true but entries is non-empty"))
    sources = {item["id"]: f"{item.get('text', '')}"
               for item in bundle.get("items") or [] if item.get("id")}
    for e in parsed.get("entries") or []:
        if isinstance(e, dict):
            problems.extend(cog_core.verbatim_quote_check(
                e, sources, quote_field="evidence_quote", ids_field="item_ids"))
    return problems
