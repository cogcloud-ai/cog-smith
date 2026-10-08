"""Decision Cogs (the `decision` class of `kind: context`): creation, the
checker rules, and the process seam.

A decision Cog's context is a typed System One question set. Its output
schema's `$defs.answers` is derived from those questions, and the checker
refuses drift. Created Cogs are exercised as processes, the way Workbench and
Ops reach them.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_check     # noqa: E402
import smith_core      # noqa: E402
import smith_migrate   # noqa: E402

TEMPLATE = ROOT / "templates" / "decision-cog"


def create_decision_cog(tmp, name="cog-ticket-router", manifest_format="pixi",
                        overlays=None):
    dest = Path(tmp) / name
    tokens = smith_core.default_tokens(name)
    smith_core.create(dest, tokens, template="decision-cog",
                      manifest_format=manifest_format, overlays=overlays)
    return dest


def errors(findings):
    return [f for f in findings if f["level"] == "error"]


def run_cli(dest, *args):
    completed = subprocess.run([sys.executable, "src/cog_cli.py", *args],
                               cwd=str(dest), capture_output=True, text=True)
    return completed


class CreateTests(unittest.TestCase):
    def test_created_decision_cog_passes_check_clean_in_both_formats(self):
        for fmt in ("pixi", "yaml"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                dest = create_decision_cog(tmp, manifest_format=fmt)
                findings = smith_check.check(dest)
                self.assertEqual(findings, [], findings)

    def test_created_suite_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                    cwd=str(dest), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_the_class_is_declared_not_inferred(self):
        self.assertEqual(smith_core.template_for({"kind": "context"}), "context-cog")
        self.assertEqual(smith_core.template_for({"kind": "code"}), "code-cog")
        self.assertEqual(smith_core.template_for(
            {"kind": "context", "extensions": {"system_one": {}}}), "decision-cog")
        # The marker means nothing on another kind.
        self.assertEqual(smith_core.template_for(
            {"kind": "code", "extensions": {"system_one": {}}}), "code-cog")

    def test_decision_machinery_is_its_own_lineage(self):
        masters = smith_core.machinery_hashes("decision-cog")
        self.assertEqual(set(masters), {"cog_cli.py", "cog_core.py", "system_one_contract.py"})
        self.assertNotIn("cog_binding.py", masters)

    def test_request_questions_rederive_the_answers_schema(self):
        questions = {"spam": {"type": "noul", "instructions": "This message is spam."},
                     "topic": {"type": "choice", "instructions": "Topic?",
                               "criteria": {"billing": None, "support": None}}}
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp, overlays={
                "context/questions.json": json.dumps(questions)})
            schema = json.loads((dest / "context" / "output-schema.json").read_text())
            self.assertEqual(set(schema["$defs"]["answers"]["properties"]), {"spam", "topic"})
            # The starter's task logic still expects its own questions; the
            # package check (manifest, schema derivation) is what smith owns.
            self.assertEqual([f for f in errors(smith_check.check(dest))
                              if f["check"] == "decision"], [])

    def test_an_invalid_request_question_set_is_refused_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(smith_core.CreateError):
                create_decision_cog(tmp, overlays={"context/questions.json": json.dumps(
                    {"one": {"type": "choice", "instructions": "x", "criteria": {"only": None}}})})
            self.assertFalse((Path(tmp) / "cog-ticket-router").exists())


class CheckTests(unittest.TestCase):
    def test_machinery_drift_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            core = dest / "src" / "cog_core.py"
            core.write_text(core.read_text() + "\n# local edit\n")
            found = [f for f in errors(smith_check.check(dest)) if f["check"] == "machinery"]
            self.assertTrue(found)

    def test_questions_out_of_step_with_the_output_schema_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            path = dest / "context" / "questions.json"
            questions = json.loads(path.read_text())
            questions["extra"] = {"type": "noul", "instructions": "Anything else?"}
            path.write_text(json.dumps(questions))
            found = [f["detail"] for f in errors(smith_check.check(dest)) if f["check"] == "decision"]
            self.assertTrue(any("derive-schema" in d for d in found), found)
            # The package's own repair path fixes it.
            completed = run_cli(dest, "derive-schema", "--write")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            found = [f for f in errors(smith_check.check(dest)) if f["check"] == "decision"]
            self.assertEqual(found, [])

    def test_invalid_questions_are_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            (dest / "context" / "questions.json").write_text(json.dumps(
                {"q": {"type": "score", "instructions": "x", "criteria": ["only one"]}}))
            found = [f for f in errors(smith_check.check(dest)) if f["check"] == "decision"]
            self.assertTrue(found)

    def test_composition_must_request_the_system_one_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            pixi = dest / "pixi.toml"
            pixi.write_text(pixi.read_text().replace(
                'capability = "system-one/decisions"\nbridge_interface',
                'capability = "agentic-harness/chat"\nbridge_interface'))
            found = [f["detail"] for f in errors(smith_check.check(dest)) if f["check"] == "decision"]
            self.assertTrue(any("workbench_composition.capability" in d for d in found), found)

    def test_decision_cogs_need_composition_tasks_not_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            pixi = dest / "pixi.toml"
            pixi.write_text(pixi.read_text().replace(
                'ask-composed = "python scripts/composed_usage.py"\n', ''))
            details = [f["detail"] for f in errors(smith_check.check(dest))]
            self.assertTrue(any("'ask-composed'" in d for d in details), details)
            self.assertFalse(any("'resolve'" in d for d in details), details)


class SeamTests(unittest.TestCase):
    def test_bridge_prepare_and_finish_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            bundle = json.loads((dest / "examples" / "sample-bundle.json").read_text())
            request = Path(tmp) / "prepare.json"
            request.write_text(json.dumps({"bundle": bundle}))
            completed = run_cli(dest, "bridge", "prepare", "--request", str(request))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            prepared = json.loads(completed.stdout)
            self.assertEqual(set(prepared), {"consumer", "context", "task"})
            self.assertEqual(set(prepared["task"]), {"state", "questions"})
            result = json.loads((dest / "examples" / "sample-result.json").read_text())
            request.write_text(json.dumps({"bundle": bundle, "result": result,
                                           "provenance": {"composition": "test"}}))
            completed = run_cli(dest, "bridge", "finish", "--request", str(request))
            envelope = json.loads(completed.stdout)
            self.assertEqual(envelope["envelope"], 1)
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["binding"], {"composition": "test"})

    def test_envelope_replay_and_exported_foreach_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            bundle = json.loads((dest / "examples/sample-bundle.json").read_text())
            result = json.loads((dest / "examples/sample-result.json").read_text())
            live = Path(tmp) / "finish.json"
            live.write_text(json.dumps({"bundle": bundle, "result": result, "provenance": {}}))
            call = run_cli(dest, "bridge", "finish", "--request", str(live))
            env = json.loads(call.stdout)
            self.assertEqual(env["provider_result"], result)
            run = Path(tmp) / "run"
            run.mkdir()
            elements = []
            for index in range(2):
                request = run / f"request-{index}.json"
                envelope = run / f"envelope-{index}.json"
                request.write_text(json.dumps(bundle))
                envelope.write_text(json.dumps(env))
                elements.append({"request": str(request), "envelope": str(envelope)})
            (run / "track.json").write_text(json.dumps({"steps": [
                {"id": "decide", "cog": env["cog"], "elements": elements}]}))
            replay = run_cli(dest, "replay", "--bundle", str(request), "--result", str(envelope))
            self.assertEqual(replay.returncode, 0, replay.stdout)
            self.assertEqual(json.loads(replay.stdout)["payload"], env["payload"])
            export = run_cli(dest, "export-fixtures", "--run", str(run), "--step", "decide", "--name", "live")
            self.assertEqual(export.returncode, 0, export.stdout)
            self.assertEqual(json.loads(export.stdout)["fixtures"], 2)
            tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                   cwd=dest, capture_output=True, text=True)
            self.assertEqual(tests.returncode, 0, tests.stderr)
            # Saved decisions are assertions, not regenerated expectations.
            decision = dest / "tests/fixtures/live/0/decision.json"
            decision.write_text('{}')
            tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                   cwd=dest, capture_output=True, text=True)
            self.assertNotEqual(tests.returncode, 0)
            duplicate = run_cli(dest, "export-fixtures", "--run", str(run), "--step", "decide", "--name", "live")
            self.assertEqual(duplicate.returncode, 1)

    def test_export_single_and_refusals(self):
        import copy
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            run = Path(tmp) / "run"
            run.mkdir()
            bundle = json.loads((dest / "examples/sample-bundle.json").read_text())
            result = json.loads((dest / "examples/sample-result.json").read_text())
            finish = run / "finish.json"
            finish.write_text(json.dumps({"bundle": bundle, "result": result, "provenance": {}}))
            env = json.loads(run_cli(dest, "bridge", "finish", "--request", str(finish)).stdout)
            request, envelope = run / "request.json", run / "envelope.json"
            request.write_text(json.dumps(bundle))
            slot = {"request": str(request), "envelope": str(envelope)}
            step = {"id": "decide", "cog": env["cog"], **slot}

            def export(steps, document=env, name="single"):
                envelope.write_text(json.dumps(document))
                (run / "track.json").write_text(json.dumps({"steps": steps}))
                return run_cli(dest, "export-fixtures", "--run", str(run),
                               "--step", "decide", "--name", name)

            call = export([step])
            self.assertEqual(call.returncode, 0, call.stdout)
            fixture = dest / "tests/fixtures/single"
            self.assertEqual(json.loads((fixture / "0/bundle.json").read_text()), bundle)
            self.assertEqual(json.loads((fixture / "0/result.json").read_text()), result)
            self.assertEqual(json.loads((fixture / "0/decision.json").read_text()), env["payload"]["decision"])
            self.assertEqual(fixture.stat().st_mode & 0o777,
                             fixture.parent.stat().st_mode & 0o777)
            # Use discovery so the generated TestCase actually executes.
            test = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                  cwd=dest, capture_output=True, text=True)
            self.assertEqual(test.returncode, 0, test.stderr)
            shutil.rmtree(fixture / "0")
            test = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                  cwd=dest, capture_output=True, text=True)
            self.assertNotEqual(test.returncode, 0)
            self.assertIn("No recorded elements", test.stderr)

            outside = Path(tmp) / "outside.json"
            outside.write_text(json.dumps(bundle))
            (run / "link.json").symlink_to(outside)
            cases = [
                ([], env, "missing step"),
                ([step, step], env, "duplicate step"),
                ([{**step, "cog": {**env["cog"], "id": "other"}}], env, "other id"),
                ([{**step, "cog": {**env["cog"], "version": "99"}}], env, "other version"),
                ([step], {**env, "ok": False}, "failed envelope"),
                ([step], {**env, "cog": {**env["cog"], "id": "other"}}, "other envelope id"),
                ([step], {**env, "cog": {**env["cog"], "version": "99"}}, "other envelope version"),
                ([step], {k: v for k, v in env.items() if k != "provider_result"}, "missing result"),
                ([{**step, "repeats": []}], env, "repeated step"),
                ([{**step, "elements": [{**slot, "repeats": []}]}], env, "repeated element"),
                ([{**step, "elements": [{}]}], env, "not reached"),
                ([{**step, "elements": []}], env, "empty foreach"),
                ([{**step, "request": str(outside)}], env, "absolute escape"),
                ([{**step, "request": "../outside.json"}], env, "relative escape"),
                ([{**step, "request": "link.json"}], env, "symlink escape"),
            ]
            mismatch = copy.deepcopy(env)
            mismatch["payload"]["decision"]["escalate"] = not env["payload"]["decision"]["escalate"]
            cases.append(([step], mismatch, "decision disagreement"))
            for steps, document, label in cases:
                with self.subTest(case=label):
                    call = export(steps, document, "refused")
                    self.assertEqual(call.returncode, 1, call.stdout)
                    self.assertFalse((fixture.parent / "refused").exists())
                    self.assertEqual(list(fixture.parent.glob(".fixture-*")), [])
            for name in ("../escape", "/absolute", "bad.name", "", "single"):
                with self.subTest(name=name):
                    self.assertEqual(export([step], name=name).returncode, 1)
            self.assertEqual(export([step], name="a-b").returncode, 0)
            collision = export([step], name="a_b")
            self.assertEqual(collision.returncode, 1)
            self.assertIn("Generated test already exists", collision.stdout)
            self.assertFalse((fixture.parent / "a_b").exists())
            self.assertEqual(list(fixture.parent.glob(".fixture-*")), [])
            for version in (True, 2):
                envelope.write_text(json.dumps({**env, "envelope": version}))
                call = run_cli(dest, "replay", "--bundle", str(request), "--result", str(envelope))
                self.assertEqual(call.returncode, 1)
                self.assertIn("Expected envelope v1", call.stdout)
            envelope.write_text(json.dumps({**env, "ok": False}))
            call = run_cli(dest, "replay", "--bundle", str(request), "--result", str(envelope))
            self.assertEqual(call.returncode, 1)
            self.assertIn("successful envelope", call.stdout)

    def test_old_envelope_cannot_be_reconstructed_as_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            result = Path(tmp) / "old.json"
            result.write_text(json.dumps({"envelope": 1, "ok": True, "raw": None}))
            call = run_cli(dest, "replay", "--bundle", "examples/sample-bundle.json", "--result", str(result))
            self.assertEqual(call.returncode, 1)
            self.assertIn("no validated provider_result", call.stdout)

    def test_a_failed_turn_is_still_an_envelope_on_the_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            bundle = json.loads((dest / "examples" / "sample-bundle.json").read_text())
            request = Path(tmp) / "finish.json"
            request.write_text(json.dumps({"bundle": bundle, "provenance": None, "result": {
                "model": "m", "answer_source": "llm-adapter", "answers": {},
                "usage": {"input_tokens": None, "output_tokens": None}}}))
            completed = run_cli(dest, "bridge", "finish", "--request", str(request))
            self.assertEqual(completed.returncode, 0)
            envelope = json.loads(completed.stdout)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "answers-invalid")

    def test_author_code_errors_are_data_not_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            logic = dest / "src" / "task_logic.py"
            logic.write_text(logic.read_text().replace(
                'def decide(bundle, answers):\n', 'def decide(bundle, answers):\n    1 / 0\n', 1))
            completed = run_cli(dest, "replay", "--bundle", "examples/sample-bundle.json",
                                "--result", "examples/sample-result.json")
            self.assertEqual(completed.returncode, 1)
            self.assertIn("ZeroDivisionError", json.loads(completed.stdout)["error"])
            self.assertNotIn("Traceback", completed.stderr)

    def test_replay_names_the_code_and_calls_no_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp)
            completed = run_cli(dest, "replay", "--bundle", "examples/sample-bundle.json",
                                "--result", "examples/sample-result.json")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            binding = json.loads(completed.stdout)["binding"]
            self.assertEqual(binding["kind"], "replay")
            self.assertFalse(binding["provider_called"])
            self.assertEqual(binding["task_logic_sha256"], hashlib.sha256(
                (dest / "src" / "task_logic.py").read_bytes()).hexdigest())


class MigrateTests(unittest.TestCase):
    def test_migrate_resyncs_decision_machinery_not_context_machinery(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_decision_cog(tmp, manifest_format="yaml")
            plan = smith_migrate.plan(dest, to="pixi")
            copied = {Path(dst).name for _, dst in plan["copies"]}
            self.assertNotIn("cog_binding.py", copied)
            self.assertEqual(plan["copies"], [])   # machinery already in sync


class ContractTests(unittest.TestCase):
    def setUp(self):
        # Smith's own loader: no template directory on sys.path, so no
        # created-Cog module name can shadow another test's imports.
        self.contract = smith_check._decision_contract()

    def test_the_docs_example_turn_is_valid(self):
        questions = json.loads((TEMPLATE / "context" / "questions.json").read_text())
        result = json.loads((TEMPLATE / "examples" / "sample-result.json").read_text())
        self.assertEqual(self.contract.result_problems(result, questions), [])

    def test_choice_distribution_must_cover_the_declared_options(self):
        questions = {"c": {"type": "choice", "instructions": "x",
                           "criteria": {"a": None, "b": None}}}
        result = {"model": "m", "answer_source": "system-one-model",
                  "usage": {"input_tokens": 1, "output_tokens": 1},
                  "answers": {"c": {"type": "choice", "choice": "a", "confidence": 0.5,
                                    "probabilities": {"a": 0.9, "z": 0.1}}}}
        self.assertTrue(self.contract.result_problems(result, questions))

    def test_choice_must_be_the_modal_option(self):
        questions = {"c": {"type": "choice", "instructions": "x",
                           "criteria": {"a": None, "b": None}}}
        result = {"model": "m", "answer_source": "system-one-model",
                  "usage": {"input_tokens": 1, "output_tokens": 1},
                  "answers": {"c": {"type": "choice", "choice": "b", "confidence": 0.5,
                                    "probabilities": {"a": 0.9, "b": 0.1}}}}
        self.assertTrue(any("highest" in p for p in self.contract.result_problems(result, questions)))

    def test_probabilities_must_sum_to_one(self):
        questions = {"s": {"type": "score", "instructions": "x", "criteria": ["lo", "hi"]}}
        result = {"model": "m", "answer_source": "llm-adapter",
                  "usage": {"input_tokens": None, "output_tokens": None},
                  "answers": {"s": {"type": "score", "score": 0.5, "confidence": 0.1,
                                    "legend": {"0": "lo", "1": "hi"},
                                    "probabilities": {"0": 0.2, "1": 0.2}}}}
        self.assertTrue(any("sum" in p for p in self.contract.result_problems(result, questions)))

    def test_non_finite_numbers_are_refused(self):
        questions = {"n": {"type": "noul", "instructions": "x"},
                     "c": {"type": "choice", "instructions": "x", "criteria": {"a": None, "b": None}}}
        for field, value in (("noul", float("nan")), ("confidence", float("inf"))):
            result = {"model": "m", "answer_source": "system-one-model",
                      "usage": {"input_tokens": 1, "output_tokens": 1},
                      "answers": {"n": {"type": "noul", "noul": 0.5},
                                  "c": {"type": "choice", "choice": "a", "confidence": 0.5,
                                        "probabilities": {"a": 0.6, "b": 0.4}}}}
            target = result["answers"]["n" if field == "noul" else "c"]
            target[field] = value
            with self.subTest(field=field):
                # NaN passes JSON Schema bounds and is caught as non-finite;
                # infinity already fails the schema's maximum.
                self.assertTrue(self.contract.result_problems(result, questions))

    def test_rounding_tolerance_grows_with_options(self):
        options = {f"o{i}": None for i in range(8)}
        questions = {"c": {"type": "choice", "instructions": "x", "criteria": options}}
        result = {"model": "m", "answer_source": "system-one-model",
                  "usage": {"input_tokens": 1, "output_tokens": 1},
                  "answers": {"c": {"type": "choice", "choice": "o0", "confidence": 0.0,
                                    "probabilities": {k: 0.12 for k in options}}}}
        self.assertEqual(self.contract.result_problems(result, questions), [])   # sums to 0.96
        result["answers"]["c"]["probabilities"] = {k: 0.1 for k in options}      # sums to 0.80
        self.assertTrue(self.contract.result_problems(result, questions))

    def test_result_problems_do_not_echo_provider_values(self):
        questions = {"c": {"type": "choice", "instructions": "x", "criteria": {"a": None, "b": None}}}
        result = {"model": "m", "answer_source": "system-one-model",
                  "usage": {"input_tokens": 1, "output_tokens": 1},
                  "answers": {"c": {"type": "choice", "choice": "SECRET" * 40, "confidence": "SECRET",
                                    "probabilities": {"a": 0.5, "b": 0.5}}}}
        self.assertFalse(any("SECRETSECRET" in p for p in self.contract.result_problems(result, questions)))

    def test_task_state_is_bounded(self):
        task = {"state": "x" * (self.contract.MAX_STATE_BYTES + 1),
                "questions": {"q": {"type": "noul", "instructions": "x"}}}
        self.assertTrue(self.contract.task_problems(task))

    def test_score_must_agree_with_its_distribution(self):
        questions = json.loads((TEMPLATE / "context/questions.json").read_text())
        result = json.loads((TEMPLATE / "examples/sample-result.json").read_text())
        answer = result["answers"]["frustration"]
        answer.update(score=2, probabilities={"0": 1, "1": 0, "2": 0})
        self.assertTrue(any("disagrees" in p for p in
                            self.contract.result_problems(result, questions)))

    def test_score_consistency_allows_rounded_probabilities(self):
        questions = json.loads((TEMPLATE / "context/questions.json").read_text())
        result = json.loads((TEMPLATE / "examples/sample-result.json").read_text())
        answer = result["answers"]["frustration"]
        for probabilities, score in (({"0": .33, "1": .33, "2": .33}, 1),
                                     ({"0": .17, "1": .33, "2": .50}, 1.33),
                                     ({"0": 0, "1": 0, "2": 1}, 2)):
            with self.subTest(probabilities=probabilities, score=score):
                answer.update(score=score, probabilities=probabilities)
                self.assertEqual(self.contract.result_problems(result, questions), [])

    def test_large_score_integer_is_a_problem_not_an_overflow(self):
        questions = json.loads((TEMPLATE / "context/questions.json").read_text())
        result = json.loads((TEMPLATE / "examples/sample-result.json").read_text())
        result["answers"]["frustration"]["score"] = 10 ** 400
        self.assertTrue(self.contract.result_problems(result, questions))

    def test_result_diagnostics_redact_values_and_unknown_keys(self):
        questions = {"c": {"type": "choice", "instructions": "x",
                           "criteria": {"a": None, "b": None}}}
        secret = "PRIVATE_PROVIDER_TEXT"
        base = {"model": "m", "answer_source": "llm-adapter",
                "usage": {"input_tokens": None, "output_tokens": None},
                "answers": {"c": {"type": "choice", "choice": "a", "confidence": .5,
                                  "probabilities": {"a": .5, "b": .5}}}}
        for case in ("choice", "probability-key", "question-key"):
            result = json.loads(json.dumps(base))
            if case == "choice":
                result["answers"]["c"]["choice"] = secret
            elif case == "probability-key":
                result["answers"]["c"]["probabilities"][secret] = "invalid"
            else:
                result["answers"][secret] = {"type": "noul", "noul": float("nan")}
            with self.subTest(case=case):
                problems = self.contract.result_problems(result, questions)
                self.assertTrue(problems)
                self.assertNotIn(secret, " ".join(problems))


if __name__ == "__main__":
    unittest.main()
