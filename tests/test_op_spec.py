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

    def test_tool_step_is_refused_by_name_pointing_at_code_cogs(self):
        step = fx.cog_step("first")
        step["tool"] = {"name": "gh"}
        text = one(fx.spec_doc([step]))
        self.assertIn("tool:", text)
        self.assertIn("kind: code", text)
        self.assertNotIn("phase", text)

    def test_human_step_is_refused_pointing_at_the_gate_policy(self):
        # There is no human: step kind: a human Gate is a POLICY on the step
        # that produces what the human decides about (phase 3 §3).
        step = fx.cog_step("first")
        step["human"] = {"prompt": "approve?"}
        text = one(fx.spec_doc([step]))
        self.assertIn("human:", text)
        self.assertIn("gate: {policy: human}", text)
        self.assertNotIn("phase", text)

    def test_human_gate_policy_is_accepted(self):
        step = fx.cog_step("first")
        step["gate"] = {"policy": "human", "guards": []}
        op_spec.validate(fx.spec_doc([step]))

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
        self.assertIn("the Gate policies are", one(fx.spec_doc([step])))

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
                                                 {"name": "priority_rubric"},
                                                 {"name": "repo_config"}])
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

    def test_an_omitted_optional_input_without_a_default_is_absent(self):
        """Absence is not a value: the input is simply not there, so a
        declared schema never judges a null the request never carried."""
        spec = self.spec([{"name": "note", "required": False}])
        self.assertEqual(spec.build_inputs({}), {})

    def test_an_omitted_optional_typed_input_is_not_validated_as_null(self):
        spec = self.spec([{"name": "note", "required": False,
                           "schema": {"type": "string"}}])
        self.assertEqual(spec.build_inputs({}), {})

    def test_a_declared_null_default_is_still_validated(self):
        spec = self.spec([{"name": "note", "required": False, "default": None,
                           "schema": {"type": "string"}}])
        with self.assertRaises(op_spec.OpSpecError) as caught:
            spec.build_inputs({})
        self.assertIn("violates its declared schema", str(caught.exception))

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

    def test_a_known_key_set_that_is_not_an_operator_is_ordinary_data(self):
        """Contract §2: an operator is an EXACT key set. `{$from, $stem}` is
        not one, and its keys are known names, so it is walked as data."""
        step = fx.cog_step("first")
        step["input"] = {"note": {"$from": "inputs.note", "$stem": "x"}}
        self.assertEqual(problems(fx.spec_doc([step])), [])
        self.assertEqual(
            op_spec.evaluate({"$from": "inputs.note", "$stem": "x"}, self.ctx),
            {"$from": "inputs.note", "$stem": "x"})

    def test_an_unknown_dollar_key_is_refused_even_with_siblings(self):
        """Finding 5 (round 3): sibling keys do not turn `$join` into data;
        an unknown operator name is refused wherever it appears."""
        step = fx.cog_step("first")
        step["input"] = {"joined": {"$join": ["a", "b"], "label": "x"}}
        self.assertIn("unknown mapping operator", one(fx.spec_doc([step])))
        with self.assertRaises(op_spec.OpSpecError):
            op_spec.evaluate({"$join": ["a", "b"], "label": "x"}, self.ctx)

    def test_an_unknown_dollar_key_nested_in_data_is_refused(self):
        step = fx.cog_step("first")
        step["input"] = {"wrapper": {"inner": {"$concat": ["a"]}}}
        self.assertIn("unknown mapping operator", one(fx.spec_doc([step])))

    def test_an_ordinary_object_carrying_a_step_read_still_reads_steps(self):
        self.assertFalse(op_spec.reads_steps(
            {"$from": "steps.a.payload", "label": "not an operator"}))
        self.assertTrue(op_spec.reads_steps(
            {"wrapped": {"$from": "steps.a.payload"}}))


class VerificationRoundTests(unittest.TestCase):
    """Codex verification round (phase2-codex-review-3-cog-smith-verification.md),
    items 2 and 3 — the spec half."""

    def ctx(self, values):
        return {"inputs": values, "steps": {}, "run": {"dir": "/tmp", "id": "r"},
                "request": {"dir": "/tmp"}}

    def test_a_from_on_an_absent_optional_input_takes_its_default(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[
            {"name": "note", "required": False, "schema": {"type": "string"}}]))
        values = spec.build_inputs({})
        self.assertEqual(
            op_spec.evaluate({"$from": "inputs.note", "$default": "fallback"},
                             self.ctx(values)), "fallback")

    def test_a_from_on_an_absent_optional_input_without_a_default_is_named(self):
        spec = op_spec.OpSpec(fx.spec_doc(inputs=[
            {"name": "note", "required": False}]))
        values = spec.build_inputs({})
        with self.assertRaises(op_spec.OpSpecError) as caught:
            op_spec.evaluate({"$from": "inputs.note"}, self.ctx(values))
        self.assertIn("not available in this run", str(caught.exception))

    def test_a_list_valued_foreach_as_is_a_named_problem_not_a_typeerror(self):
        step = fx.cog_step("first")
        step["foreach"] = {"items": {"$from": "inputs.note"}, "as": ["item"]}
        self.assertIn("declares foreach.as", one(fx.spec_doc([step])))

    def test_a_foreach_as_that_is_not_a_name_is_a_named_problem(self):
        step = fx.cog_step("first")
        step["foreach"] = {"items": {"$from": "inputs.note"}, "as": "the item"}
        self.assertIn("declares foreach.as", one(fx.spec_doc([step])))

    def test_non_string_cog_fields_are_named_problems(self):
        for field, value in (("source", ["../cog-x"]), ("id", 7),
                             ("task", {"name": "ask"}), ("version", 1)):
            step = fx.cog_step("first")
            step["cog"][field] = value
            self.assertIn(f"declares cog.{field}", one(fx.spec_doc([step])),
                          f"cog.{field} = {value!r}")


class FinalRoundTests(unittest.TestCase):
    """Codex final round (phase2-codex-review-4-cog-smith-final.md), item 1 —
    a loop variable may not name a run-context root."""

    def test_a_foreach_as_that_collides_with_a_path_root_is_refused(self):
        for root in op_spec.PATH_ROOTS:
            step = fx.cog_step("first")
            step["foreach"] = {"items": {"$from": "inputs.note"}, "as": root}
            message = one(fx.spec_doc([step]))
            self.assertIn("declares foreach.as", message, root)
            self.assertIn("name the run context", message, root)

    def test_a_loop_variable_that_is_not_a_path_root_still_loads(self):
        step = fx.cog_step("first")
        step["foreach"] = {"items": {"$from": "inputs.note"}, "as": "item"}
        step["input"] = {"title": {"$from": "item"}}
        self.assertEqual(problems(fx.spec_doc([step])), [])


class RepeatTests(unittest.TestCase):
    """`repeat: {count, require}` — narrowing contract §2 (machinery 0.6.0).

    Everything a repeated step may NOT be is refused at LOAD, by name: the
    bounds, the types, the unknown keys, and the three shapes of step that
    are never repeated (effectful, reaching, human-gated)."""

    def step(self, repeat, **extra):
        return fx.cog_step("first", repeat=repeat, **extra)

    def test_a_declared_repeat_loads(self):
        self.assertEqual(
            problems(fx.spec_doc([self.step({"count": 3, "require": 1})])), [])

    def test_the_normalized_spec_defaults_require_to_count(self):
        # Silence never loosens a Gate: a step that states no tolerance
        # requires every repeat to pass.
        self.assertEqual(op_spec.repeat_spec(self.step({"count": 3})),
                         {"count": 3, "require": 3})
        self.assertEqual(op_spec.repeat_spec(self.step({"count": 3,
                                                        "require": 2})),
                         {"count": 3, "require": 2})
        self.assertIsNone(op_spec.repeat_spec(fx.cog_step("first")))

    def test_an_unknown_repeat_key_is_refused_by_name(self):
        text = one(fx.spec_doc([self.step({"count": 2, "until": "agreement"})]))
        self.assertIn("'until'", text)
        self.assertIn("the step vocabulary is closed", text)

    def test_a_repeat_that_is_not_an_object_is_refused(self):
        text = one(fx.spec_doc([self.step(3)]))
        self.assertIn("declares repeat 3", text)
        self.assertIn("count and require", text)

    def test_a_repeat_with_no_count_is_refused(self):
        text = one(fx.spec_doc([self.step({"require": 1})]))
        self.assertIn("declares no count", text)

    def test_a_non_integer_count_is_refused_by_type(self):
        # `True` is an int in Python; a boolean count is a wrong type here.
        for value in ("3", 2.5, True):
            text = one(fx.spec_doc([self.step({"count": value})]))
            self.assertIn("a repeat count is an integer", text, repr(value))

    def test_an_explicit_null_count_reads_as_no_count(self):
        text = one(fx.spec_doc([self.step({"count": None})]))
        self.assertIn("declares no count", text)

    def test_a_non_integer_require_is_refused_by_type(self):
        text = one(fx.spec_doc([self.step({"count": 3, "require": "one"})]))
        self.assertIn("a repeat requirement is an integer", text)

    def test_an_explicit_null_require_is_refused_by_name(self):
        """Machinery 0.6.1, finding 11: only OMISSION defaults to `count`.

        `require: null` used to load and normalize to "all of them" — a
        fail-closed reading of a malformed declaration, but a malformed one
        all the same. It is refused like any other non-integer."""
        text = one(fx.spec_doc([self.step({"count": 3, "require": None})]))
        self.assertIn("declares repeat.require None", text)
        self.assertIn("a repeat requirement is an integer", text)

    def test_the_count_bounds_are_one_to_five(self):
        for value in (0, -1, 6, 50):
            text = one(fx.spec_doc([self.step({"count": value})]))
            self.assertIn(f"declares repeat.count {value}", text)
            self.assertIn("between 1 and 5 times", text)
        self.assertEqual(problems(fx.spec_doc([self.step({"count": 5})])), [])
        self.assertEqual(problems(fx.spec_doc([self.step({"count": 1})])), [])

    def test_require_is_between_one_and_count(self):
        for value in (0, 4):
            text = one(fx.spec_doc([self.step({"count": 3, "require": value})]))
            self.assertIn(f"declares repeat.require {value}", text)
            self.assertIn("between 1 and count", text)

    def test_repeat_on_a_step_with_authority_is_refused(self):
        step = self.step({"count": 2, "require": 1})
        step["authority"] = {"requires": [{"resource": "github",
                                           "action": "read",
                                           "repositories": ["a/b"]}]}
        text = one(fx.spec_doc([step]))
        self.assertIn("declares repeat and authority", text)
        self.assertIn("never repeated", text)

    def test_repeat_with_a_human_gate_is_refused(self):
        step = self.step({"count": 2, "require": 1})
        step["gate"] = {"policy": "human", "guards": []}
        text = one(fx.spec_doc([step]))
        self.assertIn("gate.policy: human", text)
        self.assertIn("never repeated", text)

    def test_repeat_inside_a_foreach_loads(self):
        step = self.step({"count": 3, "require": 2})
        step["foreach"] = {"items": {"$from": "inputs.note"}, "as": "item"}
        step["input"] = {"title": {"$from": "item"}}
        self.assertEqual(problems(fx.spec_doc([step])), [])


class RepeatDeclarationTests(unittest.TestCase):
    """The refusal that needs the COG's manifest: a Cog that declares
    `reaches` is never repeated (narrowing contract §2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def findings(self, reaches):
        fx.write_cog(self.root, "first", reaches=reaches)
        step = fx.cog_step("first", repeat={"count": 2, "require": 1})
        return op_spec.cog_step_findings(step, self.root / "cog-first")

    def test_a_reaching_cog_refuses_repeat(self):
        findings = self.findings([{"resource": "github", "actions": ["read"]}])
        detail = " ".join(d for level, d in findings if level == "error")
        self.assertIn("reaches outside the run, and declares repeat", detail)
        self.assertIn("never repeated", detail)

    def test_a_cog_that_reaches_nothing_may_repeat(self):
        findings = self.findings([])
        self.assertEqual([d for level, d in findings if level == "error"], [])


if __name__ == "__main__":
    unittest.main()
