# Building Ops

**Audience:** Op builders, reviewers, and coding agents
**Last verified:** 2026-09-17 against cog-smith Op machinery 0.4.5
**Status:** The Op spec `openteams/op-manifest [0.1]` is the laptop side's
proposal, implemented from `planning/current/phase2-op-runner-contract.md`.
It is a runner SUBSET on purpose: tool steps, human steps, and durable state
are refused by name, with the phase that adds them.

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

## 5. Refused by name

These are refused when the spec LOADS, one sentence each, naming the
construct and the phase that adds it — never discovered mid-run:

| Construct | Message says |
|---|---|
| `tool:` step | phase 3 |
| `human:` step | phase 3 |
| `gate.policy: human` | phase 3 |
| `state:` | phase 4 |
| non-empty `gate.guards` | not in the runner subset |
| any unknown top-level or step key | the vocabulary is closed |
| any unknown `$`-operator | the mapping vocabulary is closed |
| any `schema:` but `openteams/op-manifest [0.1]` | the schema it must be |
| a `depends_on` cycle | acyclic |
| reading a step you do not depend on | add it to depends_on |

Exit codes: `0` completed (or completed-with-problems, or a planned dry run),
`1` failed, `2` an invalid spec or request. Stdout is one JSON object.

## 6. Changing an Op

Edit `op.yaml`, run `pixi run test`, and check the package. Never edit
`src/` — a machinery fix belongs in cog-smith's `templates/op/`, version
bumped in MACHINERY.md, then re-copied out. Adding a capability means adding
a Cog, not adding code to the Op.
