# cog-smith

The Cog builder — engineer-facing, "closer to a compiler." Mints complete,
immediately runnable Cogs from review-hardened machinery; validates them;
renders the catalog card consumers see.

```bash
pixi install
pixi run new -- --dir ../cog-my-worker          # interactive builder questions
pixi run new -- --dir ../cog-my-worker --yes    # scripted, defaults
cd ../cog-my-worker
pixi install && pixi run resolve && pixi run check -- --deep
pixi run ask -- --bundle examples/sample-bundle.json
```

A minted Cog ships with: manifest (in-manifest input schema — no overlays),
envelope-v1 entry points (web API + CLI), the forge binding/resolve/eval
machinery, a grounding guard (verbatim-quote checks), an eval fixture, and
a model-free test suite that passes at mint time. Edit `context/` and
`src/task_logic.py`; everything else is shared machinery enforced by
`smith check`.

Docs: `ENVELOPE.md` (the result contract — the Collab profile, decided by
template), `MACHINERY.md` (provenance + deltas from cog-forge @7fe8aca),
`COG.md` (cog-smith as a Cog), `AGENTS.md` (contributor invariants).
