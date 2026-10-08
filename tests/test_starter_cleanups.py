"""Starter and template cleanups (issue #14): a code Cog is created with a
.gitignore, a summary may contain an apostrophe, `check` warns about starter
values nobody replaced, and the guides' version statements match the
machinery constants."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import smith_check  # noqa: E402
import smith_core   # noqa: E402
import smith_manifest  # noqa: E402
import smith_op     # noqa: E402

CLI = ROOT / "src" / "cogsmith_cli.py"


def create(tmp, name, template, manifest_format="pixi", overlays=None,
           **overrides):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name, **overrides),
                      template=template, manifest_format=manifest_format,
                      overlays=overlays)
    return dest


def warnings(findings, check_name):
    return [f["detail"] for f in findings
            if f["level"] == "warn" and f["check"] == check_name]


class TestGitignore(unittest.TestCase):
    def test_every_created_cog_ignores_local_state(self):
        for template in ("context-cog", "code-cog", "decision-cog"):
            with self.subTest(template=template), \
                    tempfile.TemporaryDirectory() as tmp:
                dest = create(tmp, "cog-toy", template)
                ignored = (dest / ".gitignore").read_text().split()
                for entry in (".pixi/", "__pycache__/", "*.pyc", "runs/"):
                    self.assertIn(entry, ignored)


class TestApostropheSummary(unittest.TestCase):
    SUMMARY = "Counts a reader's words."

    def test_the_cli_accepts_an_apostrophe_in_both_formats(self):
        for kind, fmt in (("context", "pixi"), ("context", "yaml"),
                          ("code", "pixi"), ("code", "yaml")):
            with self.subTest(kind=kind, fmt=fmt), \
                    tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "cog-word-count"
                r = subprocess.run(
                    [sys.executable, str(CLI), "new", "--dir", str(dest),
                     "--kind", kind, "--manifest", fmt, "--yes",
                     "--summary", self.SUMMARY, "--produces", "word_count"],
                    capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(
                    smith_manifest.load(dest)[0]["summary"].strip(),
                    self.SUMMARY)

    def test_the_guide_gives_the_invocation_that_accepts_one(self):
        guide = (ROOT / "BUILDING_COGS.md").read_text()
        self.assertIn("apostrophe", guide)
        self.assertIn("python src/cogsmith_cli.py new", guide)


class TestStarterValues(unittest.TestCase):
    def test_starter_produces_warns_in_every_template(self):
        for template in ("context-cog", "code-cog", "decision-cog"):
            for fmt in ("pixi", "yaml"):
                with self.subTest(template=template, fmt=fmt), \
                        tempfile.TemporaryDirectory() as tmp:
                    findings = smith_check.check(
                        create(tmp, "cog-toy", template, fmt))
                    self.assertEqual(
                        [f for f in findings if f["level"] == "error"], [])
                    details = warnings(findings, "declarations")
                    self.assertEqual(
                        len([d for d in details if "io.produces" in d]), 1,
                        findings)

    def test_a_replaced_produces_does_not_warn(self):
        for template in ("context-cog", "code-cog", "decision-cog"):
            with self.subTest(template=template), \
                    tempfile.TemporaryDirectory() as tmp:
                findings = smith_check.check(
                    create(tmp, "cog-toy", template, PRODUCES="word_count"))
                self.assertFalse(
                    [d for d in warnings(findings, "declarations")
                     if "io.produces" in d], findings)

    def test_the_starter_value_beside_another_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "cog-toy", "context-cog", "yaml")
            path = dest / "cog.yaml"
            path.write_text(path.read_text().replace(
                "produces: [highlights]", "produces: [highlights, summary]"))
            findings = smith_check.check(dest)
            self.assertFalse(
                [d for d in warnings(findings, "declarations")
                 if "io.produces" in d], findings)

    def test_an_io_that_is_not_a_mapping_is_a_finding_not_a_crash(self):
        for value in ("highlights", "[highlights]"):
            with self.subTest(value=value), \
                    tempfile.TemporaryDirectory() as tmp:
                dest = create(tmp, "cog-toy", "context-cog", "yaml")
                path = dest / "cog.yaml"
                doc = yaml.safe_load(path.read_text())
                doc["io"] = yaml.safe_load(value)
                path.write_text(yaml.safe_dump(doc))
                findings = smith_check.check(dest)
                self.assertIn(
                    ("error", "profile", "manifest"),
                    [(f["level"], f["layer"], f["check"]) for f in findings
                     if "io must be a mapping" in f["detail"]], findings)
                self.assertFalse(
                    [d for d in warnings(findings, "declarations")
                     if "io.produces" in d], findings)


class TestUnusedRequiredFeatures(unittest.TestCase):
    QUESTIONS = {"is_urgent": {
        "type": "noul",
        "instructions": "The ticket conveys urgency or time-sensitivity.",
        "criteria": {"true": "Explicitly time-sensitive",
                     "false": "No urgency expressed"}}}

    def one_question_cog(self, tmp):
        """The starter with its question set cut down to one `noul`
        question, and its worked example cut down to match."""
        example = json.loads((smith_core.TEMPLATES / "decision-cog" / "context"
                              / "output-example.json").read_text())
        example["answers"] = {"is_urgent": example["answers"]["is_urgent"]}
        return create(tmp, "cog-toy", "decision-cog", "yaml",
                      overlays={"context/questions.json":
                                json.dumps(self.QUESTIONS),
                                "context/output-example.json":
                                json.dumps(example)},
                      PRODUCES="urgency")

    def test_the_starter_question_set_uses_every_declared_feature(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = smith_check.check(
                create(tmp, "cog-toy", "decision-cog", PRODUCES="routing"))
            self.assertEqual(findings, [], findings)

    def test_features_the_questions_do_not_use_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = smith_check.check(self.one_question_cog(tmp))
            self.assertEqual(
                [f for f in findings if f["level"] == "error"], [])
            details = warnings(findings, "decision")
            self.assertEqual(len(details), 1, findings)
            self.assertIn("['choice', 'score']", details[0])

    def test_narrowing_the_declaration_clears_the_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self.one_question_cog(tmp)
            path = dest / "cog.yaml"
            path.write_text(path.read_text().replace(
                "required_features: [choice, noul, score]",
                "required_features: [noul]"))
            self.assertEqual(smith_check.check(dest), [])

    def test_features_that_are_not_strings_are_a_finding_not_a_crash(self):
        for value in ("[noul, choice, 42]", "noul", "[noul, [choice]]"):
            with self.subTest(value=value), \
                    tempfile.TemporaryDirectory() as tmp:
                dest = self.one_question_cog(tmp)
                path = dest / "cog.yaml"
                path.write_text(path.read_text().replace(
                    "required_features: [choice, noul, score]",
                    f"required_features: {value}"))
                findings = smith_check.check(dest)
                self.assertIn(
                    ("error", "profile", "decision"),
                    [(f["level"], f["layer"], f["check"]) for f in findings
                     if "must be a list of strings" in f["detail"]], findings)
                self.assertEqual(warnings(findings, "decision"), [])


class TestVersionStatements(unittest.TestCase):
    """Every statement of a CURRENT machinery version in AGENTS.md and the
    guides' headers is tested against the constant it restates."""
    STATEMENT_RE = re.compile(
        r"\b(Op|context-cog|code-cog|decision-cog)\s+machinery\s+"
        r"(\d+\.\d+\.\d+)")

    @classmethod
    def setUpClass(cls):
        def constant(template, pattern):
            text = (ROOT / "templates" / template / "src"
                    / "cog_core.py").read_text()
            return re.search(pattern, text, re.M).group(1)
        cls.versions = {
            "Op": smith_op.MACHINERY_VERSION,
            "context-cog": smith_core.CONTEXT_MACHINERY_VERSION,
            "code-cog": constant(
                "code-cog", r'^MACHINERY_VERSION = "([0-9.]+)"$'),
            "decision-cog": constant(
                "decision-cog", r'^MACHINERY = "decision-cog ([0-9.]+)"$'),
        }

    def statements(self, name, header_lines=None):
        text = (ROOT / name).read_text()
        if header_lines:
            text = "\n".join(text.splitlines()[:header_lines])
        return self.STATEMENT_RE.findall(text)

    def assert_current(self, found, expected_lineages):
        self.assertEqual(sorted({lineage for lineage, _ in found}),
                         sorted(expected_lineages))
        for lineage, version in found:
            self.assertEqual(version, self.versions[lineage], lineage)

    def test_agents_md(self):
        self.assert_current(self.statements("AGENTS.md"),
                            ("Op", "code-cog", "decision-cog"))

    def test_building_ops_header(self):
        self.assert_current(self.statements("BUILDING_OPS.md", 8), ("Op",))

    def test_building_cogs_header(self):
        self.assert_current(
            self.statements("BUILDING_COGS.md", 8),
            ("Op", "context-cog", "code-cog", "decision-cog"))

    def test_the_context_constant_is_the_one_machinery_md_records(self):
        version = re.escape(smith_core.CONTEXT_MACHINERY_VERSION)
        self.assertRegex((ROOT / "MACHINERY.md").read_text(),
                         rf"(?m)^## ({version} \(|.*\bcontext {version}\b)")


if __name__ == "__main__":
    unittest.main()
