# cog-smith

The Cog builder — engineer-facing, "closer to a compiler." Mints complete,
immediately runnable Cogs from review-hardened machinery; validates them;
renders the catalog card consumers see.

**New to Cogs?** Start with [Building and Improving Cogs](BUILDING_COGS.md).
It explains what a Cog is, how Cogs are used, the complete build-and-test
workflow, and the rules coding agents should follow when improving one.

```bash
pixi install
pixi run new -- --dir ../cog-my-worker          # interactive builder questions
pixi run new -- --dir ../cog-my-worker --yes    # scripted, defaults
pixi run check -- ../cog-my-worker --envelope   # envelope-v1 JSON (Op seam)
cd ../cog-my-worker
pixi install && pixi run resolve && pixi run check -- --deep
pixi run ask -- --bundle examples/sample-bundle.json
```

A minted Cog ships with: manifest (in-manifest input schema — no overlays),
envelope-v1 entry points (web API + CLI), the forge binding/resolve/eval
machinery, a grounding contract check (verbatim quotes), an eval fixture, and
a model-free test suite that passes at mint time. Edit `context/` and
`src/task_logic.py`; everything else is shared machinery enforced by
`pixi run check` (“smith check”).

## Minting a hosting environment's model offering

```bash
pixi run mint-model-cog -- --config examples/model-catalog.yaml --out-dir ../models
```

One deployment-descriptor model cog per catalog entry (pattern:
cog-collab-qwen35b) — never a parameterized gateway cog, because identity
and pinning are per served model. Descriptors carry pinnable identity and
credential *references*; fixed endpoints must be https or loopback;
`address: install-time` defers the address to resolution. This is the
generator form of the hub model-selection work (see
output/model-cogs-hub-offering.md): consumers bind with
`pixi run resolve -- --satisfier <descriptor> [--endpoint URL]`, and the
binding record is where metering and audit attribution attach.

Docs: `BUILDING_COGS.md` (start-to-finish builder guide), `ENVELOPE.md`
(the result contract — the Collab profile, decided by template),
`MACHINERY.md` (provenance + deltas from cog-forge @7fe8aca), `COG.md`
(cog-smith as a Cog), `AGENTS.md` (contributor invariants).
