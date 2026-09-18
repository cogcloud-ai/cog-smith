"""THE per-cog module of a code Cog — the only file in src/ you edit.

Everything else in src/ is shared cog-smith machinery, byte-identical across
created code Cogs and verified by `smith check`. Your Cog's identity lives
in:
  - the manifest — [tool.cog] in pixi.toml, or cog.yaml — (what you are,
    what you REACH outside the run, what you prohibit)
  - context/ (the declared input and output schemas: a code Cog's "context"
    is its declared shapes)
  - this file (the work itself, and what makes its output trustworthy)

The template default is a working toy task: items go in, a per-item report
comes out. It reaches nothing, so it is invoked with no grant. Replace it.

The three arguments:

    run(bundle, grant, journal) -> (payload, problems)

`grant` is the invocation's authority document (None when the Cog reaches
nothing) — read it with the cog_core helpers before every external call, and
report what you attempted in the payload's `authority_use` list. `journal`
is a cog_core.Journal (or None): read it FIRST so an effect already applied
is never applied twice, and record `applying` before an external effect and
`applied`/`failed` after it.

A sketch of the reaching shape, for when you replace this:

    def run(bundle, grant, journal):
        import cog_core
        problems, used = [], []
        done = journal.phases() if journal else {}
        for change in bundle["changes"]:
            cid = change["change_id"]
            if done.get(cid) in ("applied", "failed"):
                continue                         # already decided, exactly once
            ok, detail = cog_core.write_allowed(grant, cid,
                                                change.get("content_sha256"))
            if not ok:
                used.append(cog_core.use("write", "github", cid, "denied", detail))
                problems.append(cog_core.problem("authority", detail, "warn"))
                continue
            journal.append({"change_id": cid, "phase": "applying"})
            ...                                  # the one external call
            journal.append({"change_id": cid, "phase": "applied",
                            "evidence": {...}})
            used.append(cog_core.use("write", "github", cid, "authorized"))
        return {"authority_use": used, ...}, problems
"""


def check_input(bundle):
    """Input checks beyond the declared input schema. Return a list of
    cog_core.problem(...) dicts (import inside to avoid a load-time cycle)."""
    import cog_core
    problems = []
    seen = set()
    for index, item in enumerate(bundle.get("items") or []):
        iid = item.get("id")
        if iid in seen:
            problems.append(cog_core.problem(
                "input", f"items[{index}]: duplicate id {iid}"))
        seen.add(iid)
    return problems


def run(bundle, grant, journal):
    """The work. Deterministic here: no model, no external call, no grant."""
    items = bundle.get("items") or []
    report = []
    for item in items:
        text = str(item.get("text") or "")
        report.append({"id": item.get("id"),
                       "characters": len(text),
                       "words": len(text.split())})
    payload = {
        "item_count": len(report),
        "items": report,
        # Every operation this Cog attempted outside the run, with its
        # outcome. Empty here: the starter reaches nothing.
        "authority_use": [],
    }
    return payload, []


def check_output(payload, bundle):
    """Contract checks — the Cog's OWN in-package validation, reported in
    `problems`. The Gate decides what they mean; this Cog never does."""
    import cog_core
    problems = []
    if payload.get("item_count") != len(payload.get("items") or []):
        problems.append(cog_core.problem(
            "consistency", "item_count disagrees with the reported items"))
    if len(payload.get("items") or []) != len(bundle.get("items") or []):
        problems.append(cog_core.problem(
            "coverage", "every input item must appear in the report"))
    return problems
