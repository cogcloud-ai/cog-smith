# AGENTS.md — cog-smith contributor instructions

Read `../CLAUDE.md` for workstream context. cog-smith is itself a Cog (see
COG.md); it is also the single source of the machinery every minted Cog
carries.

## Commands

- Mint: `pixi run new -- --dir ../cog-<name> [--yes ...flags]`
- Validate: `pixi run check -- <path> [--tests]`
- Card: `pixi run card -- <path> [--json]`
- Tests: `python3 -m unittest discover -s tests` (13, model-free; must pass
  on Python 3.10). Live loop: `tests/mock_model.py` + a minted cog's
  resolve/ask.

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
   never handler/runner/harness for client-side things.
6. Fast-follow queue (do not start without Trent): derive-schema,
   mint-model-cog, draft (model-backed; adds a model-endpoint requirement
   to cog.yaml when it lands).
