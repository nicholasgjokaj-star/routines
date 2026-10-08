"""Turning discovered routines into the rows the page renders.

Everything user-facing is decided here: the label a routine gets when nothing
described it, whether it counts as working, and how a timestamp is worded. The
health rules are deliberately derived from the routine's own cadence rather
than fixed thresholds -- a job that runs every six hours and last worked two
hours ago is fine, while a job that runs every minute and last worked two hours
ago is not.
"""

import re
from datetime import datetime
from typing import Any
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel

from routines.discovery import ScheduledRoutine
from routines.discovery import ServiceRoutine
from routines.ledger import InFlightRun
from routines.ledger import RunRecord
from routines.schedules import describe_cron_schedule
from routines.schedules import describe_every
from routines.schedules import JobSchedule
from routines.schedules import describe_job_schedule
from routines.schedules import next_run_at

# A routine is only called overdue once it has missed its window by this much
# again, so a run a few minutes late does not read as a problem.
_OVERDUE_GRACE_MULTIPLIER: Final[float] = 0.5

_SECONDS_PER_MINUTE: Final[int] = 60
_SECONDS_PER_HOUR: Final[int] = 3600
_SECONDS_PER_DAY: Final[int] = 86400

# supervisord states that mean the program is up and doing its job.
_HEALTHY_SUPERVISOR_STATES: Final[frozenset[str]] = frozenset({"RUNNING"})

_STATE_WORKING: Final[str] = "working"
_STATE_FAILING: Final[str] = "failing"
_STATE_OVERDUE: Final[str] = "overdue"
_STATE_PAUSED: Final[str] = "paused"
_STATE_NEVER_RUN: Final[str] = "never_run"
_STATE_STOPPED: Final[str] = "stopped"
# The loop is running, but over fixtures: the app is in its own sample-data
# mode, so nothing real is being fetched. Distinct from "working" because
# calling it working is the specific lie this app exists not to tell.
_STATE_SAMPLE: Final[str] = "sample"


# The three groups the page is divided into, in the order they are shown.
GROUP_APPS: Final[str] = "apps"
GROUP_AUTOMATIONS: Final[str] = "automations"
GROUP_SYSTEM: Final[str] = "system"

GROUP_LABELS: Final[tuple[tuple[str, str, str], ...]] = (
    (GROUP_APPS, "Apps", "Routines that keep one of your apps up to date."),
    (
        GROUP_AUTOMATIONS,
        "Automations and scheduled tasks",
        "Jobs that run on their own schedule.",
    ),
    (GROUP_SYSTEM, "System", "Built-in upkeep this workspace does for itself."),
)


class Row(FrozenModel):
    """One rendered routine, ready for the page or the API."""

    id: str
    title: str
    what: str
    schedule: str
    schedule_detail: str
    health: str
    health_label: str
    health_detail: str
    last_run_label: str
    last_trigger: str
    # A run that is under way right now. Worth its own field rather than folding
    # into health: a routine can be working, overdue, or failing AND be mid-run,
    # and the run in flight is the answer to "did my click do anything?".
    is_running: bool = False
    running_label: str = ""
    can_run_now: bool
    # Whether the user may pause/resume it, and whether its cadence is one this
    # app can rewrite (a plain cron line's is not -- see schedule_edit.py).
    can_pause: bool
    can_retime: bool
    is_paused: bool
    every: str | None
    at_hour: int | None
    # Set when the routine is in a state worth handing to an agent to debug.
    needs_fixing: bool
    # Set when the thing behind this row is serving made-up content.
    is_sample_data: bool = False
    is_described: bool
    is_built_in: bool
    app: str | None
    app_label: str | None
    kind: str
    group: str
    raw_source: str
    log_path: str | None


def group_for(app: str | None, is_built_in: bool) -> str:
    """Which section a routine belongs in.

    Built-in upkeep is System even when it belongs to an app, because the user
    did not create it and cannot be expected to recognize it. Everything else
    splits on whether it exists to serve one of their apps.
    """
    if is_built_in:
        return GROUP_SYSTEM
    return GROUP_APPS if app else GROUP_AUTOMATIONS


def humanize_id(routine_id: str) -> str:
    """``news-digest-refresh`` -> ``News digest refresh``. Used only when nothing
    described the routine, so a brand-new job still reads as words."""
    words = routine_id.replace("_", " ").replace("-", " ").split()
    if not words:
        return routine_id
    return " ".join([words[0].capitalize(), *words[1:]])


def describe_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < _SECONDS_PER_MINUTE:
        return "just now"
    if seconds < _SECONDS_PER_HOUR:
        minutes = int(seconds // _SECONDS_PER_MINUTE)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < _SECONDS_PER_DAY:
        hours = int(seconds // _SECONDS_PER_HOUR)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // _SECONDS_PER_DAY)
    return f"{days} day{'s' if days != 1 else ''} ago"


def describe_moment(moment: datetime, now: datetime) -> str:
    """An absolute time worded relative to today, for a *future* run."""
    time_of_day = moment.strftime("%-I:%M %p").lower()
    days_ahead = (moment.date() - now.date()).days
    if days_ahead == 0:
        return f"today {time_of_day}"
    if days_ahead == 1:
        return f"tomorrow {time_of_day}"
    return f"{moment.strftime('%b %-d')} {time_of_day}"


def describe_duration(seconds: float) -> str:
    if seconds < 1:
        return "under a second"
    if seconds < _SECONDS_PER_MINUTE:
        return f"{int(seconds)}s"
    minutes = int(seconds // _SECONDS_PER_MINUTE)
    remainder = int(seconds % _SECONDS_PER_MINUTE)
    return f"{minutes}m {remainder:02d}s"


def scheduled_row(
    routine: ScheduledRoutine,
    last_run: RunRecord | None,
    now: datetime,
    app_names: dict[str, str] | None = None,
    in_flight: InFlightRun | None = None,
) -> Row:
    catalog = routine.catalog
    state = routine.state

    if routine.job_schedule is not None:
        schedule_text = describe_job_schedule(routine.job_schedule)
    elif routine.cron_schedule is not None:
        schedule_text = describe_cron_schedule(routine.cron_schedule)
    else:
        schedule_text = "Unknown"

    health, health_label, health_detail, schedule_detail = _scheduled_health(routine, now)

    last_success = state.last_success if state is not None else None
    if last_run is not None:
        last_run_label = describe_age(last_run.started_at, now)
        last_trigger = last_run.trigger
    elif state is not None:
        last_run_label = describe_age(last_success, now)
        # Before the run log existed there is no record of what triggered a
        # run, so it is left unstated rather than assumed to be automatic.
        last_trigger = "unknown"
    else:
        # A plain cron line keeps no state at all, so until this routine runs
        # once through the recorder there is genuinely nothing to report --
        # which is different from knowing it never ran.
        last_run_label = "not recorded yet"
        last_trigger = "unknown"

    app = catalog.app if catalog else None
    is_built_in = bool(catalog and catalog.built_in)
    is_locked = bool(catalog and catalog.locked)
    return Row(
        is_running=in_flight is not None,
        running_label=(_describe_in_flight(in_flight, now) if in_flight is not None else ""),
        id=routine.id,
        title=(catalog.title if catalog and catalog.title else humanize_id(routine.id)),
        what=(catalog.what if catalog and catalog.what else _fallback_what(routine)),
        schedule=("Paused" if routine.is_paused else schedule_text),
        schedule_detail=schedule_detail,
        health=health,
        health_label=health_label,
        health_detail=health_detail,
        last_run_label=last_run_label,
        last_trigger=last_trigger,
        # No second run while one is in flight: two copies of the same job
        # pulling the same source at once is how duplicate work gets posted.
        can_run_now=(
            not routine.is_paused
            and bool(routine.command)
            and not is_locked
            and in_flight is None
        ),
        can_pause=not is_locked,
        # Only a run_job.sh cadence is safely rewritable; a plain cron line's
        # five fields can express things this app would mangle.
        can_retime=not is_locked and routine.job_schedule is not None,
        is_paused=routine.is_paused,
        every=(_every_token(routine.job_schedule) if routine.job_schedule else None),
        at_hour=(routine.job_schedule.at_hour if routine.job_schedule else None),
        needs_fixing=health in (_STATE_FAILING, _STATE_OVERDUE),
        is_described=catalog is not None,
        is_built_in=is_built_in,
        app=app,
        app_label=_app_label(app, app_names),
        kind="scheduled",
        group=group_for(app, is_built_in),
        raw_source=str(routine.source_file),
        log_path=(str(routine.log_path) if routine.log_path else None),
    )


def _describe_in_flight(in_flight: InFlightRun, now: datetime) -> str:
    """``Started 4 minutes ago by you``, beside a Running pill.

    Saying who started it matters on a long job: the user who pressed the button
    needs to recognize this run as theirs rather than a scheduled one that
    happened to overlap.
    """
    started = describe_age(in_flight.started_at, now)
    whose = {"manual": " by you", "scheduled": " on its schedule"}.get(in_flight.trigger, "")
    return f"Started {started}{whose}"


def _every_token(schedule: JobSchedule) -> str:
    """The cadence back in the ``15m`` / ``6h`` / ``7d`` form the cron entry
    is written in, so the picker can preselect what is currently set."""
    seconds = schedule.every_seconds
    if seconds % _SECONDS_PER_DAY == 0:
        return f"{seconds // _SECONDS_PER_DAY}d"
    if seconds % _SECONDS_PER_HOUR == 0:
        return f"{seconds // _SECONDS_PER_HOUR}h"
    return f"{max(1, seconds // _SECONDS_PER_MINUTE)}m"


def _app_label(app: str | None, app_names: dict[str, str] | None) -> str | None:
    """The app's own display name, falling back to its id so a routine whose
    app was renamed or unregistered still says which app it came from."""
    if app is None:
        return None
    return (app_names or {}).get(app) or humanize_id(app)


def _fallback_what(routine: ScheduledRoutine) -> str:
    """No description on file, so say what it actually runs. Technical, but
    true -- and better than an empty cell for a routine nobody has labelled."""
    return " ".join(routine.command) if routine.command else "Runs on a schedule."


def _scheduled_health(routine: ScheduledRoutine, now: datetime) -> tuple[str, str, str, str]:
    """(health key, pill label, detail line, schedule detail line)."""
    if routine.is_paused:
        return (_STATE_PAUSED, "Paused", "Turned off; nothing will run.", "Turned off")

    state = routine.state
    if state is None:
        # A plain cron line keeps no state, so there is nothing to judge it by
        # beyond the run log, which the caller folds in as the last-run line.
        return (_STATE_WORKING, "On", "", "")

    if state.last_success is None and state.last_attempt is None:
        return (_STATE_NEVER_RUN, "Not run yet", "Waiting for its first run.", "")

    next_at = (
        next_run_at(routine.job_schedule, state.last_success, state.last_attempt, now)
        if routine.job_schedule is not None
        else None
    )
    is_failing = state.consecutive_failures > 0
    # While a job is failing the runner is retrying it, so the next moment is a
    # retry rather than the next scheduled run -- say which, or "today 6:43 pm"
    # next to "Every day at 7:00 am" reads like a contradiction.
    next_word = "Next try" if is_failing else "Next"
    schedule_detail = (
        f"{next_word}: {describe_moment(next_at, now)}" if next_at else f"{next_word}: any minute"
    )

    if is_failing:
        tries = state.consecutive_failures
        detail = (
            f"Last worked {describe_age(state.last_success, now)}"
            f" · {tries} failed {'try' if tries == 1 else 'tries'} since"
        )
        return (_STATE_FAILING, "Failing", detail, schedule_detail)

    if routine.job_schedule is not None and state.last_success is not None:
        allowed = routine.job_schedule.every_seconds * (1 + _OVERDUE_GRACE_MULTIPLIER)
        if (now - state.last_success).total_seconds() > allowed:
            return (
                _STATE_OVERDUE,
                "Overdue",
                f"Last worked {describe_age(state.last_success, now)}.",
                schedule_detail,
            )

    return (_STATE_WORKING, "Working", "", schedule_detail)


_UPTIME_PATTERN: Final[re.Pattern[str]] = re.compile(r"uptime\s+(?:(\d+)\s+days?,\s*)?(\d+):(\d+)")


def describe_uptime(supervisor_detail: str) -> str:
    """``pid 13242, uptime 5:36:22`` -> ``Going for 5 hours``.

    supervisord's own wording carries a process id and a clock, neither of
    which means anything to someone asking whether their backup is running.
    """
    match = _UPTIME_PATTERN.search(supervisor_detail)
    if match is None:
        return "Going now"
    days = int(match.group(1) or 0)
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if days:
        return f"Going for {days} day{'s' if days != 1 else ''}"
    if hours:
        return f"Going for {hours} hour{'s' if hours != 1 else ''}"
    if minutes:
        return f"Going for {minutes} minute{'s' if minutes != 1 else ''}"
    return "Just started"


def service_row(
    service: ServiceRoutine, now: datetime, app_names: dict[str, str] | None = None
) -> Row:
    catalog = service.catalog
    is_up = service.supervisor_state in _HEALTHY_SUPERVISOR_STATES

    if catalog.every:
        schedule_text = catalog.every
    elif catalog.every_seconds:
        schedule_text = describe_every(catalog.every_seconds)
    else:
        schedule_text = "Continuous"

    is_sample = is_up and service.is_serving_sample_data
    if not is_up:
        health, health_label = _STATE_STOPPED, "Stopped"
        health_detail = "Not running right now."
    elif is_sample:
        health, health_label = _STATE_SAMPLE, "Sample data"
        health_detail = "Refreshing made-up content, not your real accounts."
    else:
        health, health_label = _STATE_WORKING, "Working"
        health_detail = ""

    return Row(
        id=service.id,
        title=(catalog.title or humanize_id(service.id)),
        what=(catalog.what or "Runs continuously in the background."),
        schedule=schedule_text,
        schedule_detail=(
            "Only while the app is open" if catalog.only_while_open else "Runs in the background"
        ),
        health=health,
        health_label=health_label,
        health_detail=health_detail,
        last_run_label=(
            describe_uptime(service.supervisor_detail) if is_up else "Not running right now."
        ),
        last_trigger="unknown",
        can_run_now=False,
        can_pause=False,
        can_retime=False,
        is_paused=False,
        every=None,
        at_hour=None,
        needs_fixing=not is_up,
        is_sample_data=is_sample,
        is_described=True,
        is_built_in=catalog.built_in,
        app=catalog.app,
        app_label=_app_label(catalog.app, app_names),
        kind="service",
        group=group_for(catalog.app, catalog.built_in),
        raw_source=f"supervisord program {service.id}",
        log_path=f"/var/log/supervisor/{service.id}-stderr.log",
    )


# Failing first, then overdue, then everything healthy -- the page is read
# top-down for "is anything wrong", so what is wrong has to be at the top.
_HEALTH_RANK: Final[dict[str, int]] = {
    _STATE_FAILING: 0,
    _STATE_OVERDUE: 1,
    _STATE_STOPPED: 2,
    _STATE_NEVER_RUN: 3,
    _STATE_SAMPLE: 4,
    _STATE_WORKING: 5,
    _STATE_PAUSED: 6,
}

# Counted as neither healthy nor broken: a demo loop is working exactly as
# built, and reporting it as one of N healthy routines would overstate how much
# of the user's real work is actually being kept up to date.
SAMPLE_HEALTH: Final[str] = _STATE_SAMPLE
UNHEALTHY_HEALTHS: Final[frozenset[str]] = frozenset(
    {_STATE_FAILING, _STATE_OVERDUE, _STATE_STOPPED}
)


def sort_rows(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda row: (_HEALTH_RANK.get(row.health, 9), row.title.lower()))


def row_as_json(row: Row) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "what": row.what,
        "schedule": row.schedule,
        "schedule_detail": row.schedule_detail,
        "health": row.health,
        "health_label": row.health_label,
        "health_detail": row.health_detail,
        "last_run_label": row.last_run_label,
        "last_trigger": row.last_trigger,
        "is_running": row.is_running,
        "running_label": row.running_label,
        "can_run_now": row.can_run_now,
        "can_pause": row.can_pause,
        "can_retime": row.can_retime,
        "is_paused": row.is_paused,
        "every": row.every,
        "at_hour": row.at_hour,
        "needs_fixing": row.needs_fixing,
        "is_sample_data": row.is_sample_data,
        "is_described": row.is_described,
        "is_built_in": row.is_built_in,
        "app": row.app,
        "app_label": row.app_label,
        "kind": row.kind,
        "group": row.group,
        "raw_source": row.raw_source,
        "log_path": row.log_path,
    }


def _result_label(record: RunRecord) -> str:
    if record.was_interrupted:
        return "Stopped early"
    return "Worked" if record.did_succeed else "Failed"


def run_as_json(record: RunRecord, now: datetime) -> dict[str, Any]:
    return {
        "started_at": record.started_at.isoformat(),
        "when": describe_age(record.started_at, now),
        "when_exact": record.started_at.strftime("%b %-d, %-I:%M %p"),
        "trigger": record.trigger,
        "trigger_label": {
            "scheduled": "Automatic",
            "manual": "Manual",
            "unknown": "Unknown",
        }.get(record.trigger, "Unknown"),
        "did_succeed": record.did_succeed,
        "result_label": _result_label(record),
        # An interrupted run has no honest duration: nothing was watching when
        # it stopped, so the only thing known is when it began.
        "duration": ("unknown" if record.was_interrupted else describe_duration(record.duration_seconds)),
        "was_interrupted": record.was_interrupted,
        "exit_code": record.exit_code,
        "output": record.output,
        "command": record.command,
    }
