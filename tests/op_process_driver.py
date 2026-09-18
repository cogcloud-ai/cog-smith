"""Run the Op machinery as a PROCESS (not a test module).

    python tests/op_process_driver.py CANNED PACKAGE [runner arguments...]

The runner, the Track, the grants, the journal and the code Cog are all
real: only the LAUNCHER is replaced. `invoke_cog` normally shells out to
`pixi run <task>`, which needs a solved environment per Cog; here a package
carrying `src/cog_cli.py` is launched directly with this interpreter, and a
fixture Cog with no code is answered from CANNED (a JSON file of
`{task: [envelope, ...]}`).

That keeps the crash-window tests honest: the runner crosses a real process
boundary, a planted `os._exit` really kills the Cog mid-write, and the
resume is a second process reading what the first one left on disk.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "templates" / "op" / "src"))
import op_runner   # noqa: E402


def make_invoke(canned):
    counts = {}

    def invoke_cog(cog_dir, task, request_path, grant_path=None, run_id=None,
                   journal_path=None):
        cog_dir = Path(cog_dir)
        cli = cog_dir / "src" / "cog_cli.py"
        if not cli.exists():
            index = counts.get(task, 0)
            counts[task] = index + 1
            answers = canned[task]
            return answers[min(index, len(answers) - 1)]
        command = [sys.executable, str(cli), "--bundle", str(request_path)]
        if grant_path:
            command += ["--grant", str(grant_path)]
        if run_id:
            command += ["--run-id", str(run_id)]
        if journal_path:
            command += ["--journal", str(journal_path)]
        completed = subprocess.run(command, cwd=str(cog_dir), text=True,
                                   capture_output=True)
        evidence = op_runner._evidence(command, completed.returncode,
                                       completed.stdout, completed.stderr, [])
        try:
            envelope = op_runner.parse_envelope(completed.stdout or "")
        except ValueError as exc:
            return op_runner.failed_envelope(
                cog_dir, task,
                (completed.stderr or "").strip() or str(exc),
                raw=completed.stdout, evidence=evidence)
        if completed.returncode != 0:
            return op_runner.failed_envelope(
                cog_dir, task,
                f"the Cog command for task {task!r} exited "
                f"{completed.returncode}",
                raw=envelope, carried=envelope, evidence=evidence)
        return envelope

    return invoke_cog


def main(argv):
    canned = json.loads(Path(argv[0]).read_text())
    op_runner.invoke_cog = make_invoke(canned)
    op_runner.ROOT = Path(argv[1]).resolve()
    return op_runner.main(argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
