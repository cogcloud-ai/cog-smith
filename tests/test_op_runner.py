"""Runner semantics: order, gates, foreach, on_fail, retries, dry run, Track.

Contract: planning/current/phase2-op-runner-contract.md §3, §4, §6. The Cog
seam is faked (`invoke_cog` is monkeypatched) — these tests are about the Op
layer's decisions, not about any Cog.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner, op_spec, op_track


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        self.real_invoke = op_runner.invoke_cog

    def tearDown(self):
        op_runner.invoke_cog = self.real_invoke
        self.tmp.cleanup()

    def go(self, doc, request=None, answers=None, dry_run=False, watcher=None):
        fx.write_package(self.package, doc)
        path = fx.write_request(self.root / "request.json",
                                request if request is not None else {"note": "hi"})
        fake = fx.FakeCog(answers or {}, watcher=watcher)
        op_runner.invoke_cog = fake
        code, output = op_runner.run(self.package, path, dry_run=dry_run)
        track = json.loads(Path(output["track"]).read_text())
        return code, output, track, fake

    def step(self, track, sid):
        return next(s for s in track["steps"] if s["id"] == sid)


class GateTests(unittest.TestCase):
    def test_three_legible_states(self):
        base = {"envelope": 1, "ok": True, "error": None, "problems": []}
        self.assertEqual(op_runner.gate_envelope(base)["status"], "pass")
        warned = dict(base, problems=[{"severity": "warn", "detail": "look"}])
        self.assertEqual(op_runner.gate_envelope(warned)["status"],
                         "pass-with-problems")
        errored = dict(base, problems=[{"severity": "error", "detail": "bad"}])
        self.assertEqual(op_runner.gate_envelope(errored)["status"], "fail")
        self.assertEqual(op_runner.gate_envelope(dict(base, ok=False))["status"],
                         "fail")

    def test_gate_records_policy_reasons_and_empty_guards(self):
        decision = op_runner.gate_envelope(
            {"envelope": 1, "ok": True,
             "problems": [{"severity": "warn", "detail": "thin evidence"}]})
        self.assertEqual(decision["policy"], op_spec.GATE_POLICY)
        self.assertEqual(decision["guards"], [])
        self.assertEqual(decision["reasons"], ["thin evidence"])

    def test_non_envelope_result_fails_the_gate(self):
        self.assertEqual(op_runner.gate_envelope({"ok": True})["status"], "fail")

    def test_parse_envelope_tolerates_a_command_preamble(self):
        value = {"envelope": 1, "ok": True}
        self.assertEqual(op_runner.parse_envelope("progress\n"
                                                  + json.dumps(value) + "\n"),
                         value)

    def test_parse_envelope_reads_a_pretty_printed_envelope(self):
        """cog-smith's context-cog machinery prints its envelope indented."""
        value = {"envelope": 1, "ok": False,
                 "problems": [{"check": "model-call-failed", "severity": "error"}]}
        stdout = "starting\n" + json.dumps(value, indent=2) + "\n"
        self.assertEqual(op_runner.parse_envelope(stdout), value)

    def test_parse_envelope_takes_the_last_envelope_printed(self):
        first = {"envelope": 1, "ok": False}
        last = {"envelope": 1, "ok": True}
        stdout = (json.dumps(first, indent=1) + "\nnoise {not json}\n"
                  + json.dumps(last, indent=1))
        self.assertEqual(op_runner.parse_envelope(stdout), last)

    def test_parse_envelope_refuses_output_without_one(self):
        with self.assertRaises(ValueError):
            op_runner.parse_envelope('trace\n{"result": "no envelope key"}\n')

    def test_combine_gates_takes_the_worst_element(self):
        passed = {"status": "pass", "reasons": []}
        warned = {"status": "pass-with-problems", "reasons": ["w"]}
        failed = {"status": "fail", "reasons": ["f"]}
        self.assertEqual(op_runner.combine_gates([passed, passed])["status"],
                         "pass")
        self.assertEqual(op_runner.combine_gates([passed, warned])["status"],
                         "pass-with-problems")
        self.assertEqual(op_runner.combine_gates([warned, failed])["status"],
                         "fail")


class LinearRunTests(RunnerCase):
    def doc(self):
        first = fx.cog_step("first", task="ask")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"},
                           "note": {"$from": "inputs.note"}}
        return fx.spec_doc([first, second],
                           outputs={"summary": {"$from": "steps.second.payload.text"}})

    def test_payload_flows_from_step_to_step(self):
        answers = {"ask": fx.envelope(payload={"text": "first output"}),
                   "summarize": fx.envelope(payload={"text": "final"})}
        code, output, track, fake = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(track["status"], "completed")
        self.assertEqual(fake.calls[1]["request"],
                         {"text": "first output", "note": "hi"})
        self.assertEqual(output["outputs"], {"summary": "final"})
        self.assertEqual(track["outputs"], {"summary": "final"})

    def test_track_shape(self):
        answers = {"ask": fx.envelope(payload={"text": "a"}),
                   "summarize": fx.envelope(payload={"text": "b"},
                                            problems=[{"severity": "warn",
                                                       "detail": "thin"}])}
        code, output, track, _ = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(track["schema"], "openteams/op-track [0.1]")
        self.assertEqual(track["op"], {"id": "openteams/op-test",
                                       "version": "0.1.0"})
        self.assertEqual(track["status"], "completed-with-problems")
        self.assertTrue(track["spec_sha256"])
        self.assertTrue(track["started_at"] and track["ended_at"])
        self.assertTrue(Path(track["input_request"]).is_absolute())
        first = self.step(track, "first")
        for key in ("id", "status", "cog", "task", "request", "envelope",
                    "binding", "problems", "gate", "elapsed_s", "attempts",
                    "elements"):
            self.assertIn(key, first)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["task"], "ask")
        self.assertEqual(first["binding"], {"model": "test-model"})
        self.assertTrue(Path(first["request"]).is_absolute())
        self.assertEqual(json.loads(Path(first["envelope"]).read_text())["ok"],
                         True)
        self.assertEqual(self.step(track, "second")["status"],
                         "passed-with-problems")

    def test_track_is_rewritten_after_every_step(self):
        seen = {}

        def watcher(fake, request_path):
            # invoked from inside the second step: the Track on disk must
            # already carry the first step's record.
            run_dir = Path(request_path).parent.parent
            track_file = run_dir / "track.json"
            if track_file.exists():
                seen[len(fake.calls)] = json.loads(track_file.read_text())

        answers = {"ask": fx.envelope(payload={"text": "a"}),
                   "summarize": fx.envelope(payload={"text": "b"})}
        self.go(self.doc(), answers=answers, watcher=watcher)
        # The Track on disk already carries the finished first step AND the
        # step that is being invoked, recorded `running` before the launch
        # (machinery 0.5.1, review B1).
        self.assertEqual([s["id"] for s in seen[2]["steps"]],
                         ["first", "second"])
        self.assertEqual([s["status"] for s in seen[2]["steps"]],
                         ["passed", "running"])
        self.assertEqual(seen[2]["status"], "running")

    def test_fan_in_reads_both_dependencies(self):
        a = fx.cog_step("a", task="ask")
        b = fx.cog_step("b", task="fetch")
        c = fx.cog_step("c", task="merge", depends_on=["a", "b"])
        c["input"] = {"left": {"$from": "steps.a.payload.text"},
                      "right": {"$from": "steps.b.payload.text"}}
        answers = {"ask": fx.envelope(payload={"text": "L"}),
                   "fetch": fx.envelope(payload={"text": "R"}),
                   "merge": fx.envelope(payload={"text": "LR"})}
        code, _, track, fake = self.go(fx.spec_doc([c, a, b]), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual([s["id"] for s in track["steps"]], ["a", "b", "c"])
        self.assertEqual(fake.calls[2]["request"], {"left": "L", "right": "R"})

    def test_invalid_request_exits_two(self):
        fx.write_package(self.package, self.doc())
        path = fx.write_request(self.root / "request.json", {"unknown": 1})
        real_root, op_runner.ROOT = op_runner.ROOT, self.package
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = op_runner.main(["--request", str(path)])
        finally:
            op_runner.ROOT = real_root
        self.assertEqual(code, 2)
        printed = json.loads(stdout.getvalue())
        self.assertFalse(printed["ok"])
        self.assertTrue(any("undeclared input" in p for p in printed["problems"]))

    def test_a_completed_run_through_main_exits_zero(self):
        fx.write_package(self.package, self.doc())
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        op_runner.invoke_cog = fx.FakeCog(
            {"ask": fx.envelope(payload={"text": "a"}),
             "summarize": fx.envelope(payload={"text": "b"})})
        real_root, op_runner.ROOT = op_runner.ROOT, self.package
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = op_runner.main(["--request", str(path)])
        finally:
            op_runner.ROOT = real_root
        self.assertEqual(code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["status"], "completed")
        self.assertEqual(sorted(printed), ["ok", "outputs", "run_dir",
                                           "status", "track"])


class OnFailTests(RunnerCase):
    def two_steps(self, on_fail):
        first = fx.cog_step("first", task="ask", on_fail=on_fail)
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        return fx.spec_doc([first, second])

    def test_stop_ends_the_run_and_names_the_failed_step(self):
        answers = {"ask": fx.envelope(ok=False),
                   "summarize": fx.envelope(payload={})}
        code, output, track, fake = self.go(self.two_steps("stop"),
                                            answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(track["status"], "failed")
        self.assertEqual(output["failed_step"], "first")
        self.assertEqual(self.step(track, "first")["status"], "failed")
        # every step of the spec is in the Track: the one that failed, and
        # the one the run never reached.
        self.assertEqual([(s["id"], s["status"]) for s in track["steps"]],
                         [("first", "failed"), ("second", "not-reached")])
        self.assertEqual(len(fake.calls), 1)

    def test_skip_blocks_dependents_and_completes_with_problems(self):
        answers = {"ask": fx.envelope(ok=False),
                   "summarize": fx.envelope(payload={})}
        code, output, track, fake = self.go(self.two_steps("skip"),
                                            answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(track["status"], "completed-with-problems")
        self.assertEqual(self.step(track, "first")["status"], "skipped")
        self.assertEqual(self.step(track, "second")["status"], "blocked")
        self.assertEqual(len(fake.calls), 1)

    def test_retry_once_reruns_only_a_transport_failure(self):
        answers = {"ask": [fx.envelope(ok=False),
                           fx.envelope(payload={"text": "second try"})],
                   "summarize": fx.envelope(payload={"text": "done"})}
        code, _, track, fake = self.go(self.two_steps("retry-once"),
                                       answers=answers)
        self.assertEqual(code, 0)
        first = self.step(track, "first")
        self.assertEqual(first["status"], "passed")
        self.assertEqual(len(first["attempts"]), 2)
        self.assertTrue(all(Path(p).exists() for p in first["attempts"]))
        self.assertEqual(
            json.loads(Path(first["attempts"][0]).read_text())["ok"], False)
        self.assertEqual(len([c for c in fake.calls if c["task"] == "ask"]), 2)

    def test_retry_once_never_retries_an_error_problem(self):
        answers = {"ask": fx.envelope(payload={"text": "x"},
                                      problems=[{"severity": "error",
                                                 "detail": "ungrounded"}]),
                   "summarize": fx.envelope(payload={})}
        code, output, track, fake = self.go(self.two_steps("retry-once"),
                                            answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "first")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.step(track, "first")["attempts"], [])

    def test_retry_once_that_fails_again_stops_the_run(self):
        answers = {"ask": [fx.envelope(ok=False), fx.envelope(ok=False)],
                   "summarize": fx.envelope(payload={})}
        code, _, track, fake = self.go(self.two_steps("retry-once"),
                                       answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(self.step(track, "first")["status"], "failed")
        self.assertEqual(len(fake.calls), 2)


class ForeachTests(RunnerCase):
    def doc(self, on_fail="stop", with_consumer=False):
        step = fx.cog_step("classify", task="classify", on_fail=on_fail)
        step["foreach"] = {"items": {"$from": "inputs.items"}, "as": "item"}
        step["input"] = {"title": {"$from": "item.title"}}
        steps = [step]
        if with_consumer:
            after = fx.cog_step("report", task="report", depends_on=["classify"])
            after["input"] = {"all": {"$from": "steps.classify.payload"}}
            steps.append(after)
        return fx.spec_doc(steps, inputs=[{"name": "items"}])

    def request(self):
        return {"items": [{"title": "one"}, {"title": "two"}]}

    def test_each_element_gets_its_own_request_envelope_and_gate(self):
        answers = {"classify": lambda n, req: fx.envelope(
            payload={"label": req["title"]})}
        code, _, track, fake = self.go(self.doc(), request=self.request(),
                                       answers=answers)
        self.assertEqual(code, 0)
        step = self.step(track, "classify")
        self.assertEqual(step["status"], "passed")
        self.assertEqual(len(step["elements"]), 2)
        self.assertEqual([e["index"] for e in step["elements"]], [0, 1])
        for element in step["elements"]:
            self.assertTrue(Path(element["request"]).exists())
            self.assertTrue(Path(element["envelope"]).exists())
            self.assertEqual(element["gate"]["status"], "pass")
        self.assertEqual([c["request"] for c in fake.calls],
                         [{"title": "one"}, {"title": "two"}])

    def test_step_payload_is_the_list_of_element_payloads(self):
        answers = {"classify": lambda n, req: fx.envelope(
            payload={"label": req["title"]}),
            "report": fx.envelope(payload={"text": "ok"})}
        code, _, _, fake = self.go(self.doc(with_consumer=True),
                                   request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls[-1]["request"],
                         {"all": [{"label": "one"}, {"label": "two"}]})

    def test_a_warning_element_makes_the_step_pass_with_problems(self):
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(payload={"label": "b"},
                                            problems=[{"severity": "warn",
                                                       "detail": "unsure"}])]}
        code, _, track, _ = self.go(self.doc(), request=self.request(),
                                    answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(self.step(track, "classify")["status"],
                         "passed-with-problems")
        self.assertEqual(track["status"], "completed-with-problems")

    def test_a_failed_element_fails_the_step_under_stop(self):
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False)]}
        code, output, track, _ = self.go(self.doc(), request=self.request(),
                                         answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "classify")
        step = self.step(track, "classify")
        self.assertEqual([e["gate"]["status"] for e in step["elements"]],
                         ["pass", "fail"])

    def test_skip_applies_to_the_whole_foreach_step(self):
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False)],
                   "report": fx.envelope(payload={})}
        code, _, track, fake = self.go(self.doc(on_fail="skip",
                                                with_consumer=True),
                                       request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(self.step(track, "classify")["status"], "skipped")
        self.assertEqual(self.step(track, "report")["status"], "blocked")
        self.assertEqual(len([c for c in fake.calls if c["task"] == "report"]), 0)

    def test_retry_once_applies_per_element(self):
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False),
                                fx.envelope(payload={"label": "b"})]}
        code, _, track, fake = self.go(self.doc(on_fail="retry-once"),
                                       request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        step = self.step(track, "classify")
        self.assertEqual(step["status"], "passed")
        self.assertEqual(len(step["elements"][1]["attempts"]), 2)
        self.assertEqual(len(fake.calls), 3)


class RepeatTests(RunnerCase):
    """`repeat: {count, require}` — narrowing contract §2 (machinery 0.6.0).

    The same request, invoked k times; a Gate per repeat; the step's payload
    is the LIST of them; the step's Gate needs n of them to have passed."""

    def doc(self, count=3, require=1, on_fail="stop", with_consumer=False,
            **extra):
        step = fx.cog_step("draft", task="draft", on_fail=on_fail,
                           repeat={"count": count, "require": require},
                           **extra)
        steps = [step]
        if with_consumer:
            after = fx.cog_step("merge", task="merge", depends_on=["draft"])
            after["input"] = {"results": {"$from": "steps.draft.payload"}}
            steps.append(after)
        return fx.spec_doc(steps)

    def test_the_same_request_is_invoked_count_times(self):
        answers = {"draft": lambda n, req: fx.envelope(payload={"n": n})}
        code, _, track, fake = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 3)
        # ONE request, invoked three times: agreeing answers are only
        # evidence when they answered the same question.
        self.assertEqual([c["request"] for c in fake.calls],
                         [{"note": "hi"}] * 3)
        self.assertEqual({c["request_path"] for c in fake.calls},
                         {self.step(track, "draft")["request"]})

    def test_each_repeat_writes_its_own_envelope_file(self):
        answers = {"draft": lambda n, req: fx.envelope(payload={"n": n})}
        _, _, track, _ = self.go(self.doc(), answers=answers)
        record = self.step(track, "draft")
        self.assertEqual([Path(r["envelope"]).name for r in record["repeats"]],
                         ["draft.r0.json", "draft.r1.json", "draft.r2.json"])
        for entry in record["repeats"]:
            self.assertTrue(Path(entry["envelope"]).exists())
        # The step has no single envelope: the repeats name one file each.
        self.assertIsNone(record["envelope"])

    def test_the_step_payload_is_the_list_of_repeat_payloads(self):
        answers = {"draft": lambda n, req: fx.envelope(payload={"n": n}),
                   "merge": fx.envelope(payload={"text": "merged"})}
        code, _, _, fake = self.go(self.doc(with_consumer=True),
                                   answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls[-1]["request"],
                         {"results": [{"n": 1}, {"n": 2}, {"n": 3}]})

    def test_a_failed_repeat_contributes_null_and_the_step_passes_with_problems(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(payload={"n": 3})],
                   "merge": fx.envelope(payload={})}
        code, _, track, fake = self.go(self.doc(require=2, with_consumer=True),
                                       answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "passed-with-problems")
        self.assertEqual([r["gate"]["status"] for r in record["repeats"]],
                         ["pass", "fail", "pass"])
        self.assertEqual(fake.calls[-1]["request"],
                         {"results": [{"n": 1}, None, {"n": 3}]})

    def test_the_step_fails_when_fewer_than_require_repeats_passed(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(ok=False)]}
        code, output, track, fake = self.go(self.doc(require=2),
                                            answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "draft")
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "failed")
        self.assertIn("1 of 3 repeats passed; 2 required.",
                      record["gate"]["reasons"])
        # every repeat still ran: the Gate decides after the evidence is in.
        self.assertEqual(len(fake.calls), 3)

    def test_all_passing_repeats_with_no_problems_pass_cleanly(self):
        answers = {"draft": fx.envelope(payload={"n": 1})}
        code, _, track, _ = self.go(self.doc(count=2, require=1),
                                    answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["gate"]["status"], "pass")

    def test_a_repeat_with_problems_makes_the_step_pass_with_problems(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(payload={"n": 2},
                                         problems=[{"severity": "warn",
                                                    "detail": "unsure"}])]}
        code, _, track, _ = self.go(self.doc(count=2, require=2),
                                    answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "passed-with-problems")
        self.assertEqual([p["detail"] for p in record["problems"]], ["unsure"])

    def test_retry_once_applies_per_repeat(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(payload={"n": 2})]}
        code, _, track, fake = self.go(
            self.doc(count=2, require=2, on_fail="retry-once"),
            answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["repeats"][0]["attempts"], [])
        self.assertEqual(len(record["repeats"][1]["attempts"]), 2)
        self.assertEqual(len(fake.calls), 3)

    def test_the_track_records_every_repeat(self):
        answers = {"draft": lambda n, req: fx.envelope(payload={"n": n})}
        _, _, track, _ = self.go(self.doc(count=2, require=1), answers=answers)
        record = self.step(track, "draft")
        self.assertEqual(record["repeat"], {"count": 2, "require": 1})
        self.assertEqual(len(record["repeats"]), 2)
        for index, entry in enumerate(record["repeats"]):
            self.assertEqual(entry["index"], index)
            for key in ("index", "envelope", "gate", "binding", "elapsed_s"):
                self.assertIn(key, entry)
            self.assertEqual(entry["binding"], {"model": "test-model"})
            self.assertIsInstance(entry["elapsed_s"], float)
        self.assertEqual(record["binding"], {"model": "test-model"})

    def test_a_step_that_does_not_repeat_keeps_the_old_shape(self):
        answers = {"ask": fx.envelope(payload={"text": "a"})}
        _, _, track, _ = self.go(fx.spec_doc(), answers=answers)
        record = self.step(track, "first")
        self.assertIsNone(record["repeat"])
        self.assertIsNone(record["repeats"])
        self.assertTrue(record["envelope"].endswith("first.json"))

    def test_the_dry_run_shows_the_repeat_count(self):
        code, _, track, fake = self.go(self.doc(count=3, require=2),
                                       answers={}, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.step(track, "draft")["repeat"],
                         {"count": 3, "require": 2})


class RepeatForeachTests(RunnerCase):
    """`repeat` inside a `foreach`: each ELEMENT is repeated, so the step's
    payload is a list of lists (narrowing contract §2)."""

    def doc(self, count=2, require=1, with_consumer=False):
        step = fx.cog_step("detect", task="detect",
                           repeat={"count": count, "require": require})
        step["foreach"] = {"items": {"$from": "inputs.batches"}, "as": "batch"}
        step["input"] = {"batch": {"$from": "batch.id"}}
        steps = [step]
        if with_consumer:
            after = fx.cog_step("merge", task="merge", depends_on=["detect"])
            after["input"] = {"results": {"$from": "steps.detect.payload"}}
            steps.append(after)
        return fx.spec_doc(steps, inputs=[{"name": "batches"}])

    def request(self):
        return {"batches": [{"id": "b1"}, {"id": "b2"}]}

    def test_every_element_is_repeated_over_one_request(self):
        answers = {"detect": lambda n, req: fx.envelope(
            payload={"batch": req["batch"], "n": n}),
            "merge": fx.envelope(payload={})}
        code, _, track, fake = self.go(self.doc(with_consumer=True),
                                       request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual([c["request"] for c in fake.calls[:4]],
                         [{"batch": "b1"}, {"batch": "b1"},
                          {"batch": "b2"}, {"batch": "b2"}])
        record = self.step(track, "detect")
        self.assertEqual(record["repeat"], {"count": 2, "require": 1})
        self.assertIsNone(record["repeats"])
        for index, element in enumerate(record["elements"]):
            self.assertEqual([Path(r["envelope"]).name
                              for r in element["repeats"]],
                             [f"{index}.r0.json", f"{index}.r1.json"])
        # a list of lists, in element order then repeat order
        self.assertEqual(fake.calls[-1]["request"],
                         {"results": [[{"batch": "b1", "n": 1},
                                       {"batch": "b1", "n": 2}],
                                      [{"batch": "b2", "n": 3},
                                       {"batch": "b2", "n": 4}]]})

    def test_a_failed_repeat_is_null_inside_its_elements_list(self):
        answers = {"detect": [fx.envelope(payload={"a": 1}),
                              fx.envelope(ok=False),
                              fx.envelope(payload={"b": 1}),
                              fx.envelope(payload={"b": 2})],
                   "merge": fx.envelope(payload={})}
        code, _, track, fake = self.go(self.doc(with_consumer=True),
                                       request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "detect")
        self.assertEqual([e["gate"]["status"] for e in record["elements"]],
                         ["pass-with-problems", "pass"])
        self.assertEqual(record["status"], "passed-with-problems")
        self.assertEqual(fake.calls[-1]["request"],
                         {"results": [[{"a": 1}, None],
                                      [{"b": 1}, {"b": 2}]]})

    def test_an_element_that_misses_require_fails_the_step(self):
        answers = {"detect": [fx.envelope(ok=False), fx.envelope(ok=False),
                              fx.envelope(payload={"b": 1}),
                              fx.envelope(payload={"b": 2})]}
        code, output, track, _ = self.go(self.doc(require=1),
                                         request=self.request(),
                                         answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "detect")
        element = self.step(track, "detect")["elements"][0]
        self.assertEqual(element["gate"]["status"], "fail")


class RepeatResumeTests(RunnerCase):
    """A resume never re-runs a repeat that passed; it re-runs the failed
    repeats of the step that stopped the run (narrowing contract §2)."""

    def doc(self):
        draft = fx.cog_step("draft", task="draft",
                            repeat={"count": 3, "require": 3})
        after = fx.cog_step("merge", task="merge", depends_on=["draft"])
        after["input"] = {"results": {"$from": "steps.draft.payload"}}
        return fx.spec_doc([draft, after])

    def test_only_the_failed_repeats_run_again(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(payload={"n": 3})],
                   "merge": fx.envelope(payload={})}
        code, output, track, fake = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(track["failed_step"], "draft")
        failed = self.step(track, "draft")
        kept = [Path(r["envelope"]) for r in failed["repeats"]]
        stamps = {p: p.stat().st_mtime_ns for p in (kept[0], kept[2])}

        # ---- the resume: one new invocation, for repeat 1 alone.
        again = fx.FakeCog({"draft": fx.envelope(payload={"n": 2}),
                            "merge": fx.envelope(payload={"text": "ok"})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, output["run_dir"])
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual(code, 0)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual([c["task"] for c in again.calls], ["draft", "merge"])
        record = self.step(resumed, "draft")
        self.assertEqual(record["status"], "passed")
        # the two that passed were read back, not paid for again
        for path, stamp in stamps.items():
            self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(again.calls[-1]["request"],
                         {"results": [{"n": 1}, {"n": 2}, {"n": 3}]})

    def test_a_resumed_repeat_keeps_the_earlier_records(self):
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(payload={"n": 3})],
                   "merge": fx.envelope(payload={})}
        _, output, track, _ = self.go(self.doc(), answers=answers)
        before = self.step(track, "draft")["repeats"][0]
        op_runner.invoke_cog = fx.FakeCog(
            {"draft": fx.envelope(payload={"n": 2}),
             "merge": fx.envelope(payload={})})
        _, output = op_runner.resume(self.package, output["run_dir"])
        resumed = json.loads(Path(output["track"]).read_text())
        after = self.step(resumed, "draft")["repeats"]
        self.assertEqual(after[0], before)
        self.assertEqual([r["gate"]["status"] for r in after],
                         ["pass", "pass", "pass"])


class RepeatRequestIdentityTests(RunnerCase):
    """A repeat result belongs to the REQUEST it answered (machinery 0.6.1,
    narrowing contract §8, finding 2).

    Between a failed run and its resume an upstream envelope can change on
    disk. Reuse used to go by position alone, so a passed repeat's answer to
    the old question could be counted beside a fresh answer to the new one.
    Each repeat record now carries `request_sha256` and reuse requires a
    match."""

    def doc(self, count=2, require=2):
        read = fx.cog_step("read", task="read")
        draft = fx.cog_step("draft", task="draft", depends_on=["read"],
                            repeat={"count": count, "require": require})
        draft["input"] = {"items": {"$from": "steps.read.payload"}}
        return fx.spec_doc([read, draft])

    def first_run(self):
        """A failed run: `read` passes, draft's repeat 0 passes and repeat 1
        fails, so the step misses `require` and the run stops there."""
        answers = {"read": fx.envelope(payload={"v": "a"}),
                   "draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False)]}
        code, output, track, _ = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(track["failed_step"], "draft")
        return output, track

    def test_the_track_records_the_request_each_repeat_answered(self):
        _, track = self.first_run()
        record = self.step(track, "draft")
        digest = op_runner.canonical_sha256({"items": {"v": "a"}})
        self.assertEqual([r["request_sha256"] for r in record["repeats"]],
                         [digest, digest])

    def test_a_changed_upstream_payload_re_runs_the_passed_repeat(self):
        output, track = self.first_run()
        run_dir = Path(output["run_dir"])
        kept = Path(self.step(track, "draft")["repeats"][0]["envelope"])
        stamp = kept.stat().st_mtime_ns

        # The upstream envelope changes on disk between the two attempts:
        # what `draft` is about to be asked is no longer what repeat 0
        # answered.
        upstream = run_dir / "envelopes" / "read.json"
        changed = json.loads(upstream.read_text())
        changed["payload"] = {"v": "b"}
        upstream.write_text(json.dumps(changed, indent=2))

        again = fx.FakeCog({"draft": lambda n, req: fx.envelope(
            payload={"n": n, "v": req["items"]["v"]})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, run_dir)
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual(code, 0)
        # BOTH repeats ran again, against the new request — no answer to the
        # old question is counted toward this step's `require`.
        self.assertEqual([c["task"] for c in again.calls], ["draft", "draft"])
        self.assertEqual([c["request"] for c in again.calls],
                         [{"items": {"v": "b"}}] * 2)
        self.assertNotEqual(kept.stat().st_mtime_ns, stamp)
        record = self.step(resumed, "draft")
        self.assertEqual(record["status"], "passed")
        self.assertEqual({r["request_sha256"] for r in record["repeats"]},
                         {op_runner.canonical_sha256({"items": {"v": "b"}})})

    def test_an_unchanged_request_still_reuses_the_passed_repeat(self):
        output, track = self.first_run()
        kept = Path(self.step(track, "draft")["repeats"][0]["envelope"])
        stamp = kept.stat().st_mtime_ns
        again = fx.FakeCog({"draft": fx.envelope(payload={"n": 2})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        # one invocation, for the repeat that failed; the passed one was read
        # back and its envelope was not rewritten.
        self.assertEqual(len(again.calls), 1)
        self.assertEqual(kept.stat().st_mtime_ns, stamp)

    def test_a_changed_element_order_reuses_by_request_not_by_index(self):
        """Inside a `foreach`, identity is PER ELEMENT: element 0 of the
        resume may be a different batch from element 0 of the failed run."""
        read = fx.cog_step("read", task="read")
        detect = fx.cog_step("detect", task="detect", depends_on=["read"],
                             repeat={"count": 2, "require": 2})
        detect["foreach"] = {"items": {"$from": "steps.read.payload.batches"},
                             "as": "batch"}
        detect["input"] = {"batch": {"$from": "batch.id"}}
        doc = fx.spec_doc([read, detect])
        fx.write_package(self.package, doc)
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        # b1 passes twice; b2 passes once and then fails, stopping the run.
        op_runner.invoke_cog = fx.FakeCog(
            {"read": fx.envelope(payload={"batches": [{"id": "b1"},
                                                      {"id": "b2"}]}),
             "detect": [fx.envelope(payload={"b": "b1.0"}),
                        fx.envelope(payload={"b": "b1.1"}),
                        fx.envelope(payload={"b": "b2.0"}),
                        fx.envelope(ok=False)]})
        code, output = op_runner.run(self.package, path)
        self.assertEqual(code, 1)

        # The upstream batches come back in the other order.
        upstream = Path(output["run_dir"]) / "envelopes" / "read.json"
        changed = json.loads(upstream.read_text())
        changed["payload"] = {"batches": [{"id": "b2"}, {"id": "b1"}]}
        upstream.write_text(json.dumps(changed, indent=2))

        again = fx.FakeCog({"detect": lambda n, req: fx.envelope(
            payload={"b": req["batch"]})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        # Nothing is reused: element 0 is b2 now, and the answers recorded at
        # position 0 were about b1.
        self.assertEqual([c["request"]["batch"] for c in again.calls],
                         ["b2", "b2", "b1", "b1"])
        resumed = json.loads(Path(output["track"]).read_text())
        record = self.step(resumed, "detect")
        self.assertEqual(
            [[p["b"] for p in element] for element
             in op_runner._payloads_of(op_runner._element_envelopes(record))],
            [["b2", "b2"], ["b1", "b1"]])


class RepeatDurabilityTests(RunnerCase):
    """Every completed repeat is on disk before the next one is invoked
    (machinery 0.6.1, narrowing contract §8, finding 5).

    A repeat that finished is evidence that was paid for. A crash between
    repeats used to leave the Track holding only the step's `running`
    record, so the resume invoked work whose envelope was already there."""

    class Interrupted(Exception):
        """Stands in for the process dying mid-step."""

    def doc(self, count=2, require=2):
        draft = fx.cog_step("draft", task="draft",
                            repeat={"count": count, "require": require})
        after = fx.cog_step("merge", task="merge", depends_on=["draft"])
        after["input"] = {"results": {"$from": "steps.draft.payload"}}
        return fx.spec_doc([draft, after])

    def interrupt_at(self, calls, answers, task_name="draft"):
        """A fake Cog that answers from `answers` and raises on the
        invocation of `task_name` numbered `calls` (1-based)."""
        script = fx.FakeCog(answers)

        def fake(cog_dir, task, request_path, **seam):
            if task == task_name and len([c for c in script.calls
                                          if c["task"] == task_name]) + 1 == calls:
                script.calls.append({"task": task, "request_path":
                                     str(request_path), "raised": True})
                raise RepeatDurabilityTests.Interrupted("power loss")
            return script(cog_dir, task, request_path, **seam)
        fake.script = script
        return fake

    def track_of(self, run_dir):
        return json.loads((Path(run_dir) / "track.json").read_text())

    def only_run_dir(self):
        return next((self.root / "runs").iterdir())

    def test_a_crash_between_repeats_leaves_repeat_zero_on_the_track(self):
        fx.write_package(self.package, self.doc())
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        op_runner.invoke_cog = self.interrupt_at(
            2, {"draft": fx.envelope(payload={"n": 1})})
        with self.assertRaises(self.Interrupted):
            op_runner.run(self.package, path, runs_dir=self.root / "runs")

        # The Track already holds repeat 0 — written before repeat 1 was
        # invoked, not after the step completed.
        run_dir = self.only_run_dir()
        record = self.step(self.track_of(run_dir), "draft")
        self.assertEqual(record["status"], "running")
        self.assertEqual(len(record["repeats"]), 1)
        self.assertEqual(record["repeats"][0]["gate"]["status"], "pass")
        self.assertTrue(Path(record["repeats"][0]["envelope"]).exists())

        # ---- the resume invokes repeat 1 alone.
        again = fx.FakeCog({"draft": fx.envelope(payload={"n": 2}),
                            "merge": fx.envelope(payload={})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, run_dir)
        self.assertEqual(code, 0)
        self.assertEqual([c["task"] for c in again.calls], ["draft", "merge"])
        self.assertEqual(again.calls[-1]["request"],
                         {"results": [{"n": 1}, {"n": 2}]})

    def test_marking_a_step_running_again_keeps_its_repeat_records(self):
        """The resume re-runs repeat 0 and crashes doing it: the repeats that
        already passed must still be on the Track afterwards."""
        fx.write_package(self.package, self.doc(count=3, require=3))
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        op_runner.invoke_cog = fx.FakeCog(
            {"draft": [fx.envelope(ok=False), fx.envelope(payload={"n": 2}),
                       fx.envelope(payload={"n": 3})],
             "merge": fx.envelope(payload={})})
        code, output = op_runner.run(self.package, path,
                                     runs_dir=self.root / "runs")
        self.assertEqual(code, 1)
        run_dir = Path(output["run_dir"])

        op_runner.invoke_cog = self.interrupt_at(1, {"draft": fx.envelope()})
        with self.assertRaises(self.Interrupted):
            op_runner.resume(self.package, run_dir)
        record = self.step(self.track_of(run_dir), "draft")
        self.assertEqual(record["status"], "running")
        self.assertEqual([r["gate"]["status"] for r in record["repeats"]],
                         ["fail", "pass", "pass"])

        # ---- the second resume pays for repeat 0 only.
        again = fx.FakeCog({"draft": fx.envelope(payload={"n": 1}),
                            "merge": fx.envelope(payload={})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, run_dir)
        self.assertEqual(code, 0)
        self.assertEqual([c["task"] for c in again.calls], ["draft", "merge"])
        self.assertEqual(again.calls[-1]["request"],
                         {"results": [{"n": 1}, {"n": 2}, {"n": 3}]})

    def test_a_crash_between_elements_keeps_the_finished_elements(self):
        detect = fx.cog_step("detect", task="detect",
                             repeat={"count": 2, "require": 2})
        detect["foreach"] = {"items": {"$from": "inputs.batches"},
                             "as": "batch"}
        detect["input"] = {"batch": {"$from": "batch.id"}}
        fx.write_package(self.package, fx.spec_doc([detect],
                                                   inputs=[{"name": "batches"}]))
        path = fx.write_request(self.root / "request.json",
                                {"batches": [{"id": "b1"}, {"id": "b2"}]})
        op_runner.invoke_cog = self.interrupt_at(
            4, {"detect": lambda n, req: fx.envelope(payload={"b": n})},
            task_name="detect")
        with self.assertRaises(self.Interrupted):
            op_runner.run(self.package, path, runs_dir=self.root / "runs")
        record = self.step(self.track_of(self.only_run_dir()), "detect")
        # element 0 finished, element 1's first repeat finished: three
        # envelopes were paid for and three are recorded.
        self.assertEqual([len(e["repeats"]) for e in record["elements"]],
                         [2, 1])


class RepeatProblemsTests(RunnerCase):
    """The step's `problems` aggregate EVERY repeat's envelope, the failed
    ones included (machinery 0.6.1, narrowing contract §8, finding 6).

    A failed repeat contributes `null` to the payload a later step reads.
    That null is a downstream masking decision; it never erases what the Cog
    reported, which is what the Track is for."""

    def test_a_failed_repeats_problems_reach_the_step_record(self):
        refused = {"check": "model-refused", "severity": "error",
                   "detail": "the model would not answer"}
        answers = {"draft": [fx.envelope(payload=None, problems=[refused]),
                             fx.envelope(payload={"n": 2})]}
        step = fx.cog_step("draft", task="draft",
                           repeat={"count": 2, "require": 1})
        code, _, track, _ = self.go(fx.spec_doc([step]), answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "draft")
        self.assertEqual(record["status"], "passed-with-problems")
        self.assertEqual([r["gate"]["status"] for r in record["repeats"]],
                         ["fail", "pass"])
        self.assertEqual(record["problems"], [refused])

    def test_a_failed_repeats_problems_reach_a_foreach_step_too(self):
        refused = {"check": "model-refused", "severity": "error",
                   "detail": "the model would not answer"}
        detect = fx.cog_step("detect", task="detect",
                             repeat={"count": 2, "require": 1})
        detect["foreach"] = {"items": {"$from": "inputs.batches"},
                             "as": "batch"}
        detect["input"] = {"batch": {"$from": "batch.id"}}
        answers = {"detect": [fx.envelope(payload=None, problems=[refused]),
                              fx.envelope(payload={"n": 2})]}
        code, _, track, _ = self.go(
            fx.spec_doc([detect], inputs=[{"name": "batches"}]),
            request={"batches": [{"id": "b1"}]}, answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "detect")
        self.assertEqual(record["problems"], [refused])
        self.assertEqual(record["elements"][0]["problems"], [refused])


class DryRunTests(RunnerCase):
    def test_dry_run_plans_without_invoking_anything(self):
        first = fx.cog_step("first", task="ask")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        code, output, track, fake = self.go(fx.spec_doc([first, second]),
                                            answers={}, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "planned")
        self.assertEqual(track["status"], "planned")
        self.assertEqual(fake.calls, [])
        self.assertEqual([s["id"] for s in track["steps"]],
                         ["first", "second"])
        self.assertTrue(all(s["status"] == "planned" for s in track["steps"]))
        planned = json.loads(Path(self.step(track, "first")["request"]).read_text())
        self.assertEqual(planned, {"note": "hi"})
        self.assertIsNone(self.step(track, "second")["request"])


class RequestFlagTests(unittest.TestCase):
    """The Cog seam negotiates the request-file flag: `--request` first, then
    `--bundle` (what cog-smith's own context-cog machinery accepts), and only
    when the CLI refused the first flag by name before doing any work."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self.real_run = op_runner.subprocess.run
        self.calls = []

    def tearDown(self):
        op_runner.subprocess.run = self.real_run

    def fake_subprocess(self, answer):
        def run(command, **kwargs):
            self.calls.append(command)
            return answer(command)
        op_runner.subprocess.run = run

    def flag_of(self, command):
        return command[-2]

    def test_request_flag_is_tried_first_and_kept(self):
        envelope = json.dumps(fx.envelope(payload={"x": 1}))
        self.fake_subprocess(lambda c: self.Result(0, envelope))
        result = op_runner.invoke_cog(Path("/nowhere/cog"), "ask", Path("/r.json"))
        self.assertTrue(result["ok"])
        self.assertEqual([self.flag_of(c) for c in self.calls], ["--request"])

    def test_bundle_flag_is_tried_when_request_is_refused_by_name(self):
        envelope = json.dumps(fx.envelope(payload={"x": 1}))

        def answer(command):
            if self.flag_of(command) == "--request":
                return self.Result(
                    2, "", "ask: error: unrecognized arguments: --request /r.json")
            return self.Result(0, envelope)

        self.fake_subprocess(answer)
        result = op_runner.invoke_cog(Path("/nowhere/cog"), "ask", Path("/r.json"))
        self.assertTrue(result["ok"])
        self.assertEqual([self.flag_of(c) for c in self.calls],
                         ["--request", "--bundle"])

    def test_an_ordinary_failure_is_never_reinvoked(self):
        self.fake_subprocess(lambda c: self.Result(1, "", "model endpoint refused"))
        result = op_runner.invoke_cog(Path("/nowhere/cog"), "ask", Path("/r.json"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invocation-failed")
        self.assertEqual([self.flag_of(c) for c in self.calls], ["--request"])

    def test_both_flags_refused_yields_one_failed_envelope(self):
        self.fake_subprocess(lambda c: self.Result(
            2, "", f"ask: error: unrecognized arguments: {self.flag_of(c)} /r.json"))
        result = op_runner.invoke_cog(Path("/nowhere/cog"), "ask", Path("/r.json"))
        self.assertFalse(result["ok"])
        self.assertEqual([self.flag_of(c) for c in self.calls],
                         ["--request", "--bundle"])


def write_cog(root, cog_id, interfaces, version="0.1.0"):
    """A minimal Cog package for the declaration check: a cog.yaml is enough
    (the machinery reads the profile manifest itself, without cog-smith)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "cog.yaml").write_text(json.dumps({
        "schema": "openteams/cog-manifest [0.1]", "id": cog_id,
        "version": version, "kind": "context", "summary": "A test Cog.",
        "owner": "t@example.com", "license": "BSD-3-Clause",
        "interfaces": interfaces,
    }, indent=2))
    return root


class DeclarationTests(RunnerCase):
    """Finding 4: every step's Cog declaration is checked BEFORE any step is
    invoked — a spec naming a lifecycle or undeclared task never runs."""

    def usage(self, task="ask"):
        return [{"name": task, "kind": "command", "task": task,
                 "audience": "usage", "default": True}]

    def test_a_declared_usage_task_runs(self):
        write_cog(self.root / "cog-first", "openteams/cog-first", self.usage())
        code, _, track, fake = self.go(
            fx.spec_doc(), answers={"ask": fx.envelope(payload={"text": "a"})})
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)

    def test_a_lifecycle_task_refuses_the_run(self):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": "ask", "kind": "command", "task": "ask",
                    "audience": "lifecycle", "default": True}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.go(fx.spec_doc(),
                    answers={"ask": fx.envelope(payload={"text": "a"})})
        self.assertIn("usage interfaces", str(caught.exception))

    def test_an_undeclared_task_on_a_later_step_stops_the_first_one_running(self):
        write_cog(self.root / "cog-first", "openteams/cog-first", self.usage())
        write_cog(self.root / "cog-second", "openteams/cog-second",
                  self.usage("chat"))
        first = fx.cog_step("first", task="ask")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        fx.write_package(self.package, fx.spec_doc([first, second]))
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        fake = fx.FakeCog({"ask": fx.envelope(payload={"text": "a"})})
        op_runner.invoke_cog = fake
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_runner.run(self.package, path)
        self.assertIn("does not declare as an interface", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_the_cli_exits_two_for_an_undeclared_task(self):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  self.usage("chat"))
        fx.write_package(self.package, fx.spec_doc())
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        op_runner.invoke_cog = fx.FakeCog({"ask": fx.envelope()})
        real_root, op_runner.ROOT = op_runner.ROOT, self.package
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = op_runner.main(["--request", str(path)])
        finally:
            op_runner.ROOT = real_root
        self.assertEqual(code, 2)
        self.assertIn("does not declare as an interface", stdout.getvalue())

    def test_a_dry_run_does_not_need_the_cogs_on_this_machine(self):
        code, _, track, _ = self.go(fx.spec_doc(), answers={}, dry_run=True)
        self.assertEqual(code, 0)


class UnavailableResultTests(RunnerCase):
    """Findings 6, 12 and gap B: what the Track and the outputs say when a
    step is skipped, blocked, never reached, or the write is interrupted."""

    def skipping_doc(self, **outputs):
        first = fx.cog_step("first", task="ask", on_fail="skip")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        return fx.spec_doc([first, second], outputs=outputs)

    def test_outputs_over_skipped_and_blocked_steps_still_complete(self):
        doc = self.skipping_doc(from_first={"$from": "steps.first.payload"},
                                from_second={"$from": "steps.second.payload"})
        answers = {"ask": fx.envelope(ok=False),
                   "summarize": fx.envelope(payload={})}
        code, output, track, _ = self.go(doc, answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(track["status"], "completed-with-problems")
        self.assertEqual(output["outputs"],
                         {"from_first": None, "from_second": None})

    def test_a_skipped_foreach_step_keeps_its_aggregate_payload(self):
        step = fx.cog_step("classify", task="classify", on_fail="skip")
        step["foreach"] = {"items": {"$from": "inputs.items"}, "as": "item"}
        step["input"] = {"title": {"$from": "item.title"}}
        doc = fx.spec_doc([step], inputs=[{"name": "items"}],
                          outputs={"all": {"$from": "steps.classify.payload"}})
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False)]}
        code, output, track, _ = self.go(
            doc, request={"items": [{"title": "one"}, {"title": "two"}]},
            answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(self.step(track, "classify")["status"], "skipped")
        self.assertEqual(output["outputs"], {"all": [{"label": "a"}, None]})

    def test_every_step_of_the_spec_is_in_the_track_when_a_run_stops(self):
        first = fx.cog_step("first", task="ask")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        third = fx.cog_step("third", task="report")
        doc = fx.spec_doc([first, second, third])
        answers = {"ask": fx.envelope(ok=False),
                   "summarize": fx.envelope(payload={}),
                   "report": fx.envelope(payload={})}
        code, _, track, fake = self.go(doc, answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual([(s["id"], s["status"]) for s in track["steps"]],
                         [("first", "failed"), ("second", "not-reached"),
                          ("third", "not-reached")])
        self.assertEqual(len(fake.calls), 1)

    def test_an_interrupted_track_rewrite_leaves_the_previous_one_readable(self):
        real_replace = op_track.os.replace
        state = {"fail": False}

        def replace(src, dst):
            if state["fail"] and str(dst).endswith("track.json"):
                raise OSError("interrupted")
            return real_replace(src, dst)

        fx.write_package(self.package, fx.spec_doc())
        path = fx.write_request(self.root / "request.json", {"note": "hi"})
        op_runner.invoke_cog = fx.FakeCog({"ask": fx.envelope(payload={})})
        op_track.os.replace = replace
        try:
            code, output = op_runner.run(self.package, path)
            before = Path(output["track"]).read_text()
            state["fail"] = True
            with self.assertRaises(OSError):
                op_track.save({"status": "half-written"},
                              Path(output["track"]).parent)
        finally:
            op_track.os.replace = real_replace
        self.assertEqual(code, 0)
        self.assertEqual(Path(output["track"]).read_text(), before)
        self.assertEqual(json.loads(before)["status"], "completed")
        self.assertEqual(
            list(Path(output["track"]).parent.glob("track.json*")),
            [Path(output["track"])])


class ProcessBoundaryTests(unittest.TestCase):
    """Findings 9 and 10: what comes back from the process itself."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self.real_run = op_runner.subprocess.run

    def tearDown(self):
        op_runner.subprocess.run = self.real_run

    def answer(self, result):
        def run(command, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result
        op_runner.subprocess.run = run

    def invoke(self):
        return op_runner.invoke_cog(Path("/nowhere/cog"), "ask",
                                    Path("/r.json"))

    def test_a_nonzero_exit_after_a_good_envelope_is_an_invocation_failure(self):
        good = fx.envelope(payload={"text": "looks fine"})
        self.answer(self.Result(139, json.dumps(good), "Segmentation fault"))
        result = self.invoke()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invocation-failed")
        self.assertIn("139", result["error"]["detail"])
        self.assertEqual(result["raw"], good)        # kept as evidence
        self.assertEqual(op_runner.gate_envelope(result)["status"], "fail")

    def test_a_launch_failure_becomes_a_failed_envelope_not_an_exception(self):
        self.answer(OSError("No such file or directory: 'pixi'"))
        result = self.invoke()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invocation-failed")
        self.assertIn("pixi", result["error"]["detail"])
        self.assertEqual(op_runner.gate_envelope(result)["status"], "fail")

    def test_a_non_boolean_ok_is_malformed_output_not_a_pass(self):
        self.answer(self.Result(0, json.dumps(
            {"envelope": 1, "ok": "false", "problems": []})))
        result = self.invoke()
        self.assertFalse(result["ok"])
        self.assertIn("not true or false", result["error"]["detail"])
        self.assertEqual(op_runner.gate_envelope(result)["status"], "fail")

    def test_problems_that_are_not_objects_are_malformed_output(self):
        self.answer(self.Result(0, json.dumps(
            {"envelope": 1, "ok": True, "problems": ["broken"]})))
        result = self.invoke()
        self.assertFalse(result["ok"])
        self.assertIn("problem objects", result["error"]["detail"])
        self.assertEqual(result["raw"]["problems"], ["broken"])
        self.assertEqual(op_runner.gate_envelope(result)["status"], "fail")

    def test_the_gate_never_crashes_on_a_malformed_envelope(self):
        for bad in ({"envelope": 1, "ok": "false", "problems": []},
                    {"envelope": 1, "ok": True, "problems": ["broken"]},
                    {"ok": True}, "not an envelope at all"):
            self.assertEqual(op_runner.gate_envelope(bad)["status"], "fail")


class ForeachBoundaryTests(RunnerCase):
    """Finding 16: the foreach boundaries §6's tests did not assert — and,
    since machinery 0.6.2, where the loop stops."""

    def doc(self, on_fail="stop"):
        step = fx.cog_step("classify", task="classify", on_fail=on_fail)
        step["foreach"] = {"items": {"$from": "inputs.items"}, "as": "item"}
        step["input"] = {"title": {"$from": "item.title"}}
        after = fx.cog_step("report", task="report", depends_on=["classify"])
        after["input"] = {"all": {"$from": "steps.classify.payload"}}
        return fx.spec_doc([step, after], inputs=[{"name": "items"}])

    def request(self):
        return {"items": [{"title": "one"}, {"title": "two"},
                          {"title": "three"}]}

    def test_a_failed_element_is_null_in_the_payload_the_next_step_reads(self):
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False),
                                fx.envelope(payload={"label": "c"})],
                   "report": fx.envelope(payload={"text": "ok"})}
        code, _, track, fake = self.go(self.doc(on_fail="skip"),
                                       request=self.request(), answers=answers)
        self.assertEqual(code, 0)
        self.assertEqual(self.step(track, "classify")["status"], "skipped")
        self.assertEqual(self.step(track, "report")["status"], "blocked")
        elements = self.step(track, "classify")["elements"]
        # The stop rule applies whatever `on_fail` says: element 2 is never
        # reached, and its payload is null exactly as a failed one's is.
        self.assertEqual([e["status"] for e in elements],
                         ["passed", "failed", "not-reached"])
        self.assertEqual([(e["gate"] or {}).get("status") for e in elements],
                         ["pass", "fail", None])
        self.assertEqual(len([c for c in fake.calls
                              if c["task"] == "classify"]), 2)

    def test_a_failed_element_stops_the_later_elements(self):
        """Machinery 0.6.2 (narrowing contract §10): a `foreach` stops at its
        first finally-failed element. The step has already failed, so every
        later element could only buy invocations for a settled verdict — a
        systematic outage used to exhaust the whole list."""
        answers = {"classify": [fx.envelope(payload={"label": "a"}),
                                fx.envelope(ok=False),
                                fx.envelope(payload={"label": "c"})],
                   "report": fx.envelope(payload={})}
        code, output, track, fake = self.go(self.doc(), request=self.request(),
                                            answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "classify")
        self.assertEqual(len([c for c in fake.calls
                              if c["task"] == "classify"]), 2)
        element = self.step(track, "classify")["elements"][2]
        self.assertEqual(element["status"], "not-reached")
        # Nothing was built for it and nothing was paid for.
        self.assertIsNone(element["request"])
        self.assertIsNone(element["envelope"])
        self.assertEqual(self.step(track, "report")["status"], "not-reached")


class FlagNegotiationTests(unittest.TestCase):
    """Verification round, item 1: an effectful Cog must never be invoked
    twice by accident (contract §0). A second flag is tried only for a real
    argument-parser rejection that produced no result."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self.real_run = op_runner.subprocess.run
        self.calls = []

    def tearDown(self):
        op_runner.subprocess.run = self.real_run

    def fake_subprocess(self, answer):
        def run(command, **kwargs):
            self.calls.append(command)
            return answer(command)
        op_runner.subprocess.run = run

    def flags(self):
        return [c[-2] for c in self.calls]

    def invoke(self):
        return op_runner.invoke_cog(Path("/nowhere/cog"), "ask",
                                    Path("/r.json"))

    def test_an_envelope_quoting_the_diagnostic_is_never_reinvoked(self):
        """The Cog RAN and reported a failure whose detail happens to quote
        `unrecognized arguments: --request` — a result, not a rejection."""
        envelope = json.dumps({**fx.envelope(ok=False), "error": {
            "code": "tool-failed",
            "detail": "gh: error: unrecognized arguments: --request"}})
        self.fake_subprocess(lambda c: self.Result(1, envelope))
        result = self.invoke()
        self.assertFalse(result["ok"])
        self.assertEqual(self.flags(), ["--request"])

    def test_an_envelope_on_stdout_of_an_exit_two_is_never_reinvoked(self):
        envelope = json.dumps(fx.envelope(payload={"x": 1}))
        self.fake_subprocess(lambda c: self.Result(
            2, envelope, "ask: error: unrecognized arguments: --request"))
        self.invoke()
        self.assertEqual(self.flags(), ["--request"])

    def test_the_diagnostic_on_stdout_alone_is_never_reinvoked(self):
        self.fake_subprocess(lambda c: self.Result(
            2, "ask: error: unrecognized arguments: --request /r.json", ""))
        self.invoke()
        self.assertEqual(self.flags(), ["--request"])

    def test_a_nonzero_exit_that_is_not_argparses_is_never_reinvoked(self):
        self.fake_subprocess(lambda c: self.Result(
            1, "", "ask: error: unrecognized arguments: --request /r.json"))
        self.invoke()
        self.assertEqual(self.flags(), ["--request"])

    def test_both_attempts_are_kept_as_evidence_when_the_fallback_fails(self):
        self.fake_subprocess(lambda c: self.Result(
            2, "", f"ask: error: unrecognized arguments: {c[-2]} /r.json"))
        result = self.invoke()
        self.assertEqual(self.flags(), ["--request", "--bundle"])
        evidence = result["error"]["evidence"]
        self.assertIn("--bundle", evidence["command"])
        self.assertIn("--bundle", evidence["stderr_tail"])
        self.assertEqual(len(evidence["previous_attempts"]), 1)
        first = evidence["previous_attempts"][0]
        self.assertIn("--request", first["command"])
        self.assertEqual(first["returncode"], 2)
        self.assertIn("--request", first["stderr_tail"])


class MalformedEnvelopeTests(unittest.TestCase):
    """Verification round, item 4: the envelope's required fields are checked
    by TYPE before the Gate decides anything."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self.real_run = op_runner.subprocess.run

    def tearDown(self):
        op_runner.subprocess.run = self.real_run

    def answer(self, value):
        op_runner.subprocess.run = (
            lambda command, **kwargs: self.Result(0, json.dumps(value)))
        return op_runner.invoke_cog(Path("/nowhere/cog"), "ask",
                                    Path("/r.json"))

    def test_a_boolean_envelope_version_is_malformed_not_version_one(self):
        """`True == 1` in Python; `type(x) is int` is the discriminator."""
        bad = {"envelope": True, "ok": True, "problems": []}
        self.assertTrue(op_runner.envelope_problems(bad))
        result = self.answer(bad)
        self.assertFalse(result["ok"])
        self.assertIn("not envelope v1", result["error"]["detail"])
        self.assertEqual(op_runner.gate_envelope(bad)["status"], "fail")

    def test_null_problems_are_malformed(self):
        bad = {"envelope": 1, "ok": True, "problems": None}
        result = self.answer(bad)
        self.assertFalse(result["ok"])
        self.assertIn("problem objects", result["error"]["detail"])
        self.assertEqual(op_runner.gate_envelope(bad)["status"], "fail")

    def test_missing_problems_are_malformed(self):
        bad = {"envelope": 1, "ok": True}
        result = self.answer(bad)
        self.assertFalse(result["ok"])
        self.assertIn("problem objects", result["error"]["detail"])
        self.assertEqual(op_runner.gate_envelope(bad)["status"], "fail")

    def test_a_float_envelope_version_is_malformed(self):
        self.assertTrue(op_runner.envelope_problems(
            {"envelope": 1.0, "ok": True, "problems": []}))


class EvidenceTests(unittest.TestCase):
    """Verification round, item 6: every synthetic failure carries the same
    process evidence, in one place — `error.evidence`."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        self.real_run = op_runner.subprocess.run

    def tearDown(self):
        op_runner.subprocess.run = self.real_run

    def answer(self, result):
        def run(command, **kwargs):
            if isinstance(result, Exception):
                raise result
            return result
        op_runner.subprocess.run = run
        return op_runner.invoke_cog(Path("/nowhere/cog"), "ask",
                                    Path("/r.json"))

    def test_a_silent_crash_records_its_return_code(self):
        result = self.answer(self.Result(139, "", ""))
        evidence = result["error"]["evidence"]
        self.assertEqual(evidence["returncode"], 139)
        self.assertEqual(evidence["stdout_tail"], "")

    def test_a_malformed_envelope_keeps_the_return_code_and_stderr(self):
        result = self.answer(self.Result(
            1, json.dumps({"envelope": 1, "ok": "false", "problems": []}),
            "model endpoint refused"))
        evidence = result["error"]["evidence"]
        self.assertEqual(evidence["returncode"], 1)
        self.assertIn("model endpoint refused", evidence["stderr_tail"])
        self.assertIn("envelope", evidence["stdout_tail"])

    def test_a_launch_failure_records_evidence_with_no_return_code(self):
        result = self.answer(OSError("No such file or directory: 'pixi'"))
        evidence = result["error"]["evidence"]
        self.assertIsNone(evidence["returncode"])
        self.assertIn("pixi", evidence["command"][0])

    def test_the_stream_tails_are_bounded(self):
        result = self.answer(self.Result(3, "x" * 5000, "y" * 5000))
        evidence = result["error"]["evidence"]
        self.assertEqual(len(evidence["stdout_tail"]), op_runner.EVIDENCE_TAIL)
        self.assertEqual(len(evidence["stderr_tail"]), op_runner.EVIDENCE_TAIL)

    def test_a_nonzero_exit_after_a_good_envelope_carries_evidence_too(self):
        good = fx.envelope(payload={"text": "looks fine"})
        result = self.answer(self.Result(139, json.dumps(good),
                                         "Segmentation fault"))
        self.assertEqual(result["error"]["evidence"]["returncode"], 139)
        self.assertIn("Segmentation fault",
                      result["error"]["evidence"]["stderr_tail"])


class PreflightOrderTests(RunnerCase):
    """Verification round, item 7: a refused declaration leaves no run
    directory and no Track stuck at `status: running`."""

    def setup_package(self, task="chat"):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": task, "kind": "command", "task": task,
                    "audience": "usage", "default": True}])
        fx.write_package(self.package, fx.spec_doc())
        op_runner.invoke_cog = fx.FakeCog({"ask": fx.envelope()})
        return fx.write_request(self.root / "request.json", {"note": "hi"})

    def test_a_refused_declaration_creates_no_run_directory(self):
        path = self.setup_package()
        runs = self.root / "runs-here"
        with self.assertRaises(op_spec.OpSpecError):
            op_runner.run(self.package, path, runs_dir=runs)
        self.assertFalse(runs.exists())
        self.assertFalse((self.package / "runs").exists())

    def test_a_dry_run_is_unaffected_by_the_preflight(self):
        path = self.setup_package()
        code, output = op_runner.run(self.package, path, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "planned")


class RunDirContainmentTests(RunnerCase):
    """Codex final round, item 1: an element carrying `dir` must not redirect
    `$run_dir`. `as: run` is refused at load (test_op_spec), and an ordinary
    loop variable leaves the run context alone."""

    def doc(self):
        step = fx.cog_step("classify", task="classify")
        step["foreach"] = {"items": {"$from": "inputs.items"}, "as": "item"}
        step["input"] = {"id": {"$from": "item.id"},
                         "out": {"$run_dir": "artifacts"}}
        return fx.spec_doc([step], inputs=[{"name": "items"}])

    def test_an_element_carrying_dir_does_not_redirect_run_dir(self):
        request = {"items": [{"dir": "/outside-the-real-run", "id": "fake"}]}
        answers = {"classify": fx.envelope(payload={"label": "a"})}
        code, output, _, fake = self.go(self.doc(), request=request,
                                        answers=answers)
        self.assertEqual(code, 0)
        run_dir = Path(output["run_dir"]).resolve()
        written = Path(fake.calls[0]["request"]["out"]).resolve()
        self.assertEqual(written, run_dir / "artifacts")
        self.assertIn(run_dir, written.parents)
        self.assertFalse(Path("/outside-the-real-run").exists())


class Interrupted(Exception):
    """Stands in for the process dying mid-step."""


def interrupting(answers, calls, task_name):
    """A fake Cog that answers from `answers` and raises on the invocation of
    `task_name` numbered `calls` (1-based)."""
    script = fx.FakeCog(answers)

    def fake(cog_dir, task, request_path, **seam):
        if task == task_name and len([c for c in script.calls
                                      if c["task"] == task_name]) + 1 == calls:
            script.calls.append({"task": task, "raised": True})
            raise Interrupted("power loss")
        return script(cog_dir, task, request_path, **seam)
    fake.script = script
    return fake


class ForeachStopTests(RunnerCase):
    """A `foreach` stops at its first finally-failed element (machinery
    0.6.2, narrowing contract §10; Codex review 3, residual 3).

    Retry is per element and the Gate used to be combined only after every
    element had run, so a systematic outage exhausted the whole batch list —
    hundreds of invocations bought for a verdict that was settled at the
    first one. The step fails either way; what the loop owes the run is to
    stop paying for it."""

    def doc(self, on_fail="stop", repeat=None, items="items"):
        step = fx.cog_step("detect", task="detect", on_fail=on_fail)
        if repeat is not None:
            step["repeat"] = repeat
        step["foreach"] = {"items": {"$from": f"inputs.{items}"}, "as": "batch"}
        step["input"] = {"batch": {"$from": "batch.id"}}
        return fx.spec_doc([step], inputs=[{"name": items}])

    def request(self, n=4):
        return {"items": [{"id": f"b{i}"} for i in range(n)]}

    def test_retry_once_is_exhausted_before_the_loop_stops(self):
        """The breaker is the FINALLY failed element: a retry-once element
        gets its second attempt, and only then does the loop end."""
        answers = {"detect": [fx.envelope(payload={"b": 0}),
                              fx.envelope(ok=False), fx.envelope(ok=False),
                              fx.envelope(payload={"b": 2})]}
        code, output, track, fake = self.go(self.doc(on_fail="retry-once"),
                                            request=self.request(),
                                            answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(output["failed_step"], "detect")
        # three invocations: element 0, then element 1 twice.
        self.assertEqual(len(fake.calls), 3)
        elements = self.step(track, "detect")["elements"]
        self.assertEqual([e["status"] for e in elements],
                         ["passed", "failed", "not-reached", "not-reached"])
        self.assertEqual(len(elements[1]["attempts"]), 2)

    def test_an_element_that_misses_require_stops_the_loop(self):
        """With `repeat`, the element is finally failed when its repeats are
        exhausted and fewer than `require` of them passed."""
        answers = {"detect": [fx.envelope(payload={"b": 0}),
                              fx.envelope(ok=False),
                              fx.envelope(payload={"b": 1})]}
        code, _, track, fake = self.go(
            self.doc(repeat={"count": 2, "require": 2}),
            request=self.request(), answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.calls), 2)
        elements = self.step(track, "detect")["elements"]
        self.assertEqual([e["status"] for e in elements],
                         ["failed", "not-reached", "not-reached",
                          "not-reached"])
        self.assertIsNone(elements[1]["repeats"])

    def test_the_unreached_elements_are_null_in_the_payload(self):
        """The payload a later step reads still lines up index for index with
        the items: an unreached element is null, like a failed one."""
        after = fx.cog_step("merge", task="merge", depends_on=["detect"])
        after["input"] = {"results": {"$from": "steps.detect.payload"}}
        doc = self.doc(on_fail="skip")
        doc["steps"].append(after)
        answers = {"detect": [fx.envelope(payload={"b": 0}),
                              fx.envelope(ok=False)],
                   "merge": fx.envelope(payload={})}
        code, _, track, _ = self.go(doc, request=self.request(n=3),
                                    answers=answers)
        self.assertEqual(code, 0)
        record = self.step(track, "detect")
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(
            op_runner._payloads_of(op_runner._element_envelopes(record)),
            [{"b": 0}, None, None])

    def test_a_resume_continues_from_the_failed_element(self):
        """The completed elements stay on the Track and are read back; the
        failed one runs again, and the unreached ones run for the first
        time."""
        answers = {"detect": [fx.envelope(payload={"b": 0}),
                              fx.envelope(ok=False)]}
        code, output, track, _ = self.go(self.doc(), request=self.request(n=3),
                                         answers=answers)
        self.assertEqual(code, 1)
        kept = Path(self.step(track, "detect")["elements"][0]["envelope"])
        stamp = kept.stat().st_mtime_ns

        again = fx.FakeCog({"detect": lambda n, req: fx.envelope(
            payload={"b": req["batch"]})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, output["run_dir"])
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual(code, 0)
        # element 0 was not paid for again; elements 1 and 2 were.
        self.assertEqual([c["request"]["batch"] for c in again.calls],
                         ["b1", "b2"])
        self.assertEqual(kept.stat().st_mtime_ns, stamp)
        record = self.step(resumed, "detect")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(
            op_runner._payloads_of(op_runner._element_envelopes(record)),
            [{"b": 0}, {"b": "b1"}, {"b": "b2"}])

    def test_a_changed_element_is_not_reused(self):
        """Element reuse is the repeats' rule: the same request, put to the
        same Cog. A resume whose element 0 is a different batch re-runs it."""
        read = fx.cog_step("read", task="read")
        detect = fx.cog_step("detect", task="detect", depends_on=["read"])
        detect["foreach"] = {"items": {"$from": "steps.read.payload.batches"},
                             "as": "batch"}
        detect["input"] = {"batch": {"$from": "batch.id"}}
        code, output, _, _ = self.go(
            fx.spec_doc([read, detect]),
            answers={"read": fx.envelope(payload={"batches": [{"id": "b1"},
                                                              {"id": "b2"}]}),
                     "detect": [fx.envelope(payload={"b": "b1"}),
                                fx.envelope(ok=False)]})
        self.assertEqual(code, 1)
        upstream = Path(output["run_dir"]) / "envelopes" / "read.json"
        changed = json.loads(upstream.read_text())
        changed["payload"] = {"batches": [{"id": "b9"}, {"id": "b2"}]}
        upstream.write_text(json.dumps(changed, indent=2))

        again = fx.FakeCog({"detect": lambda n, req: fx.envelope(
            payload={"b": req["batch"]})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        self.assertEqual([c["request"]["batch"] for c in again.calls],
                         ["b9", "b2"])


class RepeatTailDurabilityTests(RunnerCase):
    """A checkpoint keeps the records it has not revisited (machinery 0.6.2,
    narrowing contract §10; Codex review 3, residual 4).

    A checkpoint says what has happened so far, not what the step will end up
    with. Rewriting the repeat list with a prefix threw away a repeat that
    had already passed, and the next resume paid for it again."""

    def doc(self, count=3, require=3):
        draft = fx.cog_step("draft", task="draft",
                            repeat={"count": count, "require": require})
        after = fx.cog_step("merge", task="merge", depends_on=["draft"])
        after["input"] = {"results": {"$from": "steps.draft.payload"}}
        return fx.spec_doc([draft, after])

    def track_of(self, run_dir):
        return json.loads((Path(run_dir) / "track.json").read_text())

    def test_a_crash_after_a_reused_prefix_keeps_the_passed_repeat(self):
        """[pass, fail, pass]: the resume reuses repeat 0 and crashes
        retrying repeat 1 — repeat 2's durable record must still be there,
        with the request it answered."""
        answers = {"draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False),
                             fx.envelope(payload={"n": 3})],
                   "merge": fx.envelope(payload={})}
        code, output, track, _ = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 1)
        run_dir = Path(output["run_dir"])
        before = self.step(track, "draft")["repeats"][2]

        op_runner.invoke_cog = interrupting({"draft": fx.envelope()}, 1,
                                            "draft")
        with self.assertRaises(Interrupted):
            op_runner.resume(self.package, run_dir)
        record = self.step(self.track_of(run_dir), "draft")
        self.assertEqual([r["gate"]["status"] for r in record["repeats"]],
                         ["pass", "fail", "pass"])
        self.assertEqual(record["repeats"][2], before)

        # ---- the second resume pays for repeat 1 alone.
        again = fx.FakeCog({"draft": fx.envelope(payload={"n": 2}),
                            "merge": fx.envelope(payload={})})
        op_runner.invoke_cog = again
        code, output = op_runner.resume(self.package, run_dir)
        self.assertEqual(code, 0)
        self.assertEqual([c["task"] for c in again.calls], ["draft", "merge"])
        self.assertEqual(again.calls[-1]["request"],
                         {"results": [{"n": 1}, {"n": 2}, {"n": 3}]})

    def test_the_same_holds_inside_a_foreach_element(self):
        detect = fx.cog_step("detect", task="detect",
                             repeat={"count": 3, "require": 3})
        detect["foreach"] = {"items": {"$from": "inputs.batches"},
                             "as": "batch"}
        detect["input"] = {"batch": {"$from": "batch.id"}}
        doc = fx.spec_doc([detect], inputs=[{"name": "batches"}])
        request = {"batches": [{"id": "b1"}, {"id": "b2"}]}
        answers = {"detect": [fx.envelope(payload={"n": 1}),
                              fx.envelope(ok=False),
                              fx.envelope(payload={"n": 3})]}
        code, output, track, _ = self.go(doc, request=request, answers=answers)
        self.assertEqual(code, 1)
        run_dir = Path(output["run_dir"])
        # the loop stopped at element 0, which missed `require`.
        self.assertEqual([e["status"]
                          for e in self.step(track, "detect")["elements"]],
                         ["failed", "not-reached"])
        before = self.step(track, "detect")["elements"][0]["repeats"][2]

        op_runner.invoke_cog = interrupting({"detect": fx.envelope()}, 1,
                                            "detect")
        with self.assertRaises(Interrupted):
            op_runner.resume(self.package, run_dir)
        element = self.step(self.track_of(run_dir), "detect")["elements"][0]
        self.assertEqual([r["gate"]["status"] for r in element["repeats"]],
                         ["pass", "fail", "pass"])
        self.assertEqual(element["repeats"][2], before)


def write_code_cog(root, cog_id, task, logic="print('hi')\n", context=None,
                   model=None):
    """A Cog package with the files the package digest covers: a manifest,
    `src/`, `context/`, and optionally the gitignored `model.json` an
    installation leaves."""
    root = Path(root)
    write_cog(root, cog_id, [{"name": task, "kind": "command", "task": task,
                              "audience": "usage", "default": True}])
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "task_logic.py").write_text(logic)
    (root / "context").mkdir(exist_ok=True)
    (root / "context" / "prompt.md").write_text(context or "Answer well.\n")
    if model is not None:
        (root / "model.json").write_text(json.dumps(model, indent=2))
    return root


class CogPackageDigestTests(unittest.TestCase):
    """What a Cog's package digest covers, and what it must never cover
    (machinery 0.6.2, narrowing contract §10)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cog = write_code_cog(self.root / "cog-a", "openteams/cog-a",
                                  "ask")
        self.digest = op_runner.cog_package_sha256(self.cog)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_changed_src_file_changes_the_digest(self):
        (self.cog / "src" / "task_logic.py").write_text("print('bye')\n")
        self.assertNotEqual(op_runner.cog_package_sha256(self.cog),
                            self.digest)

    def test_a_changed_context_file_changes_the_digest(self):
        (self.cog / "context" / "prompt.md").write_text("Answer badly.\n")
        self.assertNotEqual(op_runner.cog_package_sha256(self.cog),
                            self.digest)

    def test_a_changed_manifest_changes_the_digest(self):
        (self.cog / "cog.yaml").write_text(
            (self.cog / "cog.yaml").read_text() + "\n")
        self.assertNotEqual(op_runner.cog_package_sha256(self.cog),
                            self.digest)

    def test_build_products_are_not_part_of_it(self):
        cache = self.cog / "src" / "__pycache__"
        cache.mkdir()
        (cache / "task_logic.cpython-311.pyc").write_bytes(b"\x00compiled")
        self.assertEqual(op_runner.cog_package_sha256(self.cog), self.digest)

    def test_the_model_and_the_response_format_are_part_of_it(self):
        installed = {"model": "qwen3-4b", "endpoint": "http://localhost:8000",
                     "api_key_env": "OPENAI_API_KEY",
                     "response_format": {"type": "json_object"}}
        (self.cog / "model.json").write_text(json.dumps(installed))
        bound = op_runner.cog_package_sha256(self.cog)
        self.assertNotEqual(bound, self.digest)
        (self.cog / "model.json").write_text(
            json.dumps(dict(installed, model="qwen3-8b")))
        self.assertNotEqual(op_runner.cog_package_sha256(self.cog), bound)
        (self.cog / "model.json").write_text(
            json.dumps(dict(installed,
                            response_format={"type": "json_schema"})))
        self.assertNotEqual(op_runner.cog_package_sha256(self.cog), bound)

    def test_the_endpoint_and_the_credential_are_not(self):
        """Relocating a served model does not change what answered, and the
        name of a credential's variable has no business in a Track."""
        installed = {"model": "qwen3-4b", "endpoint": "http://localhost:8000",
                     "api_key_env": "OPENAI_API_KEY"}
        (self.cog / "model.json").write_text(json.dumps(installed))
        bound = op_runner.cog_package_sha256(self.cog)
        (self.cog / "model.json").write_text(json.dumps(
            dict(installed, endpoint="https://hub.example/v1",
                 api_key_env="HUB_TOKEN")))
        self.assertEqual(op_runner.cog_package_sha256(self.cog), bound)
        self.assertNotIn("localhost", json.dumps(bound))

    def test_a_cog_that_is_not_here_digests_deterministically(self):
        absent = op_runner.cog_package_sha256(self.root / "cog-nowhere")
        self.assertEqual(absent,
                         op_runner.cog_package_sha256(self.root / "cog-else"))
        self.assertNotEqual(absent, self.digest)


class CogIdentityTests(RunnerCase):
    """A result belongs to a Cog as well as to a request (machinery 0.6.2,
    narrowing contract §10; Codex review 3, residual 5).

    Reuse used to compare the request alone, so a Cog could change at the
    same source path while a passed repeat was reused beside a failed one
    re-run against the new version — one union, two Cogs."""

    def doc(self, count=2, require=2):
        read = fx.cog_step("read", task="read")
        draft = fx.cog_step("draft", task="draft", depends_on=["read"],
                            repeat={"count": count, "require": require})
        draft["input"] = {"items": {"$from": "steps.read.payload"}}
        return fx.spec_doc([read, draft])

    def packages(self, model=None):
        write_code_cog(self.root / "cog-read", "openteams/cog-read", "read")
        return write_code_cog(self.root / "cog-draft", "openteams/cog-draft",
                              "draft", model=model)

    def first_run(self, model=None):
        """`read` passes; draft's repeat 0 passes and repeat 1 fails, so the
        step misses `require` and the run stops there."""
        self.packages(model=model)
        answers = {"read": fx.envelope(payload={"v": "a"}),
                   "draft": [fx.envelope(payload={"n": 1}),
                             fx.envelope(ok=False)]}
        code, output, track, _ = self.go(self.doc(), answers=answers)
        self.assertEqual(code, 1)
        self.assertEqual(track["failed_step"], "draft")
        return output, track

    def resume_calls(self):
        again = fx.FakeCog({"read": fx.envelope(payload={"v": "a"}),
                            "draft": fx.envelope(payload={"n": 2})})
        op_runner.invoke_cog = again
        return again

    def test_the_track_records_the_cogs_digest_per_step(self):
        _, track = self.first_run()
        self.assertEqual(
            self.step(track, "read")["cog_sha256"],
            op_runner.cog_package_sha256(self.root / "cog-read"))
        record = self.step(track, "draft")
        digest = op_runner.cog_package_sha256(self.root / "cog-draft")
        self.assertEqual(record["cog_sha256"], digest)
        self.assertEqual([r["cog_sha256"] for r in record["repeats"]],
                         [digest, digest])

    def test_a_changed_task_logic_re_runs_the_passed_repeats(self):
        output, track = self.first_run()
        kept = Path(self.step(track, "draft")["repeats"][0]["envelope"])
        stamp = kept.stat().st_mtime_ns
        (self.root / "cog-draft" / "src" / "task_logic.py").write_text(
            "print('fixed')\n")

        again = self.resume_calls()
        code, output = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        # BOTH repeats ran again: no answer from the old Cog is counted
        # toward this step's `require`.
        self.assertEqual([c["task"] for c in again.calls], ["draft", "draft"])
        self.assertNotEqual(kept.stat().st_mtime_ns, stamp)
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual({r["cog_sha256"]
                          for r in self.step(resumed, "draft")["repeats"]},
                         {op_runner.cog_package_sha256(
                             self.root / "cog-draft")})

    def test_a_changed_model_re_runs_them_too(self):
        installed = {"model": "qwen3-4b", "endpoint": "http://localhost:8000",
                     "api_key_env": "OPENAI_API_KEY"}
        output, _ = self.first_run(model=installed)
        (self.root / "cog-draft" / "model.json").write_text(
            json.dumps(dict(installed, model="qwen3-8b")))
        again = self.resume_calls()
        code, _ = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        self.assertEqual([c["task"] for c in again.calls], ["draft", "draft"])

    def test_a_changed_endpoint_keeps_the_reuse(self):
        """An endpoint-only relocation does not invalidate an otherwise
        identical binding: the same model answered."""
        installed = {"model": "qwen3-4b", "endpoint": "http://localhost:8000",
                     "api_key_env": "OPENAI_API_KEY"}
        output, track = self.first_run(model=installed)
        kept = Path(self.step(track, "draft")["repeats"][0]["envelope"])
        stamp = kept.stat().st_mtime_ns
        (self.root / "cog-draft" / "model.json").write_text(
            json.dumps(dict(installed, endpoint="https://hub.example/v1")))
        again = self.resume_calls()
        code, _ = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        # one invocation, for the repeat that failed.
        self.assertEqual([c["task"] for c in again.calls], ["draft"])
        self.assertEqual(kept.stat().st_mtime_ns, stamp)

    def test_a_passed_step_whose_cog_changed_is_not_re_run(self):
        """Fix-and-resume keeps working: the passed step keeps its result and
        the resume entry says which Cog is no longer the one that produced
        it."""
        output, track = self.first_run()
        was = self.step(track, "read")["cog_sha256"]
        (self.root / "cog-read" / "context" / "prompt.md").write_text("New.\n")
        now = op_runner.cog_package_sha256(self.root / "cog-read")

        again = self.resume_calls()
        code, output = op_runner.resume(self.package, output["run_dir"])
        self.assertEqual(code, 0)
        self.assertEqual([c["task"] for c in again.calls], ["draft"])
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual(resumed["resumes"][-1]["changed_cogs"],
                         [{"step": "read", "cog": "openteams/cog-read",
                           "was": was, "now": now}])
        self.assertEqual(self.step(resumed, "read")["cog_sha256"], was)

    def test_an_unchanged_run_lists_no_changed_cogs(self):
        output, _ = self.first_run()
        self.resume_calls()
        _, output = op_runner.resume(self.package, output["run_dir"])
        resumed = json.loads(Path(output["track"]).read_text())
        self.assertEqual(resumed["resumes"][-1]["changed_cogs"], [])


if __name__ == "__main__":
    unittest.main()
