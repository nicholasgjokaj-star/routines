"""Tests for the run log -- the record that answers "automatic or manual?".

The recorder is exercised as a real subprocess rather than mocked, because the
property that matters is end-to-end: a run through the wrapper must produce a
readable record AND leave the command's own exit code untouched.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from routines.ledger import RunRecord
from routines.ledger import append_run
from routines.ledger import latest_run
from routines.ledger import read_in_flight
from routines.ledger import read_runs
from routines.ledger import reap_interrupted
from routines.ledger import running_path

_RECORDER = Path(__file__).resolve().parents[2] / "bin" / "record_run.py"


def _record(routine_id: str, trigger: str, started: datetime) -> RunRecord:
    return RunRecord(
        routine_id=routine_id,
        started_at=started,
        finished_at=started,
        trigger=trigger,
        exit_code=0,
        output="",
        command="bash run.sh",
    )


def test_runs_come_back_newest_first(tmp_path: Path) -> None:
    append_run(_record("news", "scheduled", datetime(2026, 9, 1, 9, 0)), tmp_path)
    append_run(_record("news", "manual", datetime(2026, 9, 3, 9, 0)), tmp_path)
    append_run(_record("news", "scheduled", datetime(2026, 9, 2, 9, 0)), tmp_path)

    runs = read_runs("news", data_dir=tmp_path)

    assert [run.started_at.day for run in runs] == [3, 2, 1]
    assert latest_run("news", tmp_path) is not None
    assert latest_run("news", tmp_path).trigger == "manual"


def test_runs_are_kept_apart_by_routine(tmp_path: Path) -> None:
    append_run(_record("news", "scheduled", datetime(2026, 9, 1, 9, 0)), tmp_path)
    append_run(_record("weather", "manual", datetime(2026, 9, 2, 9, 0)), tmp_path)

    assert len(read_runs("news", data_dir=tmp_path)) == 1
    assert latest_run("weather", tmp_path).trigger == "manual"


def test_a_half_written_line_does_not_blank_the_history(tmp_path: Path) -> None:
    """A writer killed mid-line must cost one record, not the whole view."""
    append_run(_record("news", "scheduled", datetime(2026, 9, 1, 9, 0)), tmp_path)
    with (tmp_path / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"routine_id": "news", "started_at": "2026-')

    assert len(read_runs("news", data_dir=tmp_path)) == 1


def test_no_log_yet_is_an_empty_history_not_an_error(tmp_path: Path) -> None:
    assert read_runs("news", data_dir=tmp_path) == []
    assert latest_run("news", tmp_path) is None


def test_the_recorder_logs_a_successful_run_and_keeps_its_exit_code(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_RECORDER),
            "--job",
            "news",
            "--trigger",
            "scheduled",
            "--",
            "bash",
            "-c",
            "echo pulled 3 items",
        ],
        capture_output=True,
        text=True,
        env={"ROUTINES_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert result.returncode == 0
    # The command's own output still reaches the routine's log.
    assert "pulled 3 items" in result.stdout

    runs = read_runs("news", data_dir=tmp_path)
    assert len(runs) == 1
    assert runs[0].trigger == "scheduled"
    assert runs[0].did_succeed
    assert "pulled 3 items" in runs[0].output


def test_the_recorder_passes_a_failure_through_unchanged(tmp_path: Path) -> None:
    """run_job.sh decides whether to retry from the exit code, so the wrapper
    must be transparent -- swallowing a failure would silently break catch-up."""
    result = subprocess.run(
        [
            sys.executable,
            str(_RECORDER),
            "--job",
            "news",
            "--trigger",
            "scheduled",
            "--",
            "bash",
            "-c",
            "echo broke >&2; exit 3",
        ],
        capture_output=True,
        text=True,
        env={"ROUTINES_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert result.returncode == 3

    runs = read_runs("news", data_dir=tmp_path)
    assert not runs[0].did_succeed
    assert runs[0].exit_code == 3
    assert "broke" in runs[0].output


def test_a_manual_run_counts_toward_the_schedule(tmp_path: Path) -> None:
    """With no run_job.sh above it, the recorder writes the completion itself --
    otherwise the scheduled run would repeat the pull minutes later."""
    state_dir = tmp_path / "jobs"
    subprocess.run(
        [
            sys.executable,
            str(_RECORDER),
            "--job",
            "news",
            "--trigger",
            "manual",
            "--update-job-state",
            "--",
            "bash",
            "-c",
            "true",
        ],
        capture_output=True,
        text=True,
        env={
            "ROUTINES_DATA_DIR": str(tmp_path),
            "ROUTINES_JOB_STATE_DIR": str(state_dir),
            "PATH": "/usr/bin:/bin",
        },
        check=False,
    )

    assert (state_dir / "news" / "last_success").exists()
    assert (state_dir / "news" / "last_attempt").exists()
    assert not (state_dir / "news" / "failures").exists()


def test_a_failed_manual_run_counts_the_failure(tmp_path: Path) -> None:
    state_dir = tmp_path / "jobs"
    for _ in range(2):
        subprocess.run(
            [
                sys.executable,
                str(_RECORDER),
                "--job",
                "news",
                "--trigger",
                "manual",
                "--update-job-state",
                "--",
                "bash",
                "-c",
                "exit 1",
            ],
            capture_output=True,
            text=True,
            env={
                "ROUTINES_DATA_DIR": str(tmp_path),
                "ROUTINES_JOB_STATE_DIR": str(state_dir),
                "PATH": "/usr/bin:/bin",
            },
            check=False,
        )

    assert (state_dir / "news" / "failures").read_text(encoding="utf-8").strip() == "2"
    assert not (state_dir / "news" / "last_success").exists()


def test_a_scheduled_run_leaves_job_state_to_the_runner(tmp_path: Path) -> None:
    """Under run_job.sh the runner owns those files; writing them here too would
    be two writers racing on the same completion."""
    state_dir = tmp_path / "jobs"
    subprocess.run(
        [
            sys.executable,
            str(_RECORDER),
            "--job",
            "news",
            "--trigger",
            "scheduled",
            "--",
            "bash",
            "-c",
            "true",
        ],
        capture_output=True,
        text=True,
        env={
            "ROUTINES_DATA_DIR": str(tmp_path),
            "ROUTINES_JOB_STATE_DIR": str(state_dir),
            "PATH": "/usr/bin:/bin",
        },
        check=False,
    )

    assert not (state_dir / "news").exists()


def test_a_huge_output_is_trimmed_but_keeps_its_tail(tmp_path: Path) -> None:
    """A traceback ends at the bottom, so the tail is the part worth keeping."""
    subprocess.run(
        [
            sys.executable,
            str(_RECORDER),
            "--job",
            "news",
            "--trigger",
            "manual",
            "--",
            "bash",
            "-c",
            "for i in $(seq 1 3000); do echo 'noise line'; done; echo THE_LAST_LINE",
        ],
        capture_output=True,
        text=True,
        env={"ROUTINES_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )

    output = read_runs("news", data_dir=tmp_path)[0].output
    assert "THE_LAST_LINE" in output
    assert len(output) < 6000


def _recorder_command(job: str, trigger: str, shell: str) -> list[str]:
    return [
        sys.executable,
        str(_RECORDER),
        "--job",
        job,
        "--trigger",
        trigger,
        "--",
        "bash",
        "-c",
        shell,
    ]


def _start_and_wait_until_going(
    job: str, data_dir: Path, tmp_path: Path, shell: str = "sleep 30"
) -> subprocess.Popen:
    """Start a run and return once its command is genuinely under way.

    The command opens a pipe the test reads, and reading it blocks until the
    command runs -- no polling, and no sleep standing in for a guarantee. The
    marker is written before the command starts, so by the time this returns it
    is certainly on disk.
    """
    ready = tmp_path / "started.fifo"
    if not ready.exists():
        os.mkfifo(ready)
    process = subprocess.Popen(
        _recorder_command(job, "manual", f"echo going > {ready}; {shell}"),
        env={"ROUTINES_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with ready.open("r", encoding="utf-8") as handle:
        handle.readline()
    return process


def _write_marker(job: str, data_dir: Path, pid: int, trigger: str = "manual") -> Path:
    path = running_path(job, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "routine_id": job,
                "started_at": datetime(2026, 9, 1, 9, 0).isoformat(),
                "trigger": trigger,
                "pid": pid,
                "command": "bash run.sh",
            }
        ),
        encoding="utf-8",
    )
    return path


def _a_pid_that_is_gone() -> int:
    """A pid that has certainly exited, so nothing can be confused for it."""
    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def test_a_run_is_visible_the_whole_time_it_is_going(tmp_path: Path) -> None:
    """The point of the marker: a record only exists once a run FINISHES, so a
    long job would otherwise look exactly like one that never started."""
    running = _start_and_wait_until_going("news", tmp_path, tmp_path)
    try:
        in_flight = read_in_flight("news", tmp_path)
        assert in_flight is not None
        assert in_flight.trigger == "manual"
        assert read_runs("news", data_dir=tmp_path) == []

        # A live run is left strictly alone: not reported as stopped, not reaped.
        assert reap_interrupted(tmp_path) == []
        assert running_path("news", tmp_path).exists()
    finally:
        running.terminate()
        running.wait(timeout=10)


def test_a_finished_run_stops_saying_it_is_going(tmp_path: Path) -> None:
    subprocess.run(
        _recorder_command("news", "manual", "true"),
        env={"ROUTINES_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )

    assert read_in_flight("news", tmp_path) is None
    assert not running_path("news", tmp_path).exists()
    assert len(read_runs("news", data_dir=tmp_path)) == 1


def test_a_crash_still_clears_the_marker(tmp_path: Path) -> None:
    """The wrapper tidies up in a finally, so only a killed process can leave a
    marker behind -- which is what makes a leftover one mean something."""
    subprocess.run(
        _recorder_command("news", "manual", "exit 9"),
        env={"ROUTINES_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )

    assert not running_path("news", tmp_path).exists()


def test_a_marker_from_a_vanished_process_is_never_called_running(tmp_path: Path) -> None:
    """Saying a routine is running when it is not is the one thing this app
    exists not to do."""
    _write_marker("news", tmp_path, pid=_a_pid_that_is_gone())

    assert read_in_flight("news", tmp_path) is None


def test_a_reused_pid_is_not_mistaken_for_the_run(tmp_path: Path) -> None:
    """This test's own process is alive, but it is not that routine's run."""
    _write_marker("news", tmp_path, pid=os.getpid())

    assert read_in_flight("news", tmp_path) is None


def test_a_cut_off_run_is_recorded_as_stopped_early(tmp_path: Path) -> None:
    """The workspace restarting mid-run used to leave no trace at all, which is
    what made a pressed button look ignored."""
    _write_marker("news", tmp_path, pid=_a_pid_that_is_gone(), trigger="manual")

    assert reap_interrupted(tmp_path) == ["news"]

    runs = read_runs("news", data_dir=tmp_path)
    assert len(runs) == 1
    assert runs[0].was_interrupted
    assert not runs[0].did_succeed
    assert runs[0].trigger == "manual"
    assert runs[0].started_at == datetime(2026, 9, 1, 9, 0)
    # The marker is gone, so the same run is never recorded twice.
    assert not running_path("news", tmp_path).exists()
    assert reap_interrupted(tmp_path) == []
    assert len(read_runs("news", data_dir=tmp_path)) == 1


def test_a_killed_run_is_reaped_end_to_end(tmp_path: Path) -> None:
    """The real sequence: a run is going, its process is killed outright, and the
    history says what became of it."""
    running = _start_and_wait_until_going("news", tmp_path, tmp_path)
    running.kill()
    running.wait(timeout=10)

    assert read_in_flight("news", tmp_path) is None
    assert reap_interrupted(tmp_path) == ["news"]
    assert read_runs("news", data_dir=tmp_path)[0].was_interrupted


def test_an_unreadable_marker_is_cleared_without_inventing_a_run(tmp_path: Path) -> None:
    path = running_path("news", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert reap_interrupted(tmp_path) == []
    assert not path.exists()
    assert read_runs("news", data_dir=tmp_path) == []


def test_nothing_running_is_not_an_error(tmp_path: Path) -> None:
    assert reap_interrupted(tmp_path) == []
    assert read_in_flight("news", tmp_path) is None


def test_each_record_is_one_self_contained_line(tmp_path: Path) -> None:
    """Append-only JSON Lines is what lets two routines write concurrently."""
    append_run(_record("news", "scheduled", datetime(2026, 9, 1, 9, 0)), tmp_path)
    append_run(_record("weather", "manual", datetime(2026, 9, 1, 9, 1)), tmp_path)

    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().split("\n")

    assert len(lines) == 2
    assert [json.loads(line)["routine_id"] for line in lines] == ["news", "weather"]
