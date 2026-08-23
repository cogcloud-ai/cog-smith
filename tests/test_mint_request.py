"""Mint requests: the drafting-cog -> smith seam (builder-op note, item 3).

A mint request is one JSON document carrying the builder answers plus the
drafted context files. `new --from-request` validates it, mints atomically
with the overlays applied inside staging, and checks the result. Explicit
flags override request values; overlays never touch src/.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_core   # noqa: E402

CLI = ROOT / "src" / "cogsmith_cli.py"
EXAMPLE = ROOT / "examples" / "mint-request.json"


def run_cli(*argv):
    return subprocess.run([sys.executable, str(CLI), *argv],
                          capture_output=True, text=True, timeout=120)


def example_request(tmp, name="cog-req-toy", **edits):
    req = json.loads(EXAMPLE.read_text())
    req["dir"] = str(Path(tmp) / name)
    req["id"] = f"openteams/{name}"
    req.update(edits)
    path = Path(tmp) / "request.json"
    path.write_text(json.dumps(req))
    return path, Path(req["dir"])


class TestMintFromRequest(unittest.TestCase):
    def test_example_request_mints_and_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, dest = example_request(tmp)
            r = run_cli("new", "--from-request", str(reqfile), "--envelope")
            env = json.loads(r.stdout)
            self.assertTrue(env["ok"], env)
            self.assertEqual(env["payload"]["check"]["errors"], 0, env)
            self.assertTrue(env["payload"]["from_request"])
            self.assertIn("context/system.md", env["payload"]["overlays"])
            self.assertEqual(r.returncode, 0)
            # overlays landed verbatim
            self.assertIn("You write release briefs",
                          (dest / "context" / "system.md").read_text())
            schema = json.loads(
                (dest / "context" / "input-schema.json").read_text())
            self.assertEqual(schema["title"], "Release Brief input")
            # machinery untouched by overlays: task_logic is the starter's
            master = (ROOT / "templates" / "context-cog" / "src"
                      / "task_logic.py").read_bytes()
            self.assertEqual((dest / "src" / "task_logic.py").read_bytes(),
                             master)

    def test_flags_override_request_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, dest = example_request(tmp)
            r = run_cli("new", "--from-request", str(reqfile),
                        "--summary", "Flag summary wins.", "--envelope")
            env = json.loads(r.stdout)
            self.assertTrue(env["ok"], env)
            self.assertIn("Flag summary wins.",
                          (dest / "cog.yaml").read_text())

    def test_request_is_non_interactive_without_yes(self):
        # subprocess stdin is not a tty; --from-request alone must not
        # demand --yes
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, dest = example_request(tmp)
            r = run_cli("new", "--from-request", str(reqfile))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(dest.exists())

    def test_unknown_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, _ = example_request(tmp, surprise="?!")
            r = run_cli("new", "--from-request", str(reqfile), "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertIn("surprise", env["error"]["detail"])
            self.assertEqual(r.returncode, 2)

    def test_missing_version_marker_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, _ = example_request(tmp)
            req = json.loads(reqfile.read_text())
            del req["mint_request"]
            reqfile.write_text(json.dumps(req))
            r = run_cli("new", "--from-request", str(reqfile), "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "invalid-input")

    def test_missing_dir_everywhere_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, _ = example_request(tmp)
            req = json.loads(reqfile.read_text())
            del req["dir"]
            reqfile.write_text(json.dumps(req))
            r = run_cli("new", "--from-request", str(reqfile), "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertIn("destination required", env["error"]["detail"])

    def test_failed_overlay_leaves_no_debris(self):
        # A bad request must not half-create the destination (atomic mint).
        with tempfile.TemporaryDirectory() as tmp:
            reqfile, dest = example_request(tmp)
            req = json.loads(reqfile.read_text())
            req["context"]["input_schema"] = "not-an-object"
            reqfile.write_text(json.dumps(req))
            r = run_cli("new", "--from-request", str(reqfile), "--envelope")
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertFalse(dest.exists())
            self.assertFalse(list(Path(tmp).glob(".mint-*")), "staging debris")


class TestOverlayMechanics(unittest.TestCase):
    def test_overlay_path_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-esc-toy"
            with self.assertRaises(smith_core.MintError):
                smith_core.mint(dest, smith_core.default_tokens("cog-esc-toy"),
                                overlays={"../outside.txt": "nope"})
            self.assertFalse(dest.exists())
            self.assertFalse((Path(tmp) / "outside.txt").exists())

    def test_overlay_content_is_not_token_checked(self):
        # Drafted content may legitimately contain {{BRACES}}.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-brace-toy"
            smith_core.mint(dest, smith_core.default_tokens("cog-brace-toy"),
                            overlays={"context/system.md":
                                      "Render {{PLACEHOLDER}} literally.\n"})
            self.assertIn("{{PLACEHOLDER}}",
                          (dest / "context" / "system.md").read_text())


if __name__ == "__main__":
    unittest.main()
