"""The artifact human Gate (Op machinery 0.7.0): a decision about ONE thing.

`gate: {policy: human, decides: artifact, artifact: {...}}` asks a person to
accept or reject a versioned artifact — a designed contract, a reviewed
candidate — bound to the digests the Op names for it. These tests are about
the Op layer: what it asks, what it binds the answer to, what it refuses,
and what a rejection does to the run. The Cog seam is faked.
"""
import json
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner, op_spec, op_track

CONTRACT = {"purpose": "Count characters.", "kind": "code",
            "acceptance_criteria": [{"id": "length"}]}


def artifact_spec(artifact=None, **design_extra):
    """design (artifact Gate over its designed contract) -> build."""
    design = fx.cog_step("design", gate={
        "policy": "human", "guards": [], "decides": "artifact",
        "artifact": artifact if artifact is not None else {
            "kind": "cog-contract",
            "id": {"$from": "inputs.name"},
            "summary": {"$from": "steps.design.payload.purpose"},
            "detail": {"questions": {"$from": "steps.design.payload.questions",
                                     "$default": []}},
            "digests": {"contract": {
                "$sha256": {"$from": "steps.design.payload.contract"}}}}})
    design["input"] = {"brief": {"$from": "inputs.note"}}
    design.update(design_extra)
    build = fx.cog_step("build", depends_on=["design"])
    build["input"] = {
        "contract": {"$from": "steps.design.payload.contract"},
        "contract_sha256": {
            "$from": "steps.design.decision.artifact.digests.contract"},
        "accepted_by": {"$from": "steps.design.decision.decided_by"}}
    return fx.spec_doc([design, build], inputs=[
        {"name": "note", "description": "the brief", "required": True},
        {"name": "name", "description": "the cog name", "required": True}],
        outputs={"verdict": {"$from": "steps.design.decision.verdict"},
                 "built": {"$from": "steps.build.payload"}})


def answers(contract=CONTRACT):
    def answer(_call, request):
        if "brief" in request:
            return fx.envelope(payload={"classification": "designed",
                                        "purpose": "Count characters",
                                        "questions": [],
                                        "contract": contract})
        return fx.envelope(payload={"built": request["contract_sha256"],
                                    "by": request["accepted_by"]})
    return {"ask": answer}


class ArtifactGateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        self.real_invoke = op_runner.invoke_cog
        fx.write_cog(self.root, "design")
        fx.write_cog(self.root, "build")

    def tearDown(self):
        op_runner.invoke_cog = self.real_invoke
        self.tmp.cleanup()

    def start(self, doc=None, contract=CONTRACT, fake=None):
        fx.write_package(self.package, doc if doc is not None
                         else artifact_spec())
        self.request = fx.write_request(
            self.root / "request.json",
            {"note": "count characters", "name": "cog-count"})
        self.fake = fake or fx.FakeCog(answers(contract))
        op_runner.invoke_cog = self.fake
        code, output = op_runner.run(self.package, self.request)
        return code, output, self.track(output)

    def resume(self, output, decision=None):
        code, out = op_runner.resume(
            self.package, output["run_dir"],
            decision_path=str(decision) if decision else None)
        return code, out, self.track(out)

    def track(self, output):
        return json.loads(Path(output["track"]).read_text())

    def step(self, track, sid):
        return next(s for s in track["steps"] if s["id"] == sid)

    def pending(self, output):
        return json.loads(Path(output["pending"]).read_text())

    def decide(self, output, verdict="accept", reason="looks right",
               **overrides):
        pending = self.pending(output)
        doc = {"schema": op_runner.DECISION_SCHEMA,
               "run_id": pending["run_id"], "step": pending["step"],
               "payload_sha256": pending["payload_sha256"],
               "artifact_sha256": pending.get("artifact_sha256"),
               "verdict": verdict, "reason": reason,
               "decided_by": "trent", "decided_at": op_track.utc_now()}
        doc.update(overrides)
        for key, value in list(doc.items()):
            if value is ...:
                del doc[key]
        path = self.root / "decision.json"
        path.write_text(json.dumps(doc))
        return path


class PauseTests(ArtifactGateCase):
    def test_the_pause_asks_about_the_artifact_and_its_digests(self):
        code, output, track = self.start()
        self.assertEqual(code, 3, output)
        pending = self.pending(output)
        self.assertEqual(pending["decides"], "artifact")
        artifact = pending["artifact"]
        self.assertEqual(artifact["kind"], "cog-contract")
        self.assertEqual(artifact["id"], "cog-count")
        self.assertEqual(artifact["summary"], "Count characters")
        self.assertEqual(artifact["detail"], {"questions": []})
        self.assertEqual(artifact["digests"],
                         {"contract": op_runner.canonical_sha256(CONTRACT)})
        self.assertEqual(pending["artifact_sha256"],
                         op_runner.canonical_sha256(artifact))
        self.assertEqual(pending["payload_sha256"],
                         op_runner.canonical_sha256(pending["payload"]))
        record = self.step(track, "design")
        self.assertEqual(record["status"], "awaiting-decision")
        self.assertEqual(record["gate"]["decides"], "artifact")
        self.assertEqual(record["gate"]["artifact_sha256"],
                         pending["artifact_sha256"])
        self.assertEqual(self.step(track, "build")["status"], "not-reached")
        self.assertEqual(len(self.fake.calls), 1)

    def test_the_sheet_names_the_kind_and_every_digest(self):
        code, output, _ = self.start()
        sheet = (Path(output["run_dir"]) / "pending" / "design.md").read_text()
        self.assertIn("cog\\-contract", sheet)
        self.assertIn(op_runner.canonical_sha256(CONTRACT), sheet)
        self.assertIn("A rejection ends the run", sheet)
        # every cell is literal text: no unescaped punctuation in a cell
        for line in sheet.splitlines():
            if line.startswith("| ") and not line.startswith("|---"):
                for cell_text in line.strip("|").split(" | "):
                    self.assertEqual(op_runner.cell(cell_text.strip()
                                                    .replace("\\", "")),
                                     cell_text.strip())

    def test_resume_without_a_decision_pauses_again(self):
        code, output, _ = self.start()
        code, out, track = self.resume(output)
        self.assertEqual(code, 3)
        self.assertEqual(track["status"], "paused")
        self.assertEqual(len(self.fake.calls), 1)

    def test_an_artifact_the_gate_cannot_state_is_a_failed_gate(self):
        code, output, track = self.start(contract=None)
        self.assertEqual(code, 1, output)
        record = self.step(track, "design")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["gate"]["status"], "fail")
        self.assertEqual(record["gate"]["decides"], "artifact")
        self.assertTrue(any("nothing to digest" in r
                            for r in record["gate"]["reasons"]),
                        record["gate"]["reasons"])
        self.assertEqual(track["status"], "failed")
        self.assertEqual(track["failed_step"], "design")
        self.assertFalse((Path(output["run_dir"]) / "pending").exists())
        # the resume point is the design step: it runs again and, with a
        # contract this time, pauses for the person
        self.fake.answers = answers(CONTRACT)
        code, out, track2 = self.resume(output)
        self.assertEqual(code, 3, out)
        self.assertEqual(len(self.fake.calls), 2)

    def test_a_digest_that_is_not_a_sha256_is_refused_not_repaired(self):
        doc = artifact_spec(artifact={
            "kind": "cog-contract",
            "digests": {"contract": {"$from": "steps.design.payload.purpose"}}})
        code, output, track = self.start(doc)
        self.assertEqual(code, 1)
        reasons = self.step(track, "design")["gate"]["reasons"]
        self.assertTrue(any("never repaired" in r for r in reasons), reasons)


class DecisionTests(ArtifactGateCase):
    def test_accept_continues_and_binds_the_next_step_to_the_digest(self):
        code, output, _ = self.start()
        code, out, track = self.resume(output, self.decide(output))
        self.assertEqual(code, 0, out)
        self.assertEqual(track["status"], "completed")
        record = self.step(track, "design")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["gate"]["status"], "pass")
        self.assertEqual(record["gate"]["decides"], "artifact")
        value = record["decision"]["value"]
        self.assertEqual(value["verdict"], "accept")
        self.assertEqual(value["decided_by"], "trent")
        self.assertEqual(value["artifact"]["digests"]["contract"],
                         op_runner.canonical_sha256(CONTRACT))
        self.assertEqual(value["artifact_sha256"],
                         self.pending(output)["artifact_sha256"])
        request = self.fake.calls[-1]["request"]
        self.assertEqual(request["contract_sha256"],
                         op_runner.canonical_sha256(CONTRACT))
        self.assertEqual(request["accepted_by"], "trent")
        self.assertEqual(out["outputs"]["verdict"], "accept")
        self.assertEqual(track["resumes"][0]["decision"],
                         record["gate"]["decision"])
        self.assertEqual(record["gate"]["decision_sha256"],
                         op_runner.sha256_file(record["gate"]["decision"]))

    def test_reject_ends_the_run_and_the_run_is_never_resumed(self):
        code, output, _ = self.start()
        code, out, track = self.resume(
            output, self.decide(output, "reject", "the criteria are vague"))
        self.assertEqual(code, 1, out)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["decided_by"], "trent")
        self.assertEqual(track["status"], "rejected")
        self.assertIsNone(track["failed_step"])
        self.assertTrue(track["ended_at"])
        record = self.step(track, "design")
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["gate"]["status"], "rejected")
        self.assertIn("rejected by trent: the criteria are vague",
                      record["gate"]["reasons"])
        self.assertEqual(record["decision"]["value"]["verdict"], "reject")
        self.assertEqual(self.step(track, "build")["status"], "not-reached")
        self.assertEqual(len(self.fake.calls), 1)        # build never ran
        self.assertTrue((Path(out["run_dir"]) / "decisions" / "design.json")
                        .exists())
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(out)
        self.assertIn("rejection is final",
                      "\n".join(caught.exception.problems))
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_rejection_without_a_reason_is_refused(self):
        code, output, _ = self.start()
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, self.decide(output, "reject", "  "))
        self.assertIn("no reason", "\n".join(caught.exception.problems))
        self.assertEqual(self.track(output)["status"], "paused")

    def test_a_decision_about_another_artifact_is_refused(self):
        code, output, _ = self.start()
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, self.decide(output, artifact_sha256="0" * 64))
        self.assertIn("decided about something else",
                      "\n".join(caught.exception.problems))

    def test_an_artifact_changed_on_disk_invalidates_the_decision(self):
        code, output, _ = self.start()
        pending_path = Path(output["pending"])
        pending = json.loads(pending_path.read_text())
        pending["artifact"]["digests"]["contract"] = "f" * 64
        pending["artifact_sha256"] = op_runner.canonical_sha256(
            pending["artifact"])
        pending_path.write_text(json.dumps(pending))
        # a decision written from the edited pending file, digests and all
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, self.decide(output))
        self.assertIn("artifact changed on disk",
                      "\n".join(caught.exception.problems))
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_verdict_outside_accept_reject_is_refused(self):
        code, output, _ = self.start()
        for verdict in ("approve", "edit", None):
            with self.assertRaises(op_spec.OpSpecError) as caught:
                self.resume(output, self.decide(output, verdict=verdict))
            self.assertIn("whole", "\n".join(caught.exception.problems))

    def test_a_changes_style_decision_is_refused_for_an_artifact(self):
        code, output, _ = self.start()
        decision = self.decide(output, verdict=..., artifact_sha256=...,
                               decisions=[{"change_id": "c", "verdict":
                                           "approve"}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        text = "\n".join(caught.exception.problems)
        self.assertIn("decisions list", text)
        self.assertIn("something else", text)

    def test_a_decision_from_another_run_or_step_is_refused(self):
        code, output, _ = self.start()
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, self.decide(output, run_id="other"))
        self.assertIn("not for", "\n".join(caught.exception.problems))
        with self.assertRaises(op_spec.OpSpecError):
            self.resume(output, self.decide(output, step="build"))

    def test_who_and_when_are_required(self):
        code, output, _ = self.start()
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, self.decide(output, decided_by=" "))
        self.assertIn("who decided", "\n".join(caught.exception.problems))

    def test_an_artifact_verdict_on_a_changes_gate_is_refused(self):
        compose = fx.cog_step("compose", gate={"policy": "human",
                                               "guards": []})
        doc = fx.spec_doc([compose], inputs=[
            {"name": "note", "description": "a note", "required": True},
            {"name": "name", "description": "a name", "required": True}])
        fx.write_cog(self.root, "compose")
        fake = fx.FakeCog({"ask": fx.envelope(
            payload={"changes": [fx.change("c-a")]})})
        code, output, _ = self.start(doc, fake=fake)
        self.assertEqual(code, 3)
        pending = self.pending(output)
        self.assertEqual(pending["decides"], "changes")
        self.assertNotIn("artifact", pending)
        decision = self.decide(output, artifact_sha256=...)
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("asks about proposed changes",
                      "\n".join(caught.exception.problems))


class LoadTimeTests(unittest.TestCase):
    def problems(self, doc):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        return "\n".join(caught.exception.problems)

    def test_a_well_formed_artifact_gate_loads(self):
        op_spec.validate(artifact_spec())

    def test_decides_is_closed(self):
        doc = artifact_spec()
        doc["steps"][0]["gate"]["decides"] = "vibes"
        self.assertIn("decides about one of", self.problems(doc))

    def test_decides_belongs_to_a_human_gate(self):
        doc = artifact_spec()
        doc["steps"][0]["gate"]["policy"] = op_spec.GATE_POLICY
        self.assertIn("only a human Gate decides", self.problems(doc))

    def test_an_artifact_gate_declares_its_artifact(self):
        doc = artifact_spec()
        del doc["steps"][0]["gate"]["artifact"]
        self.assertIn("no gate.artifact", self.problems(doc))

    def test_a_changes_gate_declares_no_artifact(self):
        doc = artifact_spec()
        doc["steps"][0]["gate"]["decides"] = "changes"
        self.assertIn("decides about changes", self.problems(doc))

    def test_the_artifact_vocabulary_is_closed(self):
        doc = artifact_spec(artifact={"kind": "x", "digests": {"a": "b"},
                                      "colour": "blue"})
        self.assertIn("unknown key 'colour'", self.problems(doc))

    def test_kind_and_digests_are_required(self):
        doc = artifact_spec(artifact={"summary": "x"})
        text = self.problems(doc)
        self.assertIn("declares no kind", text)
        self.assertIn("declares no digests", text)

    def test_digests_is_a_non_empty_object(self):
        for digests in ({}, [], {"$from": "inputs.note"}):
            doc = artifact_spec(artifact={"kind": "x", "digests": digests})
            self.assertIn("non-empty object", self.problems(doc))

    def test_an_artifact_may_read_its_own_payload_but_not_its_decision(self):
        doc = artifact_spec(artifact={
            "kind": "x", "digests": {"a": {
                "$sha256": {"$from": "steps.design.decision.verdict"}}}})
        self.assertIn("has no human Gate", self.problems(doc))

    def test_an_artifact_reads_only_dependencies(self):
        doc = artifact_spec(artifact={
            "kind": "x", "digests": {"a": {
                "$sha256": {"$from": "steps.build.payload"}}}})
        self.assertIn("without depending on 'build'", self.problems(doc))

    def test_a_write_cannot_be_granted_from_an_artifact_decision(self):
        doc = artifact_spec()
        doc["steps"][1]["authority"] = {"requires": [
            {"resource": "github", "action": "write",
             "changes": {"$from": "steps.design.decision.approved"}}]}
        doc["steps"][1]["cog"]["task"] = "run"
        self.assertIn("carries no approved changes", self.problems(doc))

    def test_repeat_is_still_refused_on_an_artifact_gate(self):
        doc = artifact_spec(repeat={"count": 2, "require": 1})
        self.assertIn("human decides about ONE", self.problems(doc))


class Sha256OperatorTests(unittest.TestCase):
    def test_sha256_is_the_canonical_digest(self):
        ctx = {"inputs": {"c": {"b": 1, "a": [1, 2]}}, "steps": {},
               "run": {}, "request": {}}
        self.assertEqual(
            op_spec.evaluate({"$sha256": {"$from": "inputs.c"}}, ctx),
            op_runner.canonical_sha256({"a": [1, 2], "b": 1}))
        self.assertEqual(op_spec.evaluate({"$sha256": "x"}, ctx),
                         op_runner.canonical_sha256("x"))

    def test_sha256_of_nothing_is_refused(self):
        ctx = {"inputs": {"c": None}, "steps": {}, "run": {}, "request": {}}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.evaluate({"$sha256": {"$from": "inputs.c"}}, ctx)
        self.assertIn("nothing to digest", "\n".join(caught.exception.problems))
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$sha256": {"$from": "inputs.missing",
                                          "$default": None}}, ctx)

    def test_sha256_with_a_sibling_key_is_data_and_an_unknown_op_is_refused(self):
        doc = artifact_spec()
        doc["steps"][1]["input"]["extra"] = {"$sha256": "x", "label": "y"}
        op_spec.validate(doc)
        doc["steps"][1]["input"]["extra"] = {"$digest": "x"}
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.validate(doc)


if __name__ == "__main__":
    unittest.main()
