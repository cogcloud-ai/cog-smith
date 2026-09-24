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
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import op_fixtures as fx
from op_fixtures import op_runner, op_spec

import test_code_cog as cc

ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / "op_process_driver.py"
REPO = fx.REPO


def spec_doc():
    """compose (human Gate, canned) -> example-writer (a real code Cog)."""
    compose = fx.cog_step("compose", gate={"policy": "human", "guards": []})
    compose["input"] = {"note": {"$from": "inputs.note"}}
    write = fx.cog_step("example-writer", task="run", depends_on=["compose"],
                        authority={"requires": [
                            {"resource": "github", "action": "write",
                             "changes": {"$from":
                                         "steps.compose.decision.approved"}}]})
    write["cog"]["id"] = "openteams/cog-example-writer"
    write["input"] = {"changes": {"$from": "steps.compose.decision.approved"}}
    return fx.spec_doc([compose, write])


def unresolved_spec_doc():
    """compose (human Gate, canned) -> example-writer (unresolved once) ->
    example-recorder: the three steps the internal contract's runner-level test needs."""
    compose = fx.cog_step("compose", gate={"policy": "human", "guards": []})
    compose["input"] = {"note": {"$from": "inputs.note"}}
    write = fx.cog_step("example-writer", task="run", depends_on=["compose"],
                        authority={"requires": [
                            {"resource": "github", "action": "write",
                             "changes": {"$from":
                                         "steps.compose.decision.approved"}}]})
    write["cog"]["id"] = "openteams/cog-example-writer"
    write["input"] = {"changes": {"$from": "steps.compose.decision.approved"}}
    record = fx.cog_step("example-recorder", task="run", depends_on=["example-writer"])
    record["cog"]["id"] = "openteams/cog-example-recorder"
    record["input"] = {"outcomes": {"$from":
                                    "steps.example-writer.payload.changes"}}
    return fx.spec_doc([compose, write, record])


class ProcessCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        fx.write_cog(self.root, "compose")
        self.cog = cc.write_back_cog(self.root)          # a real code Cog
        self.assertEqual(self.cog.name, "cog-example-writer")
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

    def launch(self, *args, env_extra=None):
        """`drive`, but as a process this test keeps talking to."""
        env = dict(os.environ)
        env["FAKE_GITHUB_COUNTER"] = str(self.root / "calls.json")
        env.update(env_extra or {})
        return subprocess.Popen(
            [sys.executable, str(DRIVER), str(self.canned), str(self.package),
             *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(ROOT))

    def wait_for(self, path, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Path(path).exists():
                return
            time.sleep(0.02)
        self.fail(f"{path} never appeared")             # pragma: no cover

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
        path = Path(run_dir) / "journal" / "example-writer.jsonl"
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
                         ["example-writer"])
        self.assertEqual([e["phase"] for e in self.journal(run_dir)],
                         ["applying"])

        code, output = self.drive("--resume", run_dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "completed")
        track = self.track(run_dir)
        record = self.step(track, "example-writer")
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
        # two issuances, two files, two ids — neither overwritten
        grants = sorted((Path(run_dir) / "grants" / "example-writer").iterdir())
        self.assertEqual([p.name for p in grants], ["0.json", "1.json"])

    def test_a_second_process_cannot_resume_a_run_whose_lock_is_held(self):
        # The lock is the DESCRIPTOR: this test process takes it (leaving the
        # file EMPTY, the window the old pid file read as stale) and the
        # resume is refused by name (verification finding 1).
        paused = self.start()
        run_dir = Path(paused["run_dir"])
        holder = os.open(str(run_dir / "run.lock"), os.O_CREAT | os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(holder, 0)
        try:
            decision = self.decide(paused)
            code, output = self.drive("--resume", str(run_dir),
                                      "--decision", str(decision))
        finally:
            os.close(holder)
        self.assertEqual(code, 2, output)
        self.assertIn("one run, one process", " ".join(output["problems"]))
        self.assertEqual(self.calls(), [])               # nothing was written

    def test_two_overlapping_resumes_apply_the_change_exactly_once(self):
        # A REAL race (verification finding 1): the first resume is held
        # inside the write Cog — the Cog that has the grant and the journal —
        # while a second resume of the same run arrives.
        paused = self.start()
        run_dir = Path(paused["run_dir"])
        decision = self.decide(paused)
        hold = self.root / "hold"
        first = self.launch("--resume", str(run_dir), "--decision",
                            str(decision), env_extra={"HOLD_FOR": str(hold)})
        try:
            self.wait_for(Path(str(hold) + ".started"))
            code, output = self.drive("--resume", str(run_dir))
            self.assertEqual(code, 2, output)
            self.assertIn("one run, one process", " ".join(output["problems"]))
        finally:
            hold.write_text("go")
            out, err = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, out + err)
        self.assertEqual(self.calls(), ["c-1"])          # exactly once

    def test_the_run_lock_file_stays_and_is_free_when_the_run_ends(self):
        # No unlink on release: removing the pathname would let a second
        # process create a NEW file and lock that one instead.
        paused = self.start()
        path = Path(paused["run_dir"]) / "run.lock"
        self.assertTrue(path.exists())
        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # free again
        finally:
            os.close(fd)


class UnresolvedResumeTests(unittest.TestCase):
    """Contract §9e: uncertain means the step has not finished.

    The write Cog leaves its change uncertain and reports
    `write-back-unresolved` at error severity. The Gate fails, the run stops
    there, and the recording step downstream never runs — nothing records a
    final state that is not final. `op run --resume` then re-runs THAT step
    (Op machinery 0.5.5), the Cog reconciles, and the run finishes.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        fx.write_cog(self.root, "compose")
        cc.unresolved_write_cog(self.root)
        cc.record_cog(self.root)
        fx.write_package(self.package, unresolved_spec_doc())
        self.request = fx.write_request(self.root / "request.json",
                                        {"note": "hello"})
        self.authority = self.root / "authority.json"
        self.authority.write_text(json.dumps(fx.authority_doc()))
        self.canned = self.root / "canned.json"
        self.canned.write_text(json.dumps({"ask": [fx.envelope(
            payload={"changes": [fx.change("c-1")]})]}))

    def drive(self, *args):
        env = dict(os.environ)
        env["FAKE_GITHUB_COUNTER"] = str(self.root / "calls.json")
        completed = subprocess.run(
            [sys.executable, str(DRIVER), str(self.canned), str(self.package),
             *args], capture_output=True, text=True, env=env, cwd=str(ROOT))
        try:
            return completed.returncode, json.loads(completed.stdout)
        except json.JSONDecodeError:                     # pragma: no cover
            self.fail(f"driver printed no JSON: {completed.stdout}\n"
                      f"{completed.stderr}")

    def read(self, name, default=None):
        path = self.root / name
        return json.loads(path.read_text()) if path.exists() else default

    def track(self, run_dir):
        return json.loads((Path(run_dir) / "track.json").read_text())

    def status(self, track, sid):
        return next(s["status"] for s in track["steps"] if s["id"] == sid)

    def test_an_unresolved_write_stops_the_run_and_resume_re_runs_that_step(self):
        code, paused = self.drive("--request", str(self.request),
                                  "--authority", str(self.authority))
        self.assertEqual(code, op_runner.PAUSED_EXIT, paused)
        run_dir = paused["run_dir"]
        pending = json.loads(Path(paused["pending"]).read_text())
        decision = self.root / "decision.json"
        decision.write_text(json.dumps(fx.decision_doc(
            pending["run_id"], pending["step"], pending["payload_sha256"],
            {"c-1": "approve"})))

        # ---- the run stops at the write step, and nothing downstream runs
        code, output = self.drive("--resume", run_dir, "--decision",
                                  str(decision))
        self.assertEqual(code, 1, output)
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["failed_step"], "example-writer")
        track = self.track(run_dir)
        self.assertEqual(track["status"], "failed")
        self.assertEqual(track["failed_step"], "example-writer")
        self.assertEqual(self.status(track, "compose"), "passed")
        self.assertEqual(self.status(track, "example-writer"), "failed")
        self.assertEqual(self.status(track, "example-recorder"), "not-reached")
        self.assertIsNone(self.read("records.json"))     # nothing recorded
        self.assertEqual(self.read("write-attempts.json"), [["c-1"]])

        # ---- the resume re-runs THAT step, and only that step
        code, output = self.drive("--resume", run_dir)
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "completed")
        track = self.track(run_dir)
        self.assertIsNone(track["failed_step"])
        self.assertEqual(self.status(track, "compose"), "passed")
        self.assertEqual(self.status(track, "example-writer"), "passed")
        self.assertEqual(self.status(track, "example-recorder"), "passed")
        # the write step ran twice (it had unfinished work), the recording
        # step once — AFTER the write finished — and the human-gated step
        # not again: a second pause would have ended this run at exit 3.
        self.assertEqual(self.read("write-attempts.json"), [["c-1"], ["c-1"]])
        self.assertEqual(self.read("records.json"),
                         [[{"change_id": "c-1", "outcome": "applied"}]])
        self.assertEqual(self.read("calls.json"), ["c-1"])   # exactly once
        journal = [json.loads(line) for line in
                   (Path(run_dir) / "journal" / "example-writer.jsonl")
                   .read_text().splitlines() if line.strip()]
        self.assertEqual([e["phase"] for e in journal],
                         ["uncertain", "applied"])


#: A fake `pixi` on PATH. It does what pixi does for the runner's purposes —
#: read the package manifest, look the TASK up in `[tasks]`, run it in the
#: package directory with the trailing arguments — and nothing else. The
#: production `invoke_cog` is untouched, so the command it builds, the
#: `--grant/--run-id/--journal` flags and the request-flag fallback are all
#: exercised for real (the internal contract, verification finding 7).
FAKE_PIXI = '''#!{python}
import json
import os
import subprocess
import sys
import tomllib

argv = sys.argv[1:]
log = os.environ.get("FAKE_PIXI_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(argv) + "\\n")
pidfile = os.environ.get("FAKE_PIXI_PID")
if pidfile:
    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))
if not argv or argv[0] != "run":
    sys.exit("fake pixi understands `run` only: " + " ".join(argv))
index = argv.index("--manifest-path")
manifest = argv[index + 1]
rest = argv[index + 2:]
task, arguments = rest[0], rest[1:]
if arguments and arguments[0] == "--":
    arguments = arguments[1:]
with open(manifest, "rb") as handle:
    tasks = tomllib.load(handle).get("tasks") or {{}}
if task not in tasks:
    sys.exit("fake pixi: no task " + task + " in " + manifest)
command = tasks[task].split()
if command[0] == "python":
    command[0] = "{python}"
# `close_fds=False`: a real launcher hands the task the descriptors it was
# started with, and the run lock is one of them. Closing them here would
# make the fake a weaker launcher than pixi and hide the lifetime the tests
# are about (an internal review, finding 4).
sys.exit(subprocess.run(command + arguments, close_fds=False,
                        cwd=os.path.dirname(manifest)).returncode)
'''


#: A created Cog that reports whether a descriptor reached it: the question
#: finding 4 asks of the launcher, answered by the Cog itself.
FD_PROBE_TASK_LOGIC = '''
"""Reports whether the descriptor the caller passed arrived."""
import os


def run(bundle, grant, journal):
    try:
        os.fstat(int(bundle["fd"]))
        arrived = True
    except OSError:
        arrived = False
    return {"descriptor_arrived": arrived}, []
'''


def production_spec_doc():
    """propose (a real code Cog, human Gate) -> example-writer (a real code
    Cog). Every step goes through `pixi run <task>`."""
    compose = fx.cog_step("compose", task="run",
                          gate={"policy": "human", "guards": []})
    compose["cog"] = {"id": "openteams/cog-propose", "version": "0.1.0",
                      "source": "../cog-propose", "task": "run"}
    compose["input"] = {"note": {"$from": "inputs.note"}}
    write = fx.cog_step("example-writer", task="run", depends_on=["compose"],
                        authority={"requires": [
                            {"resource": "github", "action": "write",
                             "changes": {"$from":
                                         "steps.compose.decision.approved"}}]})
    write["cog"]["id"] = "openteams/cog-example-writer"
    write["input"] = {"changes": {"$from": "steps.compose.decision.approved"}}
    return fx.spec_doc([compose, write])


class ProductionSeamTests(unittest.TestCase):
    """The production `invoke_cog`, end to end (verification finding 7).

    Only the EXECUTABLE boundary is substituted: `pixi` itself. Command
    construction, the `--request` -> `--bundle` fallback, the authority
    flags and the Cogs are the real ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "op-test"
        cc.propose_cog(self.root)
        cc.write_back_cog(self.root)
        fx.write_package(self.package, production_spec_doc())
        self.request = fx.write_request(self.root / "request.json",
                                        {"note": "add a label"})
        self.authority = self.root / "authority.json"
        self.authority.write_text(json.dumps(fx.authority_doc()))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        pixi = self.bin / "pixi"
        pixi.write_text(FAKE_PIXI.format(python=sys.executable))
        pixi.chmod(0o755)
        self.log = self.root / "pixi-log.jsonl"

    def drive(self, *args):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_GITHUB_COUNTER"] = str(self.root / "calls.json")
        env["FAKE_PIXI_LOG"] = str(self.log)
        completed = subprocess.run(
            [sys.executable, str(DRIVER), "-", str(self.package), *args],
            capture_output=True, text=True, env=env, cwd=str(ROOT))
        try:
            return completed.returncode, json.loads(completed.stdout)
        except json.JSONDecodeError:                     # pragma: no cover
            self.fail(f"driver printed no JSON: {completed.stdout}\n"
                      f"{completed.stderr}")

    def launch(self, *args, env_extra=None):
        """`drive`, but as a process this test can kill."""
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_GITHUB_COUNTER"] = str(self.root / "calls.json")
        env["FAKE_PIXI_LOG"] = str(self.log)
        env.update(env_extra or {})
        return subprocess.Popen(
            [sys.executable, str(DRIVER), "-", str(self.package), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(ROOT))

    def wait_for(self, path, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Path(path).exists():
                return
            time.sleep(0.02)
        self.fail(f"{path} never appeared")             # pragma: no cover

    def pause_and_decide(self):
        code, paused = self.drive("--request", str(self.request),
                                  "--authority", str(self.authority))
        self.assertEqual(code, op_runner.PAUSED_EXIT, paused)
        pending = json.loads(Path(paused["pending"]).read_text())
        decision = self.root / "decision.json"
        decision.write_text(json.dumps(fx.decision_doc(
            pending["run_id"], pending["step"], pending["payload_sha256"],
            {"c-1": "approve"})))
        return paused, decision

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()
                if line.strip()]

    def test_the_lock_outlives_the_runner_while_its_cog_is_still_running(self):
        """An internal review, finding 4: the lifetime guarantee, demonstrated.

        The runner passes the lock descriptor to the launcher; the launcher
        (here a fake `pixi` that inherits descriptors the way a real one
        does) passes it on to the Cog. The runner is then KILLED while the
        Cog is still inside its write. The run must stay locked until the
        Cog is gone — that is what protects an outstanding external effect
        from a second runner — and must be free once it is."""
        paused, decision = self.pause_and_decide()
        run_dir = Path(paused["run_dir"])
        hold = self.root / "hold"
        pidfile = self.root / "pixi.pid"
        runner = self.launch("--resume", str(run_dir), "--decision",
                             str(decision),
                             env_extra={"HOLD_FOR": str(hold),
                                        "FAKE_PIXI_PID": str(pidfile)})
        try:
            self.wait_for(Path(str(hold) + ".started"))
            runner.kill()                       # no `finally`, no release
            runner.wait(timeout=60)
            runner.stdout.close()
            runner.stderr.close()
            # and the LAUNCHER too: what is left holding the run is the Cog
            # itself, still inside its write, with the descriptor it
            # inherited through the launch chain.
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
            self.assertIsNone(json.loads(
                (run_dir / "track.json").read_text()).get("ended_at"))
            # the Cog is alive and holds the inherited descriptor
            code, output = self.drive("--resume", str(run_dir))
            self.assertEqual(code, 2, output)
            self.assertIn("one run, one process", " ".join(output["problems"]))
            with self.assertRaises(op_spec.OpSpecError):
                op_runner.RunLock(run_dir).acquire()
        finally:
            hold.write_text("go")
        # and once the Cog is gone, the run is lockable again
        deadline = time.monotonic() + 60
        lock = None
        while lock is None and time.monotonic() < deadline:
            try:
                lock = op_runner.RunLock(run_dir).acquire()
            except op_spec.OpSpecError:
                time.sleep(0.05)
        self.assertIsNotNone(lock, "the lock was never released")
        lock.release()
        self.assertEqual(
            json.loads((self.root / "calls.json").read_text()), ["c-1"])

    def test_a_real_pixi_on_path_carries_the_descriptor(self):
        """The same question asked of the REAL launcher, once.

        `pass_fds` is a promise about the immediate child; everything after
        it depends on pixi's own launch chain. This runs a created Cog
        through the installed `pixi` with a locked descriptor passed in and
        RECORDS what arrived. It never fails the suite on the answer: what
        pixi does is a finding, not this machinery's behaviour."""
        pixi = shutil.which("pixi")
        if not pixi:
            raise unittest.SkipTest(
                "pixi is not on PATH: the descriptor's survival through the "
                "real launcher was not measured in this run")
        dest = cc.create_code_cog(self.root, name="cog-fd-probe")
        (dest / "src" / "task_logic.py").write_text(FD_PROBE_TASK_LOGIC)
        (dest / "context" / "input-schema.json").write_text(json.dumps(
            {"type": "object", "required": ["fd"],
             "properties": {"fd": {"type": "integer"}}}))
        (dest / "context" / "output-schema.json").write_text(json.dumps(
            {"type": "object", "required": ["descriptor_arrived"],
             "properties": {"descriptor_arrived": {"type": "boolean"}}}))
        lock_path = self.root / "probe.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            bundle = self.root / "fd-bundle.json"
            bundle.write_text(json.dumps({"fd": fd}))
            command = [pixi, "run", "--manifest-path", str(dest / "pixi.toml"),
                       "run", "--", "--bundle", str(bundle)]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, pass_fds=(fd,),
                    timeout=900, cwd=str(dest))
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"\n[real pixi] the probe did not run ({exc}); the "
                      f"descriptor's survival was not measured.")
                return
        finally:
            os.close(fd)
        try:
            envelope = json.loads(completed.stdout)
            arrived = envelope["payload"]["descriptor_arrived"]
        except (ValueError, KeyError, TypeError):
            print(f"\n[real pixi] the probe produced no envelope "
                  f"(exit {completed.returncode}); the descriptor's survival "
                  f"was not measured.\n{completed.stdout[-400:]}"
                  f"\n{completed.stderr[-400:]}")
            return
        print(f"\n[real pixi {pixi}] the run-lock descriptor "
              f"{'ARRIVED in' if arrived else 'did NOT arrive in'} the Cog "
              f"launched through `pixi run`.")

    def test_a_run_reaches_created_cogs_through_the_real_command_line(self):
        code, paused = self.drive("--request", str(self.request),
                                  "--authority", str(self.authority))
        self.assertEqual(code, op_runner.PAUSED_EXIT, paused)
        pending = json.loads(Path(paused["pending"]).read_text())
        self.assertEqual([c["change_id"] for c in
                          pending["payload"]["changes"]], ["c-1"])

        decision = self.root / "decision.json"
        decision.write_text(json.dumps(fx.decision_doc(
            pending["run_id"], pending["step"], pending["payload_sha256"],
            {"c-1": "approve"})))
        code, output = self.drive("--resume", paused["run_dir"],
                                  "--decision", str(decision))
        self.assertEqual(code, 0, output)
        self.assertEqual(output["status"], "completed")
        self.assertEqual(
            json.loads((self.root / "calls.json").read_text()), ["c-1"])

        commands = self.commands()
        # the manifest path, the declared task, and the fallback: the seam
        # tries its own --request flag first and the created Cog's CLI
        # rejects it by name before doing any work
        self.assertTrue(all(c[0] == "run" for c in commands), commands)
        self.assertIn("--manifest-path", commands[0])
        self.assertEqual(commands[0][commands[0].index("--manifest-path") + 2],
                         "run")
        flags = [c[c.index("--") + 1] for c in commands]
        self.assertEqual(flags[:2], ["--request", "--bundle"])
        # the write invocation carried the authority context, beside the
        # request and never inside it
        write = [c for c in commands if "--grant" in c]
        self.assertTrue(write)
        for flag in ("--grant", "--run-id", "--journal"):
            self.assertIn(flag, write[-1])
        request_path = write[-1][write[-1].index("--bundle") + 1]
        self.assertNotIn("grant", json.loads(Path(request_path).read_text()))


if __name__ == "__main__":
    unittest.main()
