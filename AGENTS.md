# AGENTS.md — cog-smith contributor instructions

Read `../CLAUDE.md` for workstream context and read `BUILDING_COGS.md` before
creating or changing a Cog. cog-smith is itself a Cog (see COG.md); it is also
the single source of the machinery every minted Cog carries.

## Commands

- Mint: `pixi run new -- --dir ../cog-<name> [--yes ...flags] [--envelope]`
- Validate: `pixi run check -- <path> [--tests] [--envelope]`
- Card: `pixi run card -- <path> [--json | --envelope]`
- `--envelope` emits envelope v1 for Op consumption (builder-op note):
  binding null (deterministic tooling cog), findings in `problems`,
  ok-with-problems semantics; exit codes unchanged.
- Model catalog: `pixi run mint-model-cog -- --config <yaml> --out-dir <dir>`
- Tests: `python3 -m unittest discover -s tests` — the model-free suite
  plus `tests/test_review_regressions.py` (one test per 2026-08-22 review
  finding; never delete these). The 3.10 floor is real and exercised: the
  device VM runs the suite on Python 3.10 at every delivery; pixi manifests
  declare `python = ">=3.10"` to match. Live loop: `tests/mock_model.py` +
  a minted cog's resolve/ask.

## Invariants

1. **Machinery is sacred:** template `src/` masters change only here, with
   MACHINERY.md updated; minted Cogs never edit them (task_logic.py is the
   sole author-owned src module). `smith check` enforces by hash.
2. **Envelope v1 is the emitted contract** (ENVELOPE.md): fixed `payload`
   key, structured problems, ok-may-carry-problems (gates decide), binding
   identity in every result. Changing it = versioning event, not an edit.
3. **Minted Cogs must be immediately runnable and immediately checkable:**
   `new` ends by running `check`; a template change that breaks
   fresh-mint PASS or the minted test suite is a regression.
4. **No new runtime deps** beyond python/pyyaml/jsonschema; keep the
   toml_compat fallback (3.10 floor).
5. Vocabulary: usage ops vs lifecycle ops; interfaces = entry points;
   never handler/runner/harness for client-side things. Contract checks =
   the Cog's own in-package validation of its declared contract
   (self-reported in `problems`); Guards are independent, first-class
   system-side verifiers of the SYSTEM's requirements — never call in-cog
   checks guards. (Some machinery docstrings still say "guard"; fix only
   with a deliberate machinery version bump, never inside a minted Cog.)
6. **Checker layers (review 2026-08-22, F1):** findings are labeled
   core / profile / runtime and the layers must never blur — cog.yaml
   is a PROFILE convention, not a core rule. Known gap, queued: compose
   the CogSpec reference validator into the core layer.
7. **Lockfile policy (F6):** pixi.lock files are generated ONLY on
   Trent's Mac (conda-forge is 403-blocked in the sandbox and device VM)
   and committed once generated; minted Cogs follow the same policy.
8. Fast-follow queue (do not start without Trent): derive-schema; draft
   (model-backed; adds a model-endpoint requirement when it lands);
   promote-draft (COG.md draft -> package, per CogSpec's own builder
   description); machinery diff/upgrade (hash checking needs a repair
   path before many cogs exist); honest starter variants
   (context-minimal, context-classification, complete, model);
   doctor. mint-model-cog shipped in v0.1; review hardening 2026-08-22.
