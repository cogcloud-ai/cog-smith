# cog-smith

The Cog builder — engineer-facing, "closer to a compiler." Creates complete,
immediately runnable Cogs from review-hardened machinery; validates them;
renders the catalog card consumers see.

**Composing Cogs into a workflow?** That is an Op — see
[Building Ops](BUILDING_OPS.md) (`smith op new --from-spec`, `smith op check`).

**New to Cogs?** Start with [Building and Improving Cogs](BUILDING_COGS.md).
It explains what a Cog is, how Cogs are used, the complete build-and-test
workflow, and the rules coding agents should follow when improving one.

```bash
pixi install
pixi run new -- --dir ../cog-my-worker          # interactive builder questions
pixi run new -- --dir ../cog-my-worker --yes    # scripted, defaults
pixi run new -- --dir ../cog-my-worker --yes --manifest yaml  # standalone cog.yaml
                                                # instead of [tool.cog] in pixi.toml
pixi run check -- ../cog-my-worker --envelope   # envelope-v1 JSON (Op seam)
pixi run migrate -- ../cog-older-worker          # cog.yaml -> [tool.cog] in pixi.toml,
                                                # re-sync machinery (--dry-run to preview)
pixi run new -- --from-request request.json --envelope  # create from a drafting
                                                # cog's request (see examples/)
cd ../cog-my-worker
pixi install && pixi run resolve && pixi run check -- --deep
pixi run ask -- --bundle examples/sample-bundle.json
```

A created Cog ships with: manifest (in-manifest input schema — no overlays),
envelope-v1 entry points (web API + CLI), the forge binding/resolve/eval
machinery, a grounding contract check (verbatim quotes), an eval fixture, and
a model-free test suite that passes at creation time. Edit `context/` and
`src/task_logic.py`; everything else is shared machinery enforced by
`pixi run check` (“smith check”).

**Manifest format.** By default the profile manifest (`openteams/cog-manifest
[0.1]`) is written into `pixi.toml` under `[tool.cog]` — one file that Nebi
already publishes, with `version` and the summary stated once in
`[workspace]` (cog-execution ADR D9). `--manifest yaml` writes the standalone
`cog.yaml` instead. A package carries exactly one; COG.md's `manifest:`
pointer names it, and `check`, `card`, and the created Cog's own machinery
read either. `migrate` converts an existing package in either direction
(`--to yaml` for the reverse) without regenerating pixi.toml — comments,
tasks, and dependencies stay as they are.

## Creating a hosting environment's model offering

```bash
pixi run generate-descriptors -- --config examples/model-catalog.yaml --out-dir ../models
```

The catalog can be generated from a hub's llm-serving-pack `LLMModel` CRs
(files, directories, or `kubectl get llmmodels -o yaml | ...`):

```bash
python scripts/llmmodel_catalog.py path/to/models/ \
  --surface internal --base-domain cluster.example.com -o /tmp/catalog.yaml
pixi run generate-descriptors -- --config /tmp/catalog.yaml --out-dir ../models
```

The generator is pack-neutral (stdlib + pyyaml, no smith imports) so it can
move into a hub-side pack unchanged.

One deployment-descriptor model cog per catalog entry (pattern:
cog-collab-qwen35b) — never a parameterized gateway cog, because identity
and pinning are per served model. Descriptors carry pinnable identity and
credential *references*; fixed endpoints must be https or loopback;
`address: install-time` defers the address to resolution. This is the
generator form of the hub model-selection work (see
output/model-cogs-hub-offering.md): consumers bind with
`pixi run resolve -- --satisfier <descriptor> [--endpoint URL]`, and the
binding record is where metering and audit attribution attach.

Docs: `BUILDING_COGS.md` (start-to-finish builder guide), `BUILDING_OPS.md`
(the Op half: spec, runner, Track), `ENVELOPE.md`
(the result contract — the Collab profile, decided by template),
`MACHINERY.md` (provenance + deltas from cog-forge @7fe8aca), `COG.md`
(cog-smith as a Cog), `AGENTS.md` (contributor invariants).
