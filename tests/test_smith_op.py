"""`smith op new` and `smith op check`: creation, collisions, machinery
hashes, and step declarations checked against the Cogs they name.

Contract: planning/current/phase2-op-runner-contract.md §1 and §6.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

import op_fixtures as fx
from op_fixtures import SMITH_ROOT, op_spec

sys.path.insert(0, str(SMITH_ROOT / "src"))
import smith_op  # noqa: E402

CLI = SMITH_ROOT / "src" / "cogsmith_cli.py"


def write_cog(root, cog_id, interfaces, version="0.1.0"):
    """A minimal Cog package: enough manifest for `op check` to read its
    declared interfaces."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "cog.yaml").write_text(yaml.safe_dump({
        "schema": "openteams/cog-manifest [0.1]",
        "id": cog_id, "version": version, "kind": "context",
        "summary": "A test Cog.", "owner": "t@example.com",
        "license": "BSD-3-Clause",
        "io": {"accepts": ["items"], "produces": ["notes"]},
        "interfaces": interfaces,
    }, sort_keys=False))
    return root


class SmithOpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "op-sample"
        self.spec_path = self.root / "spec.yaml"
        self.spec_path.write_text(yaml.safe_dump(self.spec_doc(),
                                                 sort_keys=False))

    def tearDown(self):
        self.tmp.cleanup()

    def spec_doc(self):
        first = fx.cog_step("first", task="ask")
        second = fx.cog_step("second", task="summarize", depends_on=["first"])
        second["input"] = {"text": {"$from": "steps.first.payload.text"}}
        return fx.spec_doc([first, second])

    def errors(self, findings):
        return [f for f in findings if f["level"] == "error"]

    def warnings(self, findings):
        return [f for f in findings if f["level"] == "warn"]


class CreateTests(SmithOpCase):
    def test_creates_a_complete_package_into_an_empty_directory(self):
        result = smith_op.create(self.spec_path, self.dest)
        self.assertEqual(result["op_id"], "openteams/op-test")
        self.assertEqual(result["steps"], ["first", "second"])
        for rel in ("op.yaml", "pixi.toml", "README.md", ".gitignore",
                    "src/op_runner.py", "src/op_spec.py", "src/op_track.py",
                    "tests/test_op.py", "examples/request.json"):
            self.assertTrue((self.dest / rel).exists(), rel)

    def test_op_yaml_is_the_spec_with_nothing_added(self):
        smith_op.create(self.spec_path, self.dest)
        written = yaml.safe_load((self.dest / "op.yaml").read_text())
        self.assertEqual(written, self.spec_doc())

    def test_the_package_carries_no_cog_manifest_and_declares_op_tasks(self):
        smith_op.create(self.spec_path, self.dest)
        pixi = (self.dest / "pixi.toml").read_text()
        self.assertNotIn("[tool.cog]", pixi)
        self.assertIn('op = "python src/op_runner.py"', pixi)
        self.assertIn("test =", pixi)

    def test_example_request_carries_defaults_and_placeholders(self):
        doc = self.spec_doc()
        doc["inputs"] = [{"name": "note", "default": "a default"},
                         {"name": "items", "schema": {"type": "array"}},
                         {"name": "maybe", "schema": {"type": ["string", "null"]}},
                         {"name": "free"}]
        self.spec_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        smith_op.create(self.spec_path, self.dest)
        example = json.loads((self.dest / "examples" / "request.json").read_text())
        self.assertEqual(example, {"note": "a default", "items": [],
                                   "maybe": "REPLACE_ME", "free": "REPLACE_ME"})

    def test_creates_into_an_existing_directory_without_collisions(self):
        self.dest.mkdir()
        (self.dest / "NOTES.md").write_text("the design record\n")
        smith_op.create(self.spec_path, self.dest)
        self.assertTrue((self.dest / "NOTES.md").exists())
        self.assertTrue((self.dest / "op.yaml").exists())

    def test_collisions_refuse_by_filename(self):
        (self.dest / "src").mkdir(parents=True)
        (self.dest / "op.yaml").write_text("mine\n")
        (self.dest / "src" / "op_spec.py").write_text("mine\n")
        with self.assertRaises(smith_op.OpCreateError) as caught:
            smith_op.create(self.spec_path, self.dest)
        message = str(caught.exception)
        self.assertIn("op.yaml", message)
        self.assertIn("src/op_spec.py", message)
        self.assertEqual((self.dest / "op.yaml").read_text(), "mine\n")

    def test_an_invalid_spec_is_refused_before_anything_is_written(self):
        doc = self.spec_doc()
        doc["steps"][0]["tool"] = {"name": "gh"}
        self.spec_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        with self.assertRaises(op_spec.OpSpecError):
            smith_op.create(self.spec_path, self.dest)
        self.assertFalse(self.dest.exists())

    def test_the_created_package_checks_clean(self):
        smith_op.create(self.spec_path, self.dest)
        findings = smith_op.check(self.dest)
        self.assertEqual(self.errors(findings), [])

    def test_the_created_package_passes_check_with_tests(self):
        smith_op.create(self.spec_path, self.dest)
        findings = smith_op.check(self.dest, run_tests=True)
        self.assertEqual(self.errors(findings), [])


class CheckTests(SmithOpCase):
    def setUp(self):
        super().setUp()
        smith_op.create(self.spec_path, self.dest)

    def details(self, findings, level="error"):
        return " ".join(f["detail"] for f in findings if f["level"] == level)

    def test_missing_op_yaml_is_an_error(self):
        (self.dest / "op.yaml").unlink()
        findings = smith_op.check(self.dest)
        self.assertIn("no op.yaml", self.details(findings))

    def test_spec_refusals_surface_as_check_errors(self):
        doc = self.spec_doc()
        doc["steps"][0]["human"] = {"prompt": "ok?"}
        (self.dest / "op.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
        findings = smith_op.check(self.dest)
        self.assertIn("phase 3", self.details(findings))

    def test_edited_machinery_is_an_error(self):
        path = self.dest / "src" / "op_spec.py"
        path.write_text(path.read_text() + "\n# local tweak\n")
        findings = smith_op.check(self.dest)
        self.assertIn("differs from the cog-smith master",
                      self.details(findings))

    def test_missing_machinery_is_an_error(self):
        (self.dest / "src" / "op_track.py").unlink()
        self.assertIn("op_track.py missing",
                      self.details(smith_op.check(self.dest)))

    def test_per_op_python_is_an_error(self):
        (self.dest / "src" / "helpers.py").write_text("x = 1\n")
        self.assertIn("no per-Op Python",
                      self.details(smith_op.check(self.dest)))

    def test_missing_tasks_are_errors(self):
        (self.dest / "pixi.toml").write_text(
            '[workspace]\nname = "op-sample"\nversion = "0.1.0"\n')
        details = self.details(smith_op.check(self.dest))
        self.assertIn("'op' task", details)
        self.assertIn("'test' task", details)

    def test_a_cog_manifest_in_an_op_package_is_an_error(self):
        pixi = self.dest / "pixi.toml"
        pixi.write_text(pixi.read_text()
                        + '\n[tool.cog]\nid = "openteams/cog-not-an-op"\n')
        self.assertIn("an Op, not a Cog", self.details(smith_op.check(self.dest)))

    def test_an_absent_cog_source_is_a_warning_not_an_error(self):
        findings = smith_op.check(self.dest)
        self.assertEqual(self.errors(findings), [])
        self.assertIn("not present here", self.details(findings, "warn"))

    def test_a_declared_usage_task_checks_clean(self):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": "ask", "kind": "command", "task": "ask",
                    "audience": "usage", "default": True}])
        findings = smith_op.check(self.dest)
        self.assertEqual(self.errors(findings), [])
        self.assertNotIn("cog-first", self.details(findings, "warn"))

    def test_an_undeclared_task_is_an_error(self):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": "chat", "kind": "command", "task": "chat",
                    "audience": "usage", "default": True}])
        self.assertIn("does not declare as an interface",
                      self.details(smith_op.check(self.dest)))

    def test_a_lifecycle_task_is_an_error(self):
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": "ask", "kind": "command", "task": "ask",
                    "audience": "lifecycle", "default": True}])
        self.assertIn("usage interfaces", self.details(smith_op.check(self.dest)))

    def test_a_conventional_lifecycle_task_is_an_error_without_a_declaration(self):
        doc = self.spec_doc()
        doc["steps"][0]["cog"]["task"] = "resolve"
        (self.dest / "op.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
        write_cog(self.root / "cog-first", "openteams/cog-first",
                  [{"name": "resolve", "kind": "command", "task": "resolve",
                    "default": True}])
        self.assertIn("lifecycle", self.details(smith_op.check(self.dest)))

    def test_an_identity_mismatch_is_an_error_and_a_version_drift_a_warning(self):
        write_cog(self.root / "cog-first", "openteams/cog-other",
                  [{"name": "ask", "kind": "command", "task": "ask",
                    "audience": "usage", "default": True}], version="0.2.0")
        findings = smith_op.check(self.dest)
        self.assertIn("identifies as", self.details(findings))
        self.assertIn("0.2.0", self.details(findings, "warn"))


class CliTests(SmithOpCase):
    def smith(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args],
                              capture_output=True, text=True)

    def test_op_new_then_op_check_through_the_cli(self):
        created = self.smith("op", "new", "--from-spec", str(self.spec_path),
                             "--dir", str(self.dest))
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("created openteams/op-test", created.stdout)
        checked = self.smith("op", "check", str(self.dest), "--tests")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("PASS", checked.stdout)

    def test_op_new_emits_an_envelope(self):
        result = self.smith("op", "new", "--from-spec", str(self.spec_path),
                            "--dir", str(self.dest), "--envelope")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(result.stdout)
        self.assertEqual(env["envelope"], 1)
        self.assertEqual(env["task"], "op new")
        self.assertTrue(env["ok"])
        self.assertIsNone(env["binding"])
        self.assertEqual(env["payload"]["op_id"], "openteams/op-test")
        self.assertEqual(env["payload"]["steps"], ["first", "second"])

    def test_a_refused_spec_exits_two_with_the_named_construct(self):
        doc = self.spec_doc()
        doc["state"] = {"store": "sqlite"}
        self.spec_path.write_text(yaml.safe_dump(doc, sort_keys=False))
        result = self.smith("op", "new", "--from-spec", str(self.spec_path),
                            "--dir", str(self.dest))
        self.assertEqual(result.returncode, 2)
        self.assertIn("phase 4", result.stderr)

    def test_op_run_execs_the_packages_runner(self):
        self.smith("op", "new", "--from-spec", str(self.spec_path),
                   "--dir", str(self.dest))
        request = self.dest / "examples" / "request.json"
        result = self.smith("op", "run", str(self.dest), "--request",
                            str(request), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "planned")


if __name__ == "__main__":
    unittest.main()
