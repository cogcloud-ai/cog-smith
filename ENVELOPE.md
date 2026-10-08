# Result envelope v1 (the Collab profile, decided by template)

Every Cog created by cog-smith emits this envelope from its task entry
points. This is the concrete form of two decisions the spec deliberately
left to hosting environments (the Op–Cog seam and the capability-list
framing): the payload key and the pass/fail surface. It is **the Collab
profile's** answer — other environments may require otherwise; a Cog that
emits this envelope is compatible with Collab's seam.

Requirements-driven: the fields are exactly what Guards check, Gates read,
and Tracks record. Nothing else.

```json
{
  "envelope": 1,
  "cog":     {"id": "openteams/<name>", "version": "0.1.0"},
  "task":    "ask",
  "ok":      true,
  "error":   null,
  "payload": { ... },
  "raw":     "<verbatim model text>",
  "problems": [
    {"check": "grounding", "detail": "…not a verbatim span…", "severity": "error"}
  ],
  "binding": { "record": {...}, "model_echoed": "...", "model_identity":
               "verified|unverified|mismatch", "violations": [],
               "resolved_from": "model.json" },
  "timing":  {"latency_s": 2.31}
}
```

Field rules:

- **`envelope`** — the version discriminator for this contract (`1` today);
  consumers detect the envelope version here, not by sniffing field shapes.
- **`payload`** — the fixed key. The value's
  shape is the Cog's own `context/output-schema.json`; `null` when the run
  errored or output did not parse.
- **`ok`** — transport-level success: the invocation completed and produced
  a parseable payload. **`ok: true` MAY coexist with a non-empty
  `problems` list** — "passed with integrity problems" is deliberately
  legible, and deciding it is a Gate's job (human or reviewer-cog), never
  the Cog's (closes GAPS #4 for this profile).
- **`error`** — `null`, or `{code, detail}` with codes: `binding-invalid`,
  `invalid-input`, `model-unavailable`, `model-call-failed`,
  `model-response-malformed`. When set, `ok` is false and HTTP status maps
  4xx/5xx as in the forge Cogs (422 invalid-input; 502 upstream fault;
  503 unavailable/binding).
- **`problems`** — the Cog's self-reported contract-check findings,
  structured for Guards and Gates to consume: `check` (machine-readable
  category: `schema`, `grounding`, `citation`, `identity`, `input`, …),
  `detail` (human sentence), `severity` (`error` | `warning` | `warn`; the warning spellings are equivalent). These are
  produced by the Cog checking its OWN declared contract; an independent
  Guard verifies against the system's requirements and never treats this
  self-report as its verdict. `check_input` errors refuse the invocation;
  only input `warning` or `warn` findings allow work and remain in `problems`.
  Missing or unrecognised input severity refuses the invocation. `check_output`
  findings of either severity remain in `problems` for the Op's Gate to
  assess. A warning alone does not make `ok` false.
- **`binding`** — the Track fields: the complete binding identity copied
  into every result, so a saved result identifies its run (which pinned
  model, which endpoint, identity verdict, violations) without reading
  current installation state. Unchanged from the forge machinery.
- **`raw`** — the verbatim model text, for audit and salvage review.

Decision Cogs also emit optional **`provider_result`** after validating the
System One result. It preserves `{model, answer_source, answers, usage}` as
received, separately from the derived decision. Invalid answers do not populate
it. `raw` retains its existing text-or-null meaning. Decision `replay --result`
accepts this envelope or a bare System One result. This additive field keeps
envelope v1 unchanged.

Versioning: this file is `envelope: 1`. Additive changes (new optional
fields) do not bump; changing the meaning or type of an existing field
does. Consumers must ignore unknown fields.
