"""Authority, the human Gate, resume: the phase 3 runner semantics.

Contract: planning/current/phase3-contract.md §2, §3, §5 and the cog-smith
bullets of §7. The Cog seam is faked (`invoke_cog` is monkeypatched), so
these tests are about the Op layer's decisions: what it issues, what it
denies, what it pauses for, and what it records. What a Cog does with a
grant is tests/test_code_cog.py.

Nothing here is enforcement: the runner issues and records, and the code Cog
checks its own grant before it reaches outside the run (contract §0).
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner, op_spec, op_track

REPO = fx.REPO
OTHER_REPO = "someone-else/private"


def spec_doc(**extra):
    """read-github -> compose (human Gate) -> write-github."""
    read = fx.cog_step("read-github", authority={
        "requires": [{"resource": "github", "action": "read",
                      "repositories": {"$from": "inputs.repositories"}}]})
    read["input"] = {"repositories": {"$from": "inputs.repositories"}}
    compose = fx.cog_step("compose", depends_on=["read-github"],
                          gate={"policy": "human", "guards": []})
    compose["input"] = {"items": {"$from": "steps.read-github.payload"}}
    write = fx.cog_step("write-github", depends_on=["compose"], authority={
        "requires": [{"resource": "github", "action": "write",
                      "changes": {"$from": "steps.compose.decision.approved"}}]})
    write["input"] = {"changes": {"$from": "steps.compose.decision.approved"}}
    doc = fx.spec_doc(
        [read, compose, write],
        inputs=[{"name": "repositories", "description": "the read scope",
                 "required": True}])
    doc.update(extra)
    return doc


def by_request(changes=("c-a",)):
    """Answers chosen by what the step ASKED for, so a re-run step gets the
    same answer it got the first time."""
    def answer(_call, request):
        if "repositories" in request:
            return fx.envelope(payload={"items": [1, 2]})
        if "items" in request:
            return fx.envelope(payload={"changes": [fx.change(c)
                                                    for c in changes]})
        return fx.envelope(payload={"applied": list(changes)})
    return {"ask": answer}


def envelopes(changes=("c-a",)):
    return {"ask": [fx.envelope(payload={"items": [1, 2]}),
                    fx.envelope(payload={"changes": [fx.change(c)
                                                     for c in changes]}),
                    fx.envelope(payload={"applied": list(changes)})]}


class AuthorityCase(unittest.TestCase):
    """A package, three Cog sources, an admission and a fake seam."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        self.real_invoke = op_runner.invoke_cog
        fx.write_cog(self.root, "read-github",
                     reaches=[{"resource": "github", "actions": ["read"]}])
        fx.write_cog(self.root, "compose")
        fx.write_cog(self.root, "write-github",
                     reaches=[{"resource": "github", "actions": ["write"]}])

    def tearDown(self):
        op_runner.invoke_cog = self.real_invoke
        self.tmp.cleanup()

    def start(self, doc=None, answers=None, repositories=(REPO,),
              authority=None, request=None):
        fx.write_package(self.package, doc if doc is not None else spec_doc())
        self.request = fx.write_request(
            self.root / "request.json",
            request if request is not None else
            {"repositories": list(repositories)})
        self.authority_path = None
        if authority is not False:
            self.authority_path = self.root / "authority.json"
            self.authority_path.write_text(json.dumps(
                authority if authority is not None else fx.authority_doc()))
        self.fake = fx.FakeCog(answers if answers is not None else envelopes())
        op_runner.invoke_cog = self.fake
        code, output = op_runner.run(
            self.package, self.request,
            authority_path=str(self.authority_path)
            if self.authority_path else None)
        return code, output, self.track(output)

    def resume(self, output, decision=None):
        code, out = op_runner.resume(
            self.package, output["run_dir"],
            decision_path=str(decision) if decision else None,
            authority_path=str(self.authority_path)
            if self.authority_path else None)
        return code, out, self.track(out)

    def track(self, output):
        return json.loads(Path(output["track"]).read_text())

    def step(self, track, sid):
        return next(s for s in track["steps"] if s["id"] == sid)

    def pending(self, output):
        return json.loads(Path(output["pending"]).read_text())

    def decide(self, output, verdicts, **overrides):
        pending = self.pending(output)
        doc = fx.decision_doc(pending["run_id"], pending["step"],
                              pending["payload_sha256"], verdicts)
        doc.update(overrides)
        path = self.root / "decision.json"
        path.write_text(json.dumps(doc))
        return path


# ============================================================= authority ==

class AuthorityTests(AuthorityCase):
    def test_a_configured_repository_read_is_issued(self):
        code, output, track = self.start()
        self.assertEqual(code, op_runner.PAUSED_EXIT)      # pauses at compose
        record = self.step(track, "read-github")
        self.assertEqual(record["status"], "passed")
        grant = json.loads(Path(record["grant"]).read_text())
        self.assertEqual(grant["schema"], op_runner.GRANT_SCHEMA)
        self.assertEqual(grant["operations"],
                         [{"resource": "github", "action": "read",
                           "repositories": [REPO]}])
        self.assertEqual(grant["recipient"]["cog"]["id"],
                         "openteams/cog-read-github")
        self.assertEqual(grant["issued_by"]["kind"], "admission")
        self.assertEqual(grant["valid"]["run_id"], track["run_id"])
        # the grant reaches the Cog BESIDE the request, never inside it
        seam = self.fake.calls[0]["seam"]
        self.assertEqual(seam["grant_path"], record["grant"])
        self.assertEqual(seam["run_id"], track["run_id"])
        self.assertNotIn("grant", self.fake.calls[0]["request"])

    def test_the_track_records_grant_scope_and_provenance(self):
        _, output, track = self.start()
        self.assertEqual(len(track["grants"]), 1)
        recorded = track["grants"][0]
        self.assertEqual(recorded["step"], "read-github")
        self.assertEqual(recorded["operations"],
                         [{"resource": "github", "action": "read", "count": 1}])
        self.assertEqual(recorded["issued_by"]["kind"], "admission")
        self.assertEqual(Path(track["authority"]["path"]),
                         self.authority_path.resolve())

    def test_an_out_of_scope_read_is_denied_and_the_step_never_invoked(self):
        code, output, track = self.start(repositories=(REPO, OTHER_REPO))
        self.assertEqual(code, 1)
        record = self.step(track, "read-github")
        self.assertEqual(record["status"], "denied")
        self.assertIn(OTHER_REPO, record["gate"]["reasons"][0])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(track["status"], "failed")
        self.assertIsNone(record["grant"])

    def test_no_authority_refuses_any_step_that_requires_some(self):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(authority=False)
        text = "\n".join(caught.exception.problems)
        self.assertIn("read-github", text)
        self.assertIn("requires github read", text)
        self.assertIn("admitted with none", text)

    def test_an_admission_without_the_operation_is_refused_at_load(self):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(authority=fx.authority_doc(read=None))
        self.assertIn("admission does not carry",
                      "\n".join(caught.exception.problems))

    def test_a_write_before_the_human_gate_is_refused_at_load(self):
        doc = spec_doc()
        doc["steps"][1]["gate"] = {"policy": op_spec.GATE_POLICY, "guards": []}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("has no human Gate", "\n".join(caught.exception.problems))

    def test_a_write_that_does_not_depend_on_its_gate_is_refused_at_load(self):
        doc = spec_doc()
        doc["steps"][2]["depends_on"] = ["read-github"]
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("without depending on it",
                      "\n".join(caught.exception.problems))

    def test_a_write_requirement_must_read_a_decision(self):
        doc = spec_doc()
        doc["steps"][2]["authority"]["requires"][0]["changes"] = {
            "$from": "steps.compose.payload.changes"}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("without reading a human decision",
                      "\n".join(caught.exception.problems))

    def test_grants_is_not_a_mapping_root(self):
        doc = spec_doc()
        doc["steps"][2]["input"] = {"g": {"$from": "grants.write-github"}}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("a grant is never readable from a mapping expression",
                      "\n".join(caught.exception.problems))

    def test_authority_on_a_foreach_step_is_refused_by_name(self):
        doc = spec_doc()
        doc["steps"][0]["foreach"] = {"items": {"$from": "inputs.repositories"},
                                      "as": "repo"}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("issues no per-element grants",
                      "\n".join(caught.exception.problems))

    def test_authority_on_a_non_code_cog_is_refused_by_name(self):
        fx.write_cog(self.root, "read-github", kind="context",
                     reaches=[{"resource": "github", "actions": ["read"]}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start()
        self.assertIn("authority on code Cogs only",
                      "\n".join(caught.exception.problems))

    def test_a_requirement_outside_the_cogs_reaches_is_refused(self):
        fx.write_cog(self.root, "read-github",
                     reaches=[{"resource": "github", "actions": ["write"]}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start()
        self.assertIn("does not declare in its reaches",
                      "\n".join(caught.exception.problems))

    def test_a_reaching_cog_invoked_without_authority_is_refused(self):
        doc = spec_doc()
        del doc["steps"][0]["authority"]
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(doc=doc)
        self.assertIn("runs only under a grant",
                      "\n".join(caught.exception.problems))


class WriteGrantTests(AuthorityCase):
    def approve_and_continue(self, verdicts, changes=("c-a", "c-b")):
        code, output, _ = self.start(answers=envelopes(changes))
        self.assertEqual(code, op_runner.PAUSED_EXIT)
        decision = self.decide(output, verdicts)
        return self.resume(output, decision)

    def test_approving_a_issues_a_grant_for_a_only(self):
        code, output, track = self.approve_and_continue(
            {"c-a": "approve", "c-b": "reject"})
        self.assertEqual(code, 0, output)
        record = self.step(track, "write-github")
        self.assertEqual(record["status"], "passed")
        grant = json.loads(Path(record["grant"]).read_text())
        write = grant["operations"][0]
        self.assertEqual([c["change_id"] for c in write["changes"]], ["c-a"])
        self.assertEqual(grant["issued_by"]["kind"], "gate")
        self.assertEqual(grant["issued_by"]["step"], "compose")
        self.assertEqual(grant["issued_by"]["decision_sha256"],
                         op_runner.sha256_file(grant["issued_by"]["decision"]))

    def test_a_step_requiring_an_unapproved_change_is_denied(self):
        # The step asks to write the changes the human REJECTED. The
        # requirement reads the decision, as every write requirement must —
        # and asking for anything outside the approved list is a denial,
        # never a trim.
        doc = spec_doc()
        doc["steps"][2]["authority"]["requires"][0]["changes"] = {
            "$from": "steps.compose.decision.rejected"}
        code, output, _ = self.start(doc=doc, answers=envelopes(("c-a", "c-b")))
        decision = self.decide(output, {"c-a": "approve", "c-b": "reject"})
        code, out, track = self.resume(output, decision)
        record = self.step(track, "write-github")
        self.assertEqual(record["status"], "denied")
        self.assertIn("c-b", record["gate"]["reasons"][0])
        self.assertIn("did not approve", record["gate"]["reasons"][0])
        self.assertEqual(code, 1)

    def test_an_edited_change_is_re_hashed_and_that_hash_is_granted(self):
        edited = dict(fx.change("c-a"), summary="a better summary")
        code, output, track = self.approve_and_continue(
            {"c-a": edited, "c-b": "reject"})
        self.assertEqual(code, 0, output)
        grant = json.loads(
            Path(self.step(track, "write-github")["grant"]).read_text())
        granted = grant["operations"][0]["changes"][0]
        self.assertEqual(granted["change_id"], "c-a")
        self.assertEqual(granted["content_sha256"],
                         op_runner.change_content_sha256(edited))
        self.assertNotEqual(granted["content_sha256"], "0" * 64)
        self.assertIn("c-a", self.step(track, "compose")["decision"]["value"]
                      ["edited"])

    def test_a_change_for_an_unadmitted_repository_is_denied(self):
        code, output, _ = self.start(
            answers={"ask": [fx.envelope(payload={"items": []}),
                             fx.envelope(payload={"changes": [
                                 fx.change("c-a", repository=OTHER_REPO)]}),
                             fx.envelope(payload={})]})
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        record = self.step(track, "write-github")
        self.assertEqual(record["status"], "denied")
        self.assertIn("not admitted to write", record["gate"]["reasons"][0])

    def test_a_tampered_decision_record_releases_no_write_grant(self):
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        # rewrite the copied decision record and resume again from scratch
        copied = Path(self.step(track, "compose")["decision"]["decision"])
        copied.write_text(copied.read_text() + "\n")
        record = self.step(track, "write-github")
        record["status"] = "not-reached"      # pretend the write never ran
        Path(out["track"]).write_text(json.dumps(track))
        code, out2, track2 = self.resume(out)
        self.assertEqual(self.step(track2, "write-github")["status"], "denied")
        self.assertIn("changed since the Track recorded it",
                      self.step(track2, "write-github")["gate"]["reasons"][0])

    def test_a_failed_gate_releases_no_downstream_write_grant(self):
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(ok=False),
                           fx.envelope(payload={})]}
        code, output, track = self.start(answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(self.step(track, "compose")["status"], "failed")
        self.assertEqual(self.step(track, "write-github")["status"],
                         "not-reached")
        self.assertEqual([g["step"] for g in track["grants"]], ["read-github"])
        self.assertFalse((Path(output["run_dir"]) / "grants"
                          / "write-github.json").exists())

    def test_a_denied_read_releases_no_downstream_write_grant(self):
        doc = spec_doc()
        doc["steps"][0]["on_fail"] = "skip"
        code, output, track = self.start(doc=doc,
                                         repositories=(OTHER_REPO,))
        self.assertEqual(self.step(track, "read-github")["status"], "denied")
        self.assertEqual(self.step(track, "write-github")["status"], "blocked")
        self.assertEqual(track["grants"], [])         # neither step got one

    def test_the_journal_is_created_for_a_granted_step_and_recorded(self):
        code, output, track = self.approve_and_continue({"c-a": "approve",
                                                         "c-b": "reject"})
        record = self.step(track, "write-github")
        self.assertTrue(Path(record["journal"]).exists())
        self.assertEqual(Path(record["journal"]).name, "write-github.jsonl")
        seam = self.fake.calls[-1]["seam"]
        self.assertEqual(seam["journal_path"], record["journal"])


# ============================================================ human gate ==

class HumanGateTests(AuthorityCase):
    def test_pause_writes_pending_files_and_exits_three(self):
        code, output, track = self.start()
        self.assertEqual(code, 3)
        self.assertEqual(output["ok"], False)
        self.assertEqual(output["status"], "paused")
        self.assertEqual(set(output) >= {"ok", "status", "run_dir", "pending"},
                         True)
        run_dir = Path(output["run_dir"])
        self.assertTrue((run_dir / "pending" / "compose.json").exists())
        self.assertTrue((run_dir / "pending" / "compose.md").exists())
        pending = self.pending(output)
        self.assertEqual(pending["schema"], op_runner.PENDING_SCHEMA)
        self.assertEqual(pending["step"], "compose")
        self.assertEqual(pending["payload_sha256"],
                         op_runner.canonical_sha256(pending["payload"]))
        self.assertIn("c-a", (run_dir / "pending" / "compose.md").read_text())
        self.assertEqual(track["status"], "paused")
        record = self.step(track, "compose")
        self.assertEqual(record["status"], "awaiting-decision")
        self.assertEqual(record["gate"]["policy"], "human")
        self.assertEqual(record["gate"]["status"], "pending")
        self.assertTrue(record["gate"]["asked_at"])
        # the write step was never reached, and no write grant exists
        self.assertEqual(self.step(track, "write-github")["status"],
                         "not-reached")
        self.assertEqual(len(self.fake.calls), 2)

    def test_only_a_passing_envelope_reaches_the_human(self):
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": []},
                                       problems=[{"severity": "error",
                                                  "detail": "bad"}]),
                           fx.envelope(payload={})]}
        code, output, track = self.start(answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(self.step(track, "compose")["status"], "failed")
        self.assertFalse((Path(output["run_dir"]) / "pending").exists())

    def test_a_human_gated_payload_without_changes_is_refused(self):
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"nothing": True}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("must carry a `changes` list",
                      "\n".join(caught.exception.problems))

    def test_resume_with_a_mismatched_payload_sha256_is_refused(self):
        code, output, _ = self.start()
        decision = self.decide(output, {"c-a": "approve"},
                               payload_sha256="0" * 64)
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("decided about something else",
                      "\n".join(caught.exception.problems))

    def test_a_decision_cannot_add_a_change_that_was_not_proposed(self):
        code, output, _ = self.start()
        decision = self.decide(output, {"c-a": "approve", "c-invented":
                                        "approve"})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("never proposed", "\n".join(caught.exception.problems))

    def test_every_proposed_change_needs_exactly_one_decision(self):
        code, output, _ = self.start(answers=envelopes(("c-a", "c-b")))
        decision = self.decide(output, {"c-a": "approve"})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("undecided", "\n".join(caught.exception.problems))

    def test_resume_without_a_decision_pauses_again(self):
        code, output, _ = self.start()
        code, out, track = self.resume(output)
        self.assertEqual(code, 3)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(track["status"], "paused")
        self.assertEqual(len(self.fake.calls), 2)      # nothing re-run

    def test_resume_with_a_decision_continues_and_exposes_the_decision(self):
        code, output, _ = self.start(answers=envelopes(("c-a", "c-b")))
        decision = self.decide(output, {"c-a": "approve", "c-b": "reject"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        self.assertEqual(track["status"], "completed")
        record = self.step(track, "compose")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["gate"]["status"], "pass")
        self.assertEqual(record["gate"]["decision_sha256"],
                         op_runner.sha256_file(record["gate"]["decision"]))
        self.assertTrue(Path(record["gate"]["decision"]).exists())
        self.assertEqual(record["decision"]["value"]["rejected"], ["c-b"])
        # the downstream step's REQUEST carried the approved changes
        request = self.fake.calls[-1]["request"]
        self.assertEqual([c["change_id"] for c in request["changes"]], ["c-a"])
        self.assertEqual(track["resumes"][0]["decision"],
                         record["gate"]["decision"])

    def test_a_second_resume_never_re_runs_passed_steps(self):
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0)
        calls = len(self.fake.calls)
        code, out2, track2 = self.resume(out)
        self.assertEqual(len(self.fake.calls), calls)     # nothing re-run
        self.assertEqual([s["status"] for s in track2["steps"]],
                         [s["status"] for s in track["steps"]])
        self.assertEqual(len(track2["resumes"]), 2)

    def test_a_crashed_step_is_re_run_on_resume(self):
        code, output, track = self.start(answers=by_request(("c-a",)))
        # plant a crash: compose is recorded `running`, nothing decided
        record = self.step(track, "compose")
        record["status"] = "running"
        record["gate"] = None
        track["status"] = "running"
        Path(output["track"]).write_text(json.dumps(track))
        code, out, track2 = self.resume(output)
        self.assertEqual(code, 3)              # it ran again and paused
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(self.fake.calls[-1]["task"], "ask")


class CliTests(AuthorityCase):
    """The process boundary: exit 3 with the paused stdout shape, and the
    same pause/resume through the runner's own CLI."""

    def main(self, argv):
        real_root, op_runner.ROOT = op_runner.ROOT, self.package
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = op_runner.main(argv)
        finally:
            op_runner.ROOT = real_root
        return code, json.loads(stdout.getvalue())

    def setup_package(self, changes=("c-a",)):
        fx.write_package(self.package, spec_doc())
        self.request = fx.write_request(self.root / "request.json",
                                        {"repositories": [REPO]})
        self.authority_path = self.root / "authority.json"
        self.authority_path.write_text(json.dumps(fx.authority_doc()))
        self.fake = fx.FakeCog(by_request(changes))
        op_runner.invoke_cog = self.fake

    def test_the_cli_exits_three_when_it_pauses_and_zero_when_resumed(self):
        self.setup_package()
        code, output = self.main(["--request", str(self.request),
                                  "--authority", str(self.authority_path)])
        self.assertEqual(code, op_runner.PAUSED_EXIT)
        self.assertEqual(output["ok"], False)
        self.assertEqual(output["status"], "paused")
        self.assertTrue(Path(output["pending"]).exists())
        decision = self.decide(output, {"c-a": "approve"})
        code, output = self.main(["--resume", output["run_dir"],
                                  "--decision", str(decision),
                                  "--authority", str(self.authority_path)])
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "completed")

    def test_the_cli_exits_two_for_a_decision_about_something_else(self):
        self.setup_package()
        code, output = self.main(["--request", str(self.request),
                                  "--authority", str(self.authority_path)])
        decision = self.decide(output, {"c-a": "approve"},
                               payload_sha256="0" * 64)
        code, output = self.main(["--resume", output["run_dir"],
                                  "--decision", str(decision)])
        self.assertEqual(code, 2)
        self.assertIn("decided about something else",
                      " ".join(output["problems"]))

    def test_a_resume_remembers_the_admission_it_started_with(self):
        self.setup_package()
        code, output = self.main(["--request", str(self.request),
                                  "--authority", str(self.authority_path)])
        decision = self.decide(output, {"c-a": "approve"})
        code, output = self.main(["--resume", output["run_dir"],
                                  "--decision", str(decision)])
        self.assertEqual(code, 0, output)

    def test_an_edited_admission_file_refuses_the_resume(self):
        self.setup_package()
        code, output = self.main(["--request", str(self.request),
                                  "--authority", str(self.authority_path)])
        self.authority_path.write_text(json.dumps(
            fx.authority_doc(read=[REPO, OTHER_REPO])))
        decision = self.decide(output, {"c-a": "approve"})
        code, output = self.main(["--resume", output["run_dir"],
                                  "--decision", str(decision)])
        self.assertEqual(code, 2)
        self.assertIn("admission file has changed",
                      " ".join(output["problems"]))


class DocumentTests(unittest.TestCase):
    def test_an_admission_with_the_wrong_schema_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "authority.json"
            path.write_text(json.dumps(
                dict(fx.authority_doc(), schema="openteams/op-grant [0.1]")))
            with self.assertRaises(op_spec.OpSpecError) as caught:
                op_runner.load_authority(path)
            self.assertIn("openteams/op-authority [0.1]",
                          "\n".join(caught.exception.problems))

    def test_an_admission_may_not_name_change_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "authority.json"
            doc = fx.authority_doc()
            doc["operations"][1]["changes"] = [{"change_id": "c-a"}]
            path.write_text(json.dumps(doc))
            with self.assertRaises(op_spec.OpSpecError) as caught:
                op_runner.load_authority(path)
            self.assertIn("only a human decision names change ids",
                          "\n".join(caught.exception.problems))

    def test_a_decision_with_the_wrong_schema_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(json.dumps({"schema": "openteams/op-decision [0.2]"}))
            with self.assertRaises(op_spec.OpSpecError) as caught:
                op_runner.load_decision(path)
            self.assertIn("openteams/op-decision [0.1]",
                          "\n".join(caught.exception.problems))

    def test_the_default_ttl_is_an_hour_and_the_spec_may_change_it(self):
        doc = spec_doc()
        self.assertEqual(op_spec.ttl_minutes(doc), 60)
        doc["authority"] = {"ttl_minutes": 5}
        self.assertEqual(op_spec.OpSpec(doc).ttl_minutes, 5)

    def test_a_negative_ttl_is_refused(self):
        doc = spec_doc(authority={"ttl_minutes": -1})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("positive number of minutes",
                      "\n".join(caught.exception.problems))

    def test_an_unknown_authority_key_is_refused(self):
        doc = spec_doc(authority={"ttl_days": 1})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("authority vocabulary is closed",
                      "\n".join(caught.exception.problems))

    def test_the_grant_carries_no_credentials(self):
        grant = op_runner.grant_document(
            {"id": "write", "cog": {"id": "openteams/cog-write-github",
                                    "version": "0.1.0"}},
            "run-1", [{"resource": "github", "action": "write",
                       "changes": []}], {"kind": "admission"}, 60)
        text = json.dumps(grant).lower()
        for secret in ("token", "password", "secret", "api_key"):
            self.assertNotIn(secret, text)


class TtlTests(AuthorityCase):
    def test_the_grant_expires_after_the_declared_ttl(self):
        from datetime import datetime
        doc = spec_doc(authority={"ttl_minutes": 1})
        code, output, track = self.start(doc=doc)
        grant = json.loads(
            Path(self.step(track, "read-github")["grant"]).read_text())
        issued = datetime.fromisoformat(grant["issued_at"])
        expires = datetime.fromisoformat(grant["valid"]["expires_at"])
        self.assertAlmostEqual((expires - issued).total_seconds(), 60, delta=5)


if __name__ == "__main__":
    unittest.main()
