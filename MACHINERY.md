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

## Op machinery (0.4.3, 2026-09-17): `templates/op/src/`

A second lineage, on the same terms: `templates/op/src/` are the masters an
Op package carries verbatim, and `smith op check` enforces them by hash
(`smith_op.MACHINERY_VERSION` names the lineage). An Op has no author-owned
module at all — the equivalent of `task_logic.py` is `op.yaml` itself. If an
Op needs code, the answer is a Cog step, never a script in the Op.

| File | Provenance |
|---|---|
| `op_runner.py` | `gate_envelope`, `parse_envelope`, and `invoke_cog` lifted from `op-video-transcription/src/run_op.py` (the 2026-08-25 sample Op, at its current working-tree state — that package is not itself a git repo); the rest is new: topological step order, foreach, `on_fail`, retry-once, dry run |
| `op_spec.py` | NEW — `openteams/op-manifest [0.1]` load + validate + refuse-by-name, and the closed mapping-expression vocabulary (`$from`, `$default`, `$path`, `$run_dir`, `$stem`, `$literal`) |
| `op_track.py` | Track shape extended from `op-video-transcription`'s `track.json`: step `status`, `attempts`, foreach `elements`, `spec_sha256` |

Semantics implemented from `planning/current/phase2-op-runner-contract.md`
(§1–§4, §6). Gate wording is unchanged from the sample Op: three states
(`pass`, `pass-with-problems`, `fail`) with the reasons listed, `guards: []`
recorded honestly, and the Gate — never the Cog — deciding acceptance.

### 0.4.3 — the phase-2 review fixes

One bullet per finding of `planning/current/phase2-codex-review-1-cog-smith.md`
(findings 1 and 2 landed as 0.4.1 and 0.4.2); each has a regression test that
failed before the fix.

- **3 — `$run_dir` stays inside the run.** The subpath must be a relative
  string, and the resolved destination must be under the run directory;
  absolute operands, `..` escapes, and symlinks out of the run are refused
  BEFORE anything is created (`op_spec.run_dir_path`), including when the
  subpath arrived as mapped data. A literal escape is also refused at load.
- **4 — declarations are checked before anything runs.** The runner calls
  `op_spec.declaration_problems` for EVERY step before invoking the first
  one: the source must carry that Cog and the task must be one of its
  declared usage interfaces, or the run exits 2 having invoked nothing.
  `smith op check` now shares that code (`op_spec.cog_step_findings`), which
  reads a Cog's manifest straight from `pixi.toml [tool.cog]` or `cog.yaml`
  so the vendored machinery never imports cog-smith. (A declaration check —
  not authority enforcement; see the contract's §0 amendment.)
- **5 — `smith op run` resolves its paths first.** Package, runner, request,
  and `--runs-dir` are resolved against the CALLER's directory before the
  child is launched into the package directory.
- **6 — skipped and blocked steps have a result in the output context.** A
  blocked step's payload is null, a skipped single step's payload is null
  (it failed its Gate), and a skipped `foreach` step keeps its aggregate
  list. Outputs over them resolve instead of raising, so the run still ends
  `completed-with-problems`/0. If an output mapping does raise, the Track is
  finalised and saved before the error propagates.
- **7 — presence, not truthiness, for inputs.** An explicit `null` is a
  supplied value (never replaced by a default), a declared `default: null`
  satisfies an omitted required input, and only a genuinely absent input is
  missing.
- **8 — declared input schemas are checked at load and always applied.**
  `schema:` is validated with `check_schema` when the spec loads; at request
  time validation runs on schema PRESENCE (so `false` and `null` values are
  validated too), and a jsonschema complaint becomes an `OpSpecError`.
- **9 — the process boundary is part of the result.** A launch failure
  (`OSError`) and a nonzero exit — even after an envelope was printed — are
  invocation failures with a synthetic `ok: false` envelope carrying the exit
  code, so they route through the ordinary Gate, retry, and Track handling.
  The emitted envelope is kept in `raw` (with its identity, binding and
  problems carried across) rather than thrown away.
- **10 — a malformed envelope fails in a controlled way.** Field TYPES are
  checked (`envelope: 1`, boolean `ok`, `problems` a list of objects); output
  that is not an envelope becomes an invocation failure with the output kept
  as evidence, and `gate_envelope` itself never raises on malformed input.
- **11 — one ready step at a time.** `_order` takes the EARLIEST ready step
  in spec order each round, so a step made ready mid-batch runs before a
  later independent one.
- **12 — Track rewrites are atomic.** `op_track.write_json` writes a
  temporary sibling, flushes and fsyncs it, then `os.replace`s it into place,
  so an interrupted rewrite leaves the previous Track readable.
- **13 — malformed spec field types are named problems.** A non-string step
  id or input name, a non-list `depends_on`, a non-string dependency, and a
  non-string mapping key are reported as `OpSpecError` problems (exit 2)
  instead of raising `TypeError`/`AttributeError`.
- **14 — only an EXACT key set is an operator.** `{"$from": "literal text",
  "label": "x"}` is ordinary data and is walked recursively; an object whose
  keys are ALL `$`-prefixed but unrecognized is still refused by name.
- **15 — `op check` exits 2 for an invalid spec.** Spec problems carry the
  `opspec-invalid` check name, and both output modes exit 2 for them, as
  `op new` and the runner do. Ordinary package findings still exit 1.
- **16 — the generated suite only skips an unfilled starter.** The template
  validates `examples/request.json` against the declared inputs and skips on
  THAT; the dry run then runs outside the `try`, so a broken mapping fails.

Also in 0.4.3, from the integration agent's report: `op new` writes a
placeholder only where a REQUIRED input declares no default (an optional
input, and one declaring `default: null`, get `null`), and a run stopped by
`on_fail: stop` records every step it never reached with status
`not-reached`, so a Track always lists every step of the spec.

### 0.4.2 — an envelope may be pretty-printed

`parse_envelope` read stdout LINE by line, so it could only see an envelope
printed as one line. cog-smith's context-cog machinery prints its envelope
indented, so the Op layer threw away a perfectly good `ok: false` envelope —
binding, problems and all — and replaced it with a synthetic
`invocation-failed` carrying an empty detail. It now scans stdout for JSON
objects (`raw_decode` from each `{`) and takes the LAST one carrying an
`envelope` key, so a progress preamble, a pretty-printed envelope, and
non-JSON noise between objects are all tolerated. Output with no envelope at
all still raises, as before.

### 0.4.1 — the request-file flag is negotiated at the seam

`invoke_cog` invoked every Cog as `... <task> -- --request <file>`, but
cog-smith's OWN context-cog machinery (`templates/context-cog/src/cog_cli.py`)
accepts `--bundle`, so an Op could not call a created context Cog at all — the
first live triage run failed with `ask: error: unrecognized arguments:
--request`. The seam now tries `--request` and then `--bundle`, and only when
the Cog's CLI refused the first flag BY NAME (argparse exits non-zero with
"unrecognized arguments") — before doing any work, so nothing effectful can run
twice. An ordinary failure is never re-invoked. The lasting fix is to make the
two lineages agree on one flag; until they do, this keeps the Op layer able to
call the Cogs cog-smith itself creates.

Rules are the Cog rules: fixes happen HERE, version-bumped, and roll out to
Op packages by re-copying. A created Op package must be immediately runnable
(`pixi run op -- --request examples/request.json --dry-run`) and immediately
checkable (`smith op check --tests`).
