"""Code Cogs (`kind: code`): creation, the checker rules, and the seam.

Contract: planning/current/phase3-contract.md §1 and the grant/journal parts
of §2 and §4. The created Cog is exercised as a PROCESS (`python
src/cog_cli.py ...`), which is how an Op reaches it, so nothing here depends
on importing a created package's machinery into this interpreter.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import smith_check     # noqa: E402
import smith_core      # noqa: E402
import smith_manifest  # noqa: E402

GRANT_SCHEMA = "openteams/op-grant [0.1]"


def create_code_cog(tmp, name="cog-widget", manifest_format="pixi", **over):
    dest = Path(tmp) / name
    tokens = smith_core.default_tokens(name, **over)
    smith_core.create(dest, tokens, template="code-cog",
                      manifest_format=manifest_format)
    return dest


def run_cog(dest, *args):
    """Invoke the created Cog the way an Op does: as a process. Returns
    (returncode, envelope-or-None, stdout)."""
    completed = subprocess.run(
        [sys.executable, str(Path(dest) / "src" / "cog_cli.py"), *args],
        cwd=str(dest), capture_output=True, text=True)
    envelope = None
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        pass
    return completed.returncode, envelope, completed.stdout + completed.stderr


def declare_reaches(dest, resource="github", actions=("read", "write")):
    """Declare `reaches` in the created Cog's pixi.toml manifest."""
    path = Path(dest) / "pixi.toml"
    entry = json.dumps(list(actions))
    path.write_text(path.read_text().replace(
        "reaches = []",
        f'reaches = [{{ resource = "{resource}", actions = {entry} }}]'))


def grant(cog_id, run_id="run-1", operations=None, expires_in_minutes=60,
          recipient_id=None, step="write-step"):
    expires = datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)
    return {
        "schema": GRANT_SCHEMA,
        "grant_id": f"{run_id}/{step}/0",
        "run_id": run_id,
        "recipient": {"step": step,
                      "cog": {"id": recipient_id or cog_id,
                              "version": "0.1.0"}},
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "issued_by": {"kind": "admission"},
        "operations": operations if operations is not None else [
            {"resource": "github", "action": "read",
             "repositories": ["openteams-ai/apollo-desktop"]}],
        "valid": {"expires_at": expires.isoformat(), "run_id": run_id},
    }


class TestCreate(unittest.TestCase):
    def test_created_package_has_the_code_cog_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            for rel in ("COG.md", "pixi.toml", "context/input-schema.json",
                        "context/output-schema.json",
                        "examples/sample-bundle.json", "tests/test_cog.py",
                        "src/cog_core.py", "src/cog_cli.py",
                        "src/task_logic.py"):
                self.assertTrue((dest / rel).exists(), rel)
            # everything about a model is gone
            for rel in ("src/cog_resolve.py", "src/cog_binding.py",
                        "src/cog_use.py", "src/cog_eval.py", "src/cog_api.py",
                        "context/system.md", "context/output-example.json"):
                self.assertFalse((dest / rel).exists(), rel)

    def test_manifest_declares_kind_code_with_no_requires_and_no_reaches(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = smith_manifest.load(create_code_cog(tmp))[0]
            self.assertEqual(m["kind"], "code")
            self.assertEqual(m["requires"], [])
            self.assertEqual(m["reaches"], [])
            self.assertEqual(set(m["context"]),
                             {"input_schema", "output_schema"})

    def test_pixi_tasks_are_run_check_test_and_never_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = smith_manifest.read_pixi(create_code_cog(tmp))["tasks"]
            self.assertEqual(set(tasks), {"run", "check", "test"})

    def test_created_code_cog_passes_check_and_its_own_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            findings = smith_check.check(dest, run_tests=True)
            self.assertEqual([f for f in findings if f["level"] == "error"], [])

    def test_yaml_manifest_format_also_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp, manifest_format="yaml")
            self.assertTrue((dest / "cog.yaml").exists())
            findings = smith_check.check(dest)
            self.assertEqual([f for f in findings if f["level"] == "error"], [])

    def test_cli_new_kind_code_takes_a_positional_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "src" / "cogsmith_cli.py"), "new",
                 "cog-positional", "--kind", "code", "--yes"],
                cwd=tmp, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((Path(tmp) / "cog-positional" / "COG.md").exists())


class TestCheckerRules(unittest.TestCase):
    def details(self, findings, level="error"):
        return " | ".join(f["detail"] for f in findings if f["level"] == level)

    def edit_manifest(self, dest, old, new):
        path = Path(dest) / "pixi.toml"
        path.write_text(path.read_text().replace(old, new))

    def test_machinery_is_enforced_by_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            core = dest / "src" / "cog_core.py"
            core.write_text(core.read_text() + "\n# local tweak\n")
            self.assertIn("differs from cog-smith master",
                          self.details(smith_check.check(dest)))

    def test_code_cog_with_requires_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            self.edit_manifest(
                dest, "requires = []",
                'requires = [{ capability = "model-endpoint/openai-compatible" }]')
            self.assertIn("kind: code declares requires",
                          self.details(smith_check.check(dest)))

    def test_reaches_on_a_non_code_cog_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cog-toy"
            smith_core.create(dest, smith_core.default_tokens("cog-toy"))
            path = dest / "pixi.toml"
            path.write_text(path.read_text().replace(
                'kind = "context"',
                'kind = "context"\nreaches = [{ resource = "github", '
                'actions = ["read"] }]'))
            self.assertIn("only code Cogs declare what they reach",
                          self.details(smith_check.check(dest)))

    def test_reaches_entries_must_be_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            self.edit_manifest(
                dest, "reaches = []",
                'reaches = [{ resource = 3, actions = ["read"] }]')
            self.assertIn("resource must be a string",
                          self.details(smith_check.check(dest)))

    def test_reaches_actions_must_be_a_list_of_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            self.edit_manifest(
                dest, "reaches = []",
                'reaches = [{ resource = "github", actions = "read" }]')
            self.assertIn("actions must be a list of strings",
                          self.details(smith_check.check(dest)))


class TestTheSeam(unittest.TestCase):
    def test_a_reaching_cog_invoked_without_a_grant_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            declare_reaches(dest)
            code, env, _ = run_cog(dest, "--bundle",
                                   "examples/sample-bundle.json")
            self.assertEqual(code, 1)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "no-grant")

    def test_an_expired_grant_is_refused_by_the_cog(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            declare_reaches(dest)
            path = Path(tmp) / "grant.json"
            path.write_text(json.dumps(
                grant("openteams/cog-widget", expires_in_minutes=-1)))
            _, env, _ = run_cog(dest, "--bundle", "examples/sample-bundle.json",
                                "--grant", str(path), "--run-id", "run-1")
            self.assertEqual(env["error"]["code"], "grant-expired")

    def test_a_grant_from_another_run_is_refused_by_the_cog(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            declare_reaches(dest)
            path = Path(tmp) / "grant.json"
            path.write_text(json.dumps(grant("openteams/cog-widget",
                                             run_id="run-other")))
            _, env, _ = run_cog(dest, "--bundle", "examples/sample-bundle.json",
                                "--grant", str(path), "--run-id", "run-1")
            self.assertEqual(env["error"]["code"], "grant-wrong-run")

    def test_a_grant_for_another_cog_is_refused_by_the_cog(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            declare_reaches(dest)
            path = Path(tmp) / "grant.json"
            path.write_text(json.dumps(
                grant("openteams/cog-widget",
                      recipient_id="openteams/cog-someone-else")))
            _, env, _ = run_cog(dest, "--bundle", "examples/sample-bundle.json",
                                "--grant", str(path), "--run-id", "run-1")
            self.assertEqual(env["error"]["code"], "grant-wrong-recipient")

    def test_a_valid_grant_runs_and_the_binding_names_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            declare_reaches(dest)
            path = Path(tmp) / "grant.json"
            path.write_text(json.dumps(grant("openteams/cog-widget")))
            code, env, out = run_cog(dest, "--bundle",
                                     "examples/sample-bundle.json",
                                     "--grant", str(path), "--run-id", "run-1")
            self.assertEqual(code, 0, out)
            self.assertEqual(env["binding"]["kind"], "code")
            self.assertNotIn("model", env["binding"])
            self.assertEqual(
                env["binding"]["task_logic_sha256"],
                hashlib.sha256((dest / "src" / "task_logic.py")
                               .read_bytes()).hexdigest())


# --------------------------------------------------------------------------
# A fake write-back Cog: the starter package with task_logic.py (the
# AUTHOR-OWNED module) replaced by a two-phase, journal-reconciling
# implementation over a fake GitHub that counts its calls in a file. This is
# the §7 journal pair: a failing change is reported and the step passes with
# problems; a crash between "GitHub accepted" and the journal line is
# repaired on the next invocation, and the change is applied exactly once.

WRITE_BACK_TASK_LOGIC = '''
"""A fake write-back: applies approved changes against a counting stub."""
import json
import os
from pathlib import Path

COUNTER = Path(os.environ["FAKE_GITHUB_COUNTER"])


def fake_github_apply(change_id):
    calls = json.loads(COUNTER.read_text()) if COUNTER.exists() else []
    calls.append(change_id)
    COUNTER.write_text(json.dumps(calls))
    if change_id == os.environ.get("FAKE_GITHUB_FAILS"):
        raise RuntimeError("github refused this change")
    return {"url": f"https://example.invalid/{change_id}"}


def run(bundle, grant, journal):
    import cog_core
    problems, used, outcomes = [], [], []
    done = journal.phases() if journal else {}
    for change in bundle.get("changes") or []:
        cid = change["change_id"]
        if done.get(cid) in ("applied", "failed"):
            outcomes.append({"change_id": cid, "outcome": "skipped"})
            continue
        ok, detail = cog_core.write_allowed(grant, cid,
                                            change.get("content_sha256"))
        if not ok:
            used.append(cog_core.use("write", "github", cid, "denied", detail))
            problems.append(cog_core.problem("authority", detail, "warn"))
            outcomes.append({"change_id": cid, "outcome": "denied"})
            continue
        if done.get(cid) == "applying":
            # Uncertain: GitHub may have accepted it before the crash.
            # Reconcile instead of applying again.
            journal.append({"change_id": cid, "phase": "applied",
                            "reconciled": True})
            used.append(cog_core.use("write", "github", cid, "authorized",
                                     "reconciled"))
            outcomes.append({"change_id": cid, "outcome": "applied"})
            continue
        journal.append({"change_id": cid, "phase": "applying"})
        try:
            evidence = fake_github_apply(cid)
        except RuntimeError as exc:
            journal.append({"change_id": cid, "phase": "failed",
                            "detail": str(exc)})
            used.append(cog_core.use("write", "github", cid, "failed",
                                     str(exc)))
            problems.append(cog_core.problem("write-back", str(exc), "warn"))
            outcomes.append({"change_id": cid, "outcome": "failed"})
            continue
        if os.environ.get("CRASH_AFTER") == cid:
            os._exit(9)          # planted: GitHub accepted, no journal line
        journal.append({"change_id": cid, "phase": "applied",
                        "evidence": evidence})
        used.append(cog_core.use("write", "github", cid, "authorized"))
        outcomes.append({"change_id": cid, "outcome": "applied"})
    return {"changes": outcomes, "authority_use": used}, problems
'''

WRITE_BACK_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["changes"],
    "properties": {"changes": {"type": "array"}},
}
WRITE_BACK_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["changes", "authority_use"],
    "properties": {"changes": {"type": "array"},
                   "authority_use": {"type": "array"}},
}


def write_back_cog(tmp):
    dest = create_code_cog(tmp, name="cog-write-github")
    declare_reaches(dest, actions=("write",))
    (dest / "src" / "task_logic.py").write_text(WRITE_BACK_TASK_LOGIC)
    (dest / "context" / "input-schema.json").write_text(
        json.dumps(WRITE_BACK_INPUT_SCHEMA))
    (dest / "context" / "output-schema.json").write_text(
        json.dumps(WRITE_BACK_OUTPUT_SCHEMA))
    return dest


def write_grant(tmp, change_ids):
    path = Path(tmp) / "grant.json"
    path.write_text(json.dumps(grant(
        "openteams/cog-write-github",
        operations=[{"resource": "github", "action": "write",
                     "changes": [{"change_id": c, "content_sha256": None}
                                 for c in change_ids]}])))
    return path


def run_write_back(dest, tmp, bundle, env_extra=None):
    (Path(tmp) / "bundle.json").write_text(json.dumps(bundle))
    env = dict(os.environ)
    env["FAKE_GITHUB_COUNTER"] = str(Path(tmp) / "calls.json")
    env.update(env_extra or {})
    completed = subprocess.run(
        [sys.executable, str(dest / "src" / "cog_cli.py"),
         "--bundle", str(Path(tmp) / "bundle.json"),
         "--grant", str(Path(tmp) / "grant.json"),
         "--run-id", "run-1",
         "--journal", str(Path(tmp) / "journal.jsonl")],
        cwd=str(dest), capture_output=True, text=True, env=env)
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        envelope = None
    return completed, envelope


class TestJournalAndWriteBack(unittest.TestCase):
    def calls(self, tmp):
        path = Path(tmp) / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_a_failing_change_is_reported_and_the_step_passes_with_problems(self):
        sys.path.insert(0, str(ROOT / "templates" / "op" / "src"))
        import op_runner                                # noqa: E402
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-ok", "c-bad"])
            bundle = {"changes": [{"change_id": "c-ok"},
                                  {"change_id": "c-bad"}]}
            completed, env = run_write_back(dest, tmp, bundle,
                                            {"FAKE_GITHUB_FAILS": "c-bad"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(env["ok"])
            outcomes = {c["change_id"]: c["outcome"] for c in
                        env["payload"]["changes"]}
            self.assertEqual(outcomes, {"c-ok": "applied", "c-bad": "failed"})
            self.assertEqual(op_runner.gate_envelope(env)["status"],
                             "pass-with-problems")

    def test_a_change_not_in_the_grant_is_denied_by_the_cog(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-approved"])
            bundle = {"changes": [{"change_id": "c-not-approved"}]}
            _, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "denied")
            self.assertEqual(self.calls(tmp), [])       # never attempted
            self.assertEqual(env["payload"]["authority_use"][0]["outcome"],
                             "denied")

    def test_a_crash_after_github_accepted_applies_the_change_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = {"changes": [{"change_id": "c-1"}]}
            completed, _ = run_write_back(dest, tmp, bundle,
                                          {"CRASH_AFTER": "c-1"})
            self.assertEqual(completed.returncode, 9)   # planted crash
            self.assertEqual(self.calls(tmp), ["c-1"])
            journal = [json.loads(line) for line in
                       (Path(tmp) / "journal.jsonl").read_text().splitlines()]
            self.assertEqual([e["phase"] for e in journal], ["applying"])

            # resume: the Cog reads the journal FIRST and reconciles
            completed, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "applied")
            self.assertEqual(self.calls(tmp), ["c-1"])  # exactly once
            journal = [json.loads(line) for line in
                       (Path(tmp) / "journal.jsonl").read_text().splitlines()]
            self.assertTrue(journal[-1].get("reconciled"))

    def test_a_second_invocation_skips_a_change_with_an_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = {"changes": [{"change_id": "c-1"}]}
            run_write_back(dest, tmp, bundle)
            _, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "skipped")
            self.assertEqual(self.calls(tmp), ["c-1"])


if __name__ == "__main__":
    unittest.main()
