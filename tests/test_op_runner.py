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
from op_fixtures import op_runner, op_spec


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
        self.assertEqual([s["id"] for s in seen[2]["steps"]], ["first"])
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
        self.assertEqual(len(track["steps"]), 1)
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


if __name__ == "__main__":
    unittest.main()
