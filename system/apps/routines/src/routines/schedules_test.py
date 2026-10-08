"""Tests for schedule arithmetic and wording.

The next-run rules mirror ``run_job.sh``; if these drift, the app tells the
user a run is coming at a time the runner has no intention of running it.
"""

from datetime import datetime
from datetime import timedelta

from routines.schedules import CronSchedule
from routines.schedules import JobSchedule
from routines.schedules import describe_cron_schedule
from routines.schedules import describe_every
from routines.schedules import describe_job_schedule
from routines.schedules import next_run_at
from routines.schedules import parse_duration_seconds

_NOW = datetime(2026, 9, 24, 18, 0, 0)


def test_durations_parse_the_forms_the_runner_accepts() -> None:
    assert parse_duration_seconds("15m") == 900
    assert parse_duration_seconds("3h") == 10800
    assert parse_duration_seconds("7d") == 604800


def test_a_duration_the_runner_would_reject_is_rejected_here_too() -> None:
    assert parse_duration_seconds("3 hours") is None
    assert parse_duration_seconds("10s") is None
    assert parse_duration_seconds("") is None


def test_cadences_read_as_words() -> None:
    assert describe_every(86400) == "Every day"
    assert describe_every(604800) == "Every week"
    assert describe_every(21600) == "Every 6 hours"
    assert describe_every(900) == "Every 15 minutes"


def test_a_due_hour_is_spelled_out() -> None:
    assert describe_job_schedule(JobSchedule(every_seconds=86400, at_hour=7)) == (
        "Every day at 7:00 am"
    )
    assert describe_job_schedule(JobSchedule(every_seconds=86400, at_hour=15)) == (
        "Every day at 3:00 pm"
    )
    assert describe_job_schedule(JobSchedule(every_seconds=86400, at_hour=0)) == (
        "Every day at 12:00 am"
    )


def test_common_cron_shapes_read_as_words() -> None:
    assert describe_cron_schedule(CronSchedule(fields="*/5 * * * *")) == "Every 5 minutes"
    assert describe_cron_schedule(CronSchedule(fields="30 9 * * 1")) == "Every Monday at 9:30 am"
    assert describe_cron_schedule(CronSchedule(fields="0 3 * * *")) == "Every day at 3:00 am"


def test_an_exotic_cron_line_falls_back_to_its_raw_fields() -> None:
    """Guessing at a schedule the app does not understand would be worse than
    showing the user what is actually written."""
    assert describe_cron_schedule(CronSchedule(fields="0 1,13 * * *")) == "0 1,13 * * *"


def test_the_window_runs_from_the_last_completed_run() -> None:
    schedule = JobSchedule(every_seconds=3600, at_hour=None)
    last_success = _NOW - timedelta(minutes=20)

    assert next_run_at(schedule, last_success, last_success, _NOW) == (
        last_success + timedelta(hours=1)
    )


def test_an_elapsed_window_is_due_now() -> None:
    schedule = JobSchedule(every_seconds=3600, at_hour=None)
    last_success = _NOW - timedelta(hours=3)

    assert next_run_at(schedule, last_success, last_success, _NOW) is None


def test_a_never_run_job_is_due_now() -> None:
    assert next_run_at(JobSchedule(every_seconds=3600, at_hour=None), None, None, _NOW) is None


def test_a_run_that_died_waits_out_the_retry_gap() -> None:
    """last_attempt later than last_success means a run started and never
    finished, so the runner retries rather than waiting a whole interval."""
    schedule = JobSchedule(every_seconds=86400, at_hour=None, retry_after_seconds=120)
    last_success = _NOW - timedelta(days=3)
    last_attempt = _NOW - timedelta(seconds=30)

    assert next_run_at(schedule, last_success, last_attempt, _NOW) == (
        last_attempt + timedelta(seconds=120)
    )


def test_a_due_hour_holds_the_run_until_that_hour() -> None:
    schedule = JobSchedule(every_seconds=86400, at_hour=7)
    last_success = _NOW - timedelta(hours=25)

    # 18:00 with a 7am due hour: the next run is 7am tomorrow, not right now.
    assert next_run_at(schedule, last_success, last_success, _NOW) == datetime(2026, 9, 25, 7, 0)


def test_a_whole_extra_missed_day_overrides_the_due_hour() -> None:
    """run_job.sh catches up at any hour once a job is overdue by more than its
    interval plus a day -- so a container that was off must not leave the user
    waiting until tomorrow morning."""
    schedule = JobSchedule(every_seconds=86400, at_hour=7)
    last_success = _NOW - timedelta(days=4)

    assert next_run_at(schedule, last_success, last_success, _NOW) is None
