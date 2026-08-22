---
type: cog [0.1]
name: cog-smith
description: Context Cog. The Cog builder — mints complete runnable Cogs from review-hardened machinery, validates them, and renders their catalog cards. Engineer-facing ("closer to a compiler").
version: "0.1.0"
license: BSD-3-Clause
publisher: OpenTeams
manifest: cog.yaml
manifest_schema: openteams/cog-manifest [0.1]
---

# Cog Smith

The builder for the Cogs evaluation line. Audience: engineers (per the
2026-08-20 spec sync — op builders serve non-technical users; the cog
builder "should assume some amount of technical need"; "closer to a
compiler").

## Ops

- `new` — mint a Cog: interactive (Travis's builder questions) or flag-driven
  (`--yes`). The minted package is immediately runnable: resolve → check
  --deep → ask, with the envelope-v1 contract, in-manifest input schema,
  grounding guard, fixtures, and a model-free test suite included.
- `check` — validate any minted Cog: manifest integrity, schema/example
  agreement, machinery copy-sync against cog-smith's masters,
  interface/task consistency; `--tests` also runs the Cog's own suite.
- `card` — the catalog card: exactly what CogCloud and the Op builder's
  picker would render, derived from declarations only.

## Where the machinery comes from

`templates/context-cog/src/` holds the masters: `cog_binding.py`,
`cog_resolve.py`, `cog_use.py` verbatim from cog-forge @7fe8aca
(engineering-gates PASS); `cog_eval.py` with marked envelope-v1 deltas;
`cog_core.py` / `cog_api.py` / `cog_cli.py` genericized so that ALL
task-specific logic lives in the author-owned `task_logic.py`. See
MACHINERY.md for the delta record and ENVELOPE.md for the result contract.

## Roadmap (fast follows)

`derive-schema` (argparse → input-schema/x-cog-param), `mint-model-cog`
(descriptor model cogs from a gateway config), `draft` (model-backed
authoring of summary/context/io from a role description).
