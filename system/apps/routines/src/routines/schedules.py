"""Parsing and describing the schedules that drive this workspace's routines.

Two shapes of schedule exist, and both are read from their source of truth on
every request rather than cached here -- that is what makes a routine whose
cadence someone changed show its new cadence without this app being touched:

- A ``run_job.sh`` job. Its cron line fires every minute; ``run_job.sh`` owns
  the real cadence via ``--every`` (and optionally ``--at``), catches up after
  downtime, and retries a run that died. This is what the
  ``manage-scheduled-tasks`` skill writes for anything that must not be missed.
- A plain cron line, whose five leading fields are the whole schedule.

The next-run arithmetic below deliberately mirrors ``run_job.sh``'s own
semantics (see its header comment): a window is covered only by a *completed*
run, so ``last_success`` -- not ``last_attempt`` -- is what advances the clock.
"""

import re
from datetime import datetime
from datetime import timedelta
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel

_SECONDS_PER_DAY: Final[int] = 86400

# run_job.sh's own default gap before it retries a run that failed or was killed.
_DEFAULT_RETRY_SECONDS: Final[int] = 120

_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\d+)([mhd])$")

_DURATION_UNIT_SECONDS: Final[dict[str, int]] = {"m": 60, "h": 3600, "d": _SECONDS_PER_DAY}

_WEEKDAY_NAMES: Final[tuple[str, ...]] = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def parse_duration_seconds(text: str) -> int | None:
    """``15m`` / ``3h`` / ``7d`` -> seconds. None for anything run_job.sh would reject."""
    match = _DURATION_PATTERN.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2)]


class JobSchedule(FrozenModel):
    """A ``run_job.sh`` cadence: run once per ``every_seconds``, optionally gated
    to a local hour."""

    every_seconds: int
    at_hour: int | None
    retry_after_seconds: int = _DEFAULT_RETRY_SECONDS


class CronSchedule(FrozenModel):
    """The five leading fields of a plain cron line, kept verbatim."""

    fields: str


def describe_every(seconds: int) -> str:
    """A cadence in the words a person would use for it."""
    if seconds % _SECONDS_PER_DAY == 0:
        days = seconds // _SECONDS_PER_DAY
        if days == 1:
            return "Every day"
        if days == 7:
            return "Every week"
        return f"Every {days} days"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    minutes = max(1, seconds // 60)
    return "Every minute" if minutes == 1 else f"Every {minutes} minutes"


def format_hour(hour: int) -> str:
    """``7`` -> ``7:00 am``, in the 12-hour form the rest of the view uses."""
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:00 {suffix}"


def describe_job_schedule(schedule: JobSchedule) -> str:
    base = describe_every(schedule.every_seconds)
    if schedule.at_hour is None:
        return base
    return f"{base} at {format_hour(schedule.at_hour)}"


def describe_cron_schedule(schedule: CronSchedule) -> str:
    """Plain-English rendering of the common cron shapes, falling back to the
    raw fields for anything exotic rather than guessing at it."""
    parts = schedule.fields.split()
    if len(parts) != 5:
        return schedule.fields
    minute, hour, day_of_month, month, day_of_week = parts

    if minute == "*" and hour == "*":
        return "Every minute"
    # `*/N` in the minute or hour field is the common "every N" shorthand.
    if minute.startswith("*/") and hour == "*":
        step = minute[2:]
        return f"Every {step} minutes" if step.isdigit() else schedule.fields
    if hour.startswith("*/") and minute.isdigit():
        step = hour[2:]
        return f"Every {step} hours" if step.isdigit() else schedule.fields
    if hour == "*" and minute.isdigit():
        return "Every hour"
    if not (minute.isdigit() and hour.isdigit()):
        return schedule.fields

    time_of_day = f"{int(hour) % 12 or 12}:{int(minute):02d} {'am' if int(hour) < 12 else 'pm'}"
    if day_of_week.isdigit():
        return f"Every {_WEEKDAY_NAMES[int(day_of_week) % 7]} at {time_of_day}"
    if day_of_month.isdigit():
        return f"Monthly on the {int(day_of_month)}th at {time_of_day}"
    if day_of_month == "*" and month == "*" and day_of_week == "*":
        return f"Every day at {time_of_day}"
    return schedule.fields


def next_run_at(
    schedule: JobSchedule,
    last_success: datetime | None,
    last_attempt: datetime | None,
    now: datetime,
) -> datetime | None:
    """When ``run_job.sh`` will next actually run this job, or None if it is
    already overdue and will fire on the next tick.

    Mirrors run_job.sh: the window is measured from the last *completed* run, a
    started-but-unfinished run is retried after ``retry_after_seconds``, and the
    ``--at`` gate is skipped once a whole extra day has been missed.
    """
    if last_success is None:
        due = now
    else:
        due = last_success + timedelta(seconds=schedule.every_seconds)

    # A run that started and never completed leaves the window open; the retry
    # gate is the floor on when the next attempt may begin.
    if last_attempt is not None and (last_success is None or last_attempt > last_success):
        retry_floor = last_attempt + timedelta(seconds=schedule.retry_after_seconds)
        due = max(due, retry_floor)

    if schedule.at_hour is not None:
        overdue_cutoff = schedule.every_seconds + _SECONDS_PER_DAY
        is_very_overdue = (
            last_success is not None and (now - last_success).total_seconds() >= overdue_cutoff
        )
        if not is_very_overdue:
            due = _advance_to_hour(max(due, now), schedule.at_hour)

    return None if due <= now else due


def _advance_to_hour(moment: datetime, hour: int) -> datetime:
    """The first instant at or after ``moment`` that falls on the given local hour."""
    candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate < moment:
        candidate += timedelta(days=1)
    return candidate
