"""The two profile manifest formats (cog-execution ADR D9).

pixi: `[tool.cog]` in pixi.toml — the default; `version` and `summary` are
stated once in [workspace]. yaml: the standalone cog.yaml. A package carries
exactly one; COG.md's `manifest:` pointer names it; smith's readers and the
created Cogs' machinery accept either and produce the same declarations.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_card      # noqa: E402
import smith_check     # noqa: E402
import smith_core      # noqa: E402
import smith_manifest  # noqa: E402
import smith_models    # noqa: E402
import toml_compat     # noqa: E402

CATALOG = ROOT / "examples" / "model-catalog.yaml"
CLI = ROOT / "src" / "cogsmith_cli.py"


def create(tmp, fmt, name="cog-toy", **overrides):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name, **overrides),
                      manifest_format=fmt)
    return dest


def errors(findings):
    return [f for f in findings if f["level"] == "error"]


def frontmatter(root):
    fm = smith_check.FRONTMATTER_RE.match((root / "COG.md").read_text())
    return yaml.safe_load(fm.group(1))


class TestCreateFormats(unittest.TestCase):
    def test_default_is_pixi(self):
        self.assertEqual(smith_core.DEFAULT_MANIFEST_FORMAT, "pixi")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-toy"
            r = smith_core.create(dest, smith_core.default_tokens("cog-toy"))
            self.assertEqual(r["manifest"], "pixi.toml")
            self.assertFalse((dest / "cog.yaml").exists())
            with open(dest / "pixi.toml", "rb") as f:
                doc = toml_compat.load(f)
            self.assertIn("cog", doc["tool"])
            self.assertEqual(frontmatter(dest)["manifest"], "pixi.toml")

    def test_yaml_format_still_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "yaml")
            self.assertTrue((dest / "cog.yaml").exists())
            with open(dest / "pixi.toml", "rb") as f:
                doc = toml_compat.load(f)
            self.assertNotIn("tool", doc)
            self.assertEqual(frontmatter(dest)["manifest"], "cog.yaml")
            self.assertEqual(errors(smith_check.check(dest)), [])

    def test_unknown_format_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(smith_core.CreateError):
                create(tmp, "json")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_partials_are_never_emitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("pixi", "yaml"):
                dest = create(tmp, fmt, name=f"cog-{fmt}")
                self.assertFalse((dest / "_partials").exists())
                self.assertFalse((dest / "manifest.toml").exists())

    def test_both_formats_declare_the_same_cog(self):
        """Same answers -> same declarations, whichever file carries them."""
        with tempfile.TemporaryDirectory() as tmp:
            a = smith_manifest.load(create(tmp, "pixi", name="cog-a",
                                           PORT="8111"))[0]
            b = smith_manifest.load(create(tmp, "yaml", name="cog-b",
                                           PORT="8111"))[0]
            for m in (a, b):
                m["id"] = "openteams/cog-x"          # only the name differs
                # a YAML folded scalar keeps a trailing newline; readers
                # normalize whitespace (smith_card does) — not a difference
                m["summary"] = " ".join(m["summary"].split())
            self.assertEqual(a, b)

    def test_cards_agree_across_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            ca = smith_card.card(create(tmp, "pixi", name="cog-a"))
            cb = smith_card.card(create(tmp, "yaml", name="cog-b"))
            self.assertEqual((ca.pop("manifest"), cb.pop("manifest")),
                             ("pixi.toml", "cog.yaml"))
            ca["id"] = cb["id"] = "openteams/cog-x"
            self.assertEqual(ca, cb)

    def test_created_yaml_cog_tests_pass(self):
        """The machinery reads cog.yaml too — the created suite proves it."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "yaml")
            r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                                "-s", "tests"], cwd=str(dest),
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class TestLoader(unittest.TestCase):
    def test_stated_once_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pixi.toml").write_text(
                '[workspace]\nname = "cog-x"\nversion = "1.2.3"\n'
                'description = "Does x."\n\n[tool.cog]\nid = "openteams/cog-x"\n')
            m, fmt, path = smith_manifest.load(root)
            self.assertEqual(fmt, "pixi")
            self.assertEqual(m["version"], "1.2.3")
            self.assertEqual(m["summary"], "Does x.")

    def test_explicit_fields_win_over_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pixi.toml").write_text(
                '[workspace]\nname = "cog-x"\nversion = "1.2.3"\n'
                'description = "ws"\n\n[tool.cog]\nid = "openteams/cog-x"\n'
                'summary = "own"\n')
            self.assertEqual(smith_manifest.load(root)[0]["summary"], "own")

    def test_pixi_without_tool_cog_is_not_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pixi.toml").write_text('[workspace]\nname = "x"\n')
            self.assertEqual(smith_manifest.locate(root), (None, None))
            with self.assertRaises(smith_manifest.ManifestError):
                smith_manifest.load(root)

    def test_both_manifests_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "pixi")
            (dest / "cog.yaml").write_text("id: openteams/cog-toy\n")
            with self.assertRaises(smith_manifest.ManifestError):
                smith_manifest.locate(dest)
            self.assertTrue(any(f["check"] == "manifest" and "both" in f["detail"]
                                for f in errors(smith_check.check(dest))))

    def test_fallback_toml_parser_refuses_tool_cog(self):
        text = '[workspace]\nname = "x"\n\n[tool.cog]\nid = "openteams/cog-x"\n'
        with self.assertRaises(ValueError):
            toml_compat._fallback_parse(text)
        # plain pixi.toml without a manifest still degrades as before
        doc = toml_compat._fallback_parse('[workspace]\nname = "x"\n[tasks]\n'
                                          'ask = "python a.py"\n')
        self.assertEqual(doc["tasks"]["ask"], "python a.py")


class TestCheckRules(unittest.TestCase):
    def test_pointer_must_name_the_manifest_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "pixi")
            text = (dest / "COG.md").read_text().replace(
                "manifest: pixi.toml", "manifest: cog.yaml")
            (dest / "COG.md").write_text(text)
            self.assertTrue(any(f["check"] == "cogmd" and "manifest" in f["detail"]
                                for f in errors(smith_check.check(dest))))

    def test_version_stated_twice_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "pixi")
            pixi = dest / "pixi.toml"
            pixi.write_text(pixi.read_text().replace(
                '[tool.cog]\n', '[tool.cog]\nversion = "9.9.9"\n', 1))
            found = errors(smith_check.check(dest))
            self.assertTrue(any("[tool.cog].version" in f["detail"]
                                for f in found), found)

    def test_missing_manifest_reports_both_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create(tmp, "yaml")
            (dest / "cog.yaml").unlink()
            found = errors(smith_check.check(dest))
            self.assertTrue(any("[tool.cog]" in f["detail"] and "cog.yaml"
                                in f["detail"] for f in found), found)


class TestDescriptors(unittest.TestCase):
    def test_both_formats_pass_check(self):
        for fmt in ("pixi", "yaml"):
            with tempfile.TemporaryDirectory() as tmp:
                r = smith_models.generate_from_config(CATALOG, tmp,
                                                      manifest_format=fmt)
                for name in r["created"]:
                    root = Path(tmp) / name
                    self.assertEqual(smith_manifest.load(root)[1], fmt)
                    self.assertEqual(errors(smith_check.check(root)), [],
                                     (fmt, name))
                    self.assertEqual(frontmatter(root)["manifest"],
                                     smith_core.MANIFEST_FILES[fmt])

    def test_pixi_descriptor_is_a_publishable_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            smith_models.generate_from_config(CATALOG, tmp)
            with open(Path(tmp) / "cog-qwen35b-collab" / "pixi.toml", "rb") as f:
                doc = toml_compat.load(f)
            self.assertEqual(doc["workspace"]["name"], "cog-qwen35b-collab")
            self.assertEqual(doc["tool"]["cog"]["kind"], "model")

    def test_toml_omits_unset_identity_fields(self):
        """TOML has no null: absent quantization/runtime/revision must read
        back as None from both formats, not as the string 'null'."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.yaml"
            cfg.write_text(yaml.safe_dump({"models": [{
                "name": "bare", "served_model_id": "m",
                "endpoint": "https://x/v1"}]}))
            for fmt in ("pixi", "yaml"):
                out = Path(tmp) / fmt
                smith_models.generate_from_config(cfg, out, manifest_format=fmt)
                m = smith_manifest.load(out / "cog-bare")[0]
                for key in ("quantization", "runtime", "revision"):
                    self.assertIsNone(m["model"].get(key), (fmt, key))
                self.assertEqual(m["model"]["name"], "m")

    def test_catalog_key_sets_format_and_flag_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.yaml"
            cfg.write_text(yaml.safe_dump({"manifest": "yaml", "models": [{
                "name": "k", "served_model_id": "m",
                "endpoint": "https://x/v1"}]}))
            smith_models.generate_from_config(cfg, Path(tmp) / "a")
            self.assertEqual(smith_manifest.load(Path(tmp) / "a" / "cog-k")[1],
                             "yaml")
            smith_models.generate_from_config(cfg, Path(tmp) / "b",
                                              manifest_format="pixi")
            self.assertEqual(smith_manifest.load(Path(tmp) / "b" / "cog-k")[1],
                             "pixi")
            cfg.write_text(yaml.safe_dump({"manifest": "xml", "models": [{
                "name": "k", "served_model_id": "m",
                "endpoint": "https://x/v1"}]}))
            with self.assertRaises(smith_models.ModelConfigError):
                smith_models.generate_from_config(cfg, Path(tmp) / "c")


class TestCLI(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args],
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, cwd=str(ROOT))

    def test_new_manifest_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-y"
            r = self._run("new", "--dir", str(dest), "--yes", "--manifest",
                          "yaml", "--envelope")
            env = json.loads(r.stdout)
            self.assertTrue(env["ok"], r.stdout + r.stderr)
            self.assertEqual(env["payload"]["manifest"], "cog.yaml")
            self.assertTrue((dest / "cog.yaml").exists())

    def test_request_manifest_key_and_flag_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"cog_request": 1,
                                       "dir": str(Path(tmp) / "cog-r"),
                                       "manifest": "yaml"}))
            r = self._run("new", "--from-request", str(req), "--envelope")
            self.assertEqual(json.loads(r.stdout)["payload"]["manifest"],
                             "cog.yaml")
            req.write_text(json.dumps({"cog_request": 1,
                                       "dir": str(Path(tmp) / "cog-s"),
                                       "manifest": "yaml"}))
            r = self._run("new", "--from-request", str(req), "--manifest",
                          "pixi", "--envelope")
            self.assertEqual(json.loads(r.stdout)["payload"]["manifest"],
                             "pixi.toml")

    def test_request_rejects_unknown_manifest_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"cog_request": 1,
                                       "dir": str(Path(tmp) / "cog-r"),
                                       "manifest": "toml"}))
            r = self._run("new", "--from-request", str(req))
            self.assertEqual(r.returncode, 2)
            self.assertIn("manifest", r.stderr)
            self.assertFalse((Path(tmp) / "cog-r").exists())

    def test_envelope_identity_comes_from_pixi_manifest(self):
        r = self._run("card", ".", "--envelope")
        env = json.loads(r.stdout)
        self.assertEqual(env["cog"], {"id": "openteams/cog-smith",
                                      "version": "0.2.0"})
        self.assertEqual(env["payload"]["manifest"], "pixi.toml")


if __name__ == "__main__":
    unittest.main()
