"""Code Cogs (`kind: code`): creation, the checker rules, and the seam.

The created Cog is exercised as a PROCESS (`python
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
             "repositories": ["example-org/example-repo"]}],
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

    def test_a_crashing_output_checker_is_a_named_envelope(self):
        # Reproduced from an internal review: the package's `check_output` ran OUTSIDE
        # the machinery's task exception boundary, so a checker that tripped
        # over a payload it did not expect raised a traceback out of the CLI
        # after the task had already run. It is a named ok:false envelope
        # now (the internal contract, code-cog machinery 0.1.4).
        with tempfile.TemporaryDirectory() as tmp:
            dest = create_code_cog(tmp)
            path = dest / 'src' / 'task_logic.py'
            path.write_text(path.read_text() + CRASHING_CHECKER)
            code, env, out = run_cog(dest, '--bundle',
                                     'examples/sample-bundle.json')
            self.assertEqual(code, 1, out)
            self.assertNotIn('Traceback', out)
            self.assertFalse(env['ok'])
            self.assertEqual(env['error']['code'],
                             'output-check-failed')
            self.assertIn('TypeError', env['error']['detail'])
            self.assertEqual(env['problems'][0]['check'],
                             'output-check-failed')


#: A package checker that trips over its own payload: what an internal review
#: found in a real package, reduced to one line.
CRASHING_CHECKER = """


def check_output(payload, bundle):    # noqa: F811 (replaces the template's)
    raise TypeError("unhashable type: 'list'")
"""

PROBE = """
import json, sys
sys.path.insert(0, "src")
import cog_core
grant = json.loads(sys.argv[1])
print(json.dumps(%s))
"""


def probe(dest, expression, grant_doc):
    """Evaluate one expression over the created Cog's OWN machinery, in its
    own process (the machinery loads the package manifest at import)."""
    completed = subprocess.run(
        [sys.executable, "-c", PROBE % expression, json.dumps(grant_doc)],
        cwd=str(dest), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class TestGrantChecks(unittest.TestCase):
    """The per-call grant helpers: nothing fails open (review B4, S7)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = create_code_cog(self.tmp.name)
        declare_reaches(self.dest)
        self.addCleanup(self.tmp.cleanup)

    def doc(self, **over):
        return dict(grant("openteams/cog-widget"), **over)

    def test_a_grant_whose_two_run_ids_disagree_is_invalid(self):
        doc = self.doc(run_id="run-other")     # valid.run_id is still run-1
        self.assertEqual(
            probe(self.dest, 'cog_core.check_grant(grant, run_id="run-1")',
                  doc)[0], "grant-invalid")

    def test_an_invocation_without_a_run_id_is_invalid(self):
        self.assertEqual(
            probe(self.dest, "cog_core.check_grant(grant)", self.doc())[0],
            "grant-invalid")

    def test_a_malformed_recipient_is_named_not_crashed(self):
        code, detail = probe(self.dest,
                             'cog_core.check_grant(grant, run_id="run-1")',
                             self.doc(recipient=["bad"]))
        self.assertEqual(code, "grant-wrong-recipient")
        self.assertIn("recipient", detail)

    def test_read_allowed_re_checks_expiry_on_every_call(self):
        expired = grant("openteams/cog-widget", expires_in_minutes=-1)
        ok, detail = probe(
            self.dest,
            'cog_core.read_allowed(grant, "example-org/example-repo", '
            'run_id="run-1")', expired)
        self.assertFalse(ok)
        self.assertIn("grant-expired", detail)

    def test_read_allowed_re_checks_the_run_binding_on_every_call(self):
        ok, detail = probe(
            self.dest,
            'cog_core.read_allowed(grant, "example-org/example-repo", '
            'run_id="run-somewhere-else")', self.doc())
        self.assertFalse(ok)
        self.assertIn("grant-wrong-run", detail)

    def test_write_allowed_never_fails_open_on_a_null_hash(self):
        doc = self.doc(operations=[
            {"resource": "github", "action": "write",
             "changes": [{"change_id": "c-1", "content_sha256": None,
                          "target_sha256": None}]}])
        ok, detail = probe(
            self.dest,
            'cog_core.write_allowed(grant, "c-1", "whatever-the-target-says", '
            'run_id="run-1")', doc)
        self.assertFalse(ok)
        self.assertIn("carries both hashes", detail)

    def test_write_allowed_needs_a_freshly_fetched_target_hash(self):
        doc = self.doc(operations=[
            {"resource": "github", "action": "write",
             "changes": [{"change_id": "c-1", "content_sha256": "a" * 64,
                          "target_sha256": "b" * 64}]}])
        ok, detail = probe(
            self.dest,
            'cog_core.write_allowed(grant, "c-1", None, run_id="run-1")', doc)
        self.assertFalse(ok)
        self.assertIn("freshly fetched content hash", detail)

    def test_a_read_grant_whose_repositories_are_a_number_denies(self):
        # Verification finding 4: `target in 7` used to raise TypeError out
        # of the helper instead of denying with a reason.
        doc = self.doc(operations=[{"resource": "github", "action": "read",
                                    "repositories": 7}])
        ok, detail = probe(
            self.dest,
            'cog_core.read_allowed(grant, "example-org/example-repo", '
            'run_id="run-1")', doc)
        self.assertFalse(ok)
        self.assertIn("LIST of repository strings", detail)

    def test_a_read_grant_stating_one_repository_as_a_string_denies(self):
        # `"owner/repo"` used to authorize the SUBSTRING "owner": a grant is
        # never membership-tested against a bare string.
        doc = self.doc(operations=[{"resource": "github", "action": "read",
                                    "repositories": "owner/repo"}])
        ok, detail = probe(self.dest,
                           'cog_core.read_allowed(grant, "owner", '
                           'run_id="run-1")', doc)
        self.assertFalse(ok)
        self.assertIn("LIST of repository strings", detail)
        ok, _ = probe(self.dest,
                      'cog_core.read_allowed(grant, "owner/repo", '
                      'run_id="run-1")', doc)
        self.assertFalse(ok)

    def test_a_write_grant_whose_changes_are_not_a_list_denies(self):
        doc = self.doc(operations=[{"resource": "github", "action": "write",
                                    "changes": 7}])
        ok, detail = probe(
            self.dest,
            'cog_core.write_allowed(grant, "c-1", "1" * 64, run_id="run-1")',
            doc)
        self.assertFalse(ok)
        self.assertIn("never approved", detail)

    def test_a_grant_without_a_run_id_flag_is_refused_by_the_cli(self):
        path = Path(self.tmp.name) / "grant.json"
        path.write_text(json.dumps(self.doc()))
        code, env, _ = run_cog(self.dest, "--bundle",
                               "examples/sample-bundle.json",
                               "--grant", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(env["error"]["code"], "grant-invalid")
        self.assertIn("--run-id", env["error"]["detail"])

    def test_a_bundle_the_schema_refuses_never_reaches_the_packages_checker(self):
        # `{"items": [1]}` used to crash `check_input` on `item.get`; it is a
        # named invalid-input envelope (review S7).
        bundle = Path(self.tmp.name) / "bad-bundle.json"
        bundle.write_text(json.dumps({"items": [1]}))
        code, env, out = run_cog(self.dest, "--bundle", str(bundle),
                                 "--grant", str(self.write_grant()),
                                 "--run-id", "run-1")
        self.assertEqual(code, 1, out)
        self.assertEqual(env["error"]["code"], "invalid-input")
        self.assertTrue(all(p["check"] == "input" for p in env["problems"]))

    def write_grant(self):
        path = Path(self.tmp.name) / "grant.json"
        path.write_text(json.dumps(self.doc()))
        return path


# --------------------------------------------------------------------------
# A fake write-back Cog: the starter package with task_logic.py (the
# AUTHOR-OWNED module) replaced by a two-phase, journal-reconciling
# implementation over a fake GitHub that counts its calls in a file. This is
# the §7 journal pair: a failing change is reported and the step passes with
# problems; a crash between "GitHub accepted" and the journal line is
# repaired on the next invocation, and the change is applied exactly once.

WRITE_BACK_TASK_LOGIC = '''
"""A fake write-back over a fake GitHub that keeps STATE.

`calls.json` counts every attempted apply; `applied.json` is what the fake
GitHub actually holds. Reconciliation consults `applied.json` — it never
assumes an `applying` line means the effect landed — which is what makes the
two crash windows distinguishable.
"""
import json
import os
import time
from pathlib import Path

COUNTER = Path(os.environ["FAKE_GITHUB_COUNTER"])
STATE = COUNTER.with_name("applied.json")
TARGETS = COUNTER.with_name("targets.json")


def _read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def fake_github_apply(change_id):
    calls = _read(COUNTER, [])
    calls.append(change_id)
    COUNTER.write_text(json.dumps(calls))
    if change_id == os.environ.get("FAKE_GITHUB_FAILS"):
        raise RuntimeError("github refused this change")
    state = _read(STATE, [])
    state.append(change_id)
    STATE.write_text(json.dumps(state))
    return {"url": f"https://example.invalid/{change_id}"}


def fake_github_has(change_id):
    """The reconcile query: does the effect already exist on the target?"""
    return change_id in _read(STATE, [])


def fetch_target_sha256(change):
    """The target item's CURRENT content hash, fetched fresh."""
    return _read(TARGETS, {}).get(change["change_id"], "1" * 64)


def run(bundle, grant, journal):
    import cog_core
    problems, used, outcomes = [], [], []
    hold = os.environ.get("HOLD_FOR")
    if hold:
        # Overlapping-resume test: announce that the Cog is running and wait
        # for the test to release it, so a second runner really does arrive
        # while this one holds the run lock.
        Path(hold + ".started").write_text("1")
        for _ in range(600):
            if Path(hold).exists():
                break
            time.sleep(0.05)
    done = journal.phases() if journal else {}
    for change in bundle.get("changes") or []:
        cid = change["change_id"]
        if done.get(cid) in ("applied", "failed"):
            outcomes.append({"change_id": cid, "outcome": "skipped"})
            continue
        if done.get(cid) == "applying" and fake_github_has(cid):
            # Uncertain, and the target says it landed: reconcile, never
            # apply again.
            journal.append({"change_id": cid, "phase": "applied",
                            "reconciled": True})
            used.append(cog_core.use("write", "github", cid, "authorized",
                                     "reconciled"))
            outcomes.append({"change_id": cid, "outcome": "applied"})
            continue
        ok, detail = cog_core.write_allowed(
            grant, cid, fetch_target_sha256(change),
            content_sha256=cog_core.change_content_sha256(change))
        if not ok:
            used.append(cog_core.use("write", "github", cid, "denied", detail))
            problems.append(cog_core.problem("authority", detail, "warn"))
            outcomes.append({"change_id": cid,
                             "outcome": "stale"
                             if "approved against target content" in detail
                             else "denied"})
            continue
        journal.append({"change_id": cid, "phase": "applying"})
        if os.environ.get("CRASH_BEFORE") == cid:
            os._exit(9)          # planted: journal line, nothing applied
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
    dest = create_code_cog(tmp, name="cog-example-writer")
    declare_reaches(dest, actions=("write",))
    (dest / "src" / "task_logic.py").write_text(WRITE_BACK_TASK_LOGIC)
    (dest / "context" / "input-schema.json").write_text(
        json.dumps(WRITE_BACK_INPUT_SCHEMA))
    (dest / "context" / "output-schema.json").write_text(
        json.dumps(WRITE_BACK_OUTPUT_SCHEMA))
    return dest


TARGET_SHA = "1" * 64


def content_hash(change):
    """The change object's own hash, the way the runner computes it: canonical
    JSON over everything EXCEPT the two hash fields (the internal contract)."""
    body = {k: v for k, v in change.items()
            if k not in ("content_sha256", "target_sha256")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


def bundle_change(change_id, body="original"):
    """A change as it travels in the BUNDLE. Its `content_sha256` is a
    STATED digest: the Cog computes the hash from the object and never reads
    this field, so a bundle cannot agree with itself into an approval
    (the internal contract, verification finding 2)."""
    return {"change_id": change_id, "kind": "label", "body": body,
            "content_sha256": content_hash({"change_id": change_id,
                                            "kind": "label",
                                            "body": "original"})}


def write_grant(tmp, change_ids, target_sha256=TARGET_SHA, body="original",
                **over):
    """A write grant carrying BOTH hashes per change (the internal contract)."""
    path = Path(tmp) / "grant.json"
    path.write_text(json.dumps(grant(
        "openteams/cog-example-writer",
        operations=[{"resource": "github", "action": "write",
                     "changes": [{"change_id": c,
                                  "repository": "example-org/example-repo",
                                  "content_sha256": content_hash(
                                      bundle_change(c, body)),
                                  "target_sha256": target_sha256}
                                 for c in change_ids]}], **over)))
    return path


def changes_bundle(*change_ids, body="original"):
    return {"changes": [bundle_change(c, body) for c in change_ids]}


def run_write_back(dest, tmp, bundle, env_extra=None, journal=None):
    (Path(tmp) / "bundle.json").write_text(json.dumps(bundle))
    env = dict(os.environ)
    env["FAKE_GITHUB_COUNTER"] = str(Path(tmp) / "calls.json")
    env.update(env_extra or {})
    completed = subprocess.run(
        [sys.executable, str(dest / "src" / "cog_cli.py"),
         "--bundle", str(Path(tmp) / "bundle.json"),
         "--grant", str(Path(tmp) / "grant.json"),
         "--run-id", "run-1",
         "--journal", str(journal or Path(tmp) / "journal.jsonl")],
        cwd=str(dest), capture_output=True, text=True, env=env)
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        envelope = None
    return completed, envelope


# A real code Cog that PROPOSES changes — the human-gated half of the write
# pair. Used by the production-seam process test, where every Cog in the run
# is a created package invoked through the real `pixi run` command line.

PROPOSE_TASK_LOGIC = '''
"""Proposes one change for a human to decide about."""

REPOSITORY = "example-org/example-repo"


def run(bundle, grant, journal):
    change = {"change_id": "c-1", "kind": "label", "repository": REPOSITORY,
              "target": REPOSITORY + "#1",
              "summary": str(bundle.get("note") or "add a label"),
              "target_sha256": "1" * 64}
    import cog_core
    change["content_sha256"] = cog_core.change_content_sha256(change)
    return {"changes": [change]}, []
'''

PROPOSE_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["changes"],
    "properties": {"changes": {"type": "array"}},
}
PROPOSE_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"note": {"type": "string"}},
}


def propose_cog(tmp):
    dest = create_code_cog(tmp, name="cog-propose")
    (dest / "src" / "task_logic.py").write_text(PROPOSE_TASK_LOGIC)
    (dest / "context" / "input-schema.json").write_text(
        json.dumps(PROPOSE_INPUT_SCHEMA))
    (dest / "context" / "output-schema.json").write_text(
        json.dumps(PROPOSE_OUTPUT_SCHEMA))
    return dest


# A write-back that leaves its change UNRESOLVED the first time, and a
# recorder that counts its invocations: the pair the runner-level resume test
# of the internal contract needs. The first write invocation journals `uncertain` (the
# request went out, the answer was lost) and reports `write-back-unresolved`
# at ERROR severity, so the Gate fails and the run stops there. The next
# invocation reads the journal, asks the fake GitHub whether the effect
# landed, records `applied` and passes.

UNRESOLVED_WRITE_TASK_LOGIC = '''
"""A fake write-back whose first attempt ends uncertain (the internal contract)."""
import json
import os
from pathlib import Path

COUNTER = Path(os.environ["FAKE_GITHUB_COUNTER"])
STATE = COUNTER.with_name("applied.json")
ATTEMPTS = COUNTER.with_name("write-attempts.json")


def _read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _append(path, value):
    items = _read(path, [])
    items.append(value)
    path.write_text(json.dumps(items))


def run(bundle, grant, journal):
    import cog_core
    problems, used, outcomes, unresolved = [], [], [], []
    _append(ATTEMPTS, [c["change_id"] for c in bundle.get("changes") or []])
    done = journal.phases() if journal else {}
    for change in bundle.get("changes") or []:
        cid = change["change_id"]
        if done.get(cid) in ("applied", "failed", "denied"):
            outcomes.append({"change_id": cid, "outcome": "skipped"})
            continue
        if done.get(cid) == "uncertain":
            # Reconciliation is a READ, and it is the only call made here.
            used.append(cog_core.use("read", "github", cid, "authorized",
                                     "reconciled"))
            if cid in _read(STATE, []):
                journal.append({"change_id": cid, "phase": "applied",
                                "reconciled": True})
                outcomes.append({"change_id": cid, "outcome": "applied"})
                continue
        journal.append({"change_id": cid, "phase": "uncertain"})
        _append(COUNTER, cid)
        state = _read(STATE, [])
        state.append(cid)
        STATE.write_text(json.dumps(state))
        used.append(cog_core.use("write", "github", cid, "uncertain",
                                 "no answer from github"))
        outcomes.append({"change_id": cid, "outcome": "uncertain"})
        unresolved.append(cid)
    if unresolved:
        problems.append(cog_core.problem(
            "write-back-unresolved",
            "left uncertain, so this step has not finished: "
            + ", ".join(unresolved), "error"))
    return {"changes": outcomes, "authority_use": used}, problems
'''


def unresolved_write_cog(tmp):
    dest = create_code_cog(tmp, name="cog-example-writer")
    declare_reaches(dest, actions=("write",))
    (dest / "src" / "task_logic.py").write_text(UNRESOLVED_WRITE_TASK_LOGIC)
    (dest / "context" / "input-schema.json").write_text(
        json.dumps(WRITE_BACK_INPUT_SCHEMA))
    (dest / "context" / "output-schema.json").write_text(
        json.dumps(WRITE_BACK_OUTPUT_SCHEMA))
    return dest


RECORD_TASK_LOGIC = '''
"""Writes the run record, and counts the times it was asked to."""
import json
import os
from pathlib import Path

RECORDS = Path(os.environ["FAKE_GITHUB_COUNTER"]).with_name("records.json")


def run(bundle, grant, journal):
    records = json.loads(RECORDS.read_text()) if RECORDS.exists() else []
    records.append(bundle.get("outcomes") or [])
    RECORDS.write_text(json.dumps(records))
    return {"recorded": len(records)}, []
'''


def record_cog(tmp):
    dest = create_code_cog(tmp, name="cog-example-recorder")
    (dest / "src" / "task_logic.py").write_text(RECORD_TASK_LOGIC)
    (dest / "context" / "input-schema.json").write_text(json.dumps(
        {"$schema": "https://json-schema.org/draft/2020-12/schema",
         "type": "object",
         "properties": {"outcomes": {"type": "array"}}}))
    (dest / "context" / "output-schema.json").write_text(json.dumps(
        {"$schema": "https://json-schema.org/draft/2020-12/schema",
         "type": "object", "required": ["recorded"],
         "properties": {"recorded": {"type": "integer"}}}))
    return dest


class TestJournalAndWriteBack(unittest.TestCase):
    def calls(self, tmp):
        path = Path(tmp) / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def journal(self, tmp):
        path = Path(tmp) / "journal.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    def test_a_failing_change_is_reported_and_the_step_passes_with_problems(self):
        sys.path.insert(0, str(ROOT / "templates" / "op" / "src"))
        import op_runner                                # noqa: E402
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-ok", "c-bad"])
            completed, env = run_write_back(dest, tmp,
                                            changes_bundle("c-ok", "c-bad"),
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
            _, env = run_write_back(dest, tmp, changes_bundle("c-not-approved"))
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "denied")
            self.assertEqual(self.calls(tmp), [])       # never attempted
            self.assertEqual(env["payload"]["authority_use"][0]["outcome"],
                             "denied")

    def test_a_stale_target_is_denied_by_the_cog(self):
        # The grant's target_sha256 is the state the human approved against;
        # the world moved, so this change is not applied (the internal contract).
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            (Path(tmp) / "targets.json").write_text(
                json.dumps({"c-1": "9" * 64}))
            _, env = run_write_back(dest, tmp, changes_bundle("c-1"))
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "stale")
            self.assertEqual(self.calls(tmp), [])
            self.assertIn("approved against target content",
                          env["payload"]["authority_use"][0]["detail"])

    def test_a_bundle_cannot_swap_the_content_under_an_approved_id(self):
        # The CONTENT changed and the stated digest did not: the Cog hashes
        # the change it is about to apply, so forwarding the old digest no
        # longer passes (verification finding 2).
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            _, env = run_write_back(
                dest, tmp, changes_bundle("c-1", body="tampered"))
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "denied")
            self.assertEqual(self.calls(tmp), [])
            self.assertIn("but the one to apply is",
                          env["payload"]["authority_use"][0]["detail"])

    def test_a_grant_with_a_null_hash_authorizes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"], target_sha256=None)
            _, env = run_write_back(dest, tmp, changes_bundle("c-1"))
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "denied")
            self.assertEqual(self.calls(tmp), [])
            self.assertIn("carries both hashes",
                          env["payload"]["authority_use"][0]["detail"])

    def test_a_crash_after_github_accepted_applies_the_change_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = changes_bundle("c-1")
            completed, _ = run_write_back(dest, tmp, bundle,
                                          {"CRASH_AFTER": "c-1"})
            self.assertEqual(completed.returncode, 9)   # planted crash
            self.assertEqual(self.calls(tmp), ["c-1"])
            self.assertEqual([e["phase"] for e in self.journal(tmp)],
                             ["applying"])

            # resume: the Cog reads the journal FIRST and asks the target
            completed, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "applied")
            self.assertEqual(self.calls(tmp), ["c-1"])  # exactly once
            self.assertTrue(self.journal(tmp)[-1].get("reconciled"))

    def test_a_crash_before_github_accepted_still_applies_the_change_once(self):
        # The other window: the journal says `applying`, but nothing landed.
        # Reconciliation consults the target and applies it for real.
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = changes_bundle("c-1")
            completed, _ = run_write_back(dest, tmp, bundle,
                                          {"CRASH_BEFORE": "c-1"})
            self.assertEqual(completed.returncode, 9)
            self.assertEqual(self.calls(tmp), [])       # nothing applied
            self.assertEqual([e["phase"] for e in self.journal(tmp)],
                             ["applying"])

            completed, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "applied")
            self.assertEqual(self.calls(tmp), ["c-1"])  # exactly once
            self.assertFalse(self.journal(tmp)[-1].get("reconciled"))

    def test_a_second_invocation_skips_a_change_with_an_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = changes_bundle("c-1")
            run_write_back(dest, tmp, bundle)
            _, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "skipped")
            self.assertEqual(self.calls(tmp), ["c-1"])

    def test_a_torn_last_line_never_swallows_the_next_outcome(self):
        # A crash mid-write leaves an unterminated fragment. The next
        # `applied` line must be readable on its own, or a later invocation
        # loses the outcome and repeats the effect (review B6).
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            bundle = changes_bundle("c-1")
            (Path(tmp) / "journal.jsonl").write_text(
                '{"change_id": "c-0", "phase": "appl')   # torn, no newline
            _, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "applied")
            entries = self.journal(tmp)
            self.assertEqual(entries[0]["phase"], "torn")
            self.assertEqual([e["phase"] for e in entries[1:]],
                             ["applying", "applied"])
            # and the outcome is now visible to the next invocation
            _, env = run_write_back(dest, tmp, bundle)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "skipped")
            self.assertEqual(self.calls(tmp), ["c-1"])

    def test_a_torn_tail_that_splits_a_character_is_still_recoverable(self):
        # Verification finding 4: the fragment `{"phase": "appl\xc3` (a
        # multibyte character cut in half) used to raise UnicodeDecodeError
        # while decoding the WHOLE file, so the entries before it were lost
        # and no envelope came back at all. The tail is cut as bytes first.
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            (Path(tmp) / "journal.jsonl").write_bytes(
                b'{"change_id": "c-0", "phase": "applied"}\n'
                b'{"change_id": "c-1", "phase": "appl\xc3')
            completed, env = run_write_back(dest, tmp, changes_bundle("c-1"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(env["payload"]["changes"][0]["outcome"], "applied")
            phases = [e["phase"] for e in self.journal(tmp)]
            self.assertEqual(phases, ["applied", "torn", "applying", "applied"])
            self.assertEqual(self.calls(tmp), ["c-1"])

    def test_a_complete_line_that_is_not_utf8_is_named_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            (Path(tmp) / "journal.jsonl").write_bytes(
                b'{"change_id": "c-1", "phase": "\xff\xfe"}\n')
            completed, env = run_write_back(dest, tmp, changes_bundle("c-1"))
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(env["error"]["code"], "journal-corrupt")
            self.assertIn("not valid UTF-8", env["error"]["detail"])
            self.assertEqual(self.calls(tmp), [])

    def test_an_unreadable_journal_is_a_structured_envelope(self):
        # Reproduced from an internal review: a journal this process cannot
        # READ raised PermissionError out of the preflight — a traceback
        # where an ok:false envelope belongs. `journal-corrupt` is about
        # CONTENT; this is `journal-unreadable`.
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            journal = Path(tmp) / "journal.jsonl"
            journal.write_text('{"change_id": "c-1", "phase": "applied"}\n')
            journal.chmod(0o000)
            try:
                if os.access(journal, os.R_OK):      # pragma: no cover
                    self.skipTest("this process reads a mode-000 file (root?)")
                completed, env = run_write_back(dest, tmp,
                                                changes_bundle("c-1"))
            finally:
                journal.chmod(0o600)
            self.assertEqual(completed.returncode, 1)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "journal-unreadable")
            self.assertIn("PermissionError", env["error"]["detail"])
            self.assertEqual(env["problems"][0]["check"],
                             "journal-unreadable")
            self.assertEqual(self.calls(tmp), [])    # nothing was attempted

    def test_a_journal_that_cannot_be_created_is_a_structured_envelope(self):
        # The other half of finding 5: creating the journal's directory is
        # the first thing that can fail with an OSError.
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            closed = Path(tmp) / "closed"
            closed.mkdir(mode=0o500)
            try:
                if os.access(closed, os.W_OK):       # pragma: no cover
                    self.skipTest("this process writes a mode-500 directory")
                completed, env = run_write_back(
                    dest, tmp, changes_bundle("c-1"),
                    journal=closed / "sub" / "journal.jsonl")
            finally:
                closed.chmod(0o700)
            self.assertEqual(completed.returncode, 1)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(env["error"]["code"], "journal-unreadable")
            self.assertEqual(self.calls(tmp), [])

    def test_a_corrupt_complete_line_refuses_the_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = write_back_cog(tmp)
            write_grant(tmp, ["c-1"])
            (Path(tmp) / "journal.jsonl").write_text(
                'not json at all\n{"change_id": "c-1", "phase": "applied"}\n')
            completed, env = run_write_back(dest, tmp, changes_bundle("c-1"))
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(env["error"]["code"], "journal-corrupt")
            self.assertEqual(self.calls(tmp), [])


if __name__ == "__main__":
    unittest.main()
