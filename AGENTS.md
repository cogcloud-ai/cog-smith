# AGENTS.md — cog-smith contributor instructions

Read the suite guide linked from README.md and read `BUILDING_COGS.md` before
creating or changing a Cog. cog-smith is itself a Cog (see COG.md); it is also
the single source of the machinery every created Cog carries.

## Commands

- Create: `pixi run new -- --dir ../cog-<name> [--yes ...flags] [--envelope]`
- Create a CODE Cog: `pixi run new -- cog-<name> --kind code --yes` — the
  Cog shape with no model in the loop (`templates/code-cog/`, BUILDING_COGS
  §7b). No `resolve`, no binding record; a declared `reaches` and the grant
  it is checked against; machinery masters are picked by KIND.
- Create from a request: `pixi run new -- --from-request req.json` — the
  drafting-cog seam: one JSON doc of builder
  answers + drafted context overlays, created atomically then checked;
  flags override request values; overlays never touch src/. Format:
  examples/cog-request.json.
- Validate: `pixi run check -- <path> [--tests] [--envelope]`
- Ops: `python src/cogsmith_cli.py op new --from-spec <spec> --dir <dir>` and
  `op check <dir> [--tests] [--envelope]` — the Op half of the builder
  (BUILDING_OPS.md). Op machinery lives in `templates/op/src/` and is
  enforced by hash the same way Cog machinery is; an Op package carries no
  per-Op Python at all.
- Card: `pixi run card -- <path> [--json | --envelope]`
- `--envelope` emits envelope v1 for Op consumption:
  binding null (deterministic tooling cog), findings in `problems`,
  ok-with-problems semantics; exit codes unchanged.
- Model catalog: `pixi run generate-descriptors -- --config <yaml> --out-dir <dir>`
- Catalog from a hub: `python scripts/llmmodel_catalog.py <LLMModel yaml/dir>
  [--surface internal|external|install-time] [--base-domain <domain>]` —
  pack-neutral (no smith imports); emits generate-descriptors's config.
- Tests: `python3 -m unittest discover -s tests` — the model-free suite
  plus `tests/test_review_regressions.py` (one test per 2026-08-22 review
  finding; never delete these). Current Pixi manifests require Python 3.11 or newer. Live loop: `tests/mock_model.py` +
  a created cog's resolve/ask.

## Invariants

1. **Machinery is sacred:** template `src/` masters change only here, with
   MACHINERY.md updated; created Cogs never edit them (task_logic.py is the
   sole author-owned src module). `smith check` enforces by hash. The same
   rule covers `templates/op/src/` (Op machinery 0.5.0), where the
   author-owned part is op.yaml and nothing else, and
   `templates/code-cog/src/` (code-cog machinery 0.1.0).
2. **Envelope v1 is the emitted contract** (ENVELOPE.md): fixed `payload`
   key, structured problems, ok-may-carry-problems (gates decide), binding
   identity in every result. Changing it = versioning event, not an edit.
3. **Created Cogs must be immediately runnable and immediately checkable:**
   `new` ends by running `check`; a template change that breaks
   fresh-creation PASS or the created test suite is a regression.
4. **No new runtime deps** beyond python/pyyaml/jsonschema; keep the
   toml_compat fallback (3.10 floor).
5. Vocabulary: usage ops vs lifecycle ops; interfaces = entry points;
   never handler/runner/harness for client-side things. Contract checks =
   the Cog's own in-package validation of its declared contract
   (self-reported in `problems`); Guards are independent, first-class
   system-side verifiers of the SYSTEM's requirements — never call in-cog
   checks guards. (Machinery wording was swept in 0.2.0 and manifest reading changed in 0.3.0; fix any future
   drift only with a machinery version bump, never inside a created Cog.)
6. **Checker layers (from an internal review):** findings are labeled
   core / profile / runtime and the layers must never blur — the manifest
   file (`[tool.cog]` in pixi.toml by default, or cog.yaml; exactly one,
   named by COG.md's `manifest:` pointer) is a PROFILE convention, not a
   core rule. Known gap, queued: compose the CogSpec reference validator
   into the core layer.
7. **Lockfile policy:** pixi.lock files are generated ONLY on
   Trent's Mac (conda-forge is 403-blocked in the sandbox and device VM)
   and committed once generated; created Cogs follow the same policy.
8. Fast-follow queue (do not start without Trent): derive-schema; draft
   (model-backed; adds a model-endpoint requirement when it lands);
   promote-draft (COG.md draft -> package, per CogSpec's own builder
   description); machinery diff/upgrade (hash checking needs a repair
   path before many cogs exist); honest starter variants
   (context-minimal, context-classification, complete, model);
   doctor. generate-descriptors shipped in v0.1; review hardening 2026-08-22.
