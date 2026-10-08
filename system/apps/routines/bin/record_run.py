#!/usr/bin/env python3
"""Run a routine's command and append one record of the run to the ledger.

This is the single choke point both trigger paths go through, which is the only
reason the Routines app can tell an automatic run from a manual one:

  cron entry:   ... run_job.sh <id> --every 6h record_run.py --job <id> --trigger scheduled -- <cmd>
  Run now:      record_run.py --job <id> --trigger manual --update-job-state -- <cmd>

Under ``run_job.sh`` the runner already owns the job's state files, so the
wrapper leaves them alone. A manual run has no runner above it, so it passes
``--update-job-state`` and the wrapper writes ``last_attempt`` /
``last_success`` itself -- otherwise a pull the user just did by hand would not
count toward the schedule and the scheduled run would repeat it minutes later.

Standard library only, and no import of the app's own package: cron restores a
workspace environment but not this app's tool environment, and a routine must
not stop running because an app's virtualenv is mid-reinstall.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]

DEFAULT_DATA_DIR: Final[Path] = REPO_ROOT / "data" / ".apps" / "routines"
DEFAULT_JOB_STATE_DIR: Final[Path] = REPO_ROOT / "data" / ".state" / "jobs"

# Keep the tail of the output: a traceback ends at the bottom, and that is the
# part worth showing inline on a failed run.
MAX_CAPTURED_OUTPUT_CHARS: Final[int] = 4000

TRUNCATION_NOTICE: Final[str] = "... (earlier output trimmed; see the routine's log)\n"

# A run that has started but not finished leaves a marker here, named for the
# job, and removes it on the way out. A ledger record is only written when a run
# COMPLETES, so without this there is nothing on disk saying a run is under way
# -- and a job that takes ten minutes looks, to the page, exactly like a job
# that never started. The marker carries this process's pid so a reader can tell
# a live run from one whose process was killed before it could clean up.
RUNNING_DIRNAME: Final[str] = "running"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a routine's command and record the run.",
        allow_abbrev=False,
    )
    parser.add_argument("--job", required=True, help="the routine's id")
    parser.add_argument(
        "--trigger",
        required=True,
        choices=("scheduled", "manual"),
        help="what caused this run",
    )
    parser.add_argument(
        "--update-job-state",
        action="store_true",
        help="write last_attempt/last_success too (for a run with no run_job.sh above it)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given (put it after --)")

    data_dir = Path(os.environ.get("ROUTINES_DATA_DIR", str(DEFAULT_DATA_DIR)))
    job_state_dir = Path(os.environ.get("ROUTINES_JOB_STATE_DIR", str(DEFAULT_JOB_STATE_DIR)))

    started_at = datetime.now()
    if args.update_job_state:
        _write_state(job_state_dir / args.job, "last_attempt", str(int(time.time())))

    _mark_running(data_dir, args.job, args.trigger, started_at, command)
    try:
        exit_code, output = _run_capturing(command)
    finally:
        # try/finally rather than a plain call after the run: a crash or a
        # Ctrl-C here must not leave the page claiming a run is still going.
        _clear_running(data_dir, args.job)
    finished_at = datetime.now()

    if args.update_job_state:
        if exit_code == 0:
            _write_state(job_state_dir / args.job, "last_success", str(int(time.time())))
            _clear_state(job_state_dir / args.job, "failures")
        else:
            previous = _read_int(job_state_dir / args.job / "failures")
            _write_state(job_state_dir / args.job, "failures", str(previous + 1))

    _append_record(
        data_dir,
        {
            "routine_id": args.job,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "trigger": args.trigger,
            "exit_code": exit_code,
            "output": output,
            "command": " ".join(command),
        },
    )
    return exit_code


def _run_capturing(command: list[str]) -> tuple[int, str]:
    """Run the command, echoing its output to this process's stdout (so the
    routine's own log file still gets everything) while keeping a bounded tail
    for the ledger."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        message = f"could not start the command: {error}\n"
        _warn(message.rstrip())
        return 127, message

    combined = (completed.stdout or "") + (completed.stderr or "")
    sys.stdout.write(completed.stdout or "")
    sys.stderr.write(completed.stderr or "")
    sys.stdout.flush()
    sys.stderr.flush()

    if len(combined) > MAX_CAPTURED_OUTPUT_CHARS:
        combined = TRUNCATION_NOTICE + combined[-MAX_CAPTURED_OUTPUT_CHARS:]
    return completed.returncode, combined


def _append_record(data_dir: Path, record: dict[str, object]) -> None:
    """Best effort: a ledger that cannot be written must never turn a working
    routine into a failing one."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as error:
        _warn(f"could not write the run log: {error}")


def _mark_running(
    data_dir: Path, job: str, trigger: str, started_at: datetime, command: list[str]
) -> None:
    """Best effort, like the ledger write: a marker that cannot be written must
    never stop the routine from running."""
    try:
        directory = data_dir / RUNNING_DIRNAME
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{job}.json").write_text(
            json.dumps(
                {
                    "routine_id": job,
                    "started_at": started_at.isoformat(),
                    "trigger": trigger,
                    "pid": os.getpid(),
                    "command": " ".join(command),
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        _warn(f"could not mark the run as started: {error}")


def _clear_running(data_dir: Path, job: str) -> None:
    try:
        (data_dir / RUNNING_DIRNAME / f"{job}.json").unlink(missing_ok=True)
    except OSError as error:
        _warn(f"could not clear the run marker: {error}")


def _write_state(directory: Path, name: str, value: str) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(value + "\n", encoding="utf-8")
    except OSError as error:
        _warn(f"could not write {name}: {error}")


def _warn(message: str) -> None:
    """The one diagnostic channel this wrapper has. It runs from cron with no app
    environment, so there is no logger to reach for -- and its stderr is the
    routine's own log file, which is where such a note belongs anyway.
    """
    sys.stderr.write(f"record_run: {message}\n")


def _clear_state(directory: Path, name: str) -> None:
    try:
        (directory / name).unlink(missing_ok=True)
    except OSError:
        pass


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
