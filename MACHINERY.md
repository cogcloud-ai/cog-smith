# MACHINERY.md — provenance of the template machinery

`templates/context-cog/src/` are the masters that `smith check` enforces by
hash on every created Cog (everything except the author-owned
`task_logic.py`). Lineage:

| File | Provenance |
|---|---|
| `cog_binding.py` | VERBATIM from cog-forge @7fe8aca (engineering-gates PASS) |
| `cog_resolve.py` | VERBATIM from cog-forge @7fe8aca |
| `cog_use.py` | VERBATIM from cog-forge @7fe8aca |
| `cog_eval.py` | forge @7fe8aca + envelope-v1 deltas (marked in file): payload key, structured problems, timing field |
| `cog_core.py` | genericized from forge cog_core: task logic extracted to task_logic.py; input validation against the manifest-declared input schema; envelope v1 emission; reusable verbatim_quote_check |
| `cog_api.py` | genericized from forge cog_api: endpoint/port derived from the manifest; envelope passthrough; same 4xx/5xx status mapping |
| `cog_cli.py` | genericized from forge cog_cli |
| `task_logic.py` | AUTHOR-OWNED — the only per-cog module in src/; ships with a working toy task |

Rules: never edit machinery inside a created Cog (check will fail it).
Machinery fixes happen HERE, version-bumped, and roll out to created Cogs by
re-copying — the same copy-sync discipline cog-forge used, with cog-smith
as the single source.
