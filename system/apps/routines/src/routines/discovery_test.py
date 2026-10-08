"""Tests for reading routines out of the files that define them.

These assert the properties the app's whole promise rests on: that a cron entry
someone writes by hand is found, parsed with the right cadence, and re-read
rather than cached -- so adding, re-timing, pausing or deleting a routine needs
no change here.
"""

from pathlib import Path

import pytest

from routines.discovery import Catalog
from routines.discovery import CatalogEntry
from routines.discovery import app_display_names
from routines.discovery import discover
from routines.discovery import is_serving_sample_data
from routines.discovery import load_catalog
from routines.discovery import read_job_state

_RUNNER = "/home/user/workspace/system/libs/automations/run_job.sh"
_WRAPPER = "/home/user/workspace/system/libs/automations/with_agent_env.sh"
_RECORDER = "/home/user/workspace/system/apps/routines/bin/record_run.py"


def _write_entry(directory: Path, name: str, line: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(line + "\n", encoding="utf-8")


def _empty_catalog() -> Catalog:
    return Catalog(jobs={}, services={})


def test_finds_a_run_job_entry_and_its_cadence(tmp_path: Path) -> None:
    live = tmp_path / "cron.d"
    _write_entry(
        live,
        "news",
        f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d --at 9 bash run_news.sh"
        " >> /var/log/supervisor/news.log 2>&1",
    )

    found = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    assert [routine.id for routine in found.scheduled] == ["news"]
    routine = found.scheduled[0]
    assert routine.job_schedule is not None
    assert routine.job_schedule.every_seconds == 86400
    assert routine.job_schedule.at_hour == 9
    assert routine.command == ("bash", "run_news.sh")
    assert routine.log_path == Path("/var/log/supervisor/news.log")
    assert not routine.is_paused


def test_reports_the_command_under_the_run_recorder(tmp_path: Path) -> None:
    """The recorder is this app's own bookkeeping wrapper; the user cares about
    the command underneath it."""
    live = tmp_path / "cron.d"
    _write_entry(
        live,
        "news",
        f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 6h python3 {_RECORDER}"
        " --job news --trigger scheduled -- news-sync --full"
        " >> /var/log/supervisor/news.log 2>&1",
    )

    found = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    assert found.scheduled[0].command == ("news-sync", "--full")


def test_a_commented_entry_reads_as_paused_not_missing(tmp_path: Path) -> None:
    """Commenting the line out is the documented way to pause a job, so it must
    still be listed -- a paused routine the user cannot see is one they cannot
    turn back on."""
    live = tmp_path / "cron.d"
    _write_entry(
        live,
        "news",
        f"#* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d bash run_news.sh",
    )

    found = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    assert len(found.scheduled) == 1
    assert found.scheduled[0].is_paused


def test_a_re_timed_entry_reports_the_new_cadence(tmp_path: Path) -> None:
    """Nothing is cached, so editing the entry changes what the next read says."""
    live = tmp_path / "cron.d"
    _write_entry(
        live,
        "news",
        f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 30m bash run_news.sh",
    )
    first = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    _write_entry(
        live,
        "news",
        f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 12h bash run_news.sh",
    )
    second = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    assert first.scheduled[0].job_schedule is not None
    assert second.scheduled[0].job_schedule is not None
    assert first.scheduled[0].job_schedule.every_seconds == 1800
    assert second.scheduled[0].job_schedule.every_seconds == 43200


def test_a_deleted_entry_disappears(tmp_path: Path) -> None:
    live = tmp_path / "cron.d"
    _write_entry(
        live,
        "news",
        f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d bash run_news.sh",
    )
    assert discover(live, tmp_path / "d", tmp_path / "j", _empty_catalog()).scheduled

    (live / "news").unlink()

    assert not discover(live, tmp_path / "d", tmp_path / "j", _empty_catalog()).scheduled


def test_a_plain_cron_line_keeps_its_schedule_fields(tmp_path: Path) -> None:
    live = tmp_path / "cron.d"
    _write_entry(live, "weekly-report", f"30 9 * * 1   root   {_WRAPPER} bash report.sh")

    found = discover(live, tmp_path / "durable", tmp_path / "jobs", _empty_catalog())

    routine = found.scheduled[0]
    assert routine.job_schedule is None
    assert routine.cron_schedule is not None
    assert routine.cron_schedule.fields == "30 9 * * 1"
    # A plain cron line has no run_job.sh state to read.
    assert routine.state is None


def test_the_live_copy_wins_over_the_durable_one(tmp_path: Path) -> None:
    """/etc/cron.d is what cron obeys, so when the two disagree the live cadence
    is the true one."""
    live = tmp_path / "cron.d"
    durable = tmp_path / "durable"
    _write_entry(
        live, "news", f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1h bash run.sh"
    )
    _write_entry(
        durable, "news", f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 7d bash run.sh"
    )

    found = discover(live, durable, tmp_path / "jobs", _empty_catalog())

    assert len(found.scheduled) == 1
    assert found.scheduled[0].job_schedule is not None
    assert found.scheduled[0].job_schedule.every_seconds == 3600


def test_a_durable_entry_not_installed_live_is_still_surfaced(tmp_path: Path) -> None:
    """Someone intended this routine to exist; hiding it would make a broken
    install look identical to no install."""
    durable = tmp_path / "durable"
    _write_entry(
        durable, "news", f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d bash run.sh"
    )

    found = discover(tmp_path / "cron.d", durable, tmp_path / "jobs", _empty_catalog())

    assert len(found.scheduled) == 1
    assert not found.scheduled[0].is_installed_live


def test_an_entry_with_a_dot_in_its_name_is_ignored(tmp_path: Path) -> None:
    """cron itself skips these, so listing one would show a routine that never
    actually runs."""
    live = tmp_path / "cron.d"
    _write_entry(
        live, "news.bak", f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d bash run.sh"
    )

    assert not discover(live, tmp_path / "d", tmp_path / "j", _empty_catalog()).scheduled


def test_job_state_is_read_from_the_runners_own_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "jobs" / "news"
    state_dir.mkdir(parents=True)
    (state_dir / "last_attempt").write_text("1700000100\n", encoding="utf-8")
    (state_dir / "last_success").write_text("1700000000\n", encoding="utf-8")
    (state_dir / "failures").write_text("4\n", encoding="utf-8")

    state = read_job_state("news", tmp_path / "jobs")

    assert state.last_success is not None
    assert state.last_attempt is not None
    assert int(state.last_success.timestamp()) == 1700000000
    assert int(state.last_attempt.timestamp()) == 1700000100
    assert state.consecutive_failures == 4


def test_missing_job_state_is_not_an_error(tmp_path: Path) -> None:
    state = read_job_state("never-run", tmp_path / "jobs")

    assert state.last_success is None
    assert state.last_attempt is None
    assert state.consecutive_failures == 0


def test_catalog_only_supplies_wording(tmp_path: Path) -> None:
    """An undescribed routine is still discovered; the catalog is not a gate."""
    live = tmp_path / "cron.d"
    _write_entry(
        live, "news", f"* * * * *   root   {_WRAPPER} {_RUNNER} news --every 1d bash run.sh"
    )
    catalog = Catalog(
        jobs={"news": CatalogEntry(id="news", title="Morning news", app="reader")}, services={}
    )

    described = discover(live, tmp_path / "d", tmp_path / "j", catalog).scheduled[0]
    undescribed = discover(live, tmp_path / "d", tmp_path / "j", _empty_catalog()).scheduled[0]

    assert described.catalog is not None
    assert described.catalog.title == "Morning news"
    assert undescribed.catalog is None


def test_the_shipped_catalog_parses() -> None:
    catalog = load_catalog()

    assert "caretaker" in catalog.jobs
    assert catalog.jobs["caretaker"].title == "Weekly check-up"
    assert catalog.jobs["update-apply-recover"].locked
    assert "host-backup" in catalog.services


def test_app_display_names_prefers_what_the_user_sees(tmp_path: Path) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        "\n".join(
            [
                "[[apps]]",
                'name = "news-digest"',
                'display_name = "News Digest"',
                "[[apps]]",
                'name = "plumbing"',
                'display_name = "Plumbing"',
                "internal = true",
                "[[apps]]",
                'name = "bare"',
            ]
        ),
        encoding="utf-8",
    )

    names = app_display_names(registry)

    assert names["news-digest"] == "News Digest"
    assert names["bare"] == "bare"
    # Internal apps are workspace plumbing and never tag a user's routine.
    assert "plumbing" not in names


def test_a_missing_registry_is_not_an_error(tmp_path: Path) -> None:
    assert app_display_names(tmp_path / "nope.toml") == {}


def test_a_service_in_its_own_sample_mode_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read from the live process, so the answer is what the service is really
    running with rather than what some config file says it should be."""
    monkeypatch.setattr(
        "routines.discovery.Path",
        _fake_path_reading(b"HOME=/root\x00NEWS_DIGEST_DEMO=1\x00PORT=8200\x00"),
    )

    assert is_serving_sample_data(4242, "NEWS_DIGEST_DEMO")


def test_a_service_not_in_sample_mode_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "routines.discovery.Path",
        _fake_path_reading(b"HOME=/root\x00NEWS_DIGEST_DEMO=0\x00"),
    )

    assert not is_serving_sample_data(4242, "NEWS_DIGEST_DEMO")


def test_a_service_that_declares_no_sample_switch_is_never_flagged() -> None:
    assert not is_serving_sample_data(4242, None)


def test_an_unreadable_process_is_not_flagged_either_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process may have just exited; the weaker claim is the safe one."""
    monkeypatch.setattr("routines.discovery.Path", _fake_path_raising())

    assert not is_serving_sample_data(4242, "NEWS_DIGEST_DEMO")


def test_a_stopped_service_has_no_pid_to_read() -> None:
    assert not is_serving_sample_data(None, "NEWS_DIGEST_DEMO")


def _fake_path_reading(payload: bytes):
    class _Path:
        def __init__(self, _: str) -> None:
            pass

        def read_bytes(self) -> bytes:
            return payload

    return _Path


def _fake_path_raising():
    class _Path:
        def __init__(self, _: str) -> None:
            pass

        def read_bytes(self) -> bytes:
            raise OSError("no such process")

    return _Path
