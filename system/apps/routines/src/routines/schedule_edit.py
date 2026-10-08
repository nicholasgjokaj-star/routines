"""Pausing, resuming, and re-timing a routine by editing its cron entry.

Both controls change the same one thing -- the routine's line in its cron entry
file -- so they share one careful write path:

- **The durable copy is written first, then installed live.** ``/etc/cron.d``
  sits on the container rootfs and is rebuilt from ``data/.state/cron.d`` at
  every boot, so writing only the live copy would silently undo itself the next
  time the workspace restarts. This is the order the `manage-scheduled-tasks`
  skill specifies.
- **Only the routine's own line is touched.** The file is rewritten line by
  line with every comment, blank line, and unrelated entry preserved
  byte-for-byte, because those comments are usually the only record of why the
  job exists.
- **Pausing is commenting out, not deleting.** That is the documented way to
  stop a job without losing its definition, and it is what makes resuming
  restore the exact line that was running before.

Some routines are not the user's to drive: the update-recovery guard exists to
repair a workspace mid-update, so the catalog marks it locked and every
operation here refuses it.
"""

import re
import shutil
import tempfile
from pathlib import Path
from typing import Final

from loguru import logger

from routines.discovery import ScheduledRoutine
from routines.schedules import parse_duration_seconds

LIVE_CRON_DIR: Final[Path] = Path("/etc/cron.d")
DURABLE_CRON_DIR: Final[Path] = Path("data/.state/cron.d")

# The mode cron requires of a drop-in; a file it considers world-writable is ignored.
CRON_ENTRY_MODE: Final[int] = 0o644

# A cadence is a whole number and one of these units, which is exactly the
# vocabulary run_job.sh's --every accepts. The UI offers a number box and this
# list rather than free text, so any cadence is expressible but an unparseable
# string -- a schedule that silently never fires -- is not.
CADENCE_UNITS: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("m", "minutes", "minute", 60),
    ("h", "hours", "hour", 3600),
    ("d", "days", "day", 86400),
)

# Floor: the cron line that drives run_job.sh ticks once a minute, so a shorter
# gap cannot be honored and would only read as a lie on the page. Ceiling: past
# a year a "schedule" is really a one-off, and run_job.sh's catch-up would fire
# it on the next boot regardless.
MIN_CADENCE_SECONDS: Final[int] = 60
MAX_CADENCE_SECONDS: Final[int] = 366 * 86400

_UNIT_SECONDS: Final[dict[str, int]] = {unit: seconds for unit, _, _, seconds in CADENCE_UNITS}

# Below a day, "at what hour" has no meaning -- run_job.sh would hold the job
# until that hour and then not run it again until the next one.
MIN_SECONDS_FOR_DUE_HOUR: Final[int] = 86400

_EVERY_PATTERN: Final[re.Pattern[str]] = re.compile(r"(--every)\s+(\S+)")
_AT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s--at\s+\S+")

# One field of a cron schedule: `*`, `*/5`, `30`, `1-5`, `0,30`, or a name like
# `mon`. Deliberately permissive about what is *inside* a field and strict about
# the shape, since its only job is to tell a schedule apart from English.
_CRON_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\d*]([\d*/,\-]*[\d*])?|[a-z]{3}")


class ScheduleEditError(RuntimeError):
    """Raised when a routine's schedule could not be changed."""


def check_editable(routine: ScheduledRoutine) -> None:
    """Raise unless this routine is the user's to change."""
    if routine.catalog is not None and routine.catalog.locked:
        raise ScheduleEditError(
            "This one keeps the workspace working and cannot be changed from here."
        )
    if not routine.source_file.exists():
        raise ScheduleEditError("Its schedule entry is missing, so there is nothing to change.")


def set_paused(routine: ScheduledRoutine, should_pause: bool) -> None:
    """Comment the routine's line out, or put it back."""
    check_editable(routine)
    if should_pause == routine.is_paused:
        return
    _rewrite(routine, lambda line: _commented(line) if should_pause else _uncommented(line))


def build_cadence(count: object, unit: object) -> str:
    """A typed-in number and unit -> the ``20m`` / ``3h`` / ``2d`` token a cron
    entry is written in. Raises with wording the page can show as-is."""
    unit_token = str(unit).strip()
    if unit_token not in _UNIT_SECONDS:
        raise ScheduleEditError("Pick minutes, hours or days.")
    try:
        number = int(str(count).strip())
    except (TypeError, ValueError):
        raise ScheduleEditError("How often has to be a whole number.") from None
    if number < 1:
        raise ScheduleEditError("How often has to be at least 1.")
    total = number * _UNIT_SECONDS[unit_token]
    if total > MAX_CADENCE_SECONDS:
        raise ScheduleEditError("A gap longer than a year is too long to schedule.")
    return f"{number}{unit_token}"


def set_cadence(routine: ScheduledRoutine, every: str, at_hour: int | None) -> None:
    """Change how often the routine runs, and (for daily or slower) at what hour."""
    check_editable(routine)
    if routine.job_schedule is None:
        raise ScheduleEditError(
            "This one runs on a fixed clock schedule, which has to be changed by hand."
        )

    every_seconds = parse_duration_seconds(every)
    if every_seconds is None:
        raise ScheduleEditError(f"{every!r} is not a cadence this understands.")
    if every_seconds < MIN_CADENCE_SECONDS:
        raise ScheduleEditError("The shortest gap this can keep to is one minute.")
    if every_seconds > MAX_CADENCE_SECONDS:
        raise ScheduleEditError("A gap longer than a year is too long to schedule.")
    if at_hour is not None and not 0 <= at_hour <= 23:
        raise ScheduleEditError("The hour has to be between 0 and 23.")
    if at_hour is not None and every_seconds < MIN_SECONDS_FOR_DUE_HOUR:
        # run_job.sh would gate every run on that hour, so a sub-daily job with
        # a due hour effectively becomes a daily one.
        raise ScheduleEditError("A time of day only applies to routines that run daily or less.")

    _rewrite(routine, lambda line: _with_cadence(line, every, at_hour))


def _rewrite(routine: ScheduledRoutine, transform) -> None:
    """Apply ``transform`` to the routine's own line in both copies of its entry."""
    source = routine.source_file
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as error:
        raise ScheduleEditError(f"Could not read its schedule entry: {error}") from error

    updated, changed = _transform_matching_line(original, routine.id, transform)
    if not changed:
        raise ScheduleEditError("Could not find its line in the schedule entry.")

    # Durable first: /etc/cron.d is rebuilt from data/.state/cron.d at boot, so
    # a live-only write is undone by the next restart.
    durable = DURABLE_CRON_DIR / source.name
    if durable.exists() or source.parent == LIVE_CRON_DIR:
        _write_entry(durable, updated)
    _write_entry(LIVE_CRON_DIR / source.name, updated)
    logger.info("rewrote the schedule entry for {}", routine.id)


def _transform_matching_line(text: str, routine_id: str, transform) -> tuple[str, bool]:
    """Rewrite only the line that runs ``routine_id``; everything else is kept
    exactly as written, comments included."""
    lines = text.split("\n")
    changed = False
    for index, line in enumerate(lines):
        if not _line_runs(line, routine_id):
            continue
        rewritten = transform(line)
        if rewritten != line:
            lines[index] = rewritten
            changed = True
        else:
            # Already in the requested shape; the caller still succeeded.
            changed = True
    return "\n".join(lines), changed


def _line_runs(line: str, routine_id: str) -> bool:
    """Whether this line is the cron entry for the given routine.

    Matches the job id as a whole token so ``news`` never matches
    ``news-digest``, and accepts the line whether or not it is commented out --
    a paused routine is a commented line, and resuming has to find it.

    The prose comments above an entry are the usual reason a job is
    understandable at all, and they are also commented lines, so a candidate
    must additionally *look* like a cron entry before it is touched. Without
    that check, resuming a paused routine uncomments its documentation into
    lines cron cannot parse.
    """
    candidate = line.lstrip().lstrip("#").strip()
    if not candidate:
        return False
    tokens = candidate.split()
    if len(tokens) < 7 or not _is_cron_schedule(tokens[:5]):
        return False
    for index, token in enumerate(tokens):
        if token.endswith("run_job.sh") and index + 1 < len(tokens):
            return tokens[index + 1] == routine_id
    # A plain cron line carries no job id; the entry file's name identifies it,
    # so the one command line in it is the one to change.
    return True


def _is_cron_schedule(fields: list[str]) -> bool:
    """Whether these five tokens are a cron schedule rather than prose."""
    return len(fields) == 5 and all(_CRON_FIELD_PATTERN.fullmatch(field) for field in fields)


def _commented(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return line
    indent = line[: len(line) - len(stripped)]
    return f"{indent}#{stripped}"


def _uncommented(line: str) -> str:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return line
    indent = line[: len(line) - len(stripped)]
    return indent + stripped.lstrip("#").lstrip()


def _with_cadence(line: str, every: str, at_hour: int | None) -> str:
    """Replace ``--every`` (and ``--at``) in place, leaving the rest of the
    command -- including the run recorder wrapper -- untouched."""
    updated = _EVERY_PATTERN.sub(rf"\1 {every}", line, count=1)
    updated = _AT_PATTERN.sub("", updated)
    if at_hour is not None:
        updated = _EVERY_PATTERN.sub(rf"\1 {every} --at {at_hour}", updated, count=1)
    return updated


def _write_entry(path: Path, text: str) -> None:
    """Write the entry atomically, at the mode cron requires.

    Atomic because cron rescans the directory continuously: a half-written file
    caught mid-scan is a schedule cron cannot parse.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        temporary.chmod(CRON_ENTRY_MODE)
        shutil.move(str(temporary), str(path))
    except OSError as error:
        raise ScheduleEditError(f"Could not save the change: {error}") from error
