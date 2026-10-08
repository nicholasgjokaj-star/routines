"""Tests for what the page actually says about each routine.

The health verdict and the grouping are the two things a glance at this app is
supposed to answer, so both are asserted against concrete states rather than
just exercised.
"""

from datetime import datetime
from datetime import timedelta
from pathlib import Path

from routines.discovery import CatalogEntry
from routines.discovery import JobState
from routines.discovery import ScheduledRoutine
from routines.discovery import ServiceRoutine
from routines.ledger import INTERRUPTED_EXIT_CODE
from routines.ledger import InFlightRun
from routines.ledger import RunRecord
from routines.schedules import JobSchedule
from routines.view import GROUP_APPS
from routines.view import GROUP_AUTOMATIONS
from routines.view import GROUP_SYSTEM
from routines.view import describe_uptime
from routines.view import humanize_id
from routines.view import run_as_json
from routines.view import scheduled_row
from routines.view import service_row
from routines.view import sort_rows

_NOW = datetime(2026, 9, 24, 18, 0, 0)


def _routine(
    *,
    every_seconds: int = 3600,
    at_hour: int | None = None,
    last_success: datetime | None = None,
    last_attempt: datetime | None = None,
    failures: int = 0,
    is_paused: bool = False,
    catalog: CatalogEntry | None = None,
) -> ScheduledRoutine:
    return ScheduledRoutine(
        id="news",
        command=("bash", "run.sh"),
        log_path=Path("/var/log/supervisor/news.log"),
        source_file=Path("/etc/cron.d/news"),
        is_paused=is_paused,
        is_installed_live=True,
        job_schedule=JobSchedule(every_seconds=every_seconds, at_hour=at_hour),
        state=JobState(
            last_attempt=last_attempt,
            last_success=last_success,
            consecutive_failures=failures,
        ),
        catalog=catalog,
    )


def test_a_job_inside_its_window_is_working() -> None:
    row = scheduled_row(_routine(last_success=_NOW - timedelta(minutes=10)), None, _NOW)

    assert row.health == "working"
    assert row.health_label == "Working"


def test_a_job_with_failures_is_failing_and_says_how_many() -> None:
    row = scheduled_row(
        _routine(last_success=_NOW - timedelta(days=3), failures=4), None, _NOW
    )

    assert row.health == "failing"
    assert "4 failed tries" in row.health_detail
    assert "3 days ago" in row.health_detail


def test_one_failure_reads_as_one_try_not_one_tries() -> None:
    row = scheduled_row(_routine(last_success=_NOW - timedelta(days=1), failures=1), None, _NOW)

    assert "1 failed try" in row.health_detail


def test_a_failing_job_says_its_next_moment_is_a_retry() -> None:
    """"Next: 6:43 pm" beside "Every day at 7:00 am" reads as a contradiction;
    the runner is retrying, and the page has to say so."""
    row = scheduled_row(
        _routine(
            every_seconds=86400,
            at_hour=7,
            last_success=_NOW - timedelta(days=3),
            last_attempt=_NOW - timedelta(seconds=10),
            failures=2,
        ),
        None,
        _NOW,
    )

    assert row.schedule_detail.startswith("Next try:")


def test_a_job_well_past_its_window_is_overdue_even_with_no_failures() -> None:
    """Nothing recorded a failure, but the run plainly is not happening --
    silence is the failure mode this catches."""
    row = scheduled_row(_routine(every_seconds=3600, last_success=_NOW - timedelta(hours=9)), None, _NOW)

    assert row.health == "overdue"


def test_a_slow_cadence_is_not_overdue_just_because_hours_passed() -> None:
    row = scheduled_row(_routine(every_seconds=86400, last_success=_NOW - timedelta(hours=9)), None, _NOW)

    assert row.health == "working"


def test_a_paused_job_is_paused_and_cannot_be_run() -> None:
    row = scheduled_row(_routine(is_paused=True, last_success=_NOW), None, _NOW)

    assert row.health == "paused"
    assert row.schedule == "Paused"
    assert not row.can_run_now


def test_a_job_that_has_never_run_says_so() -> None:
    row = scheduled_row(_routine(), None, _NOW)

    assert row.health == "never_run"


def test_the_last_run_reports_what_triggered_it() -> None:
    record = RunRecord(
        routine_id="news",
        started_at=_NOW - timedelta(minutes=5),
        finished_at=_NOW - timedelta(minutes=4),
        trigger="manual",
        exit_code=0,
        output="",
        command="bash run.sh",
    )

    row = scheduled_row(_routine(last_success=_NOW - timedelta(minutes=4)), record, _NOW)

    assert row.last_trigger == "manual"
    assert row.last_run_label == "5 minutes ago"


def test_without_a_recorded_run_the_trigger_is_not_guessed() -> None:
    """Runs from before the run log existed have no trigger; claiming they were
    automatic would be inventing history."""
    row = scheduled_row(_routine(last_success=_NOW - timedelta(hours=2)), None, _NOW)

    assert row.last_trigger == "unknown"


def test_an_undescribed_routine_is_still_labelled_and_flagged() -> None:
    row = scheduled_row(_routine(last_success=_NOW), None, _NOW)

    assert row.title == "News"
    assert not row.is_described
    # With no description on file, saying what it runs beats an empty cell.
    assert row.what == "bash run.sh"


def test_a_routine_belonging_to_an_app_is_grouped_under_apps_and_tagged() -> None:
    catalog = CatalogEntry(id="news", title="News pull", app="reader")

    row = scheduled_row(_routine(last_success=_NOW, catalog=catalog), None, _NOW, {"reader": "Reader"})

    assert row.group == GROUP_APPS
    assert row.app_label == "Reader"


def test_an_app_tag_falls_back_to_the_apps_own_name_when_unregistered() -> None:
    catalog = CatalogEntry(id="news", title="News pull", app="news-reader")

    row = scheduled_row(_routine(last_success=_NOW, catalog=catalog), None, _NOW, {})

    assert row.app_label == "News reader"


def test_a_standalone_job_is_grouped_under_automations() -> None:
    catalog = CatalogEntry(id="news", title="News pull")

    row = scheduled_row(_routine(last_success=_NOW, catalog=catalog), None, _NOW)

    assert row.group == GROUP_AUTOMATIONS
    assert row.app_label is None


def test_built_in_upkeep_is_grouped_under_system_even_when_it_has_an_app() -> None:
    catalog = CatalogEntry(id="news", title="Guard", app="reader", built_in=True)

    row = scheduled_row(_routine(last_success=_NOW, catalog=catalog), None, _NOW)

    assert row.group == GROUP_SYSTEM


def test_a_locked_routine_offers_no_run_button() -> None:
    catalog = CatalogEntry(id="news", title="Guard", built_in=True, locked=True)

    row = scheduled_row(_routine(last_success=_NOW, catalog=catalog), None, _NOW)

    assert not row.can_run_now


def test_a_stopped_service_is_reported_as_stopped() -> None:
    service = ServiceRoutine(
        id="host-backup",
        supervisor_state="FATAL",
        supervisor_detail="Exited too quickly",
        catalog=CatalogEntry(id="host-backup", title="Workspace backup", built_in=True),
    )

    row = service_row(service, _NOW)

    assert row.health == "stopped"
    assert not row.can_run_now


def test_a_services_uptime_is_worded_without_process_detail() -> None:
    assert describe_uptime("pid 13242, uptime 5:36:22") == "Going for 5 hours"
    assert describe_uptime("pid 1, uptime 0:04:10") == "Going for 4 minutes"
    assert describe_uptime("pid 1, uptime 3 days, 2:00:00") == "Going for 3 days"


def test_anything_wrong_sorts_to_the_top() -> None:
    """The page is read top-down for "is anything broken"."""
    healthy = scheduled_row(_routine(last_success=_NOW, catalog=CatalogEntry(id="a", title="A")), None, _NOW)
    failing = scheduled_row(
        _routine(last_success=_NOW - timedelta(days=1), failures=2, catalog=CatalogEntry(id="z", title="Z")),
        None,
        _NOW,
    )

    assert [row.title for row in sort_rows([healthy, failing])] == ["Z", "A"]


def test_ids_humanize_into_words() -> None:
    assert humanize_id("news-digest-refresh") == "News digest refresh"
    assert humanize_id("news_digest") == "News digest"


def test_a_service_serving_sample_data_is_not_called_working() -> None:
    """Its loop is running, but over fixtures -- reporting it as working would
    tell the user their real accounts are being kept up to date."""
    service = ServiceRoutine(
        id="news-digest",
        supervisor_state="RUNNING",
        supervisor_detail="pid 1, uptime 5:00:00",
        catalog=CatalogEntry(id="news-digest", title="News digest refresh", app="news-digest"),
        is_serving_sample_data=True,
    )

    row = service_row(service, _NOW)

    assert row.health == "sample"
    assert row.health_label == "Sample data"
    assert "real accounts" in row.health_detail
    assert row.is_sample_data
    # Not a malfunction, so there is nothing for an agent to debug.
    assert not row.needs_fixing


def test_the_same_service_out_of_sample_mode_reads_as_working() -> None:
    service = ServiceRoutine(
        id="news-digest",
        supervisor_state="RUNNING",
        supervisor_detail="pid 1, uptime 5:00:00",
        catalog=CatalogEntry(id="news-digest", title="News digest refresh"),
        is_serving_sample_data=False,
    )

    row = service_row(service, _NOW)

    assert row.health == "working"
    assert not row.is_sample_data


def test_a_stopped_service_is_reported_stopped_even_in_sample_mode() -> None:
    """Not running at all is the more important fact."""
    service = ServiceRoutine(
        id="news-digest",
        supervisor_state="FATAL",
        supervisor_detail="",
        catalog=CatalogEntry(id="news-digest", title="News digest refresh"),
        is_serving_sample_data=True,
    )

    assert service_row(service, _NOW).health == "stopped"


def test_sample_data_sorts_below_real_problems_but_above_healthy() -> None:
    def _service(name: str, sample: bool, up: bool = True) -> object:
        return service_row(
            ServiceRoutine(
                id=name,
                supervisor_state="RUNNING" if up else "FATAL",
                supervisor_detail="pid 1, uptime 1:00:00",
                catalog=CatalogEntry(id=name, title=name),
                is_serving_sample_data=sample,
            ),
            _NOW,
        )

    rows = [_service("healthy", False), _service("demo", True), _service("down", False, up=False)]

    assert [row.title for row in sort_rows(rows)] == ["down", "demo", "healthy"]


def _in_flight(started: datetime, trigger: str = "manual") -> InFlightRun:
    return InFlightRun(
        routine_id="news", started_at=started, trigger=trigger, pid=1234, command="bash run.sh"
    )


def test_a_run_in_progress_is_said_to_be_running_and_says_when_it_started() -> None:
    row = scheduled_row(
        _routine(last_success=_NOW - timedelta(minutes=70)),
        None,
        _NOW,
        in_flight=_in_flight(_NOW - timedelta(minutes=4)),
    )

    assert row.is_running
    assert row.running_label == "Started 4 minutes ago by you"


def test_a_scheduled_run_in_progress_says_it_was_not_the_user() -> None:
    row = scheduled_row(
        _routine(last_success=_NOW - timedelta(minutes=70)),
        None,
        _NOW,
        in_flight=_in_flight(_NOW - timedelta(seconds=20), trigger="scheduled"),
    )

    assert row.running_label == "Started just now on its schedule"


def test_a_routine_already_running_offers_no_second_run() -> None:
    """Two copies of one job working the same source is how the same thing gets
    posted twice."""
    row = scheduled_row(
        _routine(last_success=_NOW - timedelta(minutes=10)),
        None,
        _NOW,
        in_flight=_in_flight(_NOW - timedelta(minutes=1)),
    )

    assert not row.can_run_now


def test_nothing_in_flight_leaves_the_row_as_it_was() -> None:
    row = scheduled_row(_routine(last_success=_NOW - timedelta(minutes=10)), None, _NOW)

    assert not row.is_running
    assert row.running_label == ""
    assert row.can_run_now


def test_a_failing_routine_can_still_be_shown_as_running() -> None:
    """Health and "going right now" are different questions: a job that has been
    failing is exactly the one whose retry the user wants to watch."""
    row = scheduled_row(
        _routine(last_success=_NOW - timedelta(hours=5), failures=3),
        None,
        _NOW,
        in_flight=_in_flight(_NOW - timedelta(minutes=2)),
    )

    assert row.health == "failing"
    assert row.is_running


def test_a_stopped_early_run_is_not_shown_as_a_failure() -> None:
    """The job did not go wrong -- it was cut off -- and its duration is unknown,
    because nothing was there to see it end."""
    record = RunRecord(
        routine_id="news",
        started_at=_NOW - timedelta(minutes=5),
        finished_at=_NOW,
        trigger="manual",
        exit_code=INTERRUPTED_EXIT_CODE,
        output="stopped before it finished",
        command="bash run.sh",
        was_interrupted=True,
    )

    rendered = run_as_json(record, _NOW)

    assert rendered["result_label"] == "Stopped early"
    assert rendered["duration"] == "unknown"
    assert rendered["was_interrupted"]
    assert not rendered["did_succeed"]


def test_a_normal_run_still_reports_worked_and_a_duration() -> None:
    record = RunRecord(
        routine_id="news",
        started_at=_NOW - timedelta(seconds=30),
        finished_at=_NOW,
        trigger="scheduled",
        exit_code=0,
        output="",
        command="bash run.sh",
    )

    rendered = run_as_json(record, _NOW)

    assert rendered["result_label"] == "Worked"
    assert rendered["duration"] == "30s"
    assert not rendered["was_interrupted"]
