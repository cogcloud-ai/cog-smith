# Building Ops

**Audience:** Op builders, reviewers, and coding agents
**Last verified:** 2026-09-18 against cog-smith Op machinery 0.5.4
**Status:** The Op spec `openteams/op-manifest [0.1]` is the laptop side's
proposal, implemented from `planning/current/phase2-op-runner-contract.md`.
It is a runner SUBSET on purpose: durable state is refused by name, with the
phase that adds it. There is no `tool:` step kind and never will be:
deterministic work in an Op is a model-free Cog of `kind: code`, invoked as
an ordinary `cog:` step (decided 2026-09-17). There is no `human:` step kind
either: a human Gate is a POLICY on the step that produces what the human
decides about.

Read [Building and Improving Cogs](BUILDING_COGS.md) first. This guide is the
layer above it.

If you remember only one thing, remember this:

> An Op is a composition, not a program. It declares which Cogs run, in what
> order, on what inputs, and what its Gates accept — and it leaves a Track
> that says what actually happened.

## 1. The shape of an Op

An Op package is:

    op-my-workflow/
    ├── op.yaml               # the executable spec — the only thing you author
    ├── pixi.toml             # tasks: op, test
    ├── src/                  # op_runner.py, op_spec.py, op_track.py — SHARED
    ├── tests/test_op.py      # generated; extend it
    ├── examples/request.json # a runnable request
    ├── README.md
    └── runs/                 # gitignored: one directory per run, with the Track

There is **no per-Op Python**. `src/` is cog-smith machinery, enforced by
hash by `smith op check`, exactly as Cog machinery is. If your Op needs code,
the answer is a Cog step — a worker with its own identity, contract, and
tests — never a script in the Op.

Ops use Cogs; they never import them. Every step runs
`pixi run --manifest-path <cog>/pixi.toml <task> -- --request <file>`, where
`<task>` is a **usage** interface that Cog declares for itself. If the Cog's
CLI refuses `--request` by name — cog-smith's context-cog machinery takes
`--bundle` — the seam retries with `--bundle`. It retries ONLY on a real
argument-parser rejection that did no work: exit code 2, the diagnostic
`unrecognized arguments: --request` on **stderr**, and no envelope anywhere on
stdout. Anything else, including a failed envelope whose text happens to quote
that diagnostic, is a RESULT, and an effectful Cog is never invoked a second
time. Two flags for one thing is an accident of two machinery lineages, not a
feature to build on.

## 2. The path

**Proposal → resolved spec → package → run → Track.**

1. **Proposal.** Prose: the work, the steps, who decides what. Keep it; it is
   the design record.
2. **Resolved spec.** The proposal with every step bound to a real Cog, a
   real task, and real mapping expressions. This is a JSON or YAML file.
3. **Package.**

   ```bash
   python src/cogsmith_cli.py op new --from-spec my-op.yaml --dir ../op-my-workflow
   ```

   The spec is validated first, so a refused construct never reaches disk.
   `--dir` may point at a directory that already exists (the folder holding
   the proposal, say) as long as none of the files being written are already
   there; a collision refuses by filename.
4. **Run.**

   ```bash
   cd ../op-my-workflow && pixi install
   pixi run op -- --request examples/request.json --dry-run   # the plan
   pixi run op -- --request examples/request.json             # the real run
   ```
5. **Track.** `runs/<run id>/track.json`, rewritten after every step: the
   input request, every step request, every Cog envelope unchanged, the
   contract-check problems the Cogs reported, the Gate decision per step,
   the model bindings, and the outputs. An interrupted run still leaves a
   readable Track.

Validate the package the same way you validate a Cog:

```bash
python src/cogsmith_cli.py op check ../op-my-workflow --tests [--envelope]
```

`op check` also reads each step's Cog and confirms that the task you named is
one that Cog declares for the **usage** audience. If the Cog simply is not on
this machine, that is a warning, not an error. **The runner makes the same
check before it invokes anything**, so a spec naming a lifecycle task or a
task the Cog does not declare exits 2 having run no step at all. An invalid
spec exits 2 from `op check` too — the same code `op new` and the runner give
it.

## 3. A complete spec

A two-step linear Op: transcribe a recording, then attribute its speakers.

```yaml
schema: openteams/op-manifest [0.1]
id: openteams/op-video-transcription
version: "0.1.0"
name: Video transcription with speaker attribution
description: Produces a timestamped, speaker-labelled transcript from a recording.

inputs:
  - name: media_path
    description: The recording, relative to the request file.
    required: true
  - name: speaker_count
    description: How many speakers to expect, when you know.
    required: false
    default: null
  - name: speaker_mapping
    description: Diarization label -> person name.
    required: false
    default: {}
    schema: {type: object}

steps:
  - id: transcribe
    name: Transcribe the recording
    cog:
      id: openteams/cog-media-transcriber
      version: "0.1.0"
      source: ../cog-media-transcriber
      task: transcribe
    input:
      media_path: {$path: {$from: inputs.media_path}}
      output_dir: {$run_dir: transcription}
      artifact_stem: {$stem: {$from: inputs.media_path}}
    expected_outcome: A timestamped transcript bundle and normalized local audio.
    gate:
      policy: envelope-ok-no-error-problems
      guards: []
    on_fail: stop

  - id: attribute-speakers
    name: Attribute transcript segments to speakers
    depends_on: [transcribe]
    cog:
      id: openteams/cog-speaker-attribution
      version: "0.1.0"
      source: ../cog-speaker-attribution
      task: attribute
    input:
      audio_path: {$from: steps.transcribe.payload.audio_path}
      transcript_json: {$from: steps.transcribe.payload.transcript_json}
      output_dir: {$run_dir: attribution}
      num_speakers: {$from: inputs.speaker_count, $default: null}
      speaker_mapping: {$from: inputs.speaker_mapping, $default: {}}
    expected_outcome: A transcript with explicit primary, mixed, or unassigned labels.
    gate:
      policy: envelope-ok-no-error-problems
      guards: []
    on_fail: stop

outputs:
  transcript: {$from: steps.attribute-speakers.payload.speaker_labelled_markdown}

track:
  records: [input_request, step_requests, cog_envelopes,
            contract_check_problems, gate_decisions, model_bindings,
            output_artifacts]
```

### Mapping expressions

The vocabulary is CLOSED. An object is an operator only when its key set is
EXACTLY one of these; every other object is walked recursively, and scalars
are literals. So `{$from: "x", label: "y"}` and `{$from: "x", $stem: "y"}` are
both ordinary data — but a `$`-prefixed key that names no operator at all
(`{$join: [...]}`, with or without siblings) is refused by name wherever it
appears.

| Operator | Meaning |
|---|---|
| `{$from: <path>}` | Read `inputs.<name>`, `steps.<id>.payload`, `steps.<id>.envelope`, `run.dir`, `run.id`, `request.dir`, or the `foreach` loop variable. Dotted keys index objects; integers index arrays. |
| `{$from: ..., $default: <expr>}` | The value when present and not null, else the default (itself an expression). |
| `{$path: <expr>}` | A filesystem path resolved against the request file's directory, absolute. |
| `{$run_dir: <subpath>}` | A directory inside this run, absolute and created. The subpath is RELATIVE and never leaves the run: an absolute value, a `..` escape, or a symlink out of the run is refused before anything is created — including when the value arrived as mapped data. |
| `{$stem: <expr>}` | `Path(value).stem`. |
| `{$literal: <any>}` | The value verbatim — the escape hatch for data that looks like an operator. |

A step may read `steps.<id>.payload` only when `<id>` is in its transitive
`depends_on`. Reading without depending on it is refused at load, because the
order would otherwise be a coincidence.

### Steps that repeat

```yaml
  - id: classify-items
    cog: {id: openteams/cog-item-classifier, version: "0.1.0",
          source: ../cog-item-classifier, task: classify}
    foreach:
      items: {$from: inputs.github_items}
      as: item
    input:
      title: {$from: item.title}
      rubric: {$from: inputs.priority_rubric}
    on_fail: retry-once
```

Elements run in order, each with its own request, envelope, and Gate
decision. The step's payload is the LIST of element payloads (null where an
element failed); the step's Gate fails if any element failed, and is
`pass-with-problems` if any element carried problems.

### When a step fails

`on_fail: stop` (the default) ends the run `failed` and names the step; every
step the run never reached is recorded with status `not-reached`, so a Track
always lists every step of the spec.
`skip` records the step as `skipped` and every step that transitively depends
on it as `blocked` — recorded, never run — and the run ends
`completed-with-problems`. `retry-once` re-invokes exactly once, and only
when the Cog reported `ok: false` (a transport or model failure); an
error-severity problem in an `ok` envelope is a judgement, not a glitch, and
is never retried. Both envelopes stay in the Track.

**`retry-once` is refused at load on an EFFECTFUL step** — one that carries
`authority:`, or whose Cog declares a non-empty `reaches`. A Cog that may
already have written outside the run is never re-invoked on a failure: it
recovers by `--resume` plus journal reconciliation, which is the only path
that can tell "it did not happen" from "it happened and I did not hear
back".

### Inputs, defaults, and null

An input is supplied when the request carries its KEY: an explicit `null` is a
value, and a declared default never replaces it. A declared `default:` (even
`default: null`) covers an omitted input; a required input with neither is
missing.

**Absence is not `null`.** An OPTIONAL input the request omits and whose
declaration carries no `default` key is simply not among the run's inputs: a
`{$from: inputs.note}` on it takes its `$default`, or is the named "not
available in this run" error. It is not validated, so
`{name: note, required: false, schema: {type: string}}` does not force every
request to carry `note`. A declared `schema:` is checked when the spec loads
and applied to every SUPPLIED value and every DECLARED default — including
`null`, so a nullable input with `default: null` declares
`type: [string, "null"]`. If a step must always send the key, declare
`default: null` and let the schema say so.

`op new` fills `examples/request.json` from the declared defaults, and writes a
type-shaped placeholder (`"REPLACE_ME"`, `{}`, `[]`, `0`, `false`) only where a
REQUIRED input declares no default; an optional input with no declared default
is OMITTED from the starter request. The
generated test skips its dry-run assertions only while that starter request
does not validate — a request that validates but maps badly FAILS the suite.

## 4. Gates, Guards, and the Track

The only policy in this subset is `envelope-ok-no-error-problems`: a step
fails when the Cog reports `ok: false` or a contract-check problem with
severity `error`; warnings produce `pass-with-problems` and the run
continues. A Cog that could not be launched, exited nonzero, or printed
something that is not a well-formed envelope v1 is an *invocation failure*:
the Op layer synthesises an `ok: false` envelope, keeps what the Cog did emit
as evidence in `raw`, and the Gate decides about that like any other result.
A well-formed envelope v1 is required to have `envelope: 1` (the integer, not
`true`), a boolean `ok`, and `problems` as a list of objects; anything else is
malformed output, not a result.

Every synthetic failure carries its process evidence in ONE place —
`error.evidence` — as `{command, returncode, stdout_tail, stderr_tail}` (each
tail the last 2000 characters), plus `previous_attempts` with the same fields
when the seam had tried the other request flag first. `returncode` is `null`
only when the command could not be launched at all.
A step that is `skipped` or `blocked` still has a result in the Track, and a
mapping over it always resolves. What it resolves TO depends on the step's
shape: a skipped or blocked SINGLE step has a **null payload** downstream
(its result failed the Gate, so it is not evidence), while a skipped FOREACH
step keeps its **aggregate list** — one entry per element, null only where
that element failed — because the elements that passed are still evidence. A Cog reports; **the Gate decides, never the Cog**. Guards —
independent, system-side verifiers of the system's requirements — are not in
this subset, and the Track records `guards: []` rather than pretending.

## 5. Authority: what a step may reach outside the run

A Cog that reaches outside the run (a repository read, a label written)
declares that in its manifest as `reaches`, and it runs only under a
**grant**. Two sources of authority, and nothing else:

- **Admission** — the owner's authority for the whole run, passed as
  `op run --authority FILE` and validated against
  `openteams/op-authority [0.1]`. It names repositories, never change ids.
  With no `--authority`, a step that requires authority is refused before
  anything is created: *"step write-github requires github write; the run
  was admitted with none."*
- **A human Gate decision** — the only thing that authorizes a WRITE, and it
  authorizes exactly the changes the human approved.

A step declares what it requires; it never grants itself anything:

```yaml
- id: read-github
  cog: {id: openteams/cog-read-github, source: ../cog-read-github, task: run}
  authority:
    requires:
      - {resource: github, action: read,
         repositories: {$from: inputs.repo_config.repositories}}

- id: compose-proposals
  depends_on: [read-github]
  cog: {id: openteams/cog-compose-proposals,
        source: ../cog-compose-proposals, task: run}
  gate: {policy: human}

- id: write-github
  depends_on: [compose-proposals]
  cog: {id: openteams/cog-write-github, source: ../cog-write-github, task: run}
  authority:
    requires:
      - {resource: github, action: write,
         changes: {$from: steps.compose-proposals.decision.approved}}
```

The runner issues the grant immediately before the invocation and writes it
to `runs/<run_id>/grants/<step>/<n>.json` — every issuance gets its own
number, id and file, so a grant a Track entry points at is never overwritten
by a reissue after an interruption — then invokes the Cog with `--grant`,
`--run-id` and `--journal` **beside** the request. A read is issued only if
its repositories are a subset of the admitted ones; a write only if every
requested change was approved by the named human decision, whose record must
still hash to what the Track recorded. The grant carries EXACTLY the approved
list — asking for more than was approved is a **denial, not a trim**. A
denied step is recorded `denied`, is never invoked, and its `on_fail` applies
as for a failure.

Each granted change carries TWO hashes, both 64 hex characters and neither
null: `content_sha256` is the hash of the change object the human approved —
canonical JSON over everything except the two hash fields — and
`target_sha256` is the content hash of the target item as the Op READ it,
the staleness precondition the write Cog checks against a fresh fetch before
applying. At issuance the runner RECOMPUTES `content_sha256` from the change
object it is about to authorize and denies a mismatch; a change missing
either hash, carrying a null one, or carrying something that is not a
sha256, is denied there too. A write Cog computes the content hash from the
change it is about to apply — never forwards the digest its bundle states —
so content cannot be swapped under an approved id.

`authority.ttl_minutes` at the top level of `op.yaml` (default 60) is how
long an issued grant stays valid. A grant carries no credentials, cannot be
read or built by any mapping expression (`grants` is not a `$from` root), and
is checked by the CODE COG that receives it. The local host runs trusted code
with the owner's ambient credentials: the Op layer issues and records; it is
not, and must never be described as, an enforced restricted environment.

The Track keeps the scope and provenance: top-level `authority`
(`{path, sha256}`), `grants` (one entry per grant: id, step, path, the
operations with their counts, and who issued it), and `resumes`; per step
`grant`, `journal`, and the `authority_use` list the Cog reported.

## 6. The human Gate, pause, and resume

`gate: {policy: human}` is a policy on a step. The step runs its Cog first
and its envelope goes through `envelope-ok-no-error-problems` as usual: only
a PASSING envelope reaches the human. Its payload must carry a `changes`
list of objects with a `change_id` — that is what the human decides about.

The runner then writes `runs/<run_id>/pending/<step>.json`
(`openteams/op-pending-decision [0.1]`, with the payload and its
`payload_sha256`) and a readable `pending/<step>.md`, one line per change;
the Track status becomes `paused`, the step is `awaiting-decision`, and the
process **exits 3** printing
`{ok: false, status: "paused", run_dir, pending}`.

A proposed change must state its own `content_sha256`, and state it
correctly: at the pause the runner recomputes the hash of the change object
and refuses BY NAME when the stated digest is null, missing, not 64 hex
characters, or simply not this change's — and refuses the same way when
`target_sha256` is not 64 hex characters. **Hashes are never repaired.** The
approval then carries the digest the proposal stated; only an EDIT is
re-hashed, from the edited content. (A digest is matched with `fullmatch`:
64 hex characters with a newline glued on is not a content hash.) A human-gated step also
keeps the verdict its ENVELOPE Gate reached: a step that passed with
problems is recorded `passed-with-problems` once the decision is applied —
approving the proposals does not erase the problems the Cog reported making
them.

A decision (`openteams/op-decision [0.1]`) gives every proposed change
exactly one verdict — `approve`, `reject`, or `edit` with the edited change —
and says who decided and when (`decided_by`, non-blank, and `decided_at`, a
timestamp — both required).
Its `payload_sha256` must match the hash of the pending PAYLOAD, recomputed
when the decision is applied, or the run refuses it: the human decided about
something else. An edited change is re-hashed from its edited content, and
THAT hash is what the write grant carries; an edit may change what is
written, never which item it targets or the `target_sha256` it was approved
against. A decision can only select among the proposed changes; it can never
add one. The applied decision is exposed as
`{approved, rejected, edited, history, decided_by, decided_at}` — `approved`
carrying the EFFECTIVE change objects (an edited change appears as the
edited one), `history` one entry per proposal with its verdict and both
hashes, and `decided_by`/`decided_at` the decision's own metadata, so a step
that records the run can say who decided and when without reading the
decision file itself (Op machinery 0.5.4).

```bash
pixi run op -- --request examples/request.json --authority admission.json
# exits 3; read runs/<id>/pending/compose-proposals.md, write a decision
pixi run op -- --resume runs/<id> --decision decision.json
```

Resume reloads the Track and the spec (op.yaml must still hash the same),
re-runs the load-time declaration and admission checks (a Cog manifest that
changed while the run was paused is caught; a dry-run Track has nothing to
resume), applies the decision, exposes `steps.<id>.decision` to later
mappings, and continues from the next step. Steps already passed are **never
re-run**; a step a crash left `running` runs again (a Cog with a journal
reconciles first). Resuming with no decision while one is pending exits 3
again, and every resume is appended to the Track's `resumes`.

**One run, one process.** `runs/<run_id>/run.lock` is locked with `flock` at
the start of a run and of every resume — before the Track is read. The lock
is the open DESCRIPTOR, not the file's content: it is held for the process
lifetime and inherited by every Cog the runner launches, so a runner killed
while a Cog is still writing keeps the run locked until that Cog is gone too.
A run another process holds is refused by name (exit 2). The kernel releases
the lock when the last holder exits, so there is no pid to parse, nothing to
take over, and no stale lock to remove — the file stays where it is, and its
JSON (pid, time) is informational only, written only AFTER the lock is held.
`run.lock` is a control file like the Track: contained in the run directory
and opened `O_NOFOLLOW`, so a `run.lock` that is a link to `track.json` is
refused rather than followed and truncated. The lock is COOPERATIVE: it
holds because trusted code takes it and keeps its descriptor, not because
the host enforces it.

**Durability order.** Nothing external happens that the run directory does
not already describe: accept the decision → record the decision and the
resume, save → issue the grant, create the journal, record the step
`running`, save → invoke. A Track on disk always says what was attempted.
Every directory the runner creates — `runs/<run_id>` itself first of all —
is created one level at a time with each new entry fsynced in its parent, so
a durable Track never sits in a directory whose own entry a power loss could
lose.

The run's own control entries — `grants/`, `pending/`, `decisions/`,
`journal/`, `run.lock` and `track.json` — are RESERVED: `$run_dir` refuses
them (and anything under them) at load, so no step can be handed the
directory that holds its own authority as an output path. The subpath is
NORMALISED before that check, so `outputs/../grants` is refused however it
is spelled, and no component of it may be a symlink: an alias inside the run
is how a step would otherwise be handed the control entries under another
name. The runner's own writes hold to the same rule — every control file
must resolve inside the run directory, reached through no link at all.

## 7. Refused by name

These are refused when the spec LOADS, one sentence each, naming the
construct and the phase that adds it — never discovered mid-run:

| Construct | Message says |
|---|---|
| `tool:` step | never — deterministic work is a Cog of `kind: code`, invoked as a `cog:` step |
| `human:` step | never — a human Gate is a policy on a step, `gate: {policy: human}` |
| `state:` | phase 4 |
| `authority` on a `foreach` step | phase 3 issues no per-element grants |
| `on_fail: retry-once` on a step with `authority` or a reaching Cog | a reaching Cog recovers by resume and journal reconciliation |
| a write requirement's `changes` that is not `{$from: steps.<id>.decision.approved}` | only the approved list can produce a grant |
| two write requirements reading different human gates | one gate per writing step |
| `$run_dir` naming `grants`, `pending`, `decisions`, `journal`, `run.lock` or `track.json` | the runner's control entries are reserved |
| `authority` on a step whose Cog is not `kind: code` | phase 3 supports authority on code Cogs only |
| a requirement outside the Cog's declared `reaches` | the Cog does not declare it |
| a step that names a reaching Cog and requires nothing | a reaching Cog runs only under a grant |
| a write requirement that reads no human decision | a write is authorized by a human decision |
| reading `steps.<id>.decision` of a step with no human Gate | only a human Gate produces a decision |
| `grants` as a mapping root | a grant is never readable from a mapping expression |
| non-empty `gate.guards` | not in the runner subset |
| any unknown top-level or step key | the vocabulary is closed |
| any unknown `$`-operator | the mapping vocabulary is closed |
| any `schema:` but `openteams/op-manifest [0.1]` | the schema it must be |
| a `depends_on` cycle | acyclic |
| reading a step you do not depend on | add it to depends_on |

Exit codes: `0` completed (or completed-with-problems, or a planned dry run),
`1` failed, `2` an invalid spec, request, admission or decision — and a
resume of a run another process holds — `3` paused for a human. Stdout is one
JSON object.

## 8. Changing an Op

Edit `op.yaml`, run `pixi run test`, and check the package. Never edit
`src/` — a machinery fix belongs in cog-smith's `templates/op/`, version
bumped in MACHINERY.md, then re-copied out. Adding a capability means adding
a Cog, not adding code to the Op.
