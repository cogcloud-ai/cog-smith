"""`smith migrate`: cog.yaml <-> [tool.cog] in pixi.toml, plus machinery
re-sync. Migration must yield the same declarations the loader reads from
a freshly created package of the target format, and must never touch
author-owned logic."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_check     # noqa: E402
import smith_core      # noqa: E402
import smith_manifest  # noqa: E402
import smith_migrate   # noqa: E402
import toml_compat     # noqa: E402

CLI = ROOT / "src" / "cogsmith_cli.py"


def create(tmp, fmt, name="cog-toy", **overrides):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name, **overrides),
                      manifest_format=fmt)
    return dest


def errors(findings):
    return [f for f in findings if f["level"] == "error"]


def norm(m):
    m = dict(m)
    m["summary"] = " ".join(str(m.get("summary", "")).split())
    return m


def migrate(root, **kw):
    p = smith_migrate.plan(root, **kw)
    smith_migrate.apply(p)
    return p


COMPLETE = {   # the shape of a "complete" Cog: nested model,
    "schema": "openteams/cog-manifest [0.1]",   # requires with credential, io
    "id": "openteams/cog-complete-toy",
    "version": "0.3.1",
    "summary": "Does a\ncomplete thing: with \"quotes\" and unicode — więcej.",
    "license": "BSD-3-Clause",
    "owner": "t@example.com",
    "kind": "complete",
    "context": {"input_schema": "context/in.json", "output_schema": "context/out.json"},
    "model": {"name": "whisper", "runtime": "mlx", "source": "org/whisper",
              "revision": None},
    "requires": [
        {"capability": "host/os-darwin"},
        {"capability": "model-registry/huggingface",
         "credential": {"api_key_env": "HF_TOKEN"}},
    ],
    "interfaces": [
        {"name": "cli", "kind": "command", "task": "run", "audience": "usage",
         "default": True},
        {"name": "health", "kind": "command", "task": "check",
         "audience": "lifecycle"},
    ],
    "io": {"accepts": ["req"], "produces": ["bundle"]},
    "memory": "none",
    "prohibits": ["fetch_remote_media"],
    "evaluation": {"tests": ["tests/test_x.py"]},
}


def write_complete(tmp):
    root = Path(tmp) / "cog-complete-toy"
    root.mkdir()
    (root / "cog.yaml").write_text(yaml.safe_dump(COMPLETE, sort_keys=False,
                                                  allow_unicode=True))
    (root / "pixi.toml").write_text(
        '[workspace]\nname = "cog-complete-toy"\nversion = "0.1.0"\n'
        'description = "old description"\nchannels = ["conda-forge"]\n'
        'platforms = ["osx-arm64"]\n\n[dependencies]\npython = ">=3.11"\n\n'
        '[tasks]\n# keep me\nrun = "python src/x.py"\ncheck = "python src/x.py --check"\n'
        'test = "python -m unittest"\n')
    (root / "COG.md").write_text(
        '---\ntype: cog [0.1]\nname: cog-complete-toy\ndescription: toy\n'
        'version: "0.3.1"\nmanifest: cog.yaml\n'
        'manifest_schema: openteams/cog-manifest [0.1]\n---\n# toy\n\nSee cog.yaml.\n')
    return root


class TestToPixi(unittest.TestCase):
    def test_created_yaml_cog_becomes_the_pixi_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            migrated = create(tmp, "yaml", name="cog-a")
            p = migrate(migrated)
            self.assertEqual((p["format"], p["to"]), ("yaml", "pixi"))
            self.assertFalse((migrated / "cog.yaml").exists())
            fresh = create(tmp, "pixi", name="cog-b")
            a = norm(smith_manifest.load(migrated)[0]); a["id"] = "x"
            b = norm(smith_manifest.load(fresh)[0]); b["id"] = "x"
            self.assertEqual(a, b)
            self.assertEqual(smith_manifest.load(migrated)[1], "pixi")
            fm = smith_check.FRONTMATTER_RE.match((migrated / "COG.md").read_text())
            self.assertEqual(yaml.safe_load(fm.group(1))["manifest"], "pixi.toml")
            self.assertEqual(errors(smith_check.check(migrated)), [])

    def test_migrated_cog_own_tests_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            # simulate an older created package whose test still reads cog.yaml
            tc = root / "tests" / "test_cog.py"
            tc.write_text(tc.read_text().replace(
                "        # either format: [tool.cog] in pixi.toml, or cog.yaml\n"
                "        self.manifest = cog_binding.load_manifest(ROOT)",
                "        " + smith_migrate.TEST_MANIFEST_LINE).replace(
                "import cog_binding  # noqa: E402\n", "").replace(
                "import unittest\nfrom pathlib import Path\n",
                "import unittest\nfrom pathlib import Path\n\nimport yaml\n"))
            self.assertIn('"cog.yaml"', tc.read_text())
            p = migrate(root)
            self.assertIn("tests/test_cog.py", p["writes"])
            self.assertNotIn('"cog.yaml"', tc.read_text())
            r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                                "-s", "tests"], cwd=str(root),
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])

    def test_complete_cog_round_trips_and_keeps_pixi_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_complete(tmp)
            p = migrate(root)
            m, fmt, _ = smith_manifest.load(root)
            self.assertEqual(fmt, "pixi")
            want = dict(COMPLETE)
            want["model"] = {k: v for k, v in COMPLETE["model"].items()
                             if v is not None}
            self.assertEqual(norm(m), norm(want))
            self.assertIn("model.revision", " ".join(p["dropped"]))
            text = (root / "pixi.toml").read_text()
            self.assertIn("# keep me", text)                # comments survive
            self.assertIn('run = "python src/x.py"', text)
            with open(root / "pixi.toml", "rb") as f:
                doc = toml_compat.load(f)
            self.assertEqual(doc["workspace"]["version"], "0.3.1")   # moved
            self.assertIn("complete thing", doc["workspace"]["description"])
            self.assertNotIn("\n", doc["workspace"]["description"])
            self.assertTrue(any("version" in n for n in p["notes"]))
            self.assertIn("COG.md", p["leftover"])       # prose still says it
            self.assertFalse((root / "cog.yaml").exists())

    def test_template_wording_rewritten_and_scan_is_quiet(self):
        """A package created by an older smith (its COG.md and pixi.toml
        comments name cog.yaml) ends up with no leftover mentions at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            cogmd = root / "COG.md"
            cogmd.write_text(cogmd.read_text().replace(
                "prohibitions in the manifest make that structural.)",
                "prohibitions in cog.yaml make that structural.)"))
            pixi = root / "pixi.toml"
            pixi.write_text(pixi.read_text().replace(
                "# reads cog.yaml-format manifests and *.fixture.yaml (eval)",
                "# reads cog.yaml (resolution) and *.fixture.yaml (eval)"))
            p = migrate(root)
            self.assertEqual(p["leftover"], [], p["leftover"])
            self.assertIn("prohibitions in the manifest", cogmd.read_text())
            # re-running is a no-op
            p2 = smith_migrate.plan(root)
            self.assertEqual((p2["writes"], p2["leftover"]), ({}, []))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_complete(tmp)
            migrate(root)
            before = (root / "pixi.toml").read_text()
            p = smith_migrate.plan(root)
            self.assertEqual((p["writes"], p["copies"], p["removes"]), ({}, [], []))
            self.assertTrue(any("already" in n for n in p["notes"]))
            self.assertEqual((root / "pixi.toml").read_text(), before)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            r = subprocess.run([sys.executable, str(CLI), "migrate", str(root),
                                "--dry-run"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dry run", r.stdout)
            self.assertIn("remove  cog.yaml", r.stdout)
            self.assertEqual({p: p.read_bytes() for p in root.rglob("*")
                              if p.is_file()}, snapshot)

    def test_machinery_is_resynced_but_task_logic_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            core = root / "src" / "cog_core.py"
            core.write_text(core.read_text() + "\n# drifted\n")
            tl = root / "src" / "task_logic.py"
            tl.write_text(tl.read_text() + "\n# mine\n")
            p = migrate(root)
            self.assertIn("src/cog_core.py", [rel for _, rel in p["copies"]])
            self.assertNotIn("# drifted", core.read_text())
            self.assertIn("# mine", tl.read_text())
            self.assertEqual(errors(smith_check.check(root)), [])

    def test_no_machinery_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            core = root / "src" / "cog_core.py"
            core.write_text(core.read_text() + "\n# drifted\n")
            p = migrate(root, machinery=False)
            self.assertEqual(p["copies"], [])
            self.assertIn("# drifted", core.read_text())

    def test_refuses_non_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cog-bare"
            root.mkdir()
            (root / "cog.yaml").write_text("id: openteams/cog-bare\nversion: '1'\n")
            with self.assertRaises(smith_migrate.MigrateError):
                smith_migrate.plan(root)

    def test_nothing_to_migrate_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(smith_migrate.MigrateError):
                smith_migrate.plan(tmp)


class TestToYaml(unittest.TestCase):
    def test_reverse_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "pixi")
            original = norm(smith_manifest.load(root)[0])
            p = migrate(root, to="yaml")
            self.assertEqual(smith_manifest.load(root)[1], "yaml")
            self.assertEqual(norm(smith_manifest.load(root)[0]), original)
            with open(root / "pixi.toml", "rb") as f:
                self.assertNotIn("tool", toml_compat.load(f))
            self.assertEqual(errors(smith_check.check(root)), [])
            migrate(root)                                    # and back
            self.assertEqual(smith_manifest.load(root)[1], "pixi")
            self.assertEqual(norm(smith_manifest.load(root)[0]), original)
            self.assertEqual(errors(smith_check.check(root)), [])


class TestTomlWriter(unittest.TestCase):
    def test_strings_are_escaped(self):
        text, dropped = smith_migrate.manifest_toml(
            {"id": 'a"b\\c\nd\ttab', "n": 3, "f": 1.5, "b": False,
             "l": ["x", 2], "t": {"k": "v"}, "none": None})
        import tomllib
        doc = tomllib.loads(text)["tool"]["cog"]
        self.assertEqual(doc["id"], 'a"b\\c\nd\ttab')
        self.assertEqual((doc["n"], doc["f"], doc["b"], doc["l"]),
                         (3, 1.5, False, ["x", 2]))
        self.assertEqual(doc["t"], {"k": "v"})
        self.assertEqual(dropped, ["tool.cog.none"])

    def test_null_in_list_is_refused(self):
        with self.assertRaises(smith_migrate.MigrateError):
            smith_migrate.manifest_toml({"l": ["a", None]})

    def test_strip_tool_cog_keeps_everything_else(self):
        text = ('[workspace]\nname = "x"\n\n[tasks]\na = "b"\n\n'
                '# banner\n# banner\n[tool.cog]\nid = "y"\n\n[tool.cog.io]\n'
                'accepts = []\n\n[[tool.cog.interfaces]]\nname = "n"\n')
        out = smith_migrate.strip_tool_cog(text)
        self.assertEqual(out, '[workspace]\nname = "x"\n\n[tasks]\na = "b"\n')
        text2 = text + '\n[feature.x.dependencies]\nfoo = "*"\n'
        self.assertIn("[feature.x.dependencies]", smith_migrate.strip_tool_cog(text2))


class TestCLI(unittest.TestCase):
    def test_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create(tmp, "yaml")
            r = subprocess.run([sys.executable, str(CLI), "migrate", str(root),
                                "--envelope"], capture_output=True, text=True)
            env = json.loads(r.stdout)
            self.assertTrue(env["ok"], r.stdout)
            self.assertEqual(env["payload"]["from"], "yaml")
            self.assertEqual(env["payload"]["removed"], ["cog.yaml"])
            self.assertTrue(env["payload"]["applied"])
            self.assertEqual(env["payload"]["check"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
