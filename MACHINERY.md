# MACHINERY.md — provenance of the template machinery

`templates/context-cog/src/` are the masters that `smith check` enforces by
hash on every created CONTEXT Cog (everything except the author-owned
`task_logic.py`). A created **code** Cog carries the `templates/code-cog/src/`
masters instead — `smith check` picks the lineage by the manifest's kind (see
"Code-cog machinery" below). Lineage:

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

## 0.4.0 (2026-09-19): the caller deadline is a binding fact

Behavior change in the masters. Contract: `planning/current/phase3-contract.md`
§11 items 3 and 4. The phase 3 live sweep sent a 161,000-character dependency
request to a cloud model and lost it to a deadline hard-coded at 180 seconds
inside `cog_core.invoke` — a number with nowhere to be said otherwise. A
deadline is a property of the BINDING (a 3B model on loopback and a cloud model
reading a whole backlog do not share one), so it moved into the record:

- `cog_binding` gains `REQUEST_TIMEOUT_DEFAULT = 180`,
  `REQUEST_TIMEOUT_MIN = 10`, `REQUEST_TIMEOUT_MAX = 1800`, the single bounds
  rule `timeout_problems(value)`, and the reader `request_timeout(record)`.
  `request_timeout_s` joins `DEFAULTS` and the record's OPTIONAL fields, and
  `validate_record` enforces the bounds on load AND write, like every other
  record rule. Optional means a model.json written before 0.4.0 keeps working
  and inherits the default — the field is new, the installation is not wrong.
  Booleans are refused explicitly (`isinstance(True, int)` is the trap).
- `cog_use` gains `--timeout N`, writes `request_timeout_s`, prints it, and
  shows it under `--show`; the check runs BEFORE anything is written, beside
  the locality and transport contract checks.
- `cog_resolve` gains `--timeout N` and states the deadline in every record it
  writes, so a binding has one however it was produced.
- `cog_core` reads it once at import (`REQUEST_TIMEOUT_S`), and
  `invoke(bundle, timeout=None, ...)` means "what the binding says". An
  explicit value is a one-call override, bounds-checked like any other, and an
  impossible one is the named envelope error `invalid-timeout` rather than a
  traceback.
- `cog_cli` gains `--timeout N` for that one call, refused by the argument
  parser (exit 2) when out of bounds.

No environment override was added: `COG_MODEL_*` exists for the fields that
change the model's IDENTITY, and a deadline does not (see the contract's Open
items).

Starter tests (§11 item 4): `templates/context-cog/tests/test_cog.py.tmpl`
gains a `canned_model` helper — the in-process twin of `tests/mock_model.py`,
patching `cog_core.health` and `urllib.request.urlopen` — and asserts that a
FULL invocation of the sample bundle carries no `schema` problem, that invoke
uses the binding's deadline and honors an override, and that an impossible
override is refused by name. The equivalent code-cog assertion is in
`templates/code-cog/tests/test_cog.py.tmpl`
(`test_a_full_run_carries_no_schema_problem`). Asserting `ok` alone is what let
`cog-read-github` emit a field its own output schema did not declare.

cog-smith coverage: `tests/test_context_cog_timeout.py` (17 tests, including
`use`/`resolve` through real processes and a pre-0.4.0 record still binding).
Rolled out by re-copying the masters into the three judgment Cogs and every
other sibling carrying this lineage; each re-verified by `smith check`.

## 0.4.1 (2026-09-19): `--check --deep --timeout N` is honored

Contract §11b, from Codex review 9. 0.4.0 taught `cog_cli` a `--timeout`, and
`--check` ignored it: `health(deep=True)` was called with no deadline and
`cog_core` used its own `max(timeout, 30)`, so a deep completion probe that
needed 40 s reported DOWN despite an explicit 600. The value was parsed and
bounds-checked and then dropped, which is the worst of the three.

`cog_core.health(timeout=3, deep=False, deep_timeout=None)`: the DEEP probe
performs a real completion — the same kind of call `invoke` makes, taking the
same kind of time — so `deep_timeout`, when given, is used as stated; without
one it keeps its 30 s floor. The SHALLOW liveness probe keeps `timeout`
unchanged: a socket that has not answered in three seconds is not alive, and
waiting longer tells nobody anything. `cog_cli` passes `--timeout` as
`deep_timeout` only when `--deep` is given, and its help text says so.

Additive signature change: every existing `health()` and `health(deep=True)`
caller is unaffected (`cog_api`, `cog_core.invoke`, the created suites).
Starter test: `templates/context-cog/tests/test_cog.py.tmpl::
test_the_deep_probe_honors_an_explicit_deadline`. cog-smith coverage:
`tests/test_context_cog_timeout.py::TestDeepHealthHonorsTheOverride` (2 —
the core, and the CLI wiring where the value was actually dropped), plus
`test_a_written_record_reaches_the_invocations_http_deadline`, which replaces
the 0.4.0 test that imported the module and printed `REQUEST_TIMEOUT_S`
(review 9, nit 2: that would have passed had `invoke` ignored the constant).
It now runs a full invocation in the created Cog's own process with the model
replaced and asserts the deadline `urlopen` was handed.

The code-starter's `test_a_full_run_carries_no_schema_problem` asserts `ok`
and a payload in the SAME test (review 9, nit 1): a `task-failed` envelope
carries no schema problem either, so the schema claim alone passed vacuously
on an early failure.

Rolled out by re-copying the masters into the nine carriers: the three
judgment Cogs (cog-issue-classifier, cog-dependency-detector,
cog-overlap-duplicate-detector), cog-author, cog-build-evaluator,
cog-explicit-action-extractor, cog-meeting-highlights, cog-op-designer and
testcog; each re-verified by `smith check`.

## Code-cog machinery (0.1.0, 2026-09-17): `templates/code-cog/src/`

A third lineage, on the same terms. `templates/code-cog/src/cog_core.py` and
`cog_cli.py` are the masters a created **code Cog** (`kind: code`) carries
verbatim, enforced by hash by `smith check`, which picks the masters by the
manifest's KIND. `task_logic.py` is author-owned, as always.
`cog_core.MACHINERY_VERSION` names the lineage and is reported in every
envelope's `binding`.

| File | Provenance |
|---|---|
| `cog_core.py` | NEW — the context-cog seam with the model half removed: manifest load (`[tool.cog]` or cog.yaml, same rules as `cog_binding`), declared input/output schema validation, `task_logic.run(bundle, grant, journal)`, envelope v1 with `binding = {kind: code, cog, task_logic_sha256, machinery}` (no `model` key), the grant checks (`no-grant`, `grant-invalid`, `grant-expired`, `grant-wrong-run`, `grant-wrong-recipient`) and the per-call `read_allowed`/`write_allowed`/`use` helpers, and the `Journal` (append-only JSONL, fsync per line, `read()`/`phases()`/`last()`) |
| `cog_cli.py` | genericized from the context-cog CLI: `--bundle [--grant --run-id --journal] \| --check`; no `--raw`, no `--deep` |
| `task_logic.py` | AUTHOR-OWNED — `run(bundle, grant, journal) -> (payload, problems)` plus optional `check_input` / `check_output`; ships with a working toy task that reaches nothing |

Contract: `planning/current/phase3-contract.md` §1. The honesty rule is part
of the machinery's doc comments and stays there: the grant is checked by the
Cog's OWN code; the local host is not an enforced restricted environment.

### Code-cog 0.1.4 (2026-09-18) — the package checker joins the boundary (review 4)

Contract §9d, from `planning/current/phase3-codex-review-4-code-cogs-and-op.md`.
Regression test: `tests/test_code_cog.py::test_a_crashing_output_checker_is_a_named_envelope`.

- **S6 — the package's output checker runs inside the same exception
  boundary as `run`.** `validate_output` (the declared output schema, then
  the package's `check_output`) used to run AFTER the `try` that wraps
  `task_logic.run`, so a checker that tripped over a payload it did not
  expect — review 4 found `cog-record-run` constructing a set from a list
  `change_id` — raised a traceback out of the CLI, after the task had
  already had its external effects. It is now an `ok: false` envelope with
  its own code, `output-check-failed`: "the task is broken" and "the task's
  self-check is broken" are different repairs, so they are different names.
  A `JournalCorrupt` raised from a checker keeps its own name.

### Code-cog 0.1.3 (2026-09-17) — the Codex confirmation fixes (review 3)

One bullet per finding of
`planning/current/phase3-codex-review-3-cog-smith-confirmation.md` that lives
in this lineage, resolved as contract §9c decides. Each fix has a regression
test that failed before it (`tests/test_code_cog.py`).

- **New 5 — journal I/O failure is a structured envelope.** An `OSError`
  reading the journal (`PermissionError`, `IsADirectoryError`, a vanished
  mount) used to escape `invoke`'s preflight, which caught only
  `JournalCorrupt`, and printed a traceback where an envelope belongs. Any
  `OSError` reading the journal is now `journal-unreadable`, an `ok: false`
  envelope; so is an `OSError` CREATING it, caught in `cog_cli` where the
  `Journal` is built. `journal-corrupt` keeps its meaning: content that
  cannot be trusted, as against a journal that cannot be reached at all.

### Code-cog 0.1.2 (2026-09-17) — the Codex verification fixes (review 2)

One bullet per finding of
`planning/current/phase3-codex-review-2-cog-smith-verification.md` that lives
in this lineage, resolved as contract §9b decides. Each behavioural fix has a
regression test that failed before it (`tests/test_code_cog.py`).

- **B4 / New 2 — the Cog hashes the change it is about to apply.** New
  helpers `canonical_sha256` and `change_content_sha256(change)` (the same
  canonical JSON, excluding both hash fields, that the runner uses). The
  starter's write sketch computes `content_sha256` from the change object
  and no longer forwards the digest the bundle states beside it — forwarding
  it only checked that the bundle agreed with itself, so content edited
  under an approved id passed.
- **B6 / New 4 — journal recovery always returns a structured error.**
  `Journal.read()` cuts the torn tail as BYTES at the last newline before
  anything is decoded, then decodes each complete line strictly: a crash
  halfway through a multibyte character used to raise `UnicodeDecodeError`
  over the whole file — losing every record before it and producing no
  envelope at all — and now repairs exactly as an ASCII fragment does. A
  complete line that is not UTF-8 is `journal-corrupt` by name.
- **S7 / New 4 — nested grant fields are type-checked.** `repositories` must
  be a LIST of strings: a number denies with a reason instead of raising
  `TypeError`, and a bare string denies instead of authorizing every
  substring of it. A `changes` that is not a list authorizes nothing.

### Code-cog 0.1.1 (2026-09-17) — the Codex review-1 fixes

One bullet per finding of `planning/current/phase3-codex-review-1-cog-smith.md`
that lives in this lineage, resolved as contract §9 decides. Each behavioural
fix has a regression test that failed before it (`tests/test_code_cog.py`).

- **B4 — the grant checks are per call and never fail open.** `check_grant`
  requires the grant's top-level `run_id` and `valid.run_id` to be non-empty
  strings that AGREE (a grant that matched on one and not the other used to
  pass), refuses an invocation carrying no `--run-id` (`grant-invalid`), and
  reads a malformed `recipient` without raising. `read_allowed` and
  `write_allowed` re-check schema, expiry, run binding and recipient on EVERY
  call — a long invocation can no longer keep authorizing operations after
  its grant expired — using the run id `invoke` recorded, so author code is
  unchanged. `write_allowed` takes the target's freshly fetched hash as a
  REQUIRED argument and refuses a granted change whose `content_sha256` or
  `target_sha256` is missing or null.
- **B6 — a torn journal tail can no longer swallow the next outcome.**
  `Journal.read()` ignores an unterminated LAST line only (a crash
  mid-write), and `append()` REPAIRS it first: the fragment is cut and a
  `{"phase": "torn", "discarded": ...}` entry records that it was, so the
  next entry starts on a line of its own. A malformed COMPLETE line anywhere
  is `JournalCorrupt` — never skipped — and `invoke` reads the journal before
  calling `run` and returns a `journal-corrupt` envelope.
- **S5 — two hashes, not one.** `content_sha256` (what the human approved)
  and `target_sha256` (the target state approved against) are separate
  fields on a granted change; staleness is `target_sha256` against a fresh
  fetch, and `content_sha256` optionally guards the bundle from swapping a
  change's content under an approved id.
- **S7 — malformed input stays inside the envelope boundary.** A bundle the
  declared schema refuses never reaches the package's `check_input`, so an
  author's callback is only ever handed the shape it declared; the starter
  checker also skips a non-object item.
- **N1 — the starter's write sketch teaches complete recovery.** The four
  required steps are numbered in `task_logic.py`: skip a decided change,
  reconcile an `applying` one by ASKING THE TARGET, check the grant against a
  freshly fetched target hash, then journal-and-apply.

## Op machinery (0.5.0, 2026-09-17): `templates/op/src/`

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

### 0.5.7 — every pending-sheet cell is literal text (2026-09-19)

Contract §11b, from Codex review 9 (blocker 3). `render_pending` interpolated
proposal fields into a Markdown table unescaped, and only the summary had its
whitespace collapsed. A valid `content_sha256` says nothing about how a
proposal READS: a target of `[owner/repo#1](https://example.invalid)` hid the
real target behind link text, a pipe shifted the columns, a newline in
`change_id` invented a row, backticks reformatted a cell, and `<!--` could
swallow everything after it in an HTML-capable renderer. The sheet is what the
human decides from, so a misleading render is a mis-approval.

`op_runner.cell(value)` is the one renderer for every cell (`change_id`, kind,
target, summary — and the step, run id and timestamp in the header): collapse
every run of whitespace, newlines included, to one space; HTML-escape `&`,
`<`, `>`; then backslash-escape the Markdown metacharacters
`\ ` `` ` `` `* _ [ ] ( ) # ! ~ | < >`. The order matters — escaping `<` to
`\<` and then HTML-escaping would leave a visible backslash. Presentation
only: the pending JSON, the hashes, the decision vocabulary and the change
shape are untouched, so a decision file made against 0.5.6 still applies.
Tests: `tests/test_op_authority.py::PendingSheetLiteralTextTests` (8), one per
hostile string the review listed, splitting the rendered row on UNESCAPED
pipes the way a table parser does.

### 0.5.6 — the pending sheet names what it shows (2026-09-19)

Contract §11 item 5. The live nexus sweep handed a human a decision sheet
whose `kind` and `target` columns were blank on all 33 rows: `render_pending`
read `kind`/`target`, and the change shape the proposing Cogs emit says
`change_type` and `target_item_ids`. Two readers, `change_kind(change)` and
`change_target(change)`, now read what is actually there — `kind`/`target`
first (nothing is demoted), then `change_type` and the FIRST of
`target_item_ids`. A change naming neither renders an empty cell as before,
and an empty `target_item_ids` is not an IndexError. Presentation only: the
pending JSON, the hashes, the decision vocabulary and the change shape itself
are untouched, so a decision file made against 0.5.5 still applies.
Tests: `tests/test_op_authority.py::PendingSheetTests` (4).

### 0.5.5 — a failed run resumes at the step that stopped it (review 5)

The starter's write sketch (`templates/code-cog/src/task_logic.py`, which is
AUTHOR-owned and so not hash-enforced — this changes what a NEW Cog starts
from, nothing already created) was updated in the same round: reconciling by
reads reports no write in `authority_use`, and unfinished work is an
error-severity `write-back-unresolved` problem so the Gate fails and a
resume finishes it.

Contract §9e, from
`planning/current/phase3-codex-review-5-code-cogs-verification.md` (new
finding 1). Regression test: `tests/test_op_process.py::UnresolvedResumeTests
::test_an_unresolved_write_stops_the_run_and_resume_re_runs_that_step` — a
real Op, as processes, whose write Cog leaves its change uncertain once.

- **New 1 — `uncertain` was resumable in the Cog but not in the Op.** A Cog
  that reports unfinished work returns an error-severity problem, so its
  Gate fails and an `on_fail: stop` step ends the run. Until now that was
  the end of the run: the only way to reconcile was to invoke the Cog by
  hand. Now the Track records `failed_step` — the step a stopping verdict
  ended the run at — and `op run --resume RUN_DIR` (no decision needed)
  takes that step out of the `done` set and runs it again, followed by the
  steps after it, which never ran. `passed` and `passed-with-problems`
  steps are still never re-run, so a resume costs only the step that asked
  for it.
- **A `denied` step that stopped the run is a resume point too.** It is the
  one stopping status that was already IN the `done` set, so without this a
  resume would have carried on past it as though it had been skipped. A
  denial that still stands simply stops the run again.
- **Older Tracks still resume.** A Track written before 0.5.5 carries no
  `failed_step`; the stopping step is then read off the records (the last
  `failed` or `denied` one), so a run started under 0.5.4 resumes under
  0.5.5 without special handling.

### 0.5.4 — the decision says who, when, and what was edited (review 4)

Contract §9d, from `planning/current/phase3-codex-review-4-code-cogs-and-op.md`.
Regression tests: `tests/test_op_authority.py::
test_the_exposed_decision_carries_decided_by_and_decided_at` and
`::test_the_exposed_approved_list_carries_the_edited_change_object`.

- **S3 — `steps.<id>.decision` gains `decided_by` and `decided_at`.** The
  value exposed to downstream mappings and recorded in the Track carried
  `approved`, `rejected`, `edited` and `history` — enough to write back, not
  enough to RECORD a decision. A step that writes the run record had no way
  to say who decided or when without reading the decision file behind the
  runner's back. Both fields are stripped of surrounding whitespace and are
  already refused when they name nobody (0.5.2).
- **S3 — `approved` carries the edited objects, confirmed.** An `edit`
  verdict already appended the normalized EDITED change (never the original
  proposal) to `approved`; review 4 read the record-run join, not the
  runner. The test above pins it so the guarantee cannot quietly regress.

### 0.5.3 — the Codex confirmation fixes (review 3)

One bullet per finding of
`planning/current/phase3-codex-review-3-cog-smith-confirmation.md` that lives
in this lineage, resolved as contract §9c decides. Each fix has a regression
test that failed before it (`tests/test_op_authority.py`,
`tests/test_op_process.py`).

- **New 1 — hashes are never repaired.** At the PAUSE, every proposed
  change's `content_sha256` must equal the canonical hash of the object it
  arrived on and its `target_sha256` must be 64 hex characters, else the
  pause is refused by name: a proposal carrying
  `"content_sha256": "placeholder"` used to pass the pause and be laundered
  into a valid digest by `normalized_change` at approval, and then pass
  issuance. Approval and rejection now PRESERVE the supplied digest; only an
  EDIT is re-hashed. Every digest check is `fullmatch`, so 64 hex characters
  followed by a newline — which `re.match` with `$` accepted — is not a
  content hash.
- **New 2 — the run directory's own entry is fsynced.** `runs/<run_id>` is
  created through `op_track.ensure_dir` rather than `mkdir(parents=True)`,
  so its entry is persisted in `runs/` the way every control directory
  beneath it already was. Syncing the Track, the grants and the journal
  inside a directory whose own entry never reached the disk is not
  durability.
- **New 3 — the lock file is a control file.** `run.lock` is contained in
  the run directory (no link out, no alias inside) and opened `O_NOFOLLOW`,
  so a `run.lock` symlinked to `track.json` is refused by name instead of
  being followed and truncated — which destroyed the Track before the
  resume read it. The lock metadata is written only AFTER the lock is held,
  and a failure while writing it releases the descriptor before re-raising,
  so an embedding process that catches the exception is not left holding a
  lock it does not know about.
- **New 4 — lock retention is demonstrated, not assumed.** The process
  test's fake `pixi` inherits descriptors the way a real launcher does
  (`close_fds=False`); the test kills the runner AND the launcher while the
  Cog is still inside its write, and what is left holding the run is the Cog
  with the descriptor it inherited: a second resume is refused by name while
  it lives, and the run is lockable once it exits. A companion test runs a
  created Cog through the REAL `pixi` on PATH with a locked descriptor
  passed in and records in the test output whether it arrived (a skip with a
  message when pixi is absent). Measured 2026-09-17 with pixi 0.69.0: it
  arrived.

### 0.5.2 — the Codex verification fixes (review 2)

One bullet per finding of
`planning/current/phase3-codex-review-2-cog-smith-verification.md` that lives
in this lineage — the Partial verdicts and the seven New findings — resolved
as contract §9b decides. Each behavioural fix has a regression test that
failed before it (`tests/test_op_authority.py`, `tests/test_op_process.py`).

- **B3 / New 1 — the run lock is an OS advisory lock, not a pid file.**
  `run.lock` is opened once and locked with `fcntl.flock(LOCK_EX |
  LOCK_NB)`; the descriptor is held for the process lifetime and passed to
  every Cog subprocess (`pass_fds`), so a runner killed mid-invocation keeps
  the run locked until its Cog is gone too. A failed `flock` refuses the run
  or resume by name (exit 2). No pid parsing, no takeover, no unlink on
  release — the file's JSON is informational only, so an EMPTY lock file, a
  lock file that is not JSON, and one naming a pid `os.kill` could never
  take are all just locked-or-not. This replaces §9's `O_EXCL` + dead-pid
  rule, and with it the `took_over_lock` entry in `resumes`.
- **B4 / New 2 — issuance validates the hashes it carries.** At issuance the
  runner RECOMPUTES each approved change's `content_sha256` from the change
  object and denies a mismatch; both hashes must be hex-64. A proposal that
  arrives with a null or missing `content_sha256` refuses the PAUSE by name
  — the runner never repairs one into a valid-looking approved change.
- **S1 / New 3 — containment is by resolved path, and no link at all.**
  `$run_dir` operands are normalised (`.`/`..` collapsed) before the
  reserved-name check, so a dynamic `outputs/../grants` names `grants`;
  `$run_dir` also refuses a symlinked component. `op_track.contained`
  realpaths the destination and refuses ANY symlinked component at or below
  the run directory — an alias inside the run (`outputs` -> `grants`) used
  to resolve inside and pass — and refuses a path that reaches the run only
  by following a link.
- **S2 — decision metadata says something.** `decided_by` may not be
  whitespace, and `decided_at` must parse as a timestamp.
- **S7 / New 4 — the lock is never parsed for meaning.** `os.kill` is gone,
  so a lock file carrying an enormous integer pid raises no `OverflowError`;
  a lock file that is not JSON is read as nothing.
- **S8 / New 7 — one process test keeps production `invoke_cog`.** Only the
  EXECUTABLE boundary is substituted (a fake `pixi` on PATH that runs the
  created Cog's declared task), so command construction, `--grant`,
  `--run-id`, `--journal` and the `--request` -> `--bundle` fallback are
  exercised end to end; plus a genuinely overlapping resume — the first
  resume held inside the write Cog while a second arrives and is refused.
- **S9 / New 5 — directory entries are durable too.** Every directory the
  runner creates for control files is created one level at a time and
  fsynced in its PARENT (`op_track.ensure_dir`), and a journal is created
  with `touch_durable`, which fsyncs its directory entry. Containment is now
  checked BEFORE anything is created, so a refused destination leaves no
  directories behind.
- **New 6 — a human-gated step keeps `passed-with-problems`.** The envelope
  Gate's verdict is recorded at pause time (`gate.envelope_status`) and is
  what the step's status becomes when the decision is applied: approving the
  proposals does not erase the problems the Cog reported making them.

### 0.5.1 — the Codex review-1 fixes

One bullet per finding of `planning/current/phase3-codex-review-1-cog-smith.md`
that lives in this lineage, resolved as contract §9 decides. Each behavioural
fix has a regression test that failed before it (`tests/test_op_authority.py`,
`tests/test_op_process.py`, `tests/test_op_runner.py`).

- **B1 — nothing external happens before the Track says so.** Durability
  order on a resume: accept the decision → record the decision AND the resume
  and save → issue the grant, create the journal, record the step `running`
  and save → invoke. Every step (not only a granted one) is recorded
  `running` with its grant and journal before its Cog is launched, and the
  record is REPLACED, not appended to, when the result arrives.
- **B2 — `retry-once` is refused at load on an effectful step.** On a step
  that carries `authority:`, and on a step whose Cog's manifest declares a
  non-empty `reaches`. An effectful step recovers by resume plus journal
  reconciliation, never by re-invocation (phase 2 §0); the message says so.
- **B3 — one run, one process.** `runs/<run_id>/run.lock` is created
  `O_CREAT|O_EXCL` at the start of a run and of every resume, BEFORE the
  Track is read, and removed on exit. A live pid refuses the resume by name
  (exit 2); a dead pid's lock is taken over and the takeover recorded in
  `resumes`.
- **B5 — the pending payload is authenticated.** `apply_decision` re-hashes
  `pending["payload"]` and checks the decision against THAT, never against
  the hash string the pending file carries; and a resume compares the
  re-hashed payload with the `payload_sha256` the Track recorded at pause
  time, so editing the proposals on disk refuses the decision by name.
- **S1 — the run's control entries are reserved.** `grants`, `pending`,
  `decisions`, `journal`, `run.lock` and `track.json` (and anything under
  them) are refused as `$run_dir` subpaths at load and at evaluation, and
  every control-file write verifies its resolved destination is inside the
  run directory and not reached through a symlink. Application-level
  integrity, not host sandboxing.
- **S2 — decision validation is complete.** Duplicate or non-string change
  ids refuse the PAUSE; `decided_by` and `decided_at` are required; an edited
  change must carry the same fields and types as the proposal, may not
  retarget it or restate its `target_sha256`; every approved and rejected
  change is recorded with a RECOMPUTED `content_sha256`, and the decision
  value carries a `history` list (one entry per proposal: verdict, both
  hashes, reason).
- **S3 — a resume re-validates.** Declaration and admission checks run on
  resume exactly as on a fresh run (a Cog manifest changed while paused is
  caught), and resuming a `planned` dry-run Track is refused by name.
- **S4 — a resume restores the whole mapping context.** The original request
  DIRECTORY is recorded in the Track (`request_dir`), so relative `$path`
  operands resolve as they first did; `steps.X.envelope` is restored from the
  envelopes the Track points at; and an empty `foreach` aggregate restores as
  `[]` instead of reading the step's envelope directory as a file.
- **S5 — two hashes on a change.** `content_sha256` is the change object's
  own hash (recomputed on an edit, checked at issuance); `target_sha256` is
  the target item's content hash as the Op read it (the staleness
  precondition the write Cog checks). A grant carries both per change, and a
  change missing either, or carrying a null one, is DENIED at issuance.
- **S6 — grant identity and provenance are preserved.** Every issuance gets
  its own number, id and file (`grants/<step>/<n>.json`); an earlier grant
  file is never overwritten. A recipient version absent from the spec is read
  from the Cog's manifest. Two write requirements reading different human
  gates are refused at load (one gate per writing step).
- **S7 — malformed values are named, not raised.** A list-valued change id or
  repository, a non-list `changes`, and a non-string repository are `Denied`
  reasons; a write requirement's expression is checked at load.
- **S8 — the tests drive the runner.** `tests/test_op_process.py` runs
  `op run` and `op run --resume` as separate PROCESSES against a real created
  code Cog whose reconcile step consults a fake GitHub that keeps state, and
  covers both crash windows (before and after the external effect landed),
  the lock refusal, and the stale-lock takeover.
- **S9 — the durability guarantee is the one implemented.** Every atomic
  write fsyncs the containing directory after the rename, and the human's
  `pending/<step>.md` is written the same way as the JSON.
- **N2 — "only the approved list can produce a grant" is now the load rule.**
  A write requirement's `changes` must be exactly
  `{$from: steps.<id>.decision.approved}`; any other sub-path of the decision
  is refused by name at load rather than denied at issuance.

### 0.5.0 — authority, grants, the human Gate, resume

Phase 3 (`planning/current/phase3-contract.md` §2, §3, §5). The runner
ISSUES and RECORDS; the code Cog checks its own grant before it reaches
outside the run. Nothing added here is an enforced restricted environment
(phase 2 contract §0), and the docs say so in every place they could be
misread.

- **Spec vocabulary.** `authority.requires` on a step, `authority.ttl_minutes`
  at the top level (default 60), `gate.policy: human` accepted, and a new
  `$from` root `steps.<id>.decision` readable only for a human-gated step.
  `grants` is refused BY NAME as a mapping root: a grant is trusted
  invocation context and no expression can read or build one.
- **`human:` steps are refused permanently.** The message now points at the
  gate policy instead of "phase 3": a human Gate is a policy on the step that
  produces what the human decides about, never a step of its own.
- **Load-time refusals** (exit 2, never mid-run): authority on a `foreach`
  step; a write requirement that does not read the decision of a human-gated
  step it depends on; and, read from the target Cog's own manifest, authority
  on a non-code Cog, a requirement outside that Cog's declared `reaches`, and
  a step that names a reaching Cog while requiring nothing. With no
  `--authority` at all, any step that requires authority is refused before
  the run directory is created.
- **Issuance.** Immediately before the invocation, and only for a step whose
  dependencies passed. A read must be a subset of the admitted repositories;
  a write must be covered by the named human decision — whose record must
  still hash to what the Track recorded — and the grant carries EXACTLY the
  approved list. Anything else is a `denied` step that is never invoked, with
  `on_fail` applying as for a failure. Grants are written to
  `runs/<run_id>/grants/<step>.json` and passed as `--grant`, beside
  `--run-id` and `--journal`, never inside the request document.
- **The human Gate.** A passing envelope pauses the run: `pending/<step>.json`
  (`openteams/op-pending-decision [0.1]`) plus a rendered `.md`, Track status
  `paused`, step `awaiting-decision`, exit **3**. A decision
  (`openteams/op-decision [0.1]`) needs one verdict per proposed change and a
  matching `payload_sha256`; an edited change is re-hashed from its edited
  content and THAT hash is granted.
- **Resume.** `--resume RUN_DIR [--decision FILE]` reloads the Track (op.yaml
  and the admission must still hash the same), applies the decision, exposes
  `steps.<id>.decision`, and continues. Passed steps are never re-run; a
  `running` step is; every resume is appended to `resumes`.
- **Track.** Top level gains `authority`, `grants`, `resumes` and the
  `paused` status; a step record gains `grant`, `journal`, `authority_use`
  and `decision`, and the statuses `denied`, `running`,
  `awaiting-decision`.

Rolled out to `op-video-transcription` by re-copying.

### 0.4.6 — `tool:` is refused permanently, pointing at `kind: code`

A vocabulary change, not a behavior change. The runner subset refused `tool:`
steps with "phase 3 adds it"; phase 3 will not add it. Deterministic work in
an Op is a model-free Cog of `kind: code` invoked as an ordinary `cog:` step
(decided 2026-09-17, recorded in the main triage plan's "Code Cogs" section),
so the op-manifest keeps one step kind. `REFUSED_STEP["tool"]` is now `None`
and the load-time message says there is no `tool:` step kind and names
`kind: code`. Rolled out to `op-video-transcription` and `op-project-triage`
by re-copying.

### 0.4.5 — the final-review fixes

One bullet per "New findings" item of
`planning/current/phase2-codex-review-4-cog-smith-final.md`, which reviewed
0.4.4. Each behavioral fix has a regression test that failed before it.

- **1 — a loop variable can no longer overwrite the run context.**
  `foreach.as` is refused at LOAD when it collides with a path root
  (`inputs`, `steps`, `run`, `request`). `as: run` used to replace the run
  context with the element, so an element carrying `dir` redirected every
  `$run_dir` in that step outside the actual run directory; containment is
  now enforced by refusing the collision, not at write time.
- **2 — a boolean JSON Schema no longer crashes package creation.** A JSON
  Schema may be `true` (accept anything); `smith_op.example_request` reads
  `type` only from DICTIONARY schemas and gives a boolean schema the generic
  `REPLACE_ME` placeholder, where a required input with `schema: true` used
  to raise `AttributeError` out of `op new`.
- **3 — BUILDING_OPS.md states the skipped-step rule correctly.** A skipped
  or blocked SINGLE step has a null payload downstream; a skipped FOREACH
  step keeps its aggregate list, null only for the elements that failed.
- **4 — the dead code is gone.** `gate_envelope`'s malformed-problems branch
  (unreachable after `envelope_problems`), the write-only `statuses`
  dictionary in `run`, and the discarded first `loop_vars` accumulator in
  `op_spec` are removed, and the unused `OpSpec.example_request` is deleted:
  `smith_op.example_request` is the one starter-request generator.

### 0.4.4 — the verification-round fixes

One bullet per "New and residual findings" item of
`planning/current/phase2-codex-review-3-cog-smith-verification.md`, which
verified 0.4.3 and completed the rows left PARTIAL (1, 9, 10, 13, 14). Each
has a regression test that failed before the fix.

- **1 — the request flag is negotiated once, safely.** `_rejected_flag` now
  requires all three of argparse's exit code (2), the diagnostic
  `unrecognized arguments: <flag>` on **stderr**, and NO envelope anywhere on
  stdout. A result — including an `ok: false` envelope whose detail quotes
  that diagnostic — is never re-invoked, so an effectful Cog cannot run twice
  (contract §0). When the fallback also fails, both attempts are kept as
  evidence. (The Cog's declared interface would be the better source, but a
  manifest does not say which request flag its CLI takes.)
- **2 — absence is not null.** An omitted OPTIONAL input with no `default`
  key is absent from the built inputs: `$from` on it takes its `$default` or
  is the named "not available in this run" error, and it is NOT validated as
  an explicit null. Supplied values and declared defaults (including
  `default: null`) are still validated. `smith_op.example_request` omits such
  inputs from `examples/request.json` instead of writing null, which used to
  make a starter request invalid against its own declared schema.
- **3 — the remaining malformed spec types are named problems.** `foreach.as`
  must be an identifier-shaped string (a list used to raise `TypeError` when
  it was added to the loop-variable set) and `cog.id/version/source/task`
  must be strings (a list-valued `source` used to load and then fail at path
  construction). CLI-level tests assert exit 2 and no traceback for both.
- **4 — malformed envelopes cannot pass the Gate.** The version discriminator
  is `type(x) is int and x == 1` (Python equates `True` with `1`), `ok` must
  be a bool, and `problems` is REQUIRED to be a list of objects — missing or
  null is malformed. `gate_envelope` shares `envelope_problems`, so a
  malformed result fails with its reasons listed instead of being decided.
- **5 — the operator boundary is the contract's.** An object is an operator
  only when its key set EXACTLY matches a known operator's; any other object
  carrying a `$`-prefixed key that is not a known operator NAME is refused
  (siblings do not launder `$join`); every other object is walked. So
  `{"$from": ..., "$stem": ...}` is ordinary data, and the 0.4.3 test that
  codified the opposite is replaced.
- **6 — synthetic failures always carry their process evidence.** ONE place:
  `error.evidence` = `{command, returncode, stdout_tail, stderr_tail}` (tails
  bounded at 2000 characters), plus `previous_attempts` when the seam tried
  the other request flag first. A silent crash and a malformed envelope with
  stderr now keep their exit code and streams; a launch failure records a
  null `returncode`. Documented in BUILDING_OPS §4.
- **7 — the declaration preflight runs before anything is created.** A
  refused declaration exits 2 leaving NO run directory and no Track stuck at
  `status: running`. Dry runs are unaffected and still need no Cogs present.

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
