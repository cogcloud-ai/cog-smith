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


if __name__ == "__main__":
    unittest.main()
