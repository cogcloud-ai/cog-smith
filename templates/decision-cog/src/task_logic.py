"""THE per-cog module of a decision Cog — the only file in src/ you edit.

Everything else in src/ is shared cog-smith decision-cog machinery,
byte-identical across created decision Cogs and verified by `smith check`.
Your Cog's identity lives in:
  - the manifest — [tool.cog] in pixi.toml, or cog.yaml
  - context/questions.json (the typed questions a System One model answers)
  - context/*-schema.json (input shape; `$defs.decision` in the output schema
    is yours, `$defs.answers` is derived: `pixi run derive-schema`)
  - this file (what state is sent, and how typed answers become a decision)

The starter is a working toy: a support ticket goes in; the department,
frustration and urgency questions come back typed and calibrated; a routing
decision comes out, with low-confidence routing sent to a person. Replace it.

The division of labour is deliberate. The model answers narrow, typed
questions with probabilities; THIS code makes the decision from them, with
thresholds you can read, test and change. Keep judgment in the questions and
policy in the code.
"""

# Policy thresholds. Tune them against real answers for the model version you
# pin; TypeSafe advises pinning a versioned model ID once thresholds are tuned.
MIN_ROUTE_CONFIDENCE = 0.5
URGENT_PROBABILITY = 0.7
ESCALATE_FRUSTRATION = 1.5


def check_input(bundle):
    """Input checks beyond the declared input schema. Return a list of
    cog_core.problem(...) dicts (import inside to avoid a load-time cycle)."""
    import cog_core
    problems = []
    if not str(bundle.get("body", "")).strip():
        problems.append(cog_core.problem("input", "body must contain text"))
    return problems


def state(bundle):
    """The state the model evaluates: text, or a JSON object whose field names
    your questions can point at in backticks. Send only what the questions
    need; state leaves the machine when the admitted provider is remote."""
    return {"subject": bundle.get("subject", ""), "body": bundle["body"]}


def questions(bundle, declared):
    """The question set for this input. `declared` is context/questions.json.
    You may adjust wording per input; ids, types, options and levels must stay
    as declared (the machinery refuses anything else)."""
    return declared


def decide(bundle, answers):
    """Typed answers -> this Cog's decision (`$defs.decision`)."""
    department = answers["department"]
    urgency = answers["is_urgent"]["noul"]
    frustration = answers["frustration"]["score"]
    reasons = []
    route = department["choice"]
    if department["confidence"] < MIN_ROUTE_CONFIDENCE:
        route = "human-review"
        reasons.append(f"department confidence {department['confidence']:.2f} "
                       f"is below {MIN_ROUTE_CONFIDENCE}")
    escalate = urgency >= URGENT_PROBABILITY or frustration >= ESCALATE_FRUSTRATION
    if urgency >= URGENT_PROBABILITY:
        reasons.append(f"urgency probability {urgency:.2f}")
    if frustration >= ESCALATE_FRUSTRATION:
        reasons.append(f"frustration score {frustration:.2f}")
    return {"ticket_id": bundle["ticket_id"], "route": route,
            "escalate": escalate, "reasons": reasons}


def check_output(payload, bundle):
    """Contract checks — the Cog's OWN validation, reported in `problems`.
    A Gate decides what they mean; this Cog never does."""
    import cog_core
    problems = []
    decision = payload["decision"]
    if decision["ticket_id"] != bundle["ticket_id"]:
        problems.append(cog_core.problem("consistency", "decision names a different ticket"))
    if decision["route"] == "human-review" and not decision["reasons"]:
        problems.append(cog_core.problem("consistency", "human review needs a stated reason"))
    return problems
