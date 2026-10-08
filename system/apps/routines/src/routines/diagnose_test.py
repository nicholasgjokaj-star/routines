"""Tests for the brief the Fix button hands to an agent.

The value of the button is entirely in the brief: if it omits the error output
or the file paths, the agent spends its first several turns rediscovering what
this page already knew, and the button saved nothing.
"""

from datetime import datetime

from routines.diagnose import RUNS_IN_BRIEF
from routines.diagnose import build_brief
from routines.diagnose import chat_name_for
from routines.diagnose import _chat_id_from
from routines.ledger import RunRecord
from routines.view import Row


def _row(**overrides) -> Row:
    fields = {
        "id": "news-digest-refresh",
        "title": "News digest refresh",
        "what": "Fetches the latest headlines from your news feeds and builds the digest.",
        "schedule": "Every day at 7:00 am",
        "schedule_detail": "Next try: any minute",
        "health": "failing",
        "health_label": "Failing",
        "health_detail": "Last worked 3 days ago · 699 failed tries since",
        "last_run_label": "3 days ago",
        "last_trigger": "unknown",
        "can_run_now": True,
        "can_pause": True,
        "can_retime": True,
        "is_paused": False,
        "every": "1d",
        "at_hour": 7,
        "needs_fixing": True,
        "is_described": True,
        "is_built_in": False,
        "app": "news-digest",
        "app_label": "News Digest",
        "kind": "scheduled",
        "group": "apps",
        "raw_source": "/etc/cron.d/news-digest-refresh",
        "log_path": "/var/log/supervisor/news-digest-refresh.log",
    }
    fields.update(overrides)
    return Row(**fields)


def _run(*, succeeded: bool = False, output: str = "FeedFetchError: 401 unauthorized") -> RunRecord:
    return RunRecord(
        routine_id="news-digest-refresh",
        started_at=datetime(2026, 9, 24, 18, 19),
        finished_at=datetime(2026, 9, 24, 18, 19, 12),
        trigger="scheduled",
        exit_code=0 if succeeded else 1,
        output=output,
        command="news-digest-refresh",
    )


def test_the_brief_invokes_the_assist_flow() -> None:
    """The agent should arrive with the workspace's diagnose-confirm-fix
    procedure, not just a complaint."""
    assert build_brief(_row(), ("news-digest-refresh",), []).startswith("/assist ")


def test_the_brief_carries_the_evidence_already_on_the_page() -> None:
    brief = build_brief(_row(), ("news-digest-refresh", "--full"), [_run()])

    assert "News digest refresh" in brief
    assert "news-digest-refresh" in brief
    assert "Every day at 7:00 am" in brief
    assert "699 failed tries" in brief
    assert "news-digest-refresh --full" in brief
    assert "News Digest" in brief


def test_the_brief_names_the_files_to_look_in() -> None:
    """Naming them beats describing them: the agent can open them directly."""
    brief = build_brief(_row(), ("news-digest-refresh",), [_run()])

    assert "/etc/cron.d/news-digest-refresh" in brief
    assert "/var/log/supervisor/news-digest-refresh.log" in brief
    assert "data/.state/jobs/news-digest-refresh/" in brief
    assert "data/.apps/routines/runs.jsonl" in brief
    assert "run_job.sh" in brief


def test_the_brief_quotes_the_actual_error_output() -> None:
    brief = build_brief(_row(), ("news-digest-refresh",), [_run()])

    assert "FeedFetchError: 401 unauthorized" in brief


def test_the_brief_says_whether_each_run_worked_and_what_started_it() -> None:
    brief = build_brief(
        _row(), ("news-digest-refresh",), [_run(), _run(succeeded=True, output="ok")]
    )

    assert "FAILED (exit 1)" in brief
    assert "worked" in brief
    assert "scheduled" in brief


def test_the_brief_is_bounded_when_a_routine_has_failed_for_days() -> None:
    """Hundreds of identical failures would bury the useful part."""
    runs = [_run() for _ in range(200)]

    brief = build_brief(_row(), ("news-digest-refresh",), runs)

    assert brief.count("FAILED (exit 1)") == RUNS_IN_BRIEF


def test_with_no_recorded_runs_the_brief_says_so_rather_than_implying_none_happened() -> None:
    brief = build_brief(_row(), ("news-digest-refresh",), [])

    assert "None recorded yet" in brief
    assert "/var/log/supervisor/news-digest-refresh.log" in brief


def test_the_brief_asks_for_a_reproduction_not_a_theory() -> None:
    brief = build_brief(_row(), ("news-digest-refresh",), [_run()])

    assert "Reproduce the failure" in brief
    # An expired third-party connection is the common non-bug cause here.
    assert "reconnect" in brief


def test_an_undescribed_routine_still_produces_a_usable_brief() -> None:
    brief = build_brief(
        _row(is_described=False, app=None, app_label=None, what="bash run.sh"),
        ("bash", "run.sh"),
        [],
    )

    assert "bash run.sh" in brief
    assert "Belongs to the app" not in brief


def test_the_chat_name_stays_short_enough_to_read_in_a_tab() -> None:
    name = chat_name_for(_row(title="A" * 100))

    assert name.startswith("Fix ")
    assert len(name) <= 40


def test_the_chat_id_is_read_from_the_creates_json_line() -> None:
    assert _chat_id_from('{"chat_id": "chat-7", "name": "Fix it"}\n') == "chat-7"


def test_an_unreadable_create_output_yields_no_id_rather_than_raising() -> None:
    """The chat exists and its window is opening either way; only the link is
    lost."""
    assert _chat_id_from("something unexpected\n") == ""
    assert _chat_id_from("") == ""
