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
import os
import tempfile
import types
import unicodedata
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
        self.assertIn("steps.<id>.decision.approved",
                      "\n".join(caught.exception.problems))

    def test_a_write_requirement_reads_exactly_the_approved_list(self):
        # Contract §9: only the approved list can produce a grant, so only
        # the approved list may be ASKED for — any other sub-path of the
        # decision is refused at LOAD, by name, not denied later.
        for path in ("steps.compose.decision.rejected",
                     "steps.compose.decision",
                     "steps.compose.decision.approved.0"):
            doc = spec_doc()
            doc["steps"][2]["authority"]["requires"][0]["changes"] = {
                "$from": path}
            with self.assertRaises(op_spec.OpSpecError) as caught:
                op_spec.validate(doc)
            self.assertIn("steps.<id>.decision.approved",
                          "\n".join(caught.exception.problems), path)

    def test_two_write_requirements_from_different_gates_are_refused(self):
        # Phase 3 issues ONE grant per step, with one provenance: a grant
        # whose writes came from two human gates could only record one of
        # them, so the spec is refused at load instead (review S6).
        doc = spec_doc()
        second = fx.cog_step("compose-more", depends_on=["read-github"],
                             gate={"policy": "human", "guards": []})
        doc["steps"].insert(2, second)
        write = doc["steps"][3]
        write["depends_on"] = ["compose", "compose-more"]
        write["authority"]["requires"].append(
            {"resource": "github", "action": "write",
             "changes": {"$from": "steps.compose-more.decision.approved"}})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        self.assertIn("one grant per step, from ONE human gate",
                      "\n".join(caught.exception.problems))

    def test_a_list_valued_repository_is_a_denial_not_a_crash(self):
        code, output, track = self.start(request={"repositories": [["a"]]})
        record = self.step(track, "read-github")
        self.assertEqual(record["status"], "denied")
        self.assertIn("not a list of repositories", record["gate"]["reasons"][0])
        self.assertEqual(self.fake.calls, [])

    def test_retry_once_is_refused_on_a_step_that_carries_authority(self):
        doc = spec_doc()
        doc["steps"][0]["on_fail"] = "retry-once"
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.validate(doc)
        text = "\n".join(caught.exception.problems)
        self.assertIn("retry-once", text)
        self.assertIn("resume and journal reconciliation", text)

    def test_retry_once_is_refused_on_a_step_whose_cog_reaches(self):
        # The same rule from the Cog's side: no `authority:` on the step at
        # all, but the Cog's manifest declares `reaches`.
        doc = spec_doc()
        doc["steps"][1]["on_fail"] = "retry-once"
        fx.write_cog(self.root, "compose",
                     reaches=[{"resource": "github", "actions": ["read"]}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(doc=doc)
        self.assertIn("never re-invoked on a failure",
                      "\n".join(caught.exception.problems))

    def test_a_run_dir_expression_cannot_reach_a_control_directory(self):
        for subpath in ("grants", "pending/compose.json", "decisions",
                        "journal", "track.json", "run.lock"):
            doc = spec_doc()
            doc["steps"][1]["input"] = {"out": {"$run_dir": subpath}}
            with self.assertRaises(op_spec.OpSpecError) as caught:
                op_spec.validate(doc)
            self.assertIn("control entries",
                          "\n".join(caught.exception.problems), subpath)

    def test_run_dir_refuses_a_control_directory_at_evaluation_too(self):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.run_dir_path("grants", str(self.root))
        self.assertIn("reserved", "\n".join(caught.exception.problems))
        self.assertFalse((self.root / "grants").exists())

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

    def test_the_grant_lists_only_the_approved_changes(self):
        # §7's "a step requiring B is denied" is two tests after contract §9.
        # This is the runner's half: whatever the step's own input says, the
        # GRANT is exactly the approved list. (The Cog's half — a change id
        # or a target hash that is not in the grant is denied — is
        # tests/test_code_cog.py.)
        doc = spec_doc()
        doc["steps"][2]["input"] = {
            "changes": {"$from": "steps.compose.payload.changes"}}  # asks for both
        code, output, _ = self.start(doc=doc, answers=envelopes(("c-a", "c-b")))
        decision = self.decide(output, {"c-a": "approve", "c-b": "reject"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        grant = json.loads(
            Path(self.step(track, "write-github")["grant"]).read_text())
        self.assertEqual(
            [c["change_id"] for c in grant["operations"][0]["changes"]],
            ["c-a"])
        # the request the Cog got asked for both; only one is authorized
        self.assertEqual(
            [c["change_id"] for c in self.fake.calls[-1]["request"]["changes"]],
            ["c-a", "c-b"])

    def test_a_granted_change_carries_both_hashes(self):
        code, output, track = self.approve_and_continue({"c-a": "approve"},
                                                        changes=("c-a",))
        self.assertEqual(code, 0, output)
        grant = json.loads(
            Path(self.step(track, "write-github")["grant"]).read_text())
        granted = grant["operations"][0]["changes"][0]
        self.assertEqual(granted["target_sha256"], "1" * 64)
        self.assertEqual(granted["content_sha256"],
                         op_runner.change_content_sha256(fx.change("c-a")))
        self.assertEqual(granted["repository"], REPO)

    def test_a_change_without_a_target_hash_refuses_the_pause(self):
        # Review 3, finding 1: the hashes are checked where the human meets
        # them — at the pause — not only at issuance.
        naked = fx.change("c-a")
        del naked["target_sha256"]
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [naked]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("never repaired", "\n".join(caught.exception.problems))

    def test_a_recorded_change_without_a_target_hash_is_denied(self):
        # The issuance check stands behind the pause check: a Track edited
        # after the decision still releases no grant.
        record = self.tampered_decision(lambda c: c.pop("target_sha256"))
        self.assertEqual(record["status"], "denied")
        self.assertIn("carries both hashes", record["gate"]["reasons"][0])

    def test_each_issuance_gets_its_own_grant_id_and_file(self):
        code, output, track = self.approve_and_continue({"c-a": "approve"},
                                                        changes=("c-a",))
        first = Path(self.step(track, "write-github")["grant"])
        self.assertEqual(first.name, "0.json")
        # plant an interruption: the write step is re-run by a second resume
        record = self.step(track, "write-github")
        record["status"] = "running"
        Path(output["track"]).write_text(json.dumps(track))
        code, out2, track2 = self.resume(output)
        second = Path(self.step(track2, "write-github")["grant"])
        self.assertNotEqual(second, first)
        self.assertTrue(first.exists())            # never overwritten
        self.assertEqual(json.loads(first.read_text())["grant_id"],
                         f"{track['run_id']}/write-github/0")
        self.assertEqual(json.loads(second.read_text())["grant_id"],
                         f"{track['run_id']}/write-github/1")

    def test_the_grant_names_the_recipient_version_from_the_manifest(self):
        doc = spec_doc()
        del doc["steps"][0]["cog"]["version"]
        code, output, track = self.start(doc=doc)
        grant = json.loads(
            Path(self.step(track, "read-github")["grant"]).read_text())
        self.assertEqual(grant["recipient"]["cog"]["version"], "0.1.0")

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

    def test_an_approved_change_that_no_longer_hashes_to_itself_is_denied(self):
        # Verification finding 2: issuance RECOMPUTES the content hash of
        # every approved change. A Track whose recorded decision was edited
        # after the fact — the change altered, the hash left alone — is a
        # denial, not a grant.
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        self.step(track, "compose")["decision"]["value"]["approved"][0][
            "summary"] = "something else entirely"
        self.step(track, "write-github")["status"] = "not-reached"
        Path(out["track"]).write_text(json.dumps(track))
        code, out2, track2 = self.resume(out)
        record = self.step(track2, "write-github")
        self.assertEqual(record["status"], "denied")
        self.assertIn("its content hashes to", record["gate"]["reasons"][0])

    def tampered_decision(self, tamper):
        """Approve one change, then edit the RECORDED decision and re-run the
        write step: what issuance does with a Track that no longer says what
        the human decided."""
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        tamper(self.step(track, "compose")["decision"]["value"]["approved"][0])
        self.step(track, "write-github")["status"] = "not-reached"
        Path(out["track"]).write_text(json.dumps(track))
        code, out2, track2 = self.resume(out)
        return self.step(track2, "write-github")

    def test_a_change_whose_target_hash_is_not_a_sha256_refuses_the_pause(self):
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [
                               fx.change("c-a", target_sha256="yes")]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("not a sha256", "\n".join(caught.exception.problems))

    def test_a_target_hash_with_a_trailing_newline_refuses_the_pause(self):
        # Review 3, finding 1, reproduced: `re.match` with `$` accepted
        # 64 hex characters followed by a newline. `fullmatch` does not.
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [fx.change(
                               "c-a", target_sha256="1" * 64 + "\n")]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("not a sha256", "\n".join(caught.exception.problems))
        self.assertIsNone(op_runner.hex64_problem("a" * 64, "content_sha256",
                                                  "change 'c'"))
        self.assertIn("not a sha256", op_runner.hex64_problem(
            "a" * 64 + "\n", "content_sha256", "change 'c'"))

    def test_a_recorded_change_whose_target_hash_is_not_a_sha256_is_denied(self):
        record = self.tampered_decision(
            lambda c: c.__setitem__("target_sha256", "yes"))
        self.assertEqual(record["status"], "denied")
        self.assertIn("not a sha256", record["gate"]["reasons"][0])

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
        self.assertIn("c\\-a", (run_dir / "pending" / "compose.md").read_text())
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


class PendingSheetTests(unittest.TestCase):
    """The decision sheet names what it shows (Op machinery 0.5.6).

    The live nexus sweep handed a human a Markdown table whose `kind` and
    `target` columns were blank for every one of 33 changes: the renderer read
    `kind`/`target`, and the change shape the proposing Cogs emit says
    `change_type` and `target_item_ids`. Neither name is promoted over the
    other — the renderer reads what is there."""

    def sheet(self, *changes):
        doc = {"schema": op_runner.PENDING_SCHEMA, "run_id": "run-1",
               "step": "compose", "payload": {"changes": list(changes)},
               "asked_at": "2026-09-19T00:00:00+00:00",
               "decide_with": "op run --resume . --decision <file>"}
        return op_runner.render_pending(doc)

    def hashed(self, body):
        return dict(body,
                    content_sha256=op_runner.change_content_sha256(body),
                    target_sha256="1" * 64)

    def test_the_cogs_change_shape_fills_both_columns(self):
        line = self.sheet(self.hashed({
            "change_id": "c-1", "change_type": "link_duplicate",
            "target_item_ids": ["nexus#33", "nexus#34"],
            "repository": REPO, "summary": "link the duplicate pair",
        })).splitlines()[-1]
        self.assertIn("| link\\_duplicate |", line)
        self.assertIn("| nexus\\#33 |", line)

    def test_kind_and_target_still_win_when_present(self):
        line = self.sheet(self.hashed({
            "change_id": "c-1", "kind": "label", "target": "nexus#35",
            "change_type": "add_label", "target_item_ids": ["nexus#99"],
            "repository": REPO, "summary": "add type:bug",
        })).splitlines()[-1]
        self.assertIn("| label |", line)
        self.assertIn("| nexus\\#35 |", line)

    def test_a_change_that_names_neither_renders_blank_not_crashed(self):
        line = self.sheet(self.hashed({
            "change_id": "c-1", "repository": REPO, "summary": "something",
        })).splitlines()[-1]
        self.assertEqual(line, "| c\\-1 |  |  | something |")

    def test_an_empty_target_id_list_is_not_an_index_error(self):
        line = self.sheet(self.hashed({
            "change_id": "c-1", "change_type": "close_item",
            "target_item_ids": [], "repository": REPO, "summary": "close it",
        })).splitlines()[-1]
        self.assertEqual(line, "| c\\-1 | close\\_item |  | close it |")


HOSTILE_CELLS = [
    # Codex review 10, finding 1, verbatim plus review 9's originals.
    "owner/repo#1​0",                     # zero-width space
    "‮reversed‬ text",               # bidi override and pop
    "before\u001b[31mred\u001b[0m after",      # terminal escape
    "\u0007bell and \u0000nul",                # other C0 controls
    ":smile: :+1:",                            # GitHub emoji shortcodes
    "@mention and @org/team",                  # mentions
    "https://example.invalid/a?b=c#d",         # bare autolink
    "www.example.invalid/path",                # www autolink
    "&#124; &amp; &lt;script&gt;",             # numeric and named entities
    "a | b || c",                              # pipes
    "`code` ```fence```",                      # code spans
    "[owner/repo#1](https://example.invalid)",  # link hiding its target
    "first\r\nsecond\n\n| x | y |",            # newlines inventing rows
    "<!-- hide the rest",                      # HTML comment opener
    "nbsp and line para",       # Zs / Zl / Zp separators
    "\U0001f600 and \U000e0041 tag-letter",    # astral: one visible, one Cf
]


def unescaped_punctuation(text):
    """Every ASCII punctuation character in `text` that does NOT have a
    backslash in front of it. The invariant is that this is empty."""
    loose, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 2
            continue
        if text[i] in op_runner.CELL_ESCAPES:
            loose.append((i, text[i]))
        i += 1
    return loose


def invisible_characters(text):
    """Every character in a Unicode `C*` or `Z*` category other than an
    ordinary space. The invariant is that this is empty."""
    return [(i, ch) for i, ch in enumerate(text)
            if ch != " " and unicodedata.category(ch)[0] in ("C", "Z")]


class PendingSheetLiteralTextTests(unittest.TestCase):
    """Every cell satisfies the literal-text INVARIANT (Op machinery 0.5.8).

    Codex review 9, blocker 3: a valid `content_sha256` says nothing about
    how a proposal READS. Review 10, finding 1: escaping a chosen list of
    metacharacters left `:emoji:`, `@mentions`, bare autolinks and — worse —
    every invisible character alive, so `owner/repo#1<U+200B>0` still read as
    `owner/repo#10`.

    So the test is not a catalogue of renderings (which would need a
    renderer) but a checkable property of the string itself, asserted over
    the hostile inputs: no ASCII punctuation character stands unescaped, and
    no character in a `C*` or `Z*` category other than a space is present at
    all.
    """

    def assertLiteral(self, text):
        self.assertEqual(unescaped_punctuation(text), [],
                         f"unescaped ASCII punctuation in {text!r}")
        self.assertEqual(invisible_characters(text), [],
                         f"invisible character in {text!r}")

    def sheet(self, **change):
        body = dict({"repository": REPO}, **change)
        doc = {"schema": op_runner.PENDING_SCHEMA, "run_id": "run-1",
               "step": "compose",
               "payload": {"changes": [dict(
                   body, content_sha256=op_runner.change_content_sha256(body),
                   target_sha256="1" * 64)]},
               "asked_at": "2026-09-19T00:00:00+00:00",
               "decide_with": "op run --resume . --decision <file>"}
        return op_runner.render_pending(doc)

    def row(self, **change):
        return self.sheet(**change).splitlines()[-1]

    def cells(self, **change):
        """Split the row the way a Markdown table parser does: on UNESCAPED
        pipes only. If the escaping were wrong this would find extra cells,
        which is the point."""
        row = self.row(**change)
        parts, buf, i = [], "", 0
        while i < len(row):
            ch = row[i]
            if ch == "\\" and i + 1 < len(row):
                buf += row[i:i + 2]
                i += 2
                continue
            if ch == "|":
                parts.append(buf)
                buf = ""
            else:
                buf += ch
            i += 1
        parts.append(buf)
        return [p.strip() for p in parts[1:-1]]

    def test_every_cell_of_a_hostile_row_satisfies_the_invariant(self):
        """The whole point, over every input Codex named: each cell of a row
        built from hostile strings is literal text by the invariant."""
        for hostile in HOSTILE_CELLS:
            with self.subTest(hostile=hostile):
                for value in self.cells(change_id=hostile,
                                        change_type=hostile,
                                        target=hostile, summary=hostile):
                    self.assertLiteral(value)

    def test_the_whole_sheet_carries_no_invisible_character(self):
        """Not only the rows: the heading, the run id and the asked-at line
        go through the same cell()."""
        sheet = self.sheet(change_id="c-1​1", change_type="add_label",
                           target="nexus#1‮", summary="ok\u0007")
        self.assertEqual(invisible_characters(sheet.replace("\n", " ")), [])

    def test_a_zero_width_space_is_shown_as_its_codepoint(self):
        """Review 10 finding 1: `owner/repo#1<U+200B>0` READ as
        `owner/repo#10`. It must now say what it contains."""
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="owner/repo#1​0", summary="plain")
        self.assertEqual(cells[2], "owner\\/repo\\#1U\\+200B0")
        self.assertLiteral(cells[2])

    def test_bidi_controls_are_shown_as_their_codepoints(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1",
                           summary="‮reversed‬")
        self.assertEqual(cells[3], "U\\+202EreversedU\\+202C")

    def test_a_terminal_escape_is_shown_as_its_codepoint(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1", summary="a\u001b[31mred")
        self.assertEqual(cells[3], "aU\\+001B\\[31mred")

    def test_an_emoji_shortcode_stays_a_shortcode(self):
        """`:smile:` became a picture on GitHub; a backslash before each
        colon is enough to stop the shortcode."""
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1", summary=":smile: done")
        self.assertEqual(cells[3], "\\:smile\\: done")

    def test_a_mention_and_an_autolink_are_not_live(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="@org/team",
                           summary="see https://example.invalid and www.x.y")
        self.assertEqual(cells[2], "\\@org\\/team")
        self.assertEqual(
            cells[3],
            "see https\\:\\/\\/example\\.invalid and www\\.x\\.y")

    def test_a_markdown_link_target_shows_the_literal_target(self):
        """`[owner/repo#1](https://example.invalid)` must not become a link
        whose text hides where the change actually goes."""
        row = self.row(change_id="c-1", change_type="add_label",
                       target="[owner/repo#1](https://example.invalid)",
                       summary="looks innocent")
        self.assertIn(
            "\\[owner\\/repo\\#1\\]\\(https\\:\\/\\/example\\.invalid\\)", row)
        self.assertNotIn("](", row)

    def test_a_pipe_in_a_cell_does_not_shift_the_columns(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1",
                           summary="add a | b to the item")
        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[3], "add a \\| b to the item")

    def test_a_newline_does_not_invent_a_row(self):
        sheet = self.sheet(change_id="c-1\nc-2", change_type="add_label",
                           target="nexus#1",
                           summary="first line\r\nsecond line\n\n| x | y | z |")
        rows = [ln for ln in sheet.splitlines() if ln.startswith("| c\\-")]
        self.assertEqual(len(rows), 1)
        self.assertIn("c\\-1 c\\-2", rows[0])
        self.assertIn("first line second line", rows[0])

    def test_an_html_comment_cannot_conceal_what_follows(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1",
                           summary="<!-- hide the rest")
        self.assertNotIn("<!--", cells[3])
        self.assertEqual(cells[3], "\\<\\!\\-\\- hide the rest")

    def test_backticks_and_emphasis_do_not_reformat_a_cell(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1",
                           summary="`code` *bold* _under_ ~strike~ #tag !bang")
        self.assertEqual(
            cells[3],
            "\\`code\\` \\*bold\\* \\_under\\_ \\~strike\\~ \\#tag \\!bang")

    def test_an_entity_is_escaped_once_and_not_twice(self):
        """0.5.7 HTML-escaped as well, so `&#124;` came out `&amp;\\#124;`
        — escaped twice, and read as an entity for `&`. The backslash rule
        alone covers it (review 10, finding 1's entity check)."""
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1", summary="&#124; &lt;script&gt;")
        self.assertEqual(cells[3],
                         "\\&\\#124\\; \\&lt\\;script\\&gt\\;")
        self.assertNotIn("&amp;", cells[3])

    def test_a_backslash_is_escaped_before_anything_is_added(self):
        cells = self.cells(change_id="c-1", change_type="add_label",
                           target="nexus#1", summary="a \\| b")
        self.assertEqual(cells[3], "a \\\\\\| b")

    def test_every_column_is_escaped_not_only_the_summary(self):
        cells = self.cells(change_id="c|1", change_type="link_*duplicate*",
                           target="[t](u)", summary="plain")
        self.assertEqual(cells[0], "c\\|1")
        self.assertEqual(cells[1], "link\\_\\*duplicate\\*")
        self.assertEqual(cells[2], "\\[t\\]\\(u\\)")

    def test_the_header_says_the_json_beside_it_is_the_authority(self):
        """The sheet is escaped, so a cell does not read back as the string
        the Cog proposed: say where the authority lives (§11c)."""
        sheet = self.sheet(change_id="c-1", change_type="add_label",
                           target="nexus#1", summary="plain")
        head = sheet.split("| change_id")[0]
        self.assertIn("compose.json", head)
        self.assertIn("authority", head)
        self.assertIn("reading aid", head)

    def test_the_header_claims_no_general_invisibility(self):
        """Machinery 0.5.9 (Codex review 11, finding 4). 0.5.8's header said
        "an invisible character shows as `U+XXXX`", which is not true in
        general: combining marks (U+034F) and variation selectors (U+FE0F)
        are `Mn`, look-alike letters are ordinary letters, and none of them
        is `C*` or `Z*`. The header now states the three things the code
        does and names what it does not detect. Wording only — `cell()` is
        unchanged."""
        head = self.sheet(change_id="c-1", change_type="add_label",
                          target="nexus#1", summary="plain"
                          ).split("| change_id")[0]
        for absolute in ("an invisible character shows",
                         "nothing invisible", "punctuation shows a backslash"):
            self.assertNotIn(absolute, head)
        self.assertIn("NOT detected", head)
        for named in ("Combining marks", "variation selectors",
                      "look-alike letters"):
            self.assertIn(named, head)
        self.assertIn("string equality is decided there", head)
        # And the same restraint in the docstring the maintainer reads.
        doc = op_runner.cell.__doc__
        self.assertIn("WHAT THIS DOES NOT DO", doc)
        self.assertNotIn("Nothing invisible can hide", doc)
        self.assertNotIn("nothing invisible survives", doc)

    def test_the_wording_change_moved_no_cell(self):
        """0.5.9 is wording: the three things the header claims, and the two
        it now says it does not do. `cell()` itself is untouched, and the
        hostile-input invariant above still holds over every one of them."""
        for hostile in HOSTILE_CELLS:
            with self.subTest(hostile=hostile):
                self.assertLiteral(op_runner.cell(hostile))
        self.assertEqual(op_runner.cell("a​b"), "aU\\+200Bb")
        self.assertEqual(op_runner.cell("  a \n b  "), "a b")
        self.assertEqual(op_runner.cell("a͏b"), "a͏b",
                         "a combining mark is not detected, as the header "
                         "now says")
        self.assertEqual(op_runner.cell("а"), "а",
                         "a Cyrillic look-alike is an ordinary letter")


class PendingAndDecisionTests(AuthorityCase):
    """What the human decided about, and that it is still what was proposed
    when the decision is applied (review B5, S2)."""

    def test_tampering_with_the_pending_payload_refuses_the_decision(self):
        code, output, _ = self.start()
        decision = self.decide(output, {"c-a": "approve"})
        # rewrite the proposal, keeping the hash the pending file carries:
        # the old approval must not be accepted against the new proposal.
        path = Path(output["pending"])
        pending = json.loads(path.read_text())
        pending["payload"]["changes"][0]["repository"] = OTHER_REPO
        path.write_text(json.dumps(pending))
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("not the one this run paused on",
                      "\n".join(caught.exception.problems))

    def test_the_decisions_hash_is_checked_against_the_payload_itself(self):
        # Even with no Track to compare against, `apply_decision` re-hashes
        # the payload rather than trusting the hash beside it.
        pending = {"run_id": "r", "step": "compose",
                   "payload": {"changes": [fx.change("c-a")]},
                   "payload_sha256": "0" * 64}
        decision = fx.decision_doc("r", "compose", "0" * 64,
                                   {"c-a": "approve"})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.apply_decision(pending, decision)
        self.assertIn("decided about something else",
                      "\n".join(caught.exception.problems))

    def test_a_proposal_with_a_null_content_hash_refuses_the_pause(self):
        # Verification finding 2: the runner never REPAIRS a missing content
        # hash into a valid-looking approved change — the pause is refused
        # by name, because the hash is what the approval is about.
        naked = dict(fx.change("c-a"), content_sha256=None)
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [naked]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("no content_sha256",
                      "\n".join(caught.exception.problems))

    def test_a_placeholder_content_hash_refuses_the_pause(self):
        # Review 3, finding 1, reproduced: a proposal carrying
        # `"content_sha256": "placeholder"` used to pass the pause and be
        # LAUNDERED into a valid canonical digest at approval, which then
        # passed issuance. The pause refuses it by name instead.
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [fx.change(
                               "c-a", content_sha256="placeholder")]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        problems = "\n".join(caught.exception.problems)
        self.assertIn("never repaired", problems)
        self.assertIn("not a sha256", problems)
        self.assertFalse((self.package / "runs").exists()
                         and any((self.package / "runs").rglob("pending")))

    def test_a_content_hash_that_is_not_its_own_refuses_the_pause(self):
        # The same refusal for a well-FORMED digest that is simply not this
        # change's: a digest is checked against the object it arrived on.
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [fx.change(
                               "c-a", content_sha256="b" * 64)]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("its content hashes to",
                      "\n".join(caught.exception.problems))

    def test_approval_preserves_the_supplied_digest_and_only_edits_rehash(self):
        # Review 3, finding 1: an approval or a rejection carries the digest
        # the proposal stated (the pause already checked it against the
        # object); ONLY an edit is re-hashed. Recomputing at approval is
        # what turned `"placeholder"` into a valid-looking hash.
        stated = fx.change("c-a")
        payload = {"changes": [stated, fx.change("c-b")]}
        pending = {"run_id": "r", "step": "compose", "payload": payload,
                   "payload_sha256": op_runner.canonical_sha256(payload)}
        edited = dict(fx.change("c-b"), summary="a better summary")
        decision = fx.decision_doc("r", "compose", pending["payload_sha256"],
                                   {"c-a": "approve", "c-b": edited})
        rehashed, real = [], op_runner.normalized_change

        def watched(change):
            rehashed.append(change["change_id"])
            return real(change)

        op_runner.normalized_change = watched
        try:
            applied = op_runner.apply_decision(pending, decision)
        finally:
            op_runner.normalized_change = real
        self.assertEqual(rehashed, ["c-b"])          # the edit, and only it
        by_id = {c["change_id"]: c for c in applied["approved"]}
        self.assertEqual(by_id["c-a"]["content_sha256"],
                         stated["content_sha256"])
        self.assertEqual(by_id["c-b"]["content_sha256"],
                         op_runner.change_content_sha256(edited))

    def test_a_decision_about_a_misstated_proposal_is_refused_too(self):
        # The pause refuses first, but `apply_decision` re-reads the pending
        # payload and refuses the same way: a pending file edited by hand
        # after the pause cannot launder a digest either.
        payload = {"changes": [fx.change("c-a", content_sha256="c" * 64)]}
        pending = {"run_id": "r", "step": "compose", "payload": payload,
                   "payload_sha256": op_runner.canonical_sha256(payload)}
        decision = fx.decision_doc("r", "compose", pending["payload_sha256"],
                                   {"c-a": "approve"})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.apply_decision(pending, decision)
        self.assertIn("its content hashes to",
                      "\n".join(caught.exception.problems))

    def test_a_human_gated_step_keeps_passed_with_problems(self):
        # Verification finding 6: approving the proposals does not erase the
        # problems the Cog reported making them.
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [fx.change("c-a")]},
                                       problems=[{"check": "coverage",
                                                  "severity": "warn",
                                                  "detail": "one item skipped"}]),
                           fx.envelope(payload={})]}
        code, output, track = self.start(answers=answers)
        self.assertEqual(code, op_runner.PAUSED_EXIT)
        self.assertEqual(self.step(track, "compose")["gate"]["envelope_status"],
                         "pass-with-problems")
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.step(track, "compose")["status"],
                         "passed-with-problems")
        self.assertEqual(track["status"], "completed-with-problems")

    def test_a_duplicate_change_id_refuses_the_pause(self):
        answers = {"ask": [fx.envelope(payload={"items": []}),
                           fx.envelope(payload={"changes": [
                               fx.change("c-a"),
                               fx.change("c-a", summary="something else")]}),
                           fx.envelope(payload={})]}
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.start(answers=answers)
        self.assertIn("more than once", "\n".join(caught.exception.problems))

    def test_a_decision_without_who_and_when_is_refused(self):
        for field in ("decided_by", "decided_at"):
            code, output, _ = self.start()
            decision = self.decide(output, {"c-a": "approve"}, **{field: None})
            with self.assertRaises(op_spec.OpSpecError) as caught:
                self.resume(output, decision)
            self.assertIn(field, "\n".join(caught.exception.problems))

    def test_a_decision_whose_who_and_when_say_nothing_is_refused(self):
        # Review S2's remainder: `decided_at: "not-a-time"` and a
        # whitespace-only `decided_by` were accepted as metadata.
        for field, value in (("decided_at", "not-a-time"),
                             ("decided_by", "   ")):
            code, output, _ = self.start()
            decision = self.decide(output, {"c-a": "approve"},
                                   **{field: value})
            with self.assertRaises(op_spec.OpSpecError) as caught:
                self.resume(output, decision)
            self.assertIn(field, "\n".join(caught.exception.problems))

    def test_an_edit_may_not_retarget_or_restate_the_target_hash(self):
        for key, value in (("repository", OTHER_REPO),
                           ("target_sha256", "9" * 64)):
            code, output, _ = self.start()
            edited = dict(fx.change("c-a"), **{key: value})
            decision = self.decide(output, {"c-a": edited})
            with self.assertRaises(op_spec.OpSpecError) as caught:
                self.resume(output, decision)
            self.assertIn(key, "\n".join(caught.exception.problems))

    def test_an_edit_may_not_add_or_drop_fields(self):
        code, output, _ = self.start()
        edited = dict(fx.change("c-a"), extra="smuggled")
        decision = self.decide(output, {"c-a": edited})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("'extra'", "\n".join(caught.exception.problems))

    def test_the_decision_history_carries_normalized_hashes(self):
        code, output, _ = self.start(answers=envelopes(("c-a", "c-b")))
        decision = self.decide(output, {"c-a": "approve", "c-b": "reject"})
        code, out, track = self.resume(output, decision)
        history = {h["change_id"]: h for h in
                   self.step(track, "compose")["decision"]["value"]["history"]}
        self.assertEqual({k: v["verdict"] for k, v in history.items()},
                         {"c-a": "approve", "c-b": "reject"})
        for entry in history.values():
            # never the "0"*64 the proposal arrived with
            self.assertEqual(entry["content_sha256"],
                             op_runner.change_content_sha256(
                                 fx.change(entry["change_id"])))
            self.assertEqual(entry["target_sha256"], "1" * 64)

    def test_the_exposed_decision_carries_decided_by_and_decided_at(self):
        # Review 4, S3: the run record could not say WHO decided or WHEN,
        # because the exposed decision value carried neither (contract §9d,
        # Op machinery 0.5.4).
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        decision_path = self.decide(output, {"c-a": "approve"},
                                    decided_by="trent",
                                    decided_at="2026-09-18T10:00:00+00:00")
        code, out, track = self.resume(output, decision_path)
        self.assertEqual(code, 0, out)
        value = self.step(track, "compose")["decision"]["value"]
        self.assertEqual(value["decided_by"], "trent")
        self.assertEqual(value["decided_at"], "2026-09-18T10:00:00+00:00")

    def test_the_exposed_approved_list_carries_the_edited_change_object(self):
        # Review 4, S3: an edited change must reach a downstream step as the
        # EDITED object, not as the original proposal.
        code, output, _ = self.start(answers=envelopes(("c-a",)))
        edited = dict(fx.change("c-a"), summary="a better summary")
        edited["content_sha256"] = op_runner.change_content_sha256(edited)
        decision_path = self.decide(output, {"c-a": edited})
        code, out, track = self.resume(output, decision_path)
        self.assertEqual(code, 0, out)
        value = self.step(track, "compose")["decision"]["value"]
        self.assertEqual(value["edited"], ["c-a"])
        approved = {c["change_id"]: c for c in value["approved"]}
        self.assertEqual(approved["c-a"]["summary"], "a better summary")
        # and the downstream step was handed that object, not the original
        request = self.fake.calls[-1]["request"]
        self.assertEqual([c["summary"] for c in request["changes"]],
                         ["a better summary"])


class ResumeValidationTests(AuthorityCase):
    """A resume is a run: the same load-time refusals apply (review S3, S4)."""

    def test_resuming_a_dry_run_is_refused_by_name(self):
        fx.write_package(self.package, spec_doc())
        request = fx.write_request(self.root / "request.json",
                                   {"repositories": [REPO]})
        code, output = op_runner.run(self.package, request, dry_run=True)
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.resume(self.package, output["run_dir"])
        self.assertIn("nothing to resume",
                      "\n".join(caught.exception.problems))

    def test_a_cog_manifest_changed_while_paused_refuses_the_resume(self):
        code, output, _ = self.start()
        decision = self.decide(output, {"c-a": "approve"})
        # the write Cog stops declaring that it reaches github
        fx.write_cog(self.root, "write-github")
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("does not declare in its reaches",
                      "\n".join(caught.exception.problems))

    def test_a_resume_resolves_relative_paths_as_the_first_run_did(self):
        doc = spec_doc()
        doc["steps"][2]["input"] = {"here": {"$path": "beside-the-request.txt"},
                                    "changes": {"$from":
                                                "steps.compose.decision.approved"}}
        (self.root / "beside-the-request.txt").write_text("x")
        code, output, _ = self.start(doc=doc)
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.calls[-1]["request"]["here"],
                         str((self.root / "beside-the-request.txt").resolve()))

    def test_a_resume_restores_an_earlier_steps_envelope(self):
        doc = spec_doc()
        doc["steps"][2]["input"] = {
            "ok": {"$from": "steps.read-github.envelope.ok"},
            "changes": {"$from": "steps.compose.decision.approved"}}
        doc["steps"][2]["depends_on"] = ["compose", "read-github"]
        code, output, _ = self.start(doc=doc)
        decision = self.decide(output, {"c-a": "approve"})
        code, out, track = self.resume(output, decision)
        self.assertEqual(code, 0, out)
        self.assertIs(self.fake.calls[-1]["request"]["ok"], True)


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


class RunDirectoryDurabilityTests(AuthorityCase):
    """Review 3, finding 2: the run directory is created like every control
    directory under it — through `ensure_dir`, so its OWN entry is fsynced
    in `runs/`. A Track inside a directory whose entry never reached the
    disk is not durable."""

    def test_the_run_directory_is_created_through_ensure_dir(self):
        created, synced = [], []
        real_ensure, real_fsync = op_track.ensure_dir, op_track._fsync_dir

        def watched(path):
            created.append(Path(path))
            return real_ensure(path)

        op_track.ensure_dir = watched
        op_track._fsync_dir = lambda path: (synced.append(Path(path)),
                                            real_fsync(path))[1]
        try:
            code, output, _ = self.start()
        finally:
            op_track.ensure_dir, op_track._fsync_dir = real_ensure, real_fsync
        run_dir = Path(output["run_dir"])
        self.assertEqual(code, op_runner.PAUSED_EXIT, output)
        self.assertIn(run_dir, created)              # not a bare mkdir
        self.assertIn(run_dir.parent, synced)        # the run dir's OWN entry
        self.assertIn(run_dir, synced)               # and what is under it

    def test_a_missing_runs_ancestor_is_created_durably_too(self):
        synced = []
        real = op_track._fsync_dir
        op_track._fsync_dir = lambda path: (synced.append(Path(path)),
                                            real(path))[1]
        try:
            code, output, _ = self.start()
        finally:
            op_track._fsync_dir = real
        runs = Path(output["run_dir"]).parent
        self.assertIn(runs.parent, synced)           # runs/'s own entry


class ControlFileTests(unittest.TestCase):
    """The runner's own records: contained, atomic, durable (S1, S9)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()

    def test_a_control_file_is_never_written_through_a_symlinked_parent(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (self.run_dir / "grants").symlink_to(outside)
        with self.assertRaises(ValueError) as caught:
            op_track.write_json(self.run_dir / "grants" / "x.json", {},
                                base=self.run_dir)
        self.assertIn("outside the run directory", str(caught.exception))
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_control_file_is_never_written_through_a_symlinked_file(self):
        target = Path(self.tmp.name) / "victim.json"
        target.write_text("{}")
        (self.run_dir / "track.json").symlink_to(target)
        with self.assertRaises(ValueError) as caught:
            op_track.save({"schema": "x"}, self.run_dir)
        self.assertIn("symlink", str(caught.exception))
        self.assertEqual(target.read_text(), "{}")

    def test_a_control_file_is_never_written_through_a_within_run_alias(self):
        # Verification finding 3: `outputs` -> `grants` resolves INSIDE the
        # run, so resolving alone let it through. A link is a link.
        (self.run_dir / "grants").mkdir()
        (self.run_dir / "outputs").symlink_to(self.run_dir / "grants")
        with self.assertRaises(ValueError) as caught:
            op_track.write_json(self.run_dir / "outputs" / "0.json",
                                {"smuggled": True}, base=self.run_dir)
        self.assertIn("symlink", str(caught.exception))
        self.assertEqual(list((self.run_dir / "grants").iterdir()), [])

    def test_a_run_dir_operand_cannot_spell_a_control_directory(self):
        # Verification finding 3: the reserved-name check reads the
        # NORMALISED subpath, so a dynamic `outputs/../grants` names grants.
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.run_dir_path("outputs/../grants", str(self.run_dir))
        self.assertIn("reserved", "\n".join(caught.exception.problems))
        self.assertFalse((self.run_dir / "grants").exists())
        self.assertFalse((self.run_dir / "outputs").exists())

    def test_run_dir_refuses_a_symlinked_component_inside_the_run(self):
        (self.run_dir / "grants").mkdir()
        (self.run_dir / "outputs").symlink_to(self.run_dir / "grants")
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.run_dir_path("outputs/here", str(self.run_dir))
        self.assertIn("symlink", "\n".join(caught.exception.problems))
        self.assertEqual(list((self.run_dir / "grants").iterdir()), [])

    def test_every_newly_created_directory_entry_is_fsynced(self):
        # Verification finding 5: `mkdir(parents=True)` alone leaves a new
        # directory's ENTRY unpersisted in its parent.
        synced = []
        real, op_track._fsync_dir = op_track._fsync_dir, \
            lambda path: synced.append(Path(path))
        try:
            op_track.write_json(self.run_dir / "grants" / "step" / "0.json",
                                {"grant": True}, base=self.run_dir)
        finally:
            op_track._fsync_dir = real
        self.assertIn(self.run_dir, synced)                    # grants/
        self.assertIn(self.run_dir / "grants", synced)          # grants/step/
        self.assertIn(self.run_dir / "grants" / "step", synced)  # the file

    def test_a_journal_is_created_with_its_directory_entry_fsynced(self):
        synced = []
        real, op_track._fsync_dir = op_track._fsync_dir, \
            lambda path: synced.append(Path(path))
        try:
            op_track.touch_durable(self.run_dir / "journal" / "s.jsonl",
                                   base=self.run_dir)
        finally:
            op_track._fsync_dir = real
        self.assertTrue((self.run_dir / "journal" / "s.jsonl").exists())
        self.assertIn(self.run_dir, synced)              # journal/'s entry
        self.assertIn(self.run_dir / "journal", synced)  # the file's entry

    def test_an_atomic_write_leaves_no_temporary_file_behind(self):
        path = op_track.write_atomic(self.run_dir / "pending" / "s.md",
                                     "# decide\n", base=self.run_dir)
        self.assertEqual(path.read_text(), "# decide\n")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["s.md"])

    def test_an_empty_foreach_aggregate_restores_as_an_empty_list(self):
        record = op_track.step_record({"id": "each", "cog": {}}, "passed",
                                      elements=[],
                                      envelope=str(self.run_dir / "envelopes"))
        self.assertEqual(op_runner._results_of(record), ([], []))


class RunLockTests(unittest.TestCase):
    """One run, one process — an advisory `flock` on an open descriptor, not
    a pid file (contract §9b, verification finding 1). `flock` is bound to
    the OPEN FILE DESCRIPTION, so a second acquire conflicts even in this
    process: the tests need no second interpreter to prove it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()

    def take(self):
        lock = op_runner.RunLock(self.run_dir).acquire()
        self.addCleanup(lock.release)
        return lock

    def refused(self):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.RunLock(self.run_dir).acquire()
        return "\n".join(caught.exception.problems)

    def test_a_held_run_refuses_a_second_acquire_by_name(self):
        self.take()
        self.assertIn("one run, one process", self.refused())

    def test_an_empty_lock_file_is_still_a_held_lock(self):
        # The old pid file read an empty lock as `{}`, called it stale,
        # unlinked it and let a second process in while the first was still
        # between creating the file and writing its JSON.
        lock = self.take()
        os.ftruncate(lock.fd, 0)
        self.assertEqual((self.run_dir / "run.lock").read_bytes(), b"")
        self.assertIn("one run, one process", self.refused())

    def test_a_lock_file_left_behind_holds_nothing(self):
        # No takeover logic: a file whose holder is gone is simply lockable,
        # and its content — here a pid `os.kill` could never take — is never
        # consulted (verification finding 4).
        (self.run_dir / "run.lock").write_text(
            json.dumps({"pid": 10 ** 30, "at": "earlier"}))
        lock = self.take()
        self.assertTrue(lock.held)

    def test_a_lock_file_that_is_not_json_holds_nothing(self):
        (self.run_dir / "run.lock").write_text("not json at all")
        self.assertTrue(self.take().held)

    def test_releasing_keeps_the_file_and_frees_the_lock(self):
        op_runner.RunLock(self.run_dir).acquire().release()
        self.assertTrue((self.run_dir / "run.lock").exists())
        self.assertTrue(self.take().held)

    def test_a_symlinked_lock_file_is_refused_not_followed(self):
        # Review 3, finding 3, reproduced: `run.lock` pointing at
        # `track.json` was opened and TRUNCATED — the resume destroyed the
        # Track before reading it.
        track = self.run_dir / "track.json"
        track.write_text('{"schema": "openteams/op-track [0.1]"}')
        (self.run_dir / "run.lock").symlink_to(track)
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.RunLock(self.run_dir).acquire()
        self.assertIn("never opened through a link",
                      "\n".join(caught.exception.problems))
        self.assertEqual(track.read_text(),
                         '{"schema": "openteams/op-track [0.1]"}')

    def test_a_lock_file_linked_outside_the_run_is_refused(self):
        outside = Path(self.tmp.name) / "victim"
        outside.write_text("mine")
        (self.run_dir / "run.lock").symlink_to(outside)
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.RunLock(self.run_dir).acquire()
        self.assertIn("never opened through a link",
                      "\n".join(caught.exception.problems))
        self.assertEqual(outside.read_text(), "mine")

    def test_the_lock_is_opened_no_follow(self):
        # The containment check and the open are two moments; `O_NOFOLLOW`
        # closes the window between them, so the open itself can never
        # follow a link planted in between.
        flags = []
        real = op_runner.os.open

        def watched(path, mode, *rest):
            flags.append(mode)
            return real(path, mode, *rest)

        op_runner.os.open = watched
        try:
            self.take()
        finally:
            op_runner.os.open = real
        self.assertTrue(any(mode & os.O_NOFOLLOW for mode in flags), flags)

    def test_a_failure_while_writing_the_metadata_releases_the_lock(self):
        # The metadata is written AFTER the lock is held; if that write
        # fails, the descriptor is released before the exception leaves —
        # an embedding process that catches it is not left holding a lock
        # it does not know about (review 3, finding 3).
        real = op_runner.os.write

        def boom(fd, data):
            raise OSError(28, "no space left on device")

        op_runner.os.write = boom
        try:
            with self.assertRaises(OSError):
                op_runner.RunLock(self.run_dir).acquire()
        finally:
            op_runner.os.write = real
        self.assertIsNone(op_runner.LOCK_FD)
        self.assertTrue(self.take().held)             # free again

    def test_the_lock_descriptor_is_passed_to_every_cog_subprocess(self):
        # The lock protects the EXECUTION, including an outstanding Cog: the
        # descriptor is inherited, so a runner killed mid-invocation keeps
        # the run locked until its Cog is gone too.
        lock = self.take()
        captured = {}
        real = op_runner.subprocess.run

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(fx.envelope()), stderr="")

        op_runner.subprocess.run = fake_run
        try:
            op_runner.invoke_cog(self.run_dir, "ask", self.run_dir / "r.json")
        finally:
            op_runner.subprocess.run = real
        self.assertEqual(captured["pass_fds"], (lock.fd,))

    def test_no_lock_means_no_inherited_descriptor(self):
        captured = {}
        real = op_runner.subprocess.run

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(fx.envelope()), stderr="")

        op_runner.subprocess.run = fake_run
        try:
            op_runner.invoke_cog(self.run_dir, "ask", self.run_dir / "r.json")
        finally:
            op_runner.subprocess.run = real
        self.assertEqual(captured["pass_fds"], ())


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
