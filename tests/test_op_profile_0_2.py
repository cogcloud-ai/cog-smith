"""`openteams/op-manifest [0.2]` beside `[0.1]` (Op machinery 0.11.0).

A `[0.2]` document is held to the profile's tightenings at load, and to its
run-time rules when it runs; a `[0.1]` document — every Op written before
the profile — loads and runs exactly as it did under 0.10.0. Each rule is
tested both ways.
"""
import unittest

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

    def test_a_0_1_document_reads_name_as_title(self):
        spec = op_spec.OpSpec(fx.spec_doc())
        self.assertEqual(spec.schema, op_spec.SCHEMA_STRING_0_1)
        self.assertEqual(spec.profile, op_spec.PROFILE_0_1)
        self.assertEqual(spec.title, "Test Op")
        self.assertEqual(op_spec.step_title(spec.steps[0]), "Step first")

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


if __name__ == "__main__":
    unittest.main()
