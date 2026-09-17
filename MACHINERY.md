# MACHINERY.md — provenance of the template machinery

`templates/context-cog/src/` are the masters that `smith check` enforces by
hash on every created Cog (everything except the author-owned
`task_logic.py`). Lineage:

| File | Provenance |
|---|---|
| `cog_binding.py` | forge @7fe8aca + manifest-format delta (0.3.0): `load_manifest` / `manifest_path` read `[tool.cog]` in pixi.toml or cog.yaml |
| `cog_resolve.py` | forge @7fe8aca + 0.3.0 delta: `load_cog` goes through `cog_binding.load_manifest` |
| `cog_use.py` | VERBATIM from cog-forge @7fe8aca |
| `cog_eval.py` | forge @7fe8aca + envelope-v1 deltas (marked in file): payload key, structured problems, timing field; 0.3.0: declared fixtures read via `cog_binding.load_manifest` |
| `cog_core.py` | genericized from forge cog_core: task logic extracted to task_logic.py; input validation against the manifest-declared input schema; envelope v1 emission; reusable verbatim_quote_check |
| `cog_api.py` | genericized from forge cog_api: endpoint/port derived from the manifest; envelope passthrough; same 4xx/5xx status mapping |
| `cog_cli.py` | genericized from forge cog_cli |
| `task_logic.py` | AUTHOR-OWNED — the only per-cog module in src/; ships with a working toy task |

Rules: never edit machinery inside a created Cog (check will fail it).
Machinery fixes happen HERE, version-bumped, and roll out to created Cogs by
re-copying — the same copy-sync discipline cog-forge used, with cog-smith
as the single source.

## 0.2.0 (2026-08-23): vocabulary sweep

Comment-, docstring-, and message-only changes across the masters — no
behavior change: in-cog validation is called a **contract check**, never a
guard (cog_core's grounding check; cog_use's locality/transport checks;
cog_binding's docstring), matching the contract-check / Guard / Gate
vocabulary; "minted" became "created" (the mint term is retired).
Rolled out by re-copying the masters into cog-meeting-highlights and
testcog; both re-verified by `smith check --tests`.

## 0.3.0 (2026-09-16): manifest in pixi.toml `[tool.cog]` (ADR D9)

Behavior change in the masters. `cog_binding.load_manifest` reads the
profile manifest from EITHER `pixi.toml` `[tool.cog]` (default; `version`
and `summary` fall back to `[workspace]` version / description, so each is
stated once) OR `cog.yaml`; it raises FileNotFoundError when a package
carries neither and ValueError when it carries both (readers must never
disagree about the Cog). `cog_binding.manifest_path` reports which file is
in use. `cog_resolve.load_cog` and `cog_eval` (declared fixtures) now go
through `load_manifest` instead of opening `cog.yaml` directly, and the
test template loads the manifest the same way. Reading `[tool.cog]`
requires tomllib, so the created-Cog Python floor is 3.11 (the master
raises a clear RuntimeError on older interpreters rather than half-reading
a manifest). These rules are mirrored by hand in cog-smith's
`smith_manifest.py`; keep the two in step.
Rolled out by `smith migrate` into cog-meeting-highlights (cog.yaml
removed, `[tool.cog]` written); re-verified by `smith check --tests`.
