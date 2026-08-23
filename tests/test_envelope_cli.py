"""Envelope-v1 emission from the smith CLI (builder-op note, seam option 1).

cog-smith is a deterministic tooling Cog: its envelopes carry binding: null
and raw: null; checker findings travel in `problems` with ok: true
(ok-with-problems — the run succeeded, a Gate decides about the findings);
expected failures (bad create request, existing destination) come back as
ok: false envelopes with error.code invalid-input.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_card   # noqa: E402
import smith_core   # noqa: E402

CLI = ROOT / "src" / "cogsmith_cli.py"

ENVELOPE_FIELDS = ("envelope", "cog", "task", "ok", "error", "payload",
                   "raw", "problems", "binding", "timing")


def run_cli(*argv):
    return subprocess.run([sys.executable, str(CLI), *argv],
                          capture_output=True, text=True, timeout=120)


def create_tmp(tmp, name="cog-envelope-toy"):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name))
    return dest


class TestCheckEnvelope(unittest.TestCase):
    def test_shape_ok_and_problem_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_tmp(tmp)
            r = run_cli("check", str(dest), "--envelope")
            env = json.loads(r.stdout)          # stdout is pure JSON
            for field in ENVELOPE_FIELDS:
                self.assertIn(field, env)
            self.assertEqual(env["envelope"], 1)
            self.assertEqual(env["task"], "check")
            self.assertTrue(env["ok"])          # the check RAN
            self.assertIsNone(env["error"])
            self.assertIsNone(env["binding"])   # deterministic tooling cog
            self.assertIsNone(env["raw"])
            self.assertEqual(env["cog"]["id"], "openteams/cog-smith")
            self.assertTrue(env["payload"]["pass"])
            self.assertEqual(env["payload"]["errors"], 0)
            for p in env["problems"]:
                self.assertIn(p["severity"], ("error", "warn"))
                self.assertIn("layer", p)
                self.assertIn("check", p)
                self.assertIn("detail", p)
            self.assertEqual(r.returncode, 0)

    def test_findings_surface_as_problems_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_tmp(tmp)
            (dest / "COG.md").unlink()          # break the core layer
            r = run_cli("check", str(dest), "--envelope")
            env = json.loads(r.stdout)
            self.assertTrue(env["ok"])          # ok-with-problems
            self.assertFalse(env["payload"]["pass"])
            self.assertTrue(any(p["severity"] == "error"
                                for p in env["problems"]))
            self.assertEqual(r.returncode, 1)


class TestNewEnvelope(unittest.TestCase):
    def test_scripted_create_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-envelope-create"
            r = run_cli("new", "--dir", str(dest), "--yes", "--envelope")
            env = json.loads(r.stdout)          # no stray human prints
            self.assertEqual(env["task"], "new")
            self.assertTrue(env["ok"])
            self.assertEqual(env["payload"]["cog_id"],
                             "openteams/cog-envelope-create")
            self.assertTrue(env["payload"]["machinery"])
            self.assertEqual(env["payload"]["check"]["errors"], 0)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(dest.exists())

    def test_existing_destination_is_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-already-there"
            dest.mkdir()
            r = run_cli("new", "--dir", str(dest), "--yes", "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "invalid-input")
            self.assertIn("refusing to overwrite", env["error"]["detail"])
            self.assertEqual(r.returncode, 2)

    def test_invalid_request_is_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Bad_Name"
            r = run_cli("new", "--dir", str(dest), "--yes", "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "invalid-input")
            self.assertTrue(env["problems"])
            self.assertEqual(r.returncode, 2)


class TestCardEnvelope(unittest.TestCase):
    def test_card_rides_as_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_tmp(tmp)
            r = run_cli("card", str(dest), "--envelope")
            env = json.loads(r.stdout)
            self.assertEqual(env["task"], "card")
            self.assertTrue(env["ok"])
            self.assertEqual(env["payload"], smith_card.card(dest))
            self.assertEqual(env["payload"]["id"], "openteams/cog-envelope-toy")
            self.assertEqual(r.returncode, 0)


class TestHumanOutputUnchanged(unittest.TestCase):
    def test_check_without_flag_still_reports_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_tmp(tmp)
            r = run_cli("check", str(dest))
            self.assertIn("PASS", r.stdout)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(r.stdout)
            self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
