"""cog-smith test suite: minting, checking, carding — model-free."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_card   # noqa: E402
import smith_check  # noqa: E402
import smith_core   # noqa: E402


def mint_tmp(tmp, name="cog-toy", **overrides):
    dest = Path(tmp) / name
    tokens = smith_core.default_tokens(name, **overrides)
    smith_core.mint(dest, tokens)
    return dest


class TestMint(unittest.TestCase):
    def test_mint_produces_complete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            for rel in ("cog.yaml", "COG.md", "pixi.toml", ".gitignore",
                        "context/system.md", "context/input-schema.json",
                        "context/output-schema.json",
                        "context/output-example.json",
                        "examples/sample-bundle.json",
                        "evals/smoke.fixture.yaml", "tests/test_cog.py",
                        "src/cog_core.py", "src/task_logic.py",
                        "src/cog_binding.py", "src/cog_resolve.py"):
                self.assertTrue((dest / rel).exists(), rel)
            m = yaml.safe_load((dest / "cog.yaml").read_text())
            self.assertEqual(m["id"], "openteams/cog-toy")
            self.assertEqual(m["schema"], "openteams/cog-manifest [0.1]")
            # in-manifest input schema — the post-freeze upgrade, no overlays
            self.assertEqual(m["context"]["input_schema"],
                             "context/input-schema.json")

    def test_no_unrendered_tokens_anywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            for p in dest.rglob("*"):
                if p.is_file() and p.suffix in (".yaml", ".md", ".json", ".toml"):
                    self.assertNotIn("{{", p.read_text(), p.name)

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            mint_tmp(tmp)
            with self.assertRaises(smith_core.MintError):
                mint_tmp(tmp)

    def test_overrides_flow_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp, name="cog-ap-triage",
                            SUMMARY="Triages AP exceptions.", PORT="8123",
                            PROHIBITS_YAML="  - approve_payment")
            m = yaml.safe_load((dest / "cog.yaml").read_text())
            self.assertIn("Triages AP exceptions.", m["summary"])
            self.assertIn(":8123/", m["interfaces"][0]["endpoint"])
            self.assertEqual(m["prohibits"], ["approve_payment"])


class TestCheck(unittest.TestCase):
    def test_fresh_mint_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = smith_check.check(mint_tmp(tmp))
            errors = [f for f in findings if f["level"] == "error"]
            self.assertEqual(errors, [], findings)

    def test_machinery_drift_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            core = dest / "src" / "cog_core.py"
            core.write_text(core.read_text() + "\n# sneaky edit\n")
            findings = smith_check.check(dest)
            self.assertTrue(any(f["check"] == "machinery" and
                                "cog_core.py" in f["detail"]
                                for f in findings if f["level"] == "error"))

    def test_task_logic_edits_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            tl = dest / "src" / "task_logic.py"
            tl.write_text(tl.read_text() + "\n# my cog's logic\n")
            errors = [f for f in smith_check.check(dest) if f["level"] == "error"]
            self.assertEqual(errors, [])

    def test_broken_example_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            (dest / "examples" / "sample-bundle.json").write_text(
                json.dumps({"focus": "x"}))     # violates input schema
            findings = smith_check.check(dest)
            self.assertTrue(any(f["check"] == "schema" and "sample-bundle"
                                in f["detail"] for f in findings
                                if f["level"] == "error"))

    def test_missing_manifest_field_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            m = yaml.safe_load((dest / "cog.yaml").read_text())
            del m["owner"]
            (dest / "cog.yaml").write_text(yaml.safe_dump(m))
            findings = smith_check.check(dest)
            self.assertTrue(any("owner" in f["detail"] for f in findings
                                if f["level"] == "error"))

    def test_minted_cog_tests_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = mint_tmp(tmp)
            r = subprocess.run([sys.executable, "-m", "unittest", "discover",
                                "-s", "tests"], cwd=str(dest),
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class TestCard(unittest.TestCase):
    def test_card_derives_from_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = smith_card.card(mint_tmp(tmp))
        self.assertEqual(c["id"], "openteams/cog-toy")
        self.assertEqual(c["ops"]["usage"], ["ask"])
        self.assertIn("resolve", c["ops"]["lifecycle"])
        self.assertTrue(c["input_contract"])
        self.assertEqual(c["envelope"], 1)
        self.assertEqual(c["requires"][0]["capability"],
                         "model-endpoint/openai-compatible")
        text = smith_card.render_text(c)
        self.assertIn("openteams/cog-toy", text)

    def test_card_on_cogsmith_itself(self):
        c = smith_card.card(ROOT)
        self.assertEqual(c["id"], "openteams/cog-smith")
        self.assertEqual(set(c["ops"]["usage"]), {"new", "card"})


class TestSelf(unittest.TestCase):
    def test_own_manifest_is_wellformed(self):
        m = yaml.safe_load((ROOT / "cog.yaml").read_text())
        for f in smith_check.REQUIRED_FIELDS:
            self.assertIn(f, m)
        self.assertEqual(m["schema"], smith_check.SCHEMA_STRING)
        defaults = [i for i in m["interfaces"] if i.get("default")]
        self.assertEqual(len(defaults), 1)


if __name__ == "__main__":
    unittest.main()
