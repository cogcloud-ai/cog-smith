"""The Op spec: what it refuses by name, and what its mapping expressions mean.

Contract: planning/current/phase2-op-runner-contract.md §2 and §6. Every
construct outside the runner subset must be refused AT LOAD, named, with the
phase that adds it — never discovered mid-run.
"""
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_spec


def problems(doc):
    try:
        op_spec.OpSpec(doc)
    except op_spec.OpSpecError as exc:
        return exc.problems
    return []


def one(doc):
    return " ".join(problems(doc))


class RefusalTests(unittest.TestCase):
    def test_a_valid_spec_loads(self):
        self.assertEqual(problems(fx.spec_doc()), [])

    def test_tool_step_is_refused_by_name_with_its_phase(self):
        step = fx.cog_step("first")
        step["tool"] = {"name": "gh"}
        text = one(fx.spec_doc([step]))
        self.assertIn("tool:", text)
        self.assertIn("phase 3", text)

    def test_human_step_is_refused_by_name_with_its_phase(self):
        step = fx.cog_step("first")
        step["human"] = {"prompt": "approve?"}
        text = one(fx.spec_doc([step]))
        self.assertIn("human:", text)
        self.assertIn("phase 3", text)

    def test_human_gate_policy_is_refused_with_its_phase(self):
        step = fx.cog_step("first")
        step["gate"] = {"policy": "human"}
        text = one(fx.spec_doc([step]))
        self.assertIn("gate.policy: human", text)
        self.assertIn("phase 3", text)

    def test_state_is_refused_with_its_phase(self):
        text = one(fx.spec_doc(state={"store": "sqlite"}))
        self.assertIn("state:", text)
        self.assertIn("phase 4", text)

    def test_non_empty_guards_are_refused(self):
        step = fx.cog_step("first")
        step["gate"] = {"policy": op_spec.GATE_POLICY, "guards": ["schema"]}
        text = one(fx.spec_doc([step]))
        self.assertIn("gate.guards", text)
        self.assertIn("not in the runner subset", text)

    def test_unknown_top_level_key_is_refused(self):
        self.assertIn("unknown top-level", one(fx.spec_doc(retries=2)))

    def test_unknown_step_key_is_refused(self):
        step = fx.cog_step("first")
        step["timeout"] = 30
        self.assertIn("'timeout'", one(fx.spec_doc([step])))

    def test_unknown_operator_is_refused(self):
        step = fx.cog_step("first")
        step["input"] = {"note": {"$join": ["a", "b"]}}
        text = one(fx.spec_doc([step]))
        self.assertIn("$join", text)
        self.assertIn("closed", text)

    def test_other_schema_values_are_refused(self):
        doc = fx.spec_doc()
        doc["schema"] = "openteams/op-manifest [0.2]"
        self.assertIn("schema must be", one(doc))

    def test_unknown_gate_policy_is_refused(self):
        step = fx.cog_step("first")
        step["gate"] = {"policy": "first-past-the-post"}
        self.assertIn("only policy", one(fx.spec_doc([step])))

    def test_unknown_on_fail_is_refused(self):
        step = fx.cog_step("first", on_fail="carry-on")
        self.assertIn("on_fail", one(fx.spec_doc([step])))

    def test_cycle_is_refused(self):
        a = fx.cog_step("a", depends_on=["b"])
        b = fx.cog_step("b", depends_on=["a"])
        text = one(fx.spec_doc([a, b]))
        self.assertIn("cycle", text)

    def test_reading_a_step_without_depending_on_it_is_refused(self):
        a = fx.cog_step("a")
        b = fx.cog_step("b")
        b["input"] = {"note": {"$from": "steps.a.payload.text"}}
        text = one(fx.spec_doc([a, b]))
        self.assertIn("without depending on", text)
        self.assertIn("'a'", text)

    def test_transitive_dependency_is_enough_to_read(self):
        a = fx.cog_step("a")
        b = fx.cog_step("b", depends_on=["a"])
        c = fx.cog_step("c", depends_on=["b"])
        c["input"] = {"note": {"$from": "steps.a.payload.text"}}
        self.assertEqual(problems(fx.spec_doc([a, b, c])), [])

    def test_reading_an_undeclared_input_is_refused(self):
        step = fx.cog_step("first")
        step["input"] = {"note": {"$from": "inputs.nowhere"}}
        self.assertIn("does not declare", one(fx.spec_doc([step])))

    def test_missing_cog_is_refused(self):
        step = fx.cog_step("first")
        del step["cog"]
        self.assertIn("only step kind", one(fx.spec_doc([step])))

    def test_duplicate_step_ids_are_refused(self):
        self.assertIn("twice",
                      one(fx.spec_doc([fx.cog_step("a"), fx.cog_step("a")])))

    def test_problems_are_one_sentence_each(self):
        step = fx.cog_step("first")
        step["tool"] = {}
        for problem in problems(fx.spec_doc([step], state={})):
            self.assertTrue(problem.endswith("."), problem)
            self.assertNotIn("\n", problem)


class GuideTests(unittest.TestCase):
    """BUILDING_OPS.md shows specs; they must be specs that load."""

    def test_the_guides_example_specs_load(self):
        import re

        import yaml

        text = (Path(__file__).resolve().parents[1] / "BUILDING_OPS.md").read_text()
        blocks = [yaml.safe_load(b) for b in
                  re.findall(r"```yaml\n(.*?)```", text, re.S)]
        self.assertTrue(blocks)
        for block in blocks:
            if isinstance(block, dict) and "schema" in block:
                spec = op_spec.OpSpec(block)
                self.assertTrue(spec.ordered)
            else:                      # a step fragment, not a whole spec
                steps = block if isinstance(block, list) else [block]
                doc = fx.spec_doc(steps, inputs=[{"name": "github_items"},
                                                 {"name": "priority_rubric"}])
                self.assertEqual(problems(doc), [])


class RequestTests(unittest.TestCase):
    def spec(self, inputs):
        return op_spec.OpSpec(fx.spec_doc(inputs=inputs))

    def test_missing_required_input_is_an_error(self):
        spec = self.spec([{"name": "note"}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({})
        self.assertIn("missing required input", str(caught.exception))

    def test_unknown_request_key_is_an_error(self):
        spec = self.spec([{"name": "note"}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({"note": "hi", "extra": 1})
        self.assertIn("undeclared input", str(caught.exception))

    def test_defaults_are_applied(self):
        spec = self.spec([{"name": "note", "default": "fallback"}])
        self.assertEqual(spec.build_inputs({}), {"note": "fallback"})

    def test_optional_input_without_default_is_null(self):
        spec = self.spec([{"name": "note", "required": False}])
        self.assertEqual(spec.build_inputs({}), {"note": None})

    def test_declared_schema_is_validated(self):
        spec = self.spec([{"name": "note", "schema": {"type": "object"}}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({"note": "a string"})
        self.assertIn("violates its declared schema", str(caught.exception))


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ctx = {
            "inputs": {"note": "hello", "items": [{"n": 1}, {"n": 2}],
                       "empty": None},
            "steps": {"a": {"payload": {"text": "from a", "list": [7, 8]},
                            "envelope": {"ok": True}}},
            "run": {"dir": str(self.root / "run"), "id": "run-1"},
            "request": {"dir": str(self.root)},
        }
        (self.root / "run").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self, expr, **extra):
        ctx = dict(self.ctx)
        ctx.update(extra)
        return op_spec.evaluate(expr, ctx)

    def test_from_reads_inputs_steps_run_and_request(self):
        self.assertEqual(self.ev({"$from": "inputs.note"}), "hello")
        self.assertEqual(self.ev({"$from": "steps.a.payload.text"}), "from a")
        self.assertEqual(self.ev({"$from": "steps.a.envelope.ok"}), True)
        self.assertEqual(self.ev({"$from": "run.id"}), "run-1")
        self.assertEqual(self.ev({"$from": "request.dir"}), str(self.root))

    def test_from_indexes_arrays_with_integers(self):
        self.assertEqual(self.ev({"$from": "steps.a.payload.list.1"}), 8)
        self.assertEqual(self.ev({"$from": "inputs.items.0.n"}), 1)

    def test_loop_variable_is_visible(self):
        self.assertEqual(self.ev({"$from": "item.n"}, item={"n": 42}), 42)

    def test_missing_path_without_default_is_an_error(self):
        with self.assertRaises(op_spec.OpSpecError) as caught:
            self.ev({"$from": "inputs.nope"})
        self.assertIn("no $default", str(caught.exception))

    def test_default_covers_missing_and_null(self):
        self.assertEqual(self.ev({"$from": "inputs.nope", "$default": "d"}), "d")
        self.assertEqual(self.ev({"$from": "inputs.empty", "$default": "d"}), "d")

    def test_default_chains(self):
        expr = {"$from": "inputs.nope",
                "$default": {"$from": "inputs.empty",
                             "$default": {"$from": "inputs.note"}}}
        self.assertEqual(self.ev(expr), "hello")

    def test_path_is_absolute_relative_to_the_request_directory(self):
        self.assertEqual(self.ev({"$path": "media/meeting.mp4"}),
                         str((self.root / "media" / "meeting.mp4").resolve()))
        self.assertEqual(self.ev({"$path": {"$from": "inputs.empty",
                                            "$default": None}}), None)

    def test_run_dir_is_absolute_and_created(self):
        value = self.ev({"$run_dir": "transcription"})
        self.assertEqual(value, str((self.root / "run" / "transcription").resolve()))
        self.assertTrue(Path(value).is_dir())

    def test_stem(self):
        self.assertEqual(self.ev({"$stem": "/tmp/meeting.mp4"}), "meeting")
        self.assertEqual(self.ev({"$stem": {"$from": "inputs.empty",
                                            "$default": None}}), None)

    def test_literal_escapes_operator_shaped_objects(self):
        self.assertEqual(self.ev({"$literal": {"$from": "not a path"}}),
                         {"$from": "not a path"})

    def test_nested_objects_and_arrays_are_walked(self):
        expr = {"outer": [{"note": {"$from": "inputs.note"}}, "plain", 3],
                "deep": {"a": {"b": {"$stem": "x/y.txt"}}}}
        self.assertEqual(self.ev(expr),
                         {"outer": [{"note": "hello"}, "plain", 3],
                          "deep": {"a": {"b": "y"}}})

    def test_unknown_operator_is_an_error_at_evaluation_too(self):
        with self.assertRaises(op_spec.OpSpecError):
            self.ev({"$nope": 1})

    def test_reads_steps_sees_step_paths_only(self):
        self.assertTrue(op_spec.reads_steps({"a": {"$from": "steps.a.payload"}}))
        self.assertFalse(op_spec.reads_steps({"a": {"$from": "inputs.note"}}))
        self.assertFalse(op_spec.reads_steps(
            {"a": {"$literal": {"$from": "steps.a.payload"}}}))


class OrderTests(unittest.TestCase):
    def test_order_respects_depends_on_and_breaks_ties_by_spec_order(self):
        a = fx.cog_step("a")
        b = fx.cog_step("b")
        c = fx.cog_step("c", depends_on=["a", "b"])
        spec = op_spec.OpSpec(fx.spec_doc([c, a, b]))
        self.assertEqual([s["id"] for s in spec.ordered], ["a", "b", "c"])

    def test_dependents_are_transitive(self):
        a = fx.cog_step("a")
        b = fx.cog_step("b", depends_on=["a"])
        c = fx.cog_step("c", depends_on=["b"])
        spec = op_spec.OpSpec(fx.spec_doc([a, b, c]))
        self.assertEqual(spec.dependents()["a"], {"b", "c"})


class ReviewRegressionTests(unittest.TestCase):
    """Codex review 2026-09-17 (phase2-codex-review-1-cog-smith.md), findings
    3, 7, 8, 11, 13, 14 — the spec half."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        self.escape = self.root / "mapped-escape"
        self.ctx = {"inputs": {"dest": str(self.escape)}, "steps": {},
                    "run": {"dir": str(self.run_dir), "id": "run-1"},
                    "request": {"dir": str(self.root)}}

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------- finding 3 --

    def test_run_dir_refuses_an_absolute_subpath_and_creates_nothing(self):
        outside = self.root / "outside"
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.evaluate({"$run_dir": str(outside)}, self.ctx)
        self.assertIn("inside this run", str(caught.exception))
        self.assertFalse(outside.exists())

    def test_run_dir_refuses_a_traversal_and_creates_nothing(self):
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$run_dir": "../escaped"}, self.ctx)
        self.assertFalse((self.run_dir.parent / "escaped").exists())

    def test_run_dir_refuses_an_escape_through_a_symlink(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.run_dir / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$run_dir": "link/artifacts"}, self.ctx)
        self.assertFalse((outside / "artifacts").exists())

    def test_run_dir_refuses_an_absolute_path_that_arrived_as_mapped_data(self):
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$run_dir": {"$from": "inputs.dest"}}, self.ctx)
        self.assertFalse(self.escape.exists())

    def test_run_dir_still_creates_a_directory_inside_the_run(self):
        value = op_spec.evaluate({"$run_dir": "transcription/audio"}, self.ctx)
        self.assertTrue(Path(value).is_dir())
        self.assertTrue(Path(value).resolve().is_relative_to(
            self.run_dir.resolve()))

    def test_a_literal_run_dir_escape_is_refused_at_load(self):
        step = fx.cog_step("first")
        step["input"] = {"out": {"$run_dir": "../escaped"}}
        self.assertIn("inside this run", one(fx.spec_doc([step])))

    # ---------------------------------------------------------- finding 7 --

    def test_an_explicit_null_satisfies_a_required_nullable_input(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[
            {"name": "note", "schema": {"type": ["string", "null"]}}]))
        self.assertEqual(spec.build_inputs({"note": None}), {"note": None})

    def test_an_explicit_null_is_not_replaced_by_the_default(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[
            {"name": "note", "required": False, "default": "fallback"}]))
        self.assertEqual(spec.build_inputs({"note": None}), {"note": None})

    def test_a_declared_null_default_satisfies_an_omitted_required_input(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[
            {"name": "note", "required": True, "default": None}]))
        self.assertEqual(spec.build_inputs({}), {"note": None})

    # ---------------------------------------------------------- finding 8 --

    def test_an_invalid_input_schema_is_refused_at_load(self):
        text = one(fx.spec_doc(inputs=[{"name": "note",
                                        "schema": {"type": "not-a-type"}}]))
        self.assertIn("not valid JSON Schema", text)

    def test_a_false_schema_rejects_every_value(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[{"name": "note",
                                                   "schema": False}]))
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({"note": "anything"})
        self.assertIn("violates its declared schema", str(caught.exception))

    def test_an_explicit_null_is_validated_against_the_schema(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[{"name": "note",
                                                   "schema": {"type": "string"}}]))
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({"note": None})
        self.assertIn("violates its declared schema", str(caught.exception))

    # --------------------------------------------------------- finding 11 --

    def test_a_step_made_ready_runs_before_a_later_independent_step(self):
        a = fx.cog_step("a", depends_on=["b"])
        b = fx.cog_step("b")
        c = fx.cog_step("c")
        a["input"] = {"note": {"$from": "steps.b.payload.text"}}
        spec = op_spec.OpSpec(fx.spec_doc([a, b, c]))
        self.assertEqual([s["id"] for s in spec.ordered], ["b", "a", "c"])

    # --------------------------------------------------------- finding 13 --

    def test_a_non_string_step_id_is_a_named_problem(self):
        step = fx.cog_step("first")
        step["id"] = ["first"]
        self.assertIn("must be an object with an id", one(fx.spec_doc([step])))

    def test_a_non_list_depends_on_is_a_named_problem(self):
        step = fx.cog_step("first")
        step["depends_on"] = 1
        self.assertIn("depends_on is a list", one(fx.spec_doc([step])))

    def test_a_non_string_input_key_is_a_named_problem(self):
        step = fx.cog_step("first")
        step["input"] = {3: {"$from": "inputs.note"}}
        self.assertIn("not a string", one(fx.spec_doc([step])))

    def test_a_non_string_input_name_is_a_named_problem(self):
        self.assertIn("must be an object with a name",
                      one(fx.spec_doc(inputs=[{"name": 7}])))

    # --------------------------------------------------------- finding 14 --

    def test_an_object_with_an_operator_key_and_siblings_is_ordinary_data(self):
        step = fx.cog_step("first")
        step["input"] = {"annotated": {"$from": "literal text", "label": "x"}}
        self.assertEqual(problems(fx.spec_doc([step])), [])
        self.assertEqual(
            op_spec.evaluate({"$from": "literal text", "label": "x"}, self.ctx),
            {"$from": "literal text", "label": "x"})

    def test_an_all_dollar_key_set_that_is_not_an_operator_is_refused(self):
        step = fx.cog_step("first")
        step["input"] = {"note": {"$from": "inputs.note", "$stem": "x"}}
        text = one(fx.spec_doc([step]))
        self.assertIn("unknown mapping operator", text)
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$from": "inputs.note", "$stem": "x"}, self.ctx)

    def test_an_ordinary_object_carrying_a_step_read_still_reads_steps(self):
        self.assertFalse(op_spec.reads_steps(
            {"$from": "steps.a.payload", "label": "not an operator"}))
        self.assertTrue(op_spec.reads_steps(
            {"wrapped": {"$from": "steps.a.payload"}}))


if __name__ == "__main__":
    unittest.main()
