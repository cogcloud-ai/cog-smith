"""The crash windows, end to end, as PROCESSES.

Contract §7 (journal) and §9 ("tests must drive the runner"). `op run` and
`op run --resume` are separate processes; the write-back Cog is a real
created code Cog invoked across a process boundary; the crash is a real
`os._exit` planted inside it; and the fake GitHub keeps state on disk that
the Cog's reconcile step consults. The runner is not asked to believe
anything the run directory does not say.

Both windows are covered: a crash AFTER the fake GitHub accepted (the
journal line was never written) and a crash BEFORE it (the journal says
`applying` but nothing landed). In both, the change ends up applied exactly
once — counted by the fake.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner

import test_code_cog as cc

ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / "op_process_driver.py"
REPO = fx.REPO


def spec_doc():
    """compose (human Gate, canned) -> write-github (a real code Cog)."""
    compose = fx.cog_step("compose", gate={"policy": "human", "guards": []})
    compose["input"] = {"note": {"$from": "inputs.note"}}
    write = fx.cog_step("write-github", task="run", depends_on=["compose"],
                        authority={"requires": [
                            {"resource": "github", "action": "write",
                             "changes": {"$from":
                                         "steps.compose.decision.approved"}}]})
    write["cog"]["id"] = "openteams/cog-write-github"
    write["input"] = {"changes": {"$from": "steps.compose.decision.approved"}}
    return fx.spec_doc([compose, write])


class ProcessCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        fx.write_cog(self.root, "compose")
        self.cog = cc.write_back_cog(self.root)          # a real code Cog
        self.assertEqual(self.cog.name, "cog-write-github")
        fx.write_package(self.package, spec_doc())
        self.request = fx.write_request(self.root / "request.json",
                                        {"note": "hello"})
        self.authority = self.root / "authority.json"
        self.authority.write_text(json.dumps(fx.authority_doc()))
        self.canned = self.root / "canned.json"
        self.canned.write_text(json.dumps({"ask": [fx.envelope(
            payload={"changes": [fx.change("c-1")]})]}))

    # ------------------------------------------------------------ helpers --
    def drive(self, *args, env_extra=None):
        env = dict(os.environ)
        env["FAKE_GITHUB_COUNTER"] = str(self.root / "calls.json")
        env.update(env_extra or {})
        completed = subprocess.run(
            [sys.executable, str(DRIVER), str(self.canned), str(self.package),
             *args],
            capture_output=True, text=True, env=env, cwd=str(ROOT))
        try:
            output = json.loads(completed.stdout)
        except json.JSONDecodeError:                     # pragma: no cover
            self.fail(f"driver printed no JSON: {completed.stdout}\n"
                      f"{completed.stderr}")
        return completed.returncode, output

    def start(self):
        code, output = self.drive("--request", str(self.request),
                                  "--authority", str(self.authority))
        self.assertEqual(code, op_runner.PAUSED_EXIT, output)
        return output

    def decide(self, output, verdict="approve"):
        pending = json.loads(Path(output["pending"]).read_text())
        doc = fx.decision_doc(pending["run_id"], pending["step"],
                              pending["payload_sha256"], {"c-1": verdict})
        path = self.root / "decision.json"
        path.write_text(json.dumps(doc))
        return path

    def calls(self):
        path = self.root / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def track(self, run_dir):
        return json.loads((Path(run_dir) / "track.json").read_text())

    def step(self, track, sid):
        return next(s for s in track["steps"] if s["id"] == sid)

    def journal(self, run_dir):
        path = Path(run_dir) / "journal" / "write-github.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]

    # -------------------------------------------------------------- tests --
    def _crash_and_resume(self, window):
        paused = self.start()
        run_dir = paused["run_dir"]
        decision = self.decide(paused)
        code, output = self.drive("--resume", run_dir,
                                  "--decision", str(decision),
                                  env_extra={window: "c-1"})
        self.assertEqual(code, 1, output)               # the Cog died
        # Whatever happened outside the run, the Track already said so: the
        # decision, the resume, the grant and a `running` record are all on
        # disk (review B1).
        track = self.track(run_dir)
        self.assertEqual(self.step(track, "compose")["status"], "passed")
        self.assertTrue(track["resumes"])
        self.assertEqual([g["step"] for g in track["grants"]],
                         ["write-github"])
        self.assertEqual([e["phase"] for e in self.journal(run_dir)],
                         ["applying"])

        code, output = self.drive("--resume", run_dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "completed")
        track = self.track(run_dir)
        record = self.step(track, "write-github")
        self.assertEqual(record["status"], "passed")
        envelope = json.loads(Path(record["envelope"]).read_text())
        self.assertEqual(envelope["payload"]["changes"][0]["outcome"],
                         "applied")
        self.assertEqual(self.calls(), ["c-1"])          # exactly once
        return run_dir

    def test_a_crash_after_github_accepted_applies_it_exactly_once(self):
        run_dir = self._crash_and_resume("CRASH_AFTER")
        self.assertTrue(self.journal(run_dir)[-1].get("reconciled"))

    def test_a_crash_before_github_accepted_applies_it_exactly_once(self):
        run_dir = self._crash_and_resume("CRASH_BEFORE")
        self.assertFalse(self.journal(run_dir)[-1].get("reconciled"))
        # two issuances, two files, two ids — neither overwritten (review S6)
        grants = sorted((Path(run_dir) / "grants" / "write-github").iterdir())
        self.assertEqual([p.name for p in grants], ["0.json", "1.json"])

    def test_a_second_process_cannot_resume_a_locked_run(self):
        paused = self.start()
        run_dir = Path(paused["run_dir"])
        (run_dir / "run.lock").write_text(
            json.dumps({"pid": os.getpid(), "at": "now"}))
        decision = self.decide(paused)
        code, output = self.drive("--resume", str(run_dir),
                                  "--decision", str(decision))
        self.assertEqual(code, 2, output)
        self.assertIn("one run, one process", " ".join(output["problems"]))
        self.assertEqual(self.calls(), [])               # nothing was written

    def test_a_lock_left_by_a_dead_process_is_taken_over_and_recorded(self):
        paused = self.start()
        run_dir = Path(paused["run_dir"])
        dead = subprocess.run([sys.executable, "-c", "pass"])
        (run_dir / "run.lock").write_text(
            json.dumps({"pid": 999999, "at": "earlier"}))
        del dead
        code, output = self.drive("--resume", str(run_dir),
                                  "--decision", str(self.decide(paused)))
        self.assertEqual(code, 0, output)
        resumes = self.track(run_dir)["resumes"]
        self.assertEqual(resumes[-1]["took_over_lock"]["pid"], 999999)
        self.assertFalse((run_dir / "run.lock").exists())

    def test_the_run_lock_is_removed_when_the_run_ends(self):
        paused = self.start()
        self.assertFalse((Path(paused["run_dir"]) / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
