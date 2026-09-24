"""Context-cog machinery 0.4.0: the caller deadline is a binding fact.

A live sweep: a 161,000-character dependency
request outran a caller deadline hard-coded at 180 s in `cog_core.invoke`,
and there was nowhere to say otherwise. The deadline now lives in the binding
record (`request_timeout_s`, default 180, bounds 10–1800), written by
`use --timeout` and `resolve --timeout`, read by `invoke`, and overridable for
one call by `cog_cli --timeout`.
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
sys.path.insert(0, str(ROOT / "templates" / "context-cog" / "src"))

import cog_binding    # noqa: E402  (the context-cog master, not a cog-smith module)
import smith_core     # noqa: E402
import smith_models   # noqa: E402


def create_tmp(tmp, name="cog-toy", **overrides):
    dest = Path(tmp) / name
    smith_core.create(dest, smith_core.default_tokens(name, **overrides))
    return dest


def record_of(cog):
    return json.loads((Path(cog) / "model.json").read_text())


def run_use(cog, *args):
    return subprocess.run([sys.executable, "src/cog_use.py", *args],
                          cwd=str(cog), capture_output=True, text=True)


class TestTheBoundsRule(unittest.TestCase):
    """One rule, shared by the record validator, `use`, `resolve` and the CLI."""

    def test_the_default_and_the_bounds_are_named_constants(self):
        self.assertEqual(cog_binding.REQUEST_TIMEOUT_DEFAULT, 180)
        self.assertEqual(cog_binding.REQUEST_TIMEOUT_MIN, 10)
        self.assertEqual(cog_binding.REQUEST_TIMEOUT_MAX, 1800)

    def test_the_bounds_are_inclusive(self):
        self.assertEqual(cog_binding.timeout_problems(10), [])
        self.assertEqual(cog_binding.timeout_problems(1800), [])

    def test_out_of_bounds_is_a_named_problem(self):
        for value in (9, 1801, 0, -1):
            problems = cog_binding.timeout_problems(value)
            self.assertTrue(problems, value)
            self.assertIn("request_timeout_s", problems[0])

    def test_a_non_integer_deadline_is_refused(self):
        for value in ("180", 180.5, None, True):
            self.assertTrue(cog_binding.timeout_problems(value), value)

    def test_a_record_without_the_field_inherits_the_default(self):
        self.assertEqual(cog_binding.request_timeout({}),
                         cog_binding.REQUEST_TIMEOUT_DEFAULT)

    def test_the_defaults_record_states_the_deadline(self):
        self.assertEqual(cog_binding.DEFAULTS["request_timeout_s"],
                         cog_binding.REQUEST_TIMEOUT_DEFAULT)

    def test_the_record_validator_refuses_an_impossible_deadline(self):
        record = dict(cog_binding.DEFAULTS, request_timeout_s=5)
        problems = cog_binding.validate_record(record)
        self.assertTrue(any("request_timeout_s" in p for p in problems), problems)


class TestUseWritesTheDeadline(unittest.TestCase):
    def test_use_without_the_flag_writes_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            r = run_use(cog, "local")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(record_of(cog)["request_timeout_s"],
                             cog_binding.REQUEST_TIMEOUT_DEFAULT)

    def test_use_timeout_writes_what_it_was_told(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            r = run_use(cog, "local", "--timeout", "900")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(record_of(cog)["request_timeout_s"], 900)
            self.assertIn("900s", r.stdout)

    def test_an_impossible_deadline_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            r = run_use(cog, "local", "--timeout", "5")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("timeout check", r.stderr + r.stdout)
            self.assertFalse((Path(cog) / "model.json").exists())

    def test_show_reports_the_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local", "--timeout", "600")
            r = run_use(cog, "--show")
            self.assertIn("timeout  : 600s", r.stdout)


class TestResolveWritesTheDeadline(unittest.TestCase):
    """`resolve` is the declared-satisfier path; it states the deadline too,
    so a binding record has one however it was produced."""

    def _resolve(self, tmp, *extra):
        cog = create_tmp(tmp)
        cfg = Path(tmp) / "cat.yaml"
        cfg.write_text(yaml.safe_dump({"models": [{
            "name": "mock-model",
            "served_model_id": "qwen2.5-3b-instruct-q4_k_m",
            "locality": "local",
            "endpoint": "http://127.0.0.1:8099/v1",
        }]}))
        smith_models.generate_from_config(cfg, tmp)
        r = subprocess.run(
            [sys.executable, "src/cog_resolve.py", "--satisfier",
             str(Path(tmp) / "cog-mock-model"), "--allow-undeclared", *extra],
            cwd=str(cog), capture_output=True, text=True)
        return cog, r

    def test_resolve_writes_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog, r = self._resolve(tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(record_of(cog)["request_timeout_s"],
                             cog_binding.REQUEST_TIMEOUT_DEFAULT)

    def test_resolve_timeout_writes_what_it_was_told(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog, r = self._resolve(tmp, "--timeout", "1200")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(record_of(cog)["request_timeout_s"], 1200)
            self.assertIn("caller deadline 1200s", r.stdout)

    def test_resolve_refuses_an_impossible_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog, r = self._resolve(tmp, "--timeout", "3600")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertFalse((Path(cog) / "model.json").exists())


class TestInvokeReadsTheBinding(unittest.TestCase):
    """The created Cog's own suite asserts the wiring in-process; here the
    point is that a WRITTEN record reaches invoke, through a real process."""

    PROBE = (
        "import json, sys\n"
        "sys.path.insert(0, 'src')\n"
        "import cog_core\n"
        "print(json.dumps({'binding': cog_core.REQUEST_TIMEOUT_S}))\n"
    )

    def _deadline_seen_by_core(self, cog):
        r = subprocess.run([sys.executable, "-c", self.PROBE], cwd=str(cog),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["binding"]

    def test_core_uses_the_deadline_the_record_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local", "--timeout", "450")
            self.assertEqual(self._deadline_seen_by_core(cog), 450)

    def test_a_record_written_before_the_field_existed_still_binds(self):
        """Backwards compatibility: `request_timeout_s` is optional in the
        record schema, so an installation from before 0.4.0 keeps working and
        inherits the default rather than failing closed."""
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local")
            path = Path(cog) / "model.json"
            old = record_of(cog)
            del old["request_timeout_s"]
            path.write_text(json.dumps(old))
            self.assertEqual(self._deadline_seen_by_core(cog),
                             cog_binding.REQUEST_TIMEOUT_DEFAULT)

    # The written record must reach the SOCKET, not merely a module constant
    # (an internal review noted that printing `REQUEST_TIMEOUT_S` would pass even if
    # `invoke` ignored it). This probe runs a full invocation in the created
    # Cog's own process with the model replaced, and reports the deadline the
    # HTTP call was actually given.
    INVOKE_PROBE = (
        "import json, sys, io\n"
        "from unittest import mock\n"
        "sys.path.insert(0, 'src')\n"
        "import cog_core\n"
        "seen = []\n"
        "example = open('context/output-example.json').read()\n"
        "class R(io.BytesIO):\n"
        "    status = 200\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *e): return False\n"
        "def _urlopen(request, timeout=None):\n"
        "    seen.append(timeout)\n"
        "    return R(json.dumps({'model': cog_core.MODEL, 'choices': ["
        "{'message': {'content': example}}]}).encode())\n"
        "with mock.patch.object(cog_core, 'health', lambda *a, **k: (True, 'canned')), \\\n"
        "     mock.patch.object(cog_core.urllib.request, 'urlopen', _urlopen):\n"
        "    env = cog_core.invoke(json.load(open('examples/sample-bundle.json')))\n"
        "    override = cog_core.invoke("
        "json.load(open('examples/sample-bundle.json')), timeout=1200)\n"
        "print(json.dumps({'binding': cog_core.REQUEST_TIMEOUT_S, 'seen': seen,\n"
        "                  'ok': bool(env.get('ok')),\n"
        "                  'override_ok': bool(override.get('ok'))}))\n"
    )

    def _invocation(self, cog):
        r = subprocess.run([sys.executable, "-c", self.INVOKE_PROBE],
                           cwd=str(cog), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_a_written_record_reaches_the_invocations_http_deadline(self):
        """From `use --timeout` on disk to the deadline urlopen is handed —
        the whole claim in one test, through a real process."""
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local", "--timeout", "450")
            seen = self._invocation(cog)
            self.assertTrue(seen["ok"], seen)
            self.assertEqual(seen["binding"], 450)
            self.assertEqual(seen["seen"][0], 450)     # the record's deadline
            self.assertEqual(seen["seen"][-1], 1200)   # the one-call override
            self.assertTrue(seen["override_ok"], seen)

    def test_the_cli_refuses_an_impossible_one_call_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local")
            r = subprocess.run(
                [sys.executable, "src/cog_cli.py", "--bundle",
                 "examples/sample-bundle.json", "--timeout", "2"],
                cwd=str(cog), capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("request_timeout_s", r.stderr)


class TestDeepHealthHonorsTheOverride(unittest.TestCase):
    """Context-cog machinery 0.4.1: `--check --deep
    --timeout 600` was parsed, bounds-checked and then dropped — `health`
    was called with no deadline at all and `cog_core` used 30 s, so a
    completion probe taking 40 s reported DOWN despite the explicit
    override. The SHALLOW liveness probe keeps its own short deadline."""

    PROBE = (
        "import json, sys, io\n"
        "from unittest import mock\n"
        "sys.path.insert(0, 'src')\n"
        "import cog_core\n"
        "seen = []\n"
        "class R(io.BytesIO):\n"
        "    status = 200\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *e): return False\n"
        "def _urlopen(request, timeout=None):\n"
        "    seen.append(timeout)\n"
        "    return R(json.dumps({'model': cog_core.MODEL, 'choices': ["
        "{'message': {'content': '{}'}}]}).encode())\n"
        "with mock.patch.object(cog_core.urllib.request, 'urlopen', _urlopen):\n"
        "    cog_core.health(deep=True)\n"
        "    cog_core.health(deep=True, deep_timeout=600)\n"
        "    cog_core.health(deep=False)\n"
        "print(json.dumps(seen))\n"
    )

    def test_the_deep_probe_uses_the_override_and_liveness_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local")
            r = subprocess.run([sys.executable, "-c", self.PROBE], cwd=str(cog),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            seen = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(seen[0], 30)     # deep, no override: its own floor
            self.assertEqual(seen[1], 600)    # deep, explicit override
            self.assertEqual(seen[2], 3)      # shallow liveness, unchanged

    def test_the_cli_passes_check_deep_timeout_through(self):
        """The CLI is where the value was dropped, so the wiring is asserted
        at the CLI: `--check --deep --timeout N` reaches `deep_timeout`."""
        probe = (
            "import sys\n"
            "from unittest import mock\n"
            "sys.path.insert(0, 'src')\n"
            "import cog_core, cog_cli\n"
            "seen = {}\n"
            "def _health(timeout=3, deep=False, deep_timeout=None):\n"
            "    seen.update(timeout=timeout, deep=deep, deep_timeout=deep_timeout)\n"
            "    return True, 'canned'\n"
            "with mock.patch.object(cog_core, 'health', _health):\n"
            "    sys.argv = ['ask', '--check', '--deep', '--timeout', '600']\n"
            "    cog_cli.main()\n"
            "print(seen)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cog = create_tmp(tmp)
            run_use(cog, "local")
            r = subprocess.run([sys.executable, "-c", probe], cwd=str(cog),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("'deep_timeout': 600", r.stdout)
            self.assertIn("'deep': True", r.stdout)


if __name__ == "__main__":
    unittest.main()
