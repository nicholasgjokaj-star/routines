"""The run log: one durable record per routine run, with what triggered it.

``run_job.sh`` tracks only two timestamps and a failure count, which cannot
answer "was that last pull automatic or did someone press the button?" -- so
every run is also wrapped by ``bin/record_run.py``, which appends a line here.
Both paths go through that same wrapper, which is what makes the two
comparable: the cron entry passes ``--trigger scheduled``, the app's Run now
button passes ``--trigger manual``.

The file is append-only JSON Lines, one object per run, and it keeps the run's
captured output alongside the verdict -- so a failure can be read here rather
than reconstructed from a service log that may already have rotated away.

A record is written when a run FINISHES, which leaves a gap this module also
closes: while a run is under way the same wrapper keeps a marker under
``running/``, and ``read_in_flight`` reports it -- but only after confirming the
process it names is still alive, so a killed run is never shown as going.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel

Trigger = Literal["scheduled", "manual", "unknown"]

DATA_DIR: Final[Path] = Path(os.environ.get("ROUTINES_DATA_DIR", "data/.apps/routines"))

RUNS_FILENAME: Final[str] = "runs.jsonl"

# Where bin/record_run.py keeps its one marker per in-flight run.
RUNNING_DIRNAME: Final[str] = "running"

# What the marker's process must still look like for the run to count as going.
# Checking the pid alone would believe any process that happened to inherit that
# number after the run died; requiring the wrapper and the job id in its command
# line makes a false "still running" effectively impossible.
_RUNNER_MARKER: Final[str] = "record_run.py"

# Stands in for a run that never reported one, since it was stopped before it
# could. Distinct from every real signal death (which arrive as small negatives)
# so it can never be mistaken for one.
INTERRUPTED_EXIT_CODE: Final[int] = -1000

INTERRUPTED_NOTE: Final[str] = (
    "This run was stopped before it finished -- most often because the workspace"
    " restarted while it was going. Nothing was recorded about how far it got.\n"
)

# Per-run output kept in the ledger. Enough to diagnose a failure inline;
# the run's full output stays in the routine's own log file, which the app
# links to.
MAX_CAPTURED_OUTPUT_CHARS: Final[int] = 4000

# How many runs the app reads back for one routine. The file is append-only and
# small (a handful of lines a day), so this is a display bound, not a cap on
# what is kept.
DEFAULT_HISTORY_LIMIT: Final[int] = 25


class RunRecord(FrozenModel):
    routine_id: str
    started_at: datetime
    finished_at: datetime
    trigger: Trigger
    exit_code: int
    output: str
    command: str
    # A run that never got to finish: its process was gone before it could write
    # its own record (the workspace restarted mid-run, or it was killed). Worth
    # distinguishing from a failure -- the job did not go wrong, it was stopped
    # -- and worth recording at all, because the alternative is the run leaving
    # no trace and the button looking like it did nothing.
    was_interrupted: bool = False

    @property
    def did_succeed(self) -> bool:
        return self.exit_code == 0 and not self.was_interrupted

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    def as_json(self) -> dict[str, Any]:
        return {
            "routine_id": self.routine_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "trigger": self.trigger,
            "exit_code": self.exit_code,
            "output": self.output,
            "command": self.command,
            "was_interrupted": self.was_interrupted,
        }


class InFlightRun(FrozenModel):
    """A run that has started and not yet finished."""

    routine_id: str
    started_at: datetime
    trigger: Trigger
    pid: int
    command: str = ""


def runs_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / RUNS_FILENAME


def running_path(routine_id: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / RUNNING_DIRNAME / f"{routine_id}.json"


def read_in_flight(routine_id: str, data_dir: Path = DATA_DIR) -> InFlightRun | None:
    """The run currently under way for this routine, or None.

    A marker whose process is gone is never reported as running: saying a
    routine is going when it is not is the one thing this app exists not to do.
    Turning that dead marker into a record is ``reap_interrupted``'s job.
    """
    marker = _read_marker(running_path(routine_id, data_dir))
    if marker is None or not _is_still_running(marker):
        return None
    return marker


def reap_interrupted(data_dir: Path = DATA_DIR) -> list[str]:
    """Close the books on runs whose process died before they could.

    ``record_run.py`` clears its own marker on the way out, even on a crash, so
    a marker left behind by a vanished process means that run was cut off --
    almost always the workspace restarting mid-run. Each one becomes a real
    history entry saying so, because the alternative is what makes a pressed
    button look ignored: the run leaves no trace at all.

    Returns the ids it closed off.
    """
    directory = data_dir / RUNNING_DIRNAME
    try:
        markers = sorted(directory.glob("*.json"))
    except OSError:
        return []

    reaped: list[str] = []
    for path in markers:
        marker = _read_marker(path)
        if marker is not None and _is_still_running(marker):
            continue
        if marker is not None:
            # When it actually died is not knowable -- nothing was there to
            # write it down -- so the duration is left unstated rather than
            # guessed at from when this noticed.
            append_run(
                RunRecord(
                    routine_id=marker.routine_id,
                    started_at=marker.started_at,
                    finished_at=datetime.now(),
                    trigger=marker.trigger,
                    exit_code=INTERRUPTED_EXIT_CODE,
                    output=INTERRUPTED_NOTE,
                    command=marker.command,
                    was_interrupted=True,
                ),
                data_dir=data_dir,
            )
            reaped.append(marker.routine_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return reaped


def _read_marker(path: Path) -> InFlightRun | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return InFlightRun(
            routine_id=raw["routine_id"],
            started_at=datetime.fromisoformat(raw["started_at"]),
            trigger=raw.get("trigger", "unknown"),
            pid=int(raw["pid"]),
            command=raw.get("command", ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_still_running(marker: InFlightRun) -> bool:
    try:
        command_line = Path(f"/proc/{marker.pid}/cmdline").read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return False
    return _RUNNER_MARKER in command_line and marker.routine_id in command_line


def append_run(record: RunRecord, data_dir: Path = DATA_DIR) -> None:
    """Append one run. Opened in append mode per call so concurrent writers
    (a cron run and a manual run of two different routines) cannot interleave
    a partial line."""
    data_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.as_json(), separators=(",", ":"))
    with runs_path(data_dir).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_runs(
    routine_id: str | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    data_dir: Path = DATA_DIR,
) -> list[RunRecord]:
    """The most recent runs first. A malformed line is skipped rather than
    raising: a half-written line from a killed writer must not blank the view."""
    path = runs_path(data_dir)
    if not path.exists():
        return []

    records: list[RunRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = _parse_line(line)
            if record is None:
                continue
            if routine_id is not None and record.routine_id != routine_id:
                continue
            records.append(record)

    records.sort(key=lambda record: record.started_at, reverse=True)
    return records[:limit]


def latest_run(routine_id: str, data_dir: Path = DATA_DIR) -> RunRecord | None:
    runs = read_runs(routine_id, limit=1, data_dir=data_dir)
    return runs[0] if runs else None


def _parse_line(line: str) -> RunRecord | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
        return RunRecord(
            routine_id=raw["routine_id"],
            started_at=datetime.fromisoformat(raw["started_at"]),
            finished_at=datetime.fromisoformat(raw["finished_at"]),
            trigger=raw.get("trigger", "unknown"),
            exit_code=int(raw["exit_code"]),
            output=raw.get("output", ""),
            command=raw.get("command", ""),
            was_interrupted=bool(raw.get("was_interrupted", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
