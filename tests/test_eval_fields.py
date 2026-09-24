"""Context-cog machinery 0.4.2: an eval fixture can assert the RESULT.

From an internal review. A fixture's vocabulary could say what a Cog
must not SAY (`forbid_tokens`) but not what it must DECIDE. A
review-priority fixture was therefore passed by a
response that assigned `P2` while citing some other grounded passage from
the bundle, and FAILED by a correct `unrated` answer whose `priority_reason`
explained that the reviewer's "Blocking (P2)" marker was ineligible — the
forbidden token appearing inside the explanation of why it was forbidden.

`expect.fields` asserts payload keys against the values the answer must
carry. The Cog is invoked through a created package's own `cog_eval`, with
`cog_core.invoke` replaced, so this drives the master the created Cogs carry.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_core  # noqa: E402

#: Run one fixture inside a created Cog with a canned payload, and report the
#: eval's own check lines. `cog_core.invoke` is replaced, so no model and no
#: network: the point is the FIXTURE vocabulary, not the model.
PROBE = """
import json, sys
sys.path.insert(0, 'src')
import cog_core, cog_eval
payload = json.loads(sys.argv[1])
cog_core.invoke = lambda bundle, **kw: {
    "ok": True, "error": None, "payload": payload, "problems": [],
    "raw": json.dumps(payload), "binding": None, "timing": {}}
report = cog_eval.run_fixture(sys.argv[2])
print(json.dumps({"passed": report["passed"], "checks": report["checks"]}))
"""


class EvalFieldsTests(unittest.TestCase):
    ANSWER = {"abstained": False, "item_id": "o/r#45", "type": "infra",
              "area": "infra", "priority": "unrated",
              "priority_reason": "The only severity marker is a reviewer's "
                                 "\"Blocking (P2)\" finding, which is not "
                                 "eligible evidence.",
              "priority_evidence": None}

    def run_fixture(self, expect, payload=None):
        with tempfile.TemporaryDirectory() as tmp:
            cog = Path(tmp) / "cog-toy"
            smith_core.create(cog, smith_core.default_tokens("cog-toy"))
            bundle = cog / "examples" / "sample-bundle.json"
            fixture = cog / "evals" / "structured.fixture.yaml"
            fixture.write_text(json.dumps(
                {"name": "structured", "bundle": "examples/sample-bundle.json",
                 "expect": expect}))
            self.assertTrue(bundle.exists())
            probe = subprocess.run(
                [sys.executable, "-c", PROBE,
                 json.dumps(payload if payload is not None else self.ANSWER),
                 str(fixture)],
                cwd=str(cog), capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            return json.loads(probe.stdout.strip().splitlines()[-1])

    def named(self, report, needle):
        return [c for c in report["checks"] if needle in c["check"]]

    def test_a_field_expectation_is_checked_and_named(self):
        report = self.run_fixture({"error": False, "parsed": True,
                                   "fields": {"priority": "unrated"}})
        checks = self.named(report, "priority ==")
        self.assertEqual(len(checks), 1, report["checks"])
        self.assertTrue(checks[0]["ok"], checks[0])
        self.assertTrue(report["passed"], report["checks"])

    def test_a_null_expectation_is_a_real_expectation(self):
        """`priority_evidence: null` is the other half of "unrated": a
        fixture that only names `priority` would accept a citation beside
        it."""
        report = self.run_fixture({"error": False, "parsed": True,
                                   "fields": {"priority_evidence": None}})
        self.assertTrue(report["passed"], report["checks"])

    def test_the_wrong_decision_fails_even_with_the_token_avoided(self):
        """The reviewer's first residual, exactly: an answer that assigns P2 while
        citing a different grounded passage never says the forbidden token,
        and a token ban therefore passes it."""
        wrong = dict(self.ANSWER, priority="P2",
                     priority_reason="Deploys nebari-dev/harbor-pack.",
                     priority_evidence="Deploys nebari-dev/harbor-pack")
        banned = self.run_fixture(
            {"error": False, "parsed": True,
             "forbid_tokens": ["Blocking (P2)"]}, payload=wrong)
        self.assertTrue(banned["passed"], "the token ban passes a wrong answer")
        asserted = self.run_fixture(
            {"error": False, "parsed": True,
             "fields": {"priority": "unrated", "priority_evidence": None}},
            payload=wrong)
        self.assertFalse(asserted["passed"], asserted["checks"])

    def test_the_right_decision_fails_a_token_ban_and_passes_fields(self):
        """The reviewer's second residual: the correct answer EXPLAINS why the
        reviewer's marker is ineligible, and so contains it."""
        banned = self.run_fixture(
            {"error": False, "parsed": True,
             "forbid_tokens": ["Blocking (P2)"]})
        self.assertFalse(banned["passed"],
                         "the token ban fails the correct answer")
        asserted = self.run_fixture(
            {"error": False, "parsed": True,
             "fields": {"priority": "unrated", "priority_evidence": None}})
        self.assertTrue(asserted["passed"], asserted["checks"])

    def test_an_abstention_has_fields_too(self):
        """The check is outside the not-abstained block on purpose."""
        report = self.run_fixture(
            {"error": False, "parsed": True, "abstained": True,
             "fields": {"priority": None}},
            payload={"abstained": True, "item_id": "o/r#45", "type": None,
                     "area": None, "priority": None, "priority_reason": None,
                     "priority_evidence": None})
        self.assertTrue(report["passed"], report["checks"])

    def test_no_fields_key_adds_no_checks(self):
        report = self.run_fixture({"error": False, "parsed": True})
        self.assertEqual(self.named(report, " == "), [])


if __name__ == "__main__":
    unittest.main()
