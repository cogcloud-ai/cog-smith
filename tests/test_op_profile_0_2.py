"""`openteams/op-manifest [0.2]` beside `[0.1]` (Op machinery 0.11.0).

A `[0.2]` document is held to the profile's tightenings at load, and to its
run-time rules when it runs; a `[0.1]` document — every Op written before
the profile — loads and runs exactly as it did under 0.10.0. Each rule is
tested both ways.
"""
import json
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner, op_spec

import test_op_authority as ta


def problems(doc):
    try:
        op_spec.OpSpec(doc)
    except op_spec.OpSpecError as exc:
        return exc.problems
    return []


def one(doc):
    return " ".join(problems(doc))


def cycle_doc(**cycle_extra):
    """draft -> review, with a cycle that restarts at draft on `revise`."""
    draft = fx.cog_step("draft")
    review = fx.cog_step("review", depends_on=["draft"])
    review["input"] = {"draft": {"$from": "steps.draft.payload"}}
    cycle = {"outcome_step": "review", "outcome_field": "verdict",
             "max_attempts": 2, "max_cost_units": 6,
             "costs": {"draft": 1, "review": 1},
             "transitions": {"revise": {"restart": "draft"}}}
    cycle.update(cycle_extra)
    return fx.spec_doc([draft, review], cycle=cycle)


class VersionTests(unittest.TestCase):
    def test_a_0_2_document_loads_with_titles(self):
        spec = op_spec.OpSpec(fx.spec_doc_0_2())
        self.assertEqual(spec.schema, op_spec.SCHEMA_STRING_0_2)
        self.assertEqual(spec.profile, op_spec.PROFILE_0_2)
        self.assertEqual(spec.title, "Test Op")
        self.assertEqual(spec.name, "Test Op")
        self.assertEqual(op_spec.step_title(spec.steps[0]), "Step first")
        self.assertEqual(spec.change_target_key, "target")

    def test_a_0_1_document_reads_name_as_title(self):
        spec = op_spec.OpSpec(fx.spec_doc())
        self.assertEqual(spec.schema, op_spec.SCHEMA_STRING_0_1)
        self.assertEqual(spec.profile, op_spec.PROFILE_0_1)
        self.assertEqual(spec.title, "Test Op")
        self.assertEqual(op_spec.step_title(spec.steps[0]), "Step first")
        self.assertEqual(spec.change_target_key, "repository")

    def test_schema_string_keeps_meaning_0_1_for_existing_callers(self):
        self.assertEqual(op_spec.SCHEMA_STRING, "openteams/op-manifest [0.1]")
        self.assertEqual(op_spec.SCHEMA_STRINGS,
                         ("openteams/op-manifest [0.1]",
                          "openteams/op-manifest [0.2]"))

    def test_0_2_refuses_name_at_the_top_level(self):
        doc = fx.spec_doc_0_2()
        doc["name"] = doc.pop("title")
        text = one(doc)
        self.assertIn("declares name:", text)
        self.assertIn("missing required field 'title'", text)

    def test_0_2_refuses_name_on_a_step(self):
        doc = fx.spec_doc_0_2()
        doc["steps"][0]["name"] = doc["steps"][0].pop("title")
        text = one(doc)
        self.assertIn("Op step 'first' declares name:", text)
        self.assertIn("rule step-title", text)
        self.assertNotIn("unknown key 'name'", text)

    def test_0_2_refuses_name_on_a_step_a_cycle_transition_prepares(self):
        doc = cycle_doc()
        doc["cycle"]["transitions"]["revise"]["prepare"] = [
            fx.cog_step("note")]
        doc["cycle"]["costs"]["note"] = 0
        doc = fx.as_0_2(doc)
        self.assertEqual(problems(doc), [])
        prepared = doc["cycle"]["transitions"]["revise"]["prepare"][0]
        prepared["name"] = prepared.pop("title")
        self.assertIn("Op step 'note' declares name:", one(doc))

    def test_0_1_keeps_refusing_title(self):
        doc = fx.spec_doc()
        doc["title"] = "Test Op"
        self.assertIn("unknown top-level Op spec key 'title'", one(doc))
        step = fx.cog_step("first", title="Step first")
        self.assertIn("unknown key 'title'", one(fx.spec_doc([step])))

    def test_0_1_still_requires_name(self):
        doc = fx.spec_doc()
        del doc["name"]
        self.assertIn("missing required field 'name'", one(doc))


class TypeTighteningTests(unittest.TestCase):
    """[0.2] refuses wrongly typed fields the reference accepted."""

    def test_identity_fields_are_strings(self):
        for field, value in (("id", 7), ("version", 1.0), ("title", ["x"])):
            with self.subTest(field=field):
                doc = fx.spec_doc_0_2()
                doc[field] = value
                text = one(doc)
                self.assertIn(f"declares {field} {value!r}, which is not a "
                              f"string", text)
                self.assertIn("rule required-identity", text)
                legacy = fx.spec_doc()
                legacy["name" if field == "title" else field] = value
                self.assertEqual(problems(legacy), [], field)

    def test_description_is_a_string(self):
        self.assertIn("description 3, which is not a string",
                      one(fx.spec_doc_0_2(description=3)))
        self.assertEqual(problems(fx.spec_doc(description=3)), [])

    def test_a_step_title_and_expected_outcome_are_strings(self):
        for field in ("title", "expected_outcome"):
            with self.subTest(field=field):
                doc = fx.spec_doc_0_2()
                doc["steps"][0][field] = ["not", "text"]
                self.assertIn(f"declares {field} ['not', 'text'], which is "
                              f"not a string", one(doc))
        legacy = fx.spec_doc([fx.cog_step("first", expected_outcome=[1])])
        self.assertEqual(problems(legacy), [])

    def test_input_required_is_a_boolean(self):
        inputs = [{"name": "note", "required": "yes"}]
        self.assertIn("declares required 'yes'; required is true or false",
                      one(fx.spec_doc_0_2(inputs=inputs)))
        self.assertEqual(problems(fx.spec_doc(inputs=inputs)), [])

    def test_track_records_is_a_list_of_strings(self):
        for records in ("track.json", ["steps", 3]):
            with self.subTest(records=records):
                doc = fx.spec_doc_0_2(track={"records": records})
                self.assertIn("track.records is a list of strings", one(doc))
                self.assertEqual(
                    problems(fx.spec_doc(track={"records": records})), [])
        self.assertEqual(
            problems(fx.spec_doc_0_2(track={"records": ["steps"]})), [])

    def test_an_empty_authority_beside_a_cycle_is_refused(self):
        doc = fx.as_0_2(cycle_doc())
        self.assertEqual(problems(doc), [])
        doc["authority"] = {}
        self.assertIn("pure steps without authority", one(doc))
        legacy = cycle_doc()
        legacy["authority"] = {}
        self.assertEqual(problems(legacy), [])


class CogVersionTests(unittest.TestCase):
    def test_0_2_refuses_a_step_whose_cog_states_no_version(self):
        for missing in (None, ""):
            with self.subTest(version=missing):
                doc = fx.spec_doc_0_2()
                if missing is None:
                    del doc["steps"][0]["cog"]["version"]
                else:
                    doc["steps"][0]["cog"]["version"] = missing
                text = one(doc)
                self.assertIn("missing cog.version", text)
                self.assertIn("rule cog-version-required", text)

    def test_0_1_accepts_a_step_whose_cog_states_no_version(self):
        doc = fx.spec_doc()
        del doc["steps"][0]["cog"]["version"]
        self.assertEqual(problems(doc), [])


class AuthorityVocabularyTests(unittest.TestCase):
    def doc(self, requirement, legacy=False):
        step = fx.cog_step("example-reader",
                           authority={"requires": [requirement]})
        return (fx.spec_doc if legacy else fx.spec_doc_0_2)([step])

    def test_0_2_refuses_repositories(self):
        requirement = {"resource": "github", "action": "read",
                       "repositories": [fx.REPO]}
        text = one(self.doc(requirement))
        self.assertIn("declares repositories", text)
        self.assertIn("rule targets-only", text)
        self.assertNotIn("unknown key", text)
        self.assertEqual(problems(self.doc(requirement, legacy=True)), [])

    def test_0_2_reads_targets(self):
        requirement = {"resource": "github", "action": "read",
                       "targets": [fx.REPO]}
        self.assertEqual(problems(self.doc(requirement)), [])

    def test_0_2_refuses_an_action_other_than_read_or_write(self):
        requirement = {"resource": "docs-site", "action": "publish",
                       "targets": [fx.REPO]}
        text = one(self.doc(requirement))
        self.assertIn("declares action 'publish'", text)
        self.assertIn("rule read-or-write", text)
        self.assertEqual(problems(self.doc(requirement, legacy=True)), [])


class OperatorKeySetTests(unittest.TestCase):
    def doc(self, value, legacy=False):
        step = fx.cog_step("first")
        step["input"] = {"note": {"$from": "inputs.note"}, "value": value}
        return (fx.spec_doc if legacy else fx.spec_doc_0_2)([step])

    def test_0_2_refuses_an_operator_key_beside_other_keys(self):
        for value in ({"$from": "inputs.note", "label": "x"},
                      {"$from": "inputs.note", "default": 1},
                      {"$default": 10},
                      {"$from": "inputs.note", "$stem": "x"},
                      {"$path": "a", "$run_dir": "b"}):
            with self.subTest(value=value):
                text = one(self.doc(value))
                self.assertIn("rule operator-exact-keyset", text)
                self.assertIn("input.value holds operator key", text)
                self.assertEqual(problems(self.doc(value, legacy=True)), [])

    def test_0_2_refuses_it_inside_ordinary_data_and_lists(self):
        value = {"outer": [{"$from": "inputs.note", "label": "x"}]}
        self.assertIn("input.value.outer[0] holds operator key",
                      one(self.doc(value)))

    def test_an_unknown_operator_is_still_refused_by_name(self):
        text = one(self.doc({"$join": ["a"], "label": "x"}))
        self.assertIn("unknown mapping operator", text)

    def test_exact_operators_and_literal_escapes_still_load(self):
        for value in ({"$from": "inputs.note", "$default": None},
                      {"$literal": {"$from": "inputs.note", "label": "x"}},
                      {"plain": {"nested": [1, "two"]}}):
            with self.subTest(value=value):
                self.assertEqual(problems(self.doc(value)), [])

    def test_outputs_and_requirement_targets_are_checked_too(self):
        doc = fx.spec_doc_0_2(outputs={"note": {"$from": "inputs.note",
                                                "label": "x"}})
        self.assertIn("outputs.note holds operator key", one(doc))
        step = fx.cog_step("example-reader", authority={"requires": [
            {"resource": "github", "action": "read",
             "targets": {"$from": "inputs.note", "label": "x"}}]})
        self.assertIn("targets holds operator key",
                      one(fx.spec_doc_0_2([step])))


class PathHomeTests(unittest.TestCase):
    def doc(self, operand, legacy=False):
        step = fx.cog_step("first")
        step["input"] = {"note": {"$from": "inputs.note"},
                         "source": {"$path": operand}}
        return (fx.spec_doc if legacy else fx.spec_doc_0_2)([step])

    def test_0_2_refuses_a_literal_path_beginning_with_a_tilde(self):
        for operand in ("~/texts.json", "~", "~other/x",
                        {"$literal": "~/texts.json"}):
            with self.subTest(operand=operand):
                text = one(self.doc(operand))
                self.assertIn("begins with ~", text)
                self.assertIn("rule path-no-home", text)
                self.assertEqual(problems(self.doc(operand, legacy=True)), [])

    def test_other_paths_and_computed_operands_load(self):
        for operand in ("texts.json", "/abs/texts.json", "a/~b",
                        {"$from": "inputs.note"}):
            with self.subTest(operand=operand):
                self.assertEqual(problems(self.doc(operand)), [])


class SharedRulesTests(unittest.TestCase):
    """The 0.10.0 refusals hold for both profiles."""

    def test_run_dir_root_and_reserved_entries(self):
        for operand in ("", ".", "envelopes/x", "requests"):
            for build in (fx.spec_doc, fx.spec_doc_0_2):
                with self.subTest(operand=operand, build=build.__name__):
                    step = fx.cog_step("first")
                    step["input"] = {"out": {"$run_dir": operand}}
                    self.assertTrue(problems(build([step])))

    def test_foreach_with_a_human_gate_over_changes(self):
        for build in (fx.spec_doc, fx.spec_doc_0_2):
            with self.subTest(build=build.__name__):
                step = fx.cog_step("propose", gate={"policy": "human"})
                step["foreach"] = {"items": {"$from": "inputs.note"},
                                   "as": "item"}
                self.assertIn("declares foreach and a human Gate over "
                              "changes", one(build([step])))



# ------------------------------------------------------------ run time --

class RunCase(unittest.TestCase):
    """A package, the Cogs it names on disk, and a faked seam whose
    envelopes name the Cog that was invoked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        self.real_invoke = op_runner.invoke_cog
        self.addCleanup(setattr, op_runner, "invoke_cog", self.real_invoke)
        fx.write_cog(self.root, "first")

    @staticmethod
    def own(payload=None):
        """An answer that names the Cog it came from, as a Cog does."""
        def answer(_call, _request, cog_dir=None):
            return fx.envelope(payload=payload or {"text": "done"},
                               cog_id="openteams/cog-first")
        return answer

    def run_op(self, doc, answers=None, request=None):
        fx.write_package(self.package, doc)
        path = fx.write_request(self.root / "request.json",
                                request if request is not None
                                else {"note": "hello"})
        self.fake = fx.FakeCog(answers if answers is not None
                               else {"ask": self.own()})
        op_runner.invoke_cog = self.fake
        return op_runner.run(self.package, path)

    def track(self, output):
        return json.loads(Path(output["track"]).read_text())

    def record(self, output, sid="first"):
        return next(s for s in self.track(output)["steps"] if s["id"] == sid)


class NamedCogTests(RunCase):
    def test_a_0_2_run_whose_cogs_are_present_at_their_versions_runs(self):
        code, output = self.run_op(fx.spec_doc_0_2())
        self.assertEqual(code, 0, output)
        self.assertEqual(self.record(output)["status"], "passed")

    def test_0_2_refuses_a_stated_version_other_than_the_cogs(self):
        doc = fx.spec_doc_0_2()
        doc["steps"][0]["cog"]["version"] = "0.2.0"
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.run_op(doc)
        self.assertIn("declares version '0.2.0'", str(caught.exception))
        self.assertFalse((self.package / "runs").exists())
        self.assertEqual(self.fake.calls, [])

    def test_0_1_only_warns_about_a_stated_version(self):
        doc = fx.spec_doc()
        doc["steps"][0]["cog"]["version"] = "0.2.0"
        code, output = self.run_op(doc)
        self.assertEqual(code, 0, output)
        findings = op_spec.cog_step_findings(
            doc["steps"][0], self.root / "cog-first")
        self.assertEqual([level for level, _ in findings], ["warn"])

    def test_0_2_refuses_a_named_cog_that_is_not_present(self):
        second = fx.cog_step("second", depends_on=["first"])
        doc = fx.spec_doc_0_2([fx.cog_step("first"), second])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.run_op(doc)
        self.assertIn("'../cog-second', which is not present",
                      str(caught.exception))
        self.assertFalse((self.package / "runs").exists())

    def test_0_1_runs_with_a_named_cog_that_is_not_present(self):
        second = fx.cog_step("second", depends_on=["first"])
        code, output = self.run_op(fx.spec_doc([fx.cog_step("first"), second]),
                                   answers={"ask": fx.envelope()})
        self.assertEqual(code, 0, output)

    def test_a_validator_still_warns_about_a_missing_cog(self):
        step = fx.as_0_2(fx.spec_doc())["steps"][0]
        findings = op_spec.cog_step_findings(step, self.root / "cog-absent",
                                             op_spec.PROFILE_0_2)
        self.assertEqual([level for level, _ in findings], ["warn"])


class Process:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class EnvelopeIdentityTests(RunCase):
    """Through the production `invoke_cog`: only the process is faked, so a
    synthesized failure carries the process evidence envelope v1 requires."""

    def answer_with(self, *envelopes, returncode=0):
        answers = list(envelopes)
        self.processes = []

        def run(command, **kwargs):
            self.processes.append(command)
            value = answers[min(len(self.processes), len(answers)) - 1]
            return Process(returncode, json.dumps(value))
        real = op_runner.subprocess.run
        self.addCleanup(setattr, op_runner.subprocess, "run", real)
        op_runner.subprocess.run = run

    def run_real(self, doc):
        fx.write_package(self.package, doc)
        path = fx.write_request(self.root / "request.json", {"note": "hello"})
        return op_runner.run(self.package, path)

    def test_0_2_passes_an_envelope_naming_the_steps_cog(self):
        self.answer_with(fx.envelope(payload={"text": "x"},
                                     cog_id="openteams/cog-first"))
        code, output = self.run_real(fx.spec_doc_0_2())
        self.assertEqual(code, 0, output)

    def test_0_2_fails_an_envelope_naming_another_cog(self):
        self.answer_with(fx.envelope(payload={"text": "x"},
                                     cog_id="openteams/cog-other"))
        code, output = self.run_real(fx.spec_doc_0_2())
        self.assertEqual(code, 1, output)
        record = self.record(output)
        self.assertEqual(record["status"], "failed")
        reasons = " ".join(record["gate"]["reasons"])
        self.assertIn("names cog id 'openteams/cog-other'", reasons)
        self.assertIn("failed invocation", reasons)
        envelope = json.loads(Path(record["envelope"]).read_text())
        self.assertIs(envelope["ok"], False)
        self.assertEqual(envelope["cog"]["id"], "openteams/cog-first")
        self.assertEqual(envelope["error"]["evidence"]["envelope"]["cog"]["id"],
                         "openteams/cog-other")
        self.assertEqual(fx.envelope_1_violations(envelope), [])

    def test_0_2_fails_an_envelope_naming_no_cog(self):
        answer = fx.envelope(payload={"text": "x"})
        del answer["cog"]
        self.answer_with(answer)
        code, output = self.run_real(fx.spec_doc_0_2())
        self.assertEqual(code, 1, output)
        self.assertIn("names cog id None",
                      " ".join(self.record(output)["gate"]["reasons"]))

    def test_an_identity_failure_is_retried_like_any_failed_invocation(self):
        self.answer_with(fx.envelope(cog_id="openteams/cog-other"),
                         fx.envelope(payload={"text": "x"},
                                     cog_id="openteams/cog-first"))
        doc = fx.spec_doc_0_2([fx.cog_step("first", on_fail="retry-once")])
        code, output = self.run_real(doc)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.processes), 2)

    def test_0_1_keeps_an_envelope_naming_another_cog(self):
        self.answer_with(fx.envelope(payload={"text": "x"},
                                     cog_id="openteams/cog-other"))
        code, output = self.run_real(fx.spec_doc())
        self.assertEqual(code, 0, output)

    def test_only_a_0_2_step_passes_the_seam_an_expected_cog(self):
        self.run_op(fx.spec_doc_0_2())
        self.assertEqual(self.fake.calls[0]["seam"],
                         {"expected_cog_id": "openteams/cog-first"})
        self.run_op(fx.spec_doc(), answers={"ask": fx.envelope()})
        self.assertEqual(self.fake.calls[0]["seam"], {})


class PathHomeAtRunTimeTests(RunCase):
    def doc(self, build):
        step = fx.cog_step("first")
        step["input"] = {"source": {"$path": {"$from": "inputs.note"}}}
        return build([step])

    def test_0_2_fails_the_step_when_a_computed_path_begins_with_a_tilde(self):
        code, output = self.run_op(self.doc(fx.spec_doc_0_2),
                                   request={"note": "~/texts.json"})
        self.assertEqual(code, 1, output)
        self.assertEqual(output["failed_step"], "first")
        self.assertIn("begins with ~", output["reason"])
        self.assertIn("rule path-no-home", output["reason"])
        self.assertEqual(self.fake.calls, [])

    def test_0_1_expands_it(self):
        code, output = self.run_op(self.doc(fx.spec_doc),
                                   request={"note": "~/texts.json"},
                                   answers={"ask": fx.envelope()})
        self.assertEqual(code, 0, output)
        self.assertEqual(self.fake.calls[0]["request"]["source"],
                         str((Path.home() / "texts.json").resolve()))

    def test_0_2_resolves_other_paths(self):
        code, output = self.run_op(self.doc(fx.spec_doc_0_2),
                                   request={"note": "texts.json"})
        self.assertEqual(code, 0, output)
        self.assertEqual(self.fake.calls[0]["request"]["source"],
                         str((self.root / "texts.json").resolve()))



# ------------------------------------------------- changes and targets --

class ChangeTargetTests(ta.AuthorityCase):
    """Under [0.2] a proposed change names what it changes as `target`: the
    write admission must list it, an edit may not change it, and the grant
    carries it. ([0.1] keeps `repository`: tests/test_op_authority.py.)"""

    def answers(self, *changes):
        def stamp(payload, sid):
            return fx.envelope(payload=payload, cog_id=f"openteams/cog-{sid}")
        return {"ask": [stamp({"items": []}, "example-reader"),
                        stamp({"changes": list(changes)}, "compose"),
                        stamp({"applied": True}, "example-writer")]}

    def write_with(self, verdicts, *changes):
        code, output, _ = self.start(doc=fx.as_0_2(ta.spec_doc()),
                                     answers=self.answers(*changes))
        self.assertEqual(code, op_runner.PAUSED_EXIT, output)
        decision = self.decide(output, verdicts)
        code, out, track = self.resume(output, decision)
        return code, out, self.step(track, "example-writer")

    def test_an_admitted_target_is_granted_under_target(self):
        code, out, record = self.write_with({"c-a": "approve"},
                                            fx.change_0_2("c-a"))
        self.assertEqual(code, 0, out)
        self.assertEqual(record["status"], "passed")
        grant = json.loads(Path(record["grant"]).read_text())
        granted = grant["operations"][0]["changes"][0]
        self.assertEqual(granted["target"], fx.REPO)
        self.assertNotIn("repository", granted)
        self.assertEqual(granted["content_sha256"],
                         op_runner.change_content_sha256(
                             fx.change_0_2("c-a")))

    def test_a_target_the_admission_does_not_list_is_denied(self):
        code, out, record = self.write_with(
            {"c-a": "approve"}, fx.change_0_2("c-a", target=ta.OTHER_REPO))
        self.assertEqual(record["status"], "denied")
        self.assertIn("names target 'someone-else/private'",
                      record["gate"]["reasons"][0])

    def test_a_0_1_shaped_change_is_denied_on_its_finer_target(self):
        """A [0.1] change names the admitted repository as `repository` and
        an item inside it as `target`; under [0.2] `target` is what is
        checked, so it is denied rather than written."""
        code, out, record = self.write_with({"c-a": "approve"},
                                            fx.change("c-a"))
        self.assertEqual(record["status"], "denied")
        self.assertIn(f"names target '{fx.REPO}#1'",
                      record["gate"]["reasons"][0])

    def test_a_change_naming_no_target_is_denied(self):
        change = fx.change_0_2("c-a")
        del change["target"]
        change["content_sha256"] = op_runner.change_content_sha256(change)
        code, out, record = self.write_with({"c-a": "approve"}, change)
        self.assertEqual(record["status"], "denied")
        self.assertIn("names target None", record["gate"]["reasons"][0])

    def test_an_edit_may_not_change_the_target(self):
        code, output, _ = self.start(doc=fx.as_0_2(ta.spec_doc()),
                                     answers=self.answers(
                                         fx.change_0_2("c-a")))
        edited = dict(fx.change_0_2("c-a"), target=ta.OTHER_REPO)
        decision = self.decide(output, {"c-a": edited})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.resume(output, decision)
        self.assertIn("retargets the change to target 'someone-else/private'",
                      "\n".join(caught.exception.problems))

    def test_an_edit_of_content_only_is_granted(self):
        edited = dict(fx.change_0_2("c-a"), summary="add two labels")
        code, out, record = self.write_with({"c-a": edited},
                                            fx.change_0_2("c-a"))
        self.assertEqual(code, 0, out)
        granted = json.loads(Path(record["grant"]).read_text())[
            "operations"][0]["changes"][0]
        self.assertEqual(granted["content_sha256"],
                         op_runner.change_content_sha256(edited))

    def test_apply_decision_defaults_to_the_0_1_field(self):
        pending = {"run_id": "r", "step": "compose",
                   "payload": {"changes": [fx.change("c-a")]}}
        pending["payload_sha256"] = op_runner.canonical_sha256(
            pending["payload"])
        edited = dict(fx.change("c-a"), target="elsewhere#2")
        decision = fx.decision_doc("r", "compose", pending["payload_sha256"],
                                   {"c-a": edited})
        value = op_runner.apply_decision(pending, decision)
        self.assertEqual(value["edited"], ["c-a"])
        with self.assertRaises(op_spec.OpSpecError):
            op_runner.apply_decision(pending, decision,
                                     change_target_key="target")


if __name__ == "__main__":
    unittest.main()
