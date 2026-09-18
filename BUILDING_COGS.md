# Building and Improving Cogs

**Audience:** New Cog builders, reviewers, and coding agents  
**Last verified:** 2026-09-17 against cog-smith Op machinery 0.5.1 /
code-cog machinery 0.1.1  
**Status:** The public CogSpec v0.1 is an experimental discussion draft. The
OpenTeams manifest and envelope described here are the current Collab profile,
not universal CogSpec requirements.

This guide explains what a Cog is, how Cogs are used, how to build a working
context Cog with Cog Smith, and how to improve one without breaking its
contracts.

If you remember only one thing, remember this:

> A Cog is a portable, installable AI worker package with a durable identity,
> a clear work contract, declared requirements and entry points, and evidence
> that it behaves as claimed.

A Cog is more than a prompt, but it is not an entire workflow or an unrestricted
agent.

## 1. The three layers you need to keep separate

Several documents and implementations use the word “Cog.” They describe
different layers:

| Layer | What it defines | Where to look |
|---|---|---|
| **CogSpec core** | The portable artifact: COG.md, its frontmatter, and the manifest it identifies | [CogSpec](../cog-spec/SPEC.md) |
| **OpenTeams/Collab profile** | The current manifest fields, entry-point conventions, result envelope, binding, and hosting expectations used by this workspace | [Envelope](ENVELOPE.md) and the generated manifest (`[tool.cog]` in pixi.toml, or cog.yaml) |
| **A particular Cog** | One worker's purpose, inputs, outputs, instructions, contract checks, tests, and declared dependencies | Its COG.md, manifest, context/, and task_logic.py |

A Cog can conform to the public core while not being runnable in a particular
hosting environment. A hosting environment is allowed to require a stricter
shape. It should describe those extra requirements as its profile rather than
claiming that they are universal CogSpec rules.

## 2. What a Cog is

At the CogSpec core, a Cog is a directory containing:

1. **COG.md**, the fixed entry file and human-readable work contract.
2. A **machine-readable manifest** named by COG.md.

The smallest possible Cog is therefore:

    cog-example/
    ├── COG.md
    └── cog.yaml            # or pixi.toml carrying a [tool.cog] table

The public core intentionally does not dictate the manifest's contents. It makes
the artifact recognizable, inspectable, and self-identifying. The OpenTeams
profile adds the structure needed to run Cogs in the current Collab environment.

A useful Cog normally answers these questions:

- What worker is this?
- What job does it perform?
- What work is supported?
- What adjacent work is explicitly unsupported?
- What input does it need?
- What output does it promise?
- What model, harness, or other capabilities does it require?
- How can a user or hosting environment invoke it?
- What must it never do?
- How do we test whether it is useful and trustworthy?
- Which exact Cog, model, and binding produced a saved result?

### Cog kinds

CogSpec describes three broad kinds:

| Kind | Carries task context | Carries a model |
|---|---:|---:|
| **context** | Yes | No; inference is supplied elsewhere |
| **model** | No | Yes, including the software needed to execute it |
| **complete** | Yes | Yes |

A fourth kind, **code**, was decided on 2026-09-17 for the triage Op's phase 3:
a package with the Cog shape (manifest, entry points, envelope v1, contract
checks, catalog card, machinery by hash) whose work is done by code with no
model in the loop. It carries task context in the same sense a program does,
and no model. The kind is declared, never inferred; `smith check` refuses a
code Cog that declares a model requirement. What a code Cog has are
dependencies and function calls, not tools; "tools" is reserved for what a
model-driven Cog is granted during a turn. cog-smith itself has this shape.
**Code Cogs have a starter** — see §7b.

Cog Smith provides supported starters for **context Cogs** and **code
Cogs** (`smith new --kind code NAME`). Its
model-catalog command, **generate-descriptors**, also creates OpenTeams deployment
descriptors:

    pixi run generate-descriptors -- --config examples/model-catalog.yaml \
      --out-dir ../models

Those descriptors are profile-specific records that identify a served model and
its endpoint requirements; do not confuse them with the public core meaning of
a model-carrying Cog.

## 3. What a Cog is not

A Cog is not:

- **A general-purpose chatbot.** It should have a bounded purpose and completion
  criteria.
- **An Op or workflow.** An Op composes Cogs, data sources, gates, guards, and
  humans into a multi-step process.
- **A Frame.** A Frame is organizational context, policy, terminology, or
  constraints that a Cog may carry or reference.
- **A tool or connector.** Tools and MCP servers expose capabilities. The
  surrounding environment grants or denies access to them.
- **An authority grant.** Installing a Cog never grants access to data,
  credentials, networks, tools, or external actions.
- **Only a prompt.** A useful Cog also carries machine-readable contracts,
  entry points, validation, tests, and versioned identity.

A quick litmus test is:

> Is it an installable, versionable worker or cognitive-capability provider
> with entry points and a declared contract?

If not, it is probably content, a tool, an Op, or part of the surrounding
runtime rather than a Cog.

## 4. How Cogs are used

A Cog normally moves through this lifecycle:

    discover → inspect → install → resolve → verify → invoke → evaluate

### Discover and inspect

Catalogs and clients read COG.md and the manifest to show:

- identity and version;
- purpose and summary;
- input and output contracts;
- usage operations;
- lifecycle operations;
- requirements and locality;
- prohibitions; and
- evaluation material.

Cog Smith's card command renders the current catalog view.

### Install

Installation makes the Cog package and its supported resources available. It
may create a Pixi environment and validate the package. Installation still does
not authorize tools, data, credentials, or external actions.

### Resolve requirements

A context Cog requires inference from somewhere else. The current OpenTeams
profile declares a capability such as:

    model-endpoint/openai-compatible

The manifest may name a default satisfier, but it is only one candidate. A
hosting environment can supply a different compatible satisfier. Resolution
checks the candidate and writes a local binding record named model.json.

The binding record is installation state, not source code. It is ignored by Git
and records the concrete endpoint, model identity, locality, credential
reference, pin state, and satisfier used by that installation.

### Verify health and identity

Before work is sent to the model, the lifecycle check verifies that the
dependency is reachable. A deep check makes a small end-to-end request and
compares the model identity echoed by the provider with the bound identity.

### Invoke a usage operation

A usage operation is the surface that users and Ops call. The default context
template provides:

- a command operation named **ask**; and
- an HTTP JSON entry point served by **serve**.

The serve task has a split audience: the HTTP endpoint it serves is a usage
surface, while starting the server is lifecycle work performed by the hosting
environment. The manifest declares this explicitly.

The input is a task bundle that conforms to the Cog's input schema.

### Read the result envelope

Cogs created by the current Smith template return envelope v1. Important fields
include:

| Field | Meaning |
|---|---|
| **envelope** | Version discriminator for this contract; 1 today |
| **cog** | Cog ID and version |
| **task** | Usage operation that ran |
| **ok** | Whether invocation completed with a parseable payload |
| **error** | Structured {code, detail} failure — binding, input, or upstream — when present |
| **payload** | Domain result shaped by the Cog's output schema |
| **raw** | Verbatim model response for audit and salvage |
| **problems** | Self-reported contract-check findings: schema, grounding, citation, identity, or other integrity problems |
| **binding** | The complete model and satisfier identity for this run |
| **timing** | Invocation latency |

An important rule is that **ok can be true while problems is non-empty**. That
means the model produced a parseable result, but one of the Cog's own contract
checks found an integrity problem. A human or automated Gate decides whether to
accept it. The Cog should not hide that distinction.

Keep the three verification tiers distinct. **Contract checks** run inside the
Cog: it declares a contract in its definition and checks that it meets that
contract before its output interacts with anything outside itself; the problems
list is that self-report. **Guards** are independent, first-class entities in
the surrounding system — first-class just like Cogs — that verify a Cog's
inputs and outputs against the *system's* requirements, not the Cog's own
commitments. A Guard may re-run the same deterministic logic a contract check
ran, but it runs independently and under the system's authority. **Gates**
decide, consuming both the self-report and any Guard verdicts. A Cog's
self-report never substitutes for a Guard, and neither substitutes for the
Gate's decision. Do not call in-Cog checks guards.

### Compose Cogs into Ops

The Op layer only needs four things from a Cog:

1. an invokable task entry point;
2. a result envelope;
3. a health probe; and
4. a catalog card.

The Cog owns its model binding, internal machinery, and its contract checks.
The Op layer owns sequencing, human approvals, gates, independent Guards, and
durable workflow state. See
[The Op–Cog seam](../output/op-cog-seam.md) for the current architecture note.

## 5. Anatomy of a Cog Smith context Cog

A fresh Cog currently looks like this (`--manifest yaml` adds a standalone
`cog.yaml` and leaves pixi.toml without the `[tool.cog]` table):

    cog-example/
    ├── .gitignore
    ├── COG.md
    ├── pixi.toml           # environment + the [tool.cog] profile manifest
    ├── context/
    │   ├── system.md
    │   ├── input-schema.json
    │   ├── output-schema.json
    │   └── output-example.json
    ├── examples/
    │   └── sample-bundle.json
    ├── evals/
    │   └── smoke.fixture.yaml
    ├── src/
    │   ├── task_logic.py
    │   ├── cog_core.py
    │   ├── cog_cli.py
    │   ├── cog_api.py
    │   ├── cog_binding.py
    │   ├── cog_resolve.py
    │   ├── cog_use.py
    │   └── cog_eval.py
    └── tests/
        └── test_cog.py

The created .gitignore already excludes model.json, the Pixi environment,
caches, and local run reports, so the installation-state items in the
definition of done are pre-wired; do not weaken it.

### Files the Cog author owns

You are expected to edit:

- **COG.md** — the readable work contract.
- **the manifest** — `[tool.cog]` in pixi.toml (default) or cog.yaml:
  identity, context references, requirements, interfaces, input/output
  labels, memory, prohibitions, and fixtures. In pixi.toml, `version` and
  the summary live once in `[workspace]` (version / description).
- **context/system.md** — the instructions sent to the model.
- **context/input-schema.json** — the normative task-bundle contract.
- **context/output-schema.json** — the normative payload contract.
- **context/output-example.json** — a complete worked example shown to the
  model.
- **examples/sample-bundle.json** — a realistic example input.
- **evals/smoke.fixture.yaml** — model-backed expectations.
- **tests/test_cog.py** — deterministic, model-free tests of the contracts
  and the contract checks.
- **src/task_logic.py** — task-specific input checks, prompt rendering, and
  semantic output checks.

### Files the Cog author does not own

Every Python file under src/ except task_logic.py is shared Cog Smith machinery.

Do not edit those files inside a created Cog. Smith verifies them by hash. A
shared runtime bug must be fixed once in cog-smith/templates/context-cog/src/,
documented in [MACHINERY.md](MACHINERY.md), regression-tested, and then rolled
out deliberately.

## 6. Before building: write the work contract

A good Cog starts with a bounded job, not with code. Answer these questions
first:

1. **Purpose:** What single kind of decision or artifact does this Cog produce?
2. **User:** Who consumes the result?
3. **Supported work:** What inputs and situations are in scope?
4. **Unsupported work:** What nearby requests should it refuse or abstain from?
5. **Input:** What evidence and parameters are required?
6. **Output:** What exact shape is useful downstream?
7. **Grounding:** How can a deterministic check prove that claims are tied to
   supplied evidence?
8. **Abstention:** When should the Cog return no answer?
9. **Requirements:** What model capability and locality are acceptable?
10. **Prohibitions:** What actions must the Cog never take?
11. **Evaluation:** What happy path, boundary case, and adversarial case prove
    the worker is useful?
12. **Done:** What does successful completion look like?

Prefer a narrow Cog that performs one job reliably over a broad “assistant”
whose success cannot be measured.

## 7. Build a Cog with Cog Smith

### Prerequisites

You need:

- this workspace;
- Pixi;
- Python 3.10 or newer through the Pixi environment; and
- a compatible model endpoint for the live invocation steps.

Start in the Cog Smith directory:

    cd cog-smith
    pixi install
    pixi run test

### Step 1: create the package

For the interactive builder:

    pixi run new -- --dir ../cog-release-brief

For a scripted create, pass the yes flag and explicit values:

    pixi run new -- \
      --dir ../cog-release-brief \
      --yes \
      --summary "Produces an evidence-grounded release brief from supplied changes." \
      --produces release_brief \
      --prohibit send_external_message \
      --prohibit modify_source_data

The Cog's name defaults to the destination directory's basename; pass the name
option (and the id option) when the directory name cannot serve. The name uses
lowercase ASCII letters, digits, and single hyphens; no leading, trailing, or
consecutive hyphens. Cog Smith's current profile is slightly stricter than the
public core and requires the name to begin with a letter. The checker warns
when the directory and the declared name differ.

Smith refuses to overwrite an existing destination. It validates the request
before writing, renders into a staging directory, verifies rendering, and only
then moves the package into place. The new command runs the package checker
immediately afterward.

### Creating from a request file

A third path suits automation — a drafting tool, or the Builder Op's drafting
Cog: put the builder answers and, optionally, the drafted context files into
one cog-request JSON document and create from it:

    pixi run new -- --from-request request.json --envelope

The request carries the identity answers (id, summary, owner, license, port,
produces, prohibits, the default model satisfier) plus optional overlays:
context/system.md, the input and output schemas, the worked example, the
sample bundle, the eval fixture, and COG.md. Overlays are applied inside the
same atomic creation, and the checker runs on the result — so a request whose
example disagrees with its schema fails visibly, not silently. Explicit flags
override request values; the flag implies non-interactive. Overlays never
touch src/: task_logic.py and the tests remain the starter's, so Step 2 still
applies when your drafted schemas diverge from the grounded-highlights shape.
See examples/cog-request.json in cog-smith for the format.

### Step 2: replace the starter task completely

The current starter is intentionally a working **grounded highlights** task.
It is not a neutral blank package. Search for starter-specific language:

    rg -n "highlight|item_ids|evidence_quote|Starter default|replace" \
      ../cog-release-brief

Use grep -rnE with the same pattern if ripgrep is not installed. Review every
match. A complete rewrite normally touches:

1. COG.md;
2. the manifest (`[tool.cog]` in pixi.toml, or cog.yaml);
3. all four files under context/;
4. examples/sample-bundle.json;
5. evals/smoke.fixture.yaml;
6. src/task_logic.py; and
7. tests/test_cog.py.

Do not leave a release-brief Cog with a highlights schema or a meeting-focused
grounding rule.

### Step 3: write COG.md for a human reader

COG.md should explain:

- purpose;
- supported work;
- unsupported work;
- required input and context;
- promised output;
- working method;
- grounding and abstention rules;
- limitations; and
- how to run the Cog.

Keep frontmatter within the restricted CogSpec YAML subset. Let Smith's checker
catch syntax and naming problems rather than inventing new frontmatter
structures.

### Step 4: update the manifest

At minimum, review these sections of the manifest (`[tool.cog]` in
pixi.toml, or cog.yaml):

- **id, version, summary, owner, license** — package identity and
  accountability.
- **kind** — normally context for the current starter; `code` for a
  model-free Cog (no starter yet).
- **context** — paths to instructions and schemas.
- **requires** — capability class, locality, and default satisfier.
- **interfaces** — names, kinds, tasks, audience, endpoint, and one default.
- **io** — high-level accepted and produced artifact labels.
- **memory** — explicit; none is the current default.
- **prohibits** — structural boundaries.
- **evaluation.fixtures** — every declared path must exist.

Usage operations face users and the Op layer. Lifecycle operations face the
hosting environment. Do not infer the audience from a task name; declare it.
The serve task shows why: the endpoint it serves is a usage surface, while
starting the server is lifecycle work.

### Step 5: design the input contract

The input schema is normative. It should:

- require the evidence and parameters the task actually needs;
- reject ambiguous or incomplete bundles early;
- describe fields clearly enough to generate a form;
- set length, item-count, enumeration, and object-shape constraints where
  useful; and
- avoid accepting arbitrary unstructured objects “just in case.”

Update the sample bundle so it is realistic and passes the schema.

The current x-cog-input annotations are profile-level UI hints for generated
forms. A related but distinct annotation, x-cog-param, declares parameter
semantics — what a value *is*, not how to render it — and is defined by the
client-side x-cog-param protocol; the two are not synonyms. Clients must be
able to ignore unknown annotations of either kind and still render ordinary
inputs.

### Step 6: design the output contract and example together

The output schema defines the envelope's payload. The worked example teaches
the model what a complete answer looks like.

They must agree exactly. A strong output contract usually includes:

- explicit required fields;
- bounded classifications or statuses where appropriate;
- evidence references;
- uncertainty or abstention fields;
- additionalProperties false when the shape should be closed; and
- structures that downstream code can consume without parsing prose.

Do not add envelope fields such as ok, problems, binding, or timing to the
output schema. The schema describes payload only; shared machinery adds the
envelope.

### Step 7: implement task_logic.py

This is the only author-owned Python module under src/. It has three jobs:

1. **check_input(bundle)** — semantic input checks not expressible cleanly in
   JSON Schema, such as duplicate IDs or cross-field constraints.
2. **render_input(bundle)** — deterministic conversion from the validated task
   bundle into the model's user message.
3. **check_output(parsed, bundle)** — deterministic contract checks beyond the
   output schema, such as citation existence, verbatim grounding, allowed
   transitions, arithmetic reconciliation, or evidence support.

Return structured problems through the shared cog_core.problem helper. Do not
raise ordinary validation failures as exceptions.

The default verbatim-quote check is valuable for evidence-grounded analysis.
Adapt it deliberately. Do not delete grounding merely because a new domain uses
different field names.

### Step 8: write deterministic tests

Model-free tests should cover:

- identity and manifest consistency;
- every declared file and task;
- valid and invalid example inputs;
- output-example/schema agreement;
- task-specific semantic input checks;
- each output contract check;
- abstention behavior;
- envelope shape;
- malformed or salvaged provider output when relevant; and
- binding and identity behavior that affects trust.

Tests should be deterministic and should not require a live model or network.

### Step 9: write model-backed evaluation fixtures

A fixture pairs an input bundle with mechanical expectations. Include at least:

- one representative happy path;
- one insufficient-evidence or abstention case;
- one adversarial or prompt-injection case; and
- one important domain boundary.

Prefer checks that can be evaluated mechanically: parsed shape, required keys,
minimum findings, expected classification, forbidden canary tokens, citation
existence, and grounding.

Evaluation against a live model complements deterministic tests. It does not
replace them.

## 7b. Build a code Cog

    pixi run smith -- new cog-read-github --kind code --yes

A code Cog is the same seam with the model half removed. What you get:

    cog-read-github/
    ├── COG.md
    ├── pixi.toml             # [tool.cog] manifest; tasks: run, check, test
    ├── context/
    │   ├── input-schema.json     # a code Cog's "context" is its declared
    │   └── output-schema.json    # SHAPES — no instructions, no example
    ├── examples/sample-bundle.json
    ├── src/
    │   ├── cog_core.py       # MACHINERY — envelope, grant checks, Journal
    │   ├── cog_cli.py        # MACHINERY — the entry point
    │   └── task_logic.py     # YOURS — the work
    └── tests/test_cog.py

There is no `resolve` task and no `model.json`: nothing to bind. `smith
check` enforces `cog_core.py` and `cog_cli.py` by hash exactly as it does the
context-cog machinery, and refuses a code Cog that declares `requires`.

You write one function:

```python
def run(bundle, grant, journal) -> tuple[dict, list[dict]]:
    """Return (payload, problems)."""
```

The machinery validates the bundle against the declared input schema, calls
`run`, validates the payload against the output schema, runs your contract
checks (`check_input` / `check_output`), and emits envelope v1. The
`binding` a code Cog reports is `{kind: code, cog, task_logic_sha256,
machinery}` — which CODE produced the result; there is no `model` key at all.

### What it reaches, and the grant it runs under

`reaches` in the manifest declares what the Cog touches OUTSIDE the run.
Declared, never inferred:

```toml
[[tool.cog.reaches]]
resource = "github"
actions = ["read"]
```

A Cog whose `reaches` is non-empty refuses to run without a grant
(`no-grant`), before `run` is ever called. The Op runner issues the grant
(BUILDING_OPS §5) and invokes the Cog as

    pixi run run -- --bundle req.json --grant g.json --run-id RUN --journal j.jsonl

`cog_core` checks the grant for you — its shape and run binding, expiry, and
that this Cog is its recipient (`grant-invalid`, `grant-expired`,
`grant-wrong-run`, `grant-wrong-recipient`; an invocation with a grant and no
`--run-id` is `grant-invalid`) — and gives you the per-call checks:

```python
ok, detail = cog_core.read_allowed(grant, "openteams-ai/apollo-desktop")
ok, detail = cog_core.write_allowed(grant, change_id,
                                    target_sha256,             # fetched NOW
                                    content_sha256=...)        # optional
```

Call one before EVERY external call — they re-check expiry and run binding
each time, so a long invocation cannot keep acting on a grant that has since
expired — and report what you attempted in the payload's `authority_use` list
(`cog_core.use(...)`). A denial is a `problems` entry, and the Gate — not
your Cog — decides what it means.

A granted change carries TWO hashes, and neither may be null:
`content_sha256` is what the human approved (pass your bundle's copy as
`content_sha256=` and a swapped-out change is denied), and `target_sha256` is
the target's content as the Op read it. `write_allowed` REQUIRES the target
hash you just fetched fresh from the target: staleness is that fetch
disagreeing, and nothing here fails open on a missing hash.

**The honesty rule.** This process runs as its owner, with the owner's
ambient credentials. A grant is not a sandbox: it is a document your code
checks before it acts. Describe it that way in COG.md and in review. Nothing
in this path is an enforced restricted environment.

### The journal

For effects that must happen exactly once, the runner passes
`--journal <file>`: append-only JSONL, one object per line, fsynced per
line. Read it FIRST:

```python
done = journal.phases()                      # change_id -> last phase
if done.get(cid) in ("applied", "failed"):
    continue                                 # already decided
journal.append({"change_id": cid, "phase": "applying"})
...                                          # the one external call
journal.append({"change_id": cid, "phase": "applied", "evidence": {...}})
```

A change left `applying` by a crash is UNCERTAIN: reconcile it against the
TARGET (does the label/comment already exist?) and record `applied` with
`reconciled: true`, rather than applying it again. Asking the target is the
point — the journal says an attempt started, not that it landed. That is what
makes a resume safe.

The journal repairs itself in one direction only: an unterminated LAST line
is a torn write, so `read()` ignores it and `append()` cuts it and records
`{"phase": "torn"}` before writing, which keeps the next entry readable. A
malformed COMPLETE line is corruption: the invocation is refused with
`journal-corrupt` rather than silently skipping a line that might record an
effect.

## 8. Validate before running a model

From cog-smith, run the package checker and the Cog's deterministic tests:

    pixi run check -- ../cog-release-brief --tests

Note that check means something different inside a created Cog, where it is the
Cog's lifecycle health operation; see section 9.

For programmatic consumption — a Builder Op, for instance — new, check, and
card accept an envelope flag and emit envelope v1 instead of text: checker
findings ride in problems (ok-with-problems; a Gate decides), and binding is
null because Cog Smith is a deterministic tooling Cog with no model
dependency.

Checker findings are labeled by layer:

- **core** — CogSpec artifact and frontmatter rules;
- **profile** — OpenTeams manifest and declaration rules; or
- **runtime** — machinery, schemas, tasks, examples, fixtures, and tests.

Fix every error. Review warnings rather than automatically suppressing them.
The current core layer catches the common CogSpec rules but does not yet compose
the complete reference validator. Before publication, also run the reference
validator explicitly:

    pixi run python ../cog-spec/tools/validate_cog.py \
      ../cog-release-brief

Then inspect the catalog card:

    pixi run card -- ../cog-release-brief
    pixi run card -- ../cog-release-brief --json

Check that purpose, entry points, usage operations, lifecycle operations,
contracts, requirements, prohibitions, and fixtures look like the Cog you
intended to build.

## 9. Bind and run the Cog

Move into the new Cog:

    cd ../cog-release-brief
    pixi install

### Resolve the declared model dependency

Use the declared default satisfier:

    pixi run resolve

Or select a different compatible descriptor, such as one created earlier with
generate-descriptors:

    pixi run resolve -- --satisfier ../models/cog-approved-model

If the descriptor defers its address until installation:

    pixi run resolve -- \
      --satisfier ../models/cog-approved-model \
      --endpoint https://model.example.org/v1

Never put a credential value in the manifest, a descriptor, model.json, or a command
line. Descriptors name an environment variable. Export the secret only in the
runtime environment:

    export APPROVED_MODEL_API_KEY="..."

### Or point at a preset endpoint with use

The created package also carries a lifecycle operation named **use** that
points the Cog at a named endpoint preset instead of a satisfier Cog:

    pixi run use -- --show

It writes the same binding-record shape as resolve, enforces the manifest's
locality constraint and refuses plain HTTP to a non-loopback host, and marks
the record pinned only when you supply a trusted revision. Prefer resolve when
a descriptor Cog exists; use is the direct path when only an endpoint does.

### Verify the binding

Run the shallow health check:

    pixi run check

Run the deep identity check before trusting live results:

    pixi run check -- --deep

This command is different from running the Smith package checker in the
cog-smith directory. Inside a created Cog, check is the Cog's lifecycle health
operation.

### Invoke the CLI operation

    pixi run ask -- --bundle examples/sample-bundle.json

Inspect all of the following, not just payload:

- ok;
- error;
- problems;
- binding.model_identity;
- binding.violations;
- binding.record and pin state; and
- raw when the payload was salvaged or flagged.

### Serve the HTTP entry point

    pixi run serve

The generated service binds to loopback and is a demonstration entry point, not
a production server. If the declared port is already in use, override the live
port for local testing:

    COG_API_PORT=8123 pixi run serve

### Run evaluation

    pixi run eval

To retain a local report:

    pixi run eval -- --report

Retaining a baseline is a separate, stricter operation:

    pixi run eval -- --baseline

Baseline retention has stricter identity and provenance rules than a plain
report. Read the command help and inspect eligibility before treating a report
as release evidence.

## 10. Definition of done for a new Cog

A Cog is ready for review when all of the following are true:

- [ ] COG.md accurately describes purpose, scope, boundaries, inputs, outputs,
      method, and limitations.
- [ ] The package name and frontmatter conform to CogSpec.
- [ ] Manifest declarations match the implementation.
- [ ] No starter-specific highlights language remains unintentionally.
- [ ] The sample bundle passes the input schema.
- [ ] The output example passes the output schema exactly.
- [ ] task_logic.py contains task-specific checks and prompt rendering.
- [ ] Every material output claim is mechanically grounded where possible.
- [ ] Abstention and insufficient-evidence behavior are explicit.
- [ ] Deterministic tests pass without a model.
- [ ] Evaluation fixtures cover happy, boundary, and adversarial cases.
- [ ] Smith reports no errors.
- [ ] The catalog card represents the intended operations and contracts.
- [ ] Resolution succeeds against an acceptable model and locality.
- [ ] The deep health check verifies, or honestly reports, model identity.
- [ ] A live invocation returns the expected envelope and no unexplained
      problems.
- [ ] No credentials, model.json files, local reports, or workstation paths are
      committed.
- [ ] The version is updated whenever a published contract or behavior changes.

## 11. How to improve an existing Cog

Start by deciding which layer owns the problem.

### Change the individual Cog when the issue is task-specific

Examples:

- unclear purpose or boundaries;
- poor instructions;
- missing input constraints;
- an inadequate output schema;
- weak examples;
- domain-specific prompt rendering;
- missing grounding rules;
- insufficient fixtures or tests; or
- an inaccurate manifest declaration.

Make those changes in COG.md, the manifest, context/, examples/, evals/, tests/, or
src/task_logic.py.

### Change Cog Smith when the issue affects every created Cog

Examples:

- envelope behavior;
- binding or resolution;
- common API or CLI behavior;
- catalog-card semantics;
- shared validation;
- template structure; or
- a security or portability defect in common machinery.

Make the correction in cog-smith, update MACHINERY.md when shared machinery
changes, add a regression test, validate a fresh creation, and verify at least one
real existing Cog. Do not patch the same shared file independently in several
created Cogs.

### Preserve contract compatibility

Before changing a published Cog, ask:

- Does the input schema change?
- Does the payload schema change?
- Does an operation disappear or change meaning?
- Does the result envelope change?
- Do requirements, locality, memory, or prohibitions change?
- Will existing saved bundles, clients, Ops, or evaluation reports still work?

Additive optional fields may be compatible. Removing a field or changing its
meaning usually requires a version change and migration notes. Envelope v1 is a
shared profile contract; changing an existing field's type or meaning is a
versioning event.

## 12. Instructions for coding agents

A coding agent working on a Cog should follow this sequence:

1. Read the nearest AGENTS.md and this guide completely.
2. Read the target Cog's COG.md and manifest before editing code.
3. Identify whether the requested change is task-specific or shared machinery.
4. Inspect input schema, output schema, worked example, fixture, and
   task_logic.py as one contract.
5. Preserve user-owned changes and do not edit unrelated files.
6. Never edit shared src/ machinery inside a created Cog.
7. Never add secrets, authority grants, or undeclared external actions.
8. Prefer deterministic validation and model-free tests before live-model work.
9. Update documentation, examples, fixtures, and tests with behavior changes.
10. Run Smith checking with tests, render the card, and report the exact
    verification performed.
11. If shared machinery must change, work in cog-smith, update provenance, and
    prove that a fresh creation still passes.
12. Call out profile proposals as proposals; do not present them as universal
    CogSpec guarantees.

A useful task handoff to an agent includes:

- target Cog and requested outcome;
- example input and expected payload;
- acceptance criteria;
- allowed files or scope;
- model and locality constraints;
- prohibitions and approval boundaries; and
- commands required for verification.

Agents should not infer permission to publish, grant credentials, contact
external systems, or change the public specification from a request to improve
one Cog.

## 13. Common problems

### Smith refuses to create without the yes flag

Non-interactive environments must opt into defaults explicitly. Add the yes flag
and provide important values as command options.

### The destination already exists

Smith refuses to overwrite it. Inspect the existing package. Choose a new
destination or update it deliberately; do not remove it merely to silence the
error.

### Resolution cannot find the default model Cog

The default source is relative to the created package and may not exist outside
this workspace layout. Pass a compatible satisfier explicitly or update the
manifest's declared default.

### Resolution asks for an endpoint

The chosen deployment descriptor uses an install-time address. Supply the
endpoint during resolution. The address belongs in the binding record, not the
portable package.

### A credential is reported missing

Set the environment variable named by the descriptor. Do not paste the secret
into YAML or JSON.

### Smith reports machinery drift

Do not “fix” the hash by editing or copying arbitrary files inside the Cog.
Determine whether the Cog contains an accidental local edit or needs an
approved Smith machinery upgrade.

### Invocation says ok true with problems

The payload parsed, but one or more of the Cog's contract checks flagged it.
Inspect the problems and raw response. A Gate or human reviewer decides whether
the result is acceptable.

### The model identity is unverified or mismatched

Unverified means the provider did not echo enough identity to prove which model
answered. Mismatch means it echoed a different identity. Never treat a mismatch
as trusted output or retain it as a baseline.

### The local HTTP port is busy

Use COG_API_PORT for local testing, or create with a different declared port.
Remember that a runtime override does not rewrite the portable manifest.

## 14. Further reading

Read these in order:

1. [CogSpec README](../cog-spec/README.md) — short public-core overview.
2. [CogSpec v0.1](../cog-spec/SPEC.md) — normative core rules.
3. [Cog Smith README](README.md) — command summary.
4. [Envelope v1](ENVELOPE.md) — current Collab result contract.
5. [Machinery provenance](MACHINERY.md) — ownership and shared-code lineage.
6. [Meeting Highlights example](../cog-meeting-highlights/COG.md) — an older
   illustrative domain adaptation; useful for its work contract, but not a
   source for current shared machinery.
7. [The Op–Cog seam](../output/op-cog-seam.md) — how Ops consume Cogs.
8. [Cog Smith review](../output/cog-smith-review-2026-08-22.md) — design and
   usability findings that led to the current hardening work.

## Closing principle

Build the contract before the implementation. Keep the worker narrow. Make
inputs and outputs explicit. Ground important claims. Preserve exact identity
and binding evidence. Treat installation and authority as separate decisions.
Then prove the Cog works with deterministic tests and realistic evaluation
fixtures.

That is what turns a prompt into a portable cognitive worker.
