"""Tests for pausing, resuming, and re-timing a routine.

These edit real cron entries, so the properties that matter are conservative
ones: nothing but the routine's own line changes, the comments explaining why
the job exists survive, and a routine the workspace depends on cannot be
touched at all.
"""

from pathlib import Path

import pytest

from routines.discovery import CatalogEntry
from routines.discovery import JobState
from routines.discovery import ScheduledRoutine
from routines.schedule_edit import ScheduleEditError
from routines.schedule_edit import check_editable
from routines.schedule_edit import build_cadence
from routines.schedule_edit import set_cadence
from routines.schedule_edit import set_paused
from routines.schedules import CronSchedule
from routines.schedules import JobSchedule

_ENTRY = (
    "# Fetch the latest headlines for the news digest.\n"
    "# This comment is the only record of why the job exists.\n"
    "* * * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh"
    " /home/user/workspace/system/libs/automations/run_job.sh news --every 6h"
    " python3 /home/user/workspace/system/apps/routines/bin/record_run.py"
    " --job news --trigger scheduled -- news-sync"
    " >> /var/log/supervisor/news.log 2>&1\n"
)


@pytest.fixture(autouse=True)
def _cron_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both copies at a scratch directory; these tests must never write
    to the real /etc/cron.d."""
    live = tmp_path / "etc-cron.d"
    durable = tmp_path / "durable-cron.d"
    live.mkdir()
    durable.mkdir()
    monkeypatch.setattr("routines.schedule_edit.LIVE_CRON_DIR", live)
    monkeypatch.setattr("routines.schedule_edit.DURABLE_CRON_DIR", durable)
    return live


def _routine(
    live: Path,
    *,
    text: str = _ENTRY,
    is_paused: bool = False,
    every_seconds: int = 21600,
    at_hour: int | None = None,
    catalog: CatalogEntry | None = None,
    is_plain_cron: bool = False,
) -> ScheduledRoutine:
    source = live / "news"
    source.write_text(text, encoding="utf-8")
    return ScheduledRoutine(
        id="news",
        command=("news-sync",),
        log_path=Path("/var/log/supervisor/news.log"),
        source_file=source,
        is_paused=is_paused,
        is_installed_live=True,
        job_schedule=None
        if is_plain_cron
        else JobSchedule(every_seconds=every_seconds, at_hour=at_hour),
        cron_schedule=CronSchedule(fields="30 9 * * 1") if is_plain_cron else None,
        state=JobState(last_attempt=None, last_success=None, consecutive_failures=0),
        catalog=catalog,
    )


def _live_text(live: Path) -> str:
    return (live / "news").read_text(encoding="utf-8")


def _durable_text(tmp_path: Path) -> str:
    return (tmp_path / "durable-cron.d" / "news").read_text(encoding="utf-8")


def test_pausing_comments_the_line_out(_cron_dirs: Path) -> None:
    set_paused(_routine(_cron_dirs), True)

    lines = _live_text(_cron_dirs).strip().split("\n")
    assert lines[-1].startswith("#* * * * *")


def test_pausing_keeps_the_definition_and_its_comments(_cron_dirs: Path) -> None:
    """Commenting out rather than deleting is what makes resuming possible."""
    set_paused(_routine(_cron_dirs), True)

    text = _live_text(_cron_dirs)
    assert "This comment is the only record of why the job exists." in text
    assert "--every 6h" in text
    assert "record_run.py" in text


def test_resuming_restores_exactly_the_line_that_was_running(_cron_dirs: Path) -> None:
    routine = _routine(_cron_dirs)
    original = _live_text(_cron_dirs)
    set_paused(routine, True)

    paused = _routine(_cron_dirs, text=_live_text(_cron_dirs), is_paused=True)
    set_paused(paused, False)

    assert _live_text(_cron_dirs) == original


def test_pausing_writes_the_durable_copy_too(_cron_dirs: Path, tmp_path: Path) -> None:
    """/etc/cron.d is rebuilt from data/.state/cron.d at boot, so a live-only
    write would silently undo itself on the next restart."""
    set_paused(_routine(_cron_dirs), True)

    assert _durable_text(tmp_path).strip().split("\n")[-1].startswith("#")


def test_pausing_an_already_paused_routine_does_nothing(_cron_dirs: Path) -> None:
    routine = _routine(_cron_dirs, is_paused=True)
    before = _live_text(_cron_dirs)

    set_paused(routine, True)

    assert _live_text(_cron_dirs) == before


def test_changing_the_cadence_rewrites_only_that_flag(_cron_dirs: Path) -> None:
    set_cadence(_routine(_cron_dirs), "15m", None)

    text = _live_text(_cron_dirs)
    assert "--every 15m" in text
    assert "--every 6h" not in text
    # The recorder wrapper and the redirect are part of the same line.
    assert "record_run.py" in text
    assert ">> /var/log/supervisor/news.log 2>&1" in text


def test_a_due_hour_is_added_and_removed(_cron_dirs: Path) -> None:
    set_cadence(_routine(_cron_dirs), "1d", 7)
    assert "--every 1d --at 7" in _live_text(_cron_dirs)

    set_cadence(_routine(_cron_dirs, text=_live_text(_cron_dirs), every_seconds=86400, at_hour=7), "6h", None)
    text = _live_text(_cron_dirs)
    assert "--every 6h" in text
    assert "--at" not in text


def test_a_due_hour_is_refused_below_a_day(_cron_dirs: Path) -> None:
    """run_job.sh would gate every run on that hour, quietly turning an hourly
    job into a daily one."""
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs), "1h", 7)


def test_a_nonsense_cadence_is_refused(_cron_dirs: Path) -> None:
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs), "sometimes", None)

    assert "--every 6h" in _live_text(_cron_dirs)


def test_an_out_of_range_hour_is_refused(_cron_dirs: Path) -> None:
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs), "1d", 25)


def test_a_plain_cron_line_is_not_retimed_here(_cron_dirs: Path) -> None:
    """Its five fields can express things this app would mangle."""
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs, is_plain_cron=True), "1d", None)


def test_a_locked_routine_refuses_every_change(_cron_dirs: Path) -> None:
    locked = CatalogEntry(id="news", title="Guard", built_in=True, locked=True)

    with pytest.raises(ScheduleEditError):
        set_paused(_routine(_cron_dirs, catalog=locked), True)
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs, catalog=locked), "1d", None)
    with pytest.raises(ScheduleEditError):
        check_editable(_routine(_cron_dirs, catalog=locked))


def test_any_whole_number_of_units_is_accepted(_cron_dirs: Path) -> None:
    """The picker is a number box, not a short menu, so an odd cadence like 20
    minutes has to go through unchanged."""
    set_cadence(_routine(_cron_dirs), build_cadence("20", "m"), None)
    assert "--every 20m" in _live_text(_cron_dirs)

    set_cadence(_routine(_cron_dirs, text=_live_text(_cron_dirs), every_seconds=1200), build_cadence(3, "d"), 6)
    assert "--every 3d --at 6" in _live_text(_cron_dirs)


def test_a_typed_number_that_is_not_a_number_is_refused() -> None:
    for count in ("", "soon", "1.5", "0", "-4"):
        with pytest.raises(ScheduleEditError):
            build_cadence(count, "m")


def test_an_unknown_unit_is_refused() -> None:
    with pytest.raises(ScheduleEditError):
        build_cadence("2", "weeks")


def test_a_gap_longer_than_a_year_is_refused(_cron_dirs: Path) -> None:
    """run_job.sh's catch-up would fire it on the next boot anyway, so the page
    would be promising something it cannot keep."""
    with pytest.raises(ScheduleEditError):
        build_cadence("400", "d")
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs), "400d", None)

    assert "--every 6h" in _live_text(_cron_dirs)


def test_a_gap_shorter_than_a_minute_is_refused(_cron_dirs: Path) -> None:
    """The cron line that drives run_job.sh only ticks once a minute."""
    with pytest.raises(ScheduleEditError):
        set_cadence(_routine(_cron_dirs), "0m", None)


def test_the_longest_allowed_gap_still_goes_through(_cron_dirs: Path) -> None:
    set_cadence(_routine(_cron_dirs), build_cadence("366", "d"), None)
    assert "--every 366d" in _live_text(_cron_dirs)


def test_only_the_matching_job_line_changes(_cron_dirs: Path) -> None:
    """One entry file can hold several jobs; re-timing one must not touch the
    others."""
    two_jobs = (
        "* * * * *   root   /x/with_agent_env.sh /x/run_job.sh news --every 6h news-sync\n"
        "* * * * *   root   /x/with_agent_env.sh /x/run_job.sh news-digest --every 1d digest\n"
    )
    set_cadence(_routine(_cron_dirs, text=two_jobs), "15m", None)

    lines = _live_text(_cron_dirs).strip().split("\n")
    assert "run_job.sh news --every 15m" in lines[0]
    assert "run_job.sh news-digest --every 1d" in lines[1]


def test_a_missing_entry_file_is_refused_not_created(_cron_dirs: Path) -> None:
    routine = _routine(_cron_dirs)
    (_cron_dirs / "news").unlink()

    with pytest.raises(ScheduleEditError):
        set_paused(routine, True)


def test_resuming_leaves_the_prose_comments_commented(_cron_dirs: Path) -> None:
    """A comment above the entry is also a commented line; uncommenting it
    would turn the job's documentation into a schedule cron cannot parse."""
    set_paused(_routine(_cron_dirs), True)
    set_paused(_routine(_cron_dirs, text=_live_text(_cron_dirs), is_paused=True), False)

    lines = _live_text(_cron_dirs).strip().split("\n")
    assert lines[0].startswith("# Fetch the latest headlines")
    assert lines[1].startswith("# This comment is the only record")
    assert lines[2].startswith("* * * * *")


def test_a_prose_line_that_starts_with_numbers_is_not_mistaken_for_a_schedule(
    _cron_dirs: Path,
) -> None:
    text = (
        "# 3 of 5 channels are covered by this job, see the notes for why\n"
        "* * * * *   root   /x/with_agent_env.sh /x/run_job.sh news --every 6h news-sync\n"
    )
    set_paused(_routine(_cron_dirs, text=text), True)

    lines = _live_text(_cron_dirs).strip().split("\n")
    assert lines[0] == "# 3 of 5 channels are covered by this job, see the notes for why"
    assert lines[1].startswith("#* * * * *")


def test_the_entry_keeps_the_mode_cron_requires(_cron_dirs: Path) -> None:
    """cron ignores a drop-in it considers world-writable."""
    set_paused(_routine(_cron_dirs), True)

    assert (_cron_dirs / "news").stat().st_mode & 0o777 == 0o644
