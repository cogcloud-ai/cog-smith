"""Regression suite for the 2026-08-22 review findings (F1–F8). Every test
here reproduces a failure the review found; none may regress."""
import json
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_card      # noqa: E402
import smith_check     # noqa: E402
import smith_core      # noqa: E402
import smith_manifest  # noqa: E402
import smith_models    # noqa: E402


def create_tmp(tmp, name="cog-toy", **overrides):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name, **overrides))
    return dest


class TestF1CheckerLayers(unittest.TestCase):
    def test_bad_name_now_fails_check(self):
        """Review repro: 'Cog--Bad_Name' created and PASSed. Now the create is
        refused outright, and a hand-made bad package fails core."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(smith_core.CreateError):
                create_tmp(tmp, name="Cog--Bad_Name")
            # hand-build one to prove check catches it independently
            dest = create_tmp(tmp, name="cog-okay")
            bad = Path(tmp) / "Cog--Bad_Name"
            dest.rename(bad)
            text = (bad / "COG.md").read_text().replace("cog-okay",
                                                        "Cog--Bad_Name")
            (bad / "COG.md").write_text(text)
            findings = smith_check.check(bad)
            self.assertTrue(any(f["check"] == "name-grammar"
                                for f in findings if f["level"] == "error"))

    def test_findings_carry_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = smith_check.check(create_tmp(tmp))
            self.assertTrue(all(f.get("layer") in ("core", "profile",
                                                   "runtime")
                                for f in findings))

    def test_missing_core_frontmatter_is_core_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_tmp(tmp)
            text = (dest / "COG.md").read_text().replace(
                "manifest_schema: openteams/cog-manifest [0.1]\n", "")
            (dest / "COG.md").write_text(text)
            findings = smith_check.check(dest)
            self.assertTrue(any(f["layer"] == "core" and
                                "manifest_schema" in f["detail"]
                                for f in findings if f["level"] == "error"))

    def test_smith_validates_itself(self):
        """Release criterion 6: smith check PASSes on cog-smith."""
        findings = smith_check.check(ROOT)
        errors = [f for f in findings if f["level"] == "error"]
        self.assertEqual(errors, [], findings)


class TestF2CreateSafety(unittest.TestCase):
    PUNCT = 'Reconciles "monthly: invoices" — & więcej; 100% #done'

    def test_punctuation_summary_survives_every_format(self):
        """Review repro: colon+quotes corrupted YAML frontmatter, TOML, and
        left a broken directory behind."""
        for fmt in ("pixi", "yaml"):
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "cog-toy"
                smith_core.create(dest, smith_core.default_tokens(
                    "cog-toy", SUMMARY=self.PUNCT), manifest_format=fmt)
                m = smith_manifest.load(dest)[0]
                self.assertIn("monthly: invoices", m["summary"])
                self.assertEqual(m["version"], "0.1.0")
                fm = smith_check.FRONTMATTER_RE.match(
                    (dest / "COG.md").read_text())
                meta = yaml.safe_load(fm.group(1))
                self.assertIn("monthly: invoices", meta["description"])
                import toml_compat
                with open(dest / "pixi.toml", "rb") as f:
                    data = toml_compat.load(f)
                self.assertIn("monthly: invoices",
                              data["workspace"]["description"])
                errors = [f for f in smith_check.check(dest)
                          if f["level"] == "error"]
                self.assertEqual(errors, [], (fmt, errors))

    def test_invalid_request_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({"name": "Cog--Bad_Name"},
                        {"name": "cog-x", "PORT": "99"},
                        {"name": "cog-x", "PRODUCES": "Not A Token"},
                        {"name": "cog-x", "SUMMARY": ""},
                        {"name": "cog-x",
                         "PROHIBITS_YAML": "  - rm -rf /"}):
                name = bad.pop("name")
                with self.assertRaises(smith_core.CreateError):
                    create_tmp(tmp, name=name, **bad)
                self.assertEqual(list(Path(tmp).iterdir()), [],
                                 f"stranded files after {name}")

    def test_failed_create_leaves_no_staging_debris(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                create_tmp(tmp, name="cog-x", PORT="not-a-port")
            except smith_core.CreateError:
                pass
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_non_tty_without_yes_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(ROOT / "src" / "cogsmith_cli.py"),
                 "new", "--dir", str(Path(tmp) / "cog-x")],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 2)
            self.assertIn("--yes", r.stderr)
            self.assertFalse((Path(tmp) / "cog-x").exists())


class _Mock(BaseHTTPRequestHandler):
    body = "not json at all {{{"

    def do_GET(self):
        payload = json.dumps({"data": [{"id": "mock"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length).decode())
        payload = json.dumps({
            "model": req.get("model"),
            "choices": [{"message": {"role": "assistant",
                                     "content": type(self).body}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class TestF3F4EnvelopeLive(unittest.TestCase):
    """Live invoke through a real socket: malformed content -> documented
    error + 502 mapping (F3); salvaged payload emitted clean (F4)."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Mock)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cog = create_tmp(cls.tmp.name)
        # bind via a created fixed-endpoint descriptor -> full resolve path
        cfg = Path(cls.tmp.name) / "cat.yaml"
        cfg.write_text(yaml.safe_dump({"models": [{
            "name": "mock-model",
            "served_model_id": "qwen2.5-3b-instruct-q4_k_m",
            "locality": "local",
            "endpoint": f"http://127.0.0.1:{cls.port}/v1",
        }]}))
        smith_models.generate_from_config(cfg, cls.tmp.name)
        r = subprocess.run(
            [sys.executable, "src/cog_resolve.py", "--satisfier",
             str(Path(cls.tmp.name) / "cog-mock-model"),
             "--allow-undeclared"],
            cwd=str(cls.cog), capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def _invoke(self):
        r = subprocess.run(
            [sys.executable, "src/cog_cli.py", "--bundle",
             "examples/sample-bundle.json"],
            cwd=str(self.cog), capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_f3_unparseable_content_gets_documented_error(self):
        _Mock.body = "not json at all {{{"
        env = self._invoke()
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "model-response-malformed")
        self.assertEqual(env["raw"], "not json at all {{{")
        sys.path.insert(0, str(self.cog / "src"))
        try:
            import importlib
            import cog_api
            importlib.reload(cog_api)
            self.assertEqual(
                cog_api.ERROR_STATUS["model-response-malformed"], 502)
        finally:
            sys.path.pop(0)

    def test_f4_salvaged_payload_validates_as_emitted(self):
        inner = {"abstained": True, "abstain_reason": "test", "entries": []}
        _Mock.body = json.dumps({"values": inner})
        env = self._invoke()
        self.assertTrue(env["ok"])
        self.assertNotIn("_unwrapped_from", env["payload"])
        self.assertEqual(env["binding"]["unwrapped"], "values")
        try:
            import jsonschema
        except ImportError:
            self.skipTest("no jsonschema")
        schema = json.loads(
            (self.cog / "context" / "output-schema.json").read_text())
        jsonschema.validate(env["payload"], schema)   # exactly as emitted


class TestF7Card(unittest.TestCase):
    def test_created_cog_ops_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = smith_card.card(create_tmp(tmp))
        self.assertEqual(c["ops"]["usage"], ["ask"])
        self.assertIn("serve", c["ops"]["lifecycle"])
        self.assertEqual(c["card"], 1)
        web = next(e for e in c["entry_points"] if e["name"] == "web-api")
        self.assertEqual(web["task"], "serve")
        self.assertEqual(c["input_contract"], "context/input-schema.json")
        self.assertEqual(c["output_contract"], "context/output-schema.json")


class TestF8Catalog(unittest.TestCase):
    def _cfg(self, models, tmp):
        p = Path(tmp) / "cfg.yaml"
        p.write_text(yaml.safe_dump({"models": models}))
        return p

    def test_invalid_late_entry_generates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg([
                {"name": "good", "served_model_id": "m",
                 "endpoint": "https://x/v1"},
                {"name": "bad", "served_model_id": "m",
                 "api_key_env": "sk-actual-secret-value"},
            ], tmp)
            out = Path(tmp) / "out"
            with self.assertRaises(smith_models.ModelConfigError):
                smith_models.generate_from_config(cfg, out)
            self.assertFalse((out / "cog-good").exists(),
                             "preflight must reject before ANY create")

    def test_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg([
                {"name": "same", "served_model_id": "a",
                 "endpoint": "https://x/v1"},
                {"name": "same", "served_model_id": "b",
                 "endpoint": "https://y/v1"},
            ], tmp)
            with self.assertRaises(smith_models.ModelConfigError):
                smith_models.generate_from_config(cfg, Path(tmp) / "out")

    def test_cog_name_without_name_works(self):
        """Review repro: documented-valid cog_name-only entry raised."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg([{"cog_name": "cog-via-cogname",
                              "served_model_id": "m",
                              "endpoint": "https://x/v1"}], tmp)
            r = smith_models.generate_from_config(cfg, Path(tmp) / "out")
            self.assertEqual(r["created"], ["cog-via-cogname"])

    def test_descriptor_marker_is_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg([{"name": "x", "served_model_id": "m",
                              "endpoint": "https://x/v1"}], tmp)
            smith_models.generate_from_config(cfg, Path(tmp) / "out")
            m = smith_manifest.load(Path(tmp) / "out" / "cog-x")[0]
            self.assertTrue(m["model"]["descriptor"])


if __name__ == "__main__":
    unittest.main()
