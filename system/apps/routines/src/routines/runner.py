"""Routines -- every scheduled job and background refresher in this workspace,
with its schedule, health, and run history.

The app keeps no list of what exists. On every request it re-reads the files
that actually define the routines (``/etc/cron.d``, ``run_job.sh``'s state
directory, supervisord), so a routine someone adds, re-times, pauses or deletes
shows up correctly without this app being edited. See ``discovery.py``.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/routines/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/routines/`` but
  honors the ``ROUTINES_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``ROUTINES_PORT`` env var.

This is a synchronous Flask app served by the threaded Werkzeug server.
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Final

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from werkzeug.serving import run_simple

from routines import diagnose
from routines import discovery
from routines import ledger
from routines import schedule_edit
from routines import view
from routines.schedules import next_run_at

DATA_DIR = Path(os.environ.get("ROUTINES_DATA_DIR", "data/.apps/routines"))

PORT = int(os.environ.get("ROUTINES_PORT", "8082"))

ASSETS_DIR: Final[Path] = Path(__file__).parent / "assets"

REPO_ROOT: Final[Path] = Path(os.environ.get("ROUTINES_REPO_ROOT", "/home/user/workspace"))

ENV_WRAPPER: Final[Path] = REPO_ROOT / "system" / "libs" / "automations" / "with_agent_env.sh"
RECORD_RUN: Final[Path] = REPO_ROOT / "system" / "apps" / "routines" / "bin" / "record_run.py"

# How much of a routine's own log file the raw view shows. The log is the
# unprocessed source behind every derived row on the page, so it stays one
# click away rather than being summarized only.
LOG_TAIL_BYTES: Final[int] = 200_000

app = Flask("routines", static_folder=None)


@app.route("/")
def index() -> Response:
    return Response(
        (ASSETS_DIR / "index.html").read_text(encoding="utf-8"),
        mimetype="text/html",
    )


@app.route("/api/routines")
def api_routines() -> Response:
    now = datetime.now()
    # A run whose process is gone cannot write its own record, so the books are
    # closed on it here -- before anything is read -- and it shows in the history
    # as stopped early rather than vanishing.
    ledger.reap_interrupted(DATA_DIR)
    found = discovery.discover()
    app_names = discovery.app_display_names()

    rows: list[view.Row] = []
    for routine in found.scheduled:
        rows.append(
            view.scheduled_row(
                routine,
                ledger.latest_run(routine.id, DATA_DIR),
                now,
                app_names,
                in_flight=ledger.read_in_flight(routine.id, DATA_DIR),
            )
        )
    for service in found.services:
        rows.append(view.service_row(service, now, app_names))

    ordered = view.sort_rows(rows)
    return jsonify(
        {
            "routines": [view.row_as_json(row) for row in ordered],
            "groups": [
                {"key": key, "label": label, "blurb": blurb}
                for key, label, blurb in view.GROUP_LABELS
            ],
            "cadence_units": [
                {"value": unit, "label": plural, "label_one": singular, "seconds": seconds}
                for unit, plural, singular, seconds in schedule_edit.CADENCE_UNITS
            ],
            "cadence_limits": {
                "min_seconds": schedule_edit.MIN_CADENCE_SECONDS,
                "max_seconds": schedule_edit.MAX_CADENCE_SECONDS,
                "hour_from_seconds": schedule_edit.MIN_SECONDS_FOR_DUE_HOUR,
            },
            "summary": _summarize(ordered, found, now),
            "generated_at": now.isoformat(),
        }
    )


def _summarize(rows: list[view.Row], found: discovery.Discovered, now: datetime) -> dict[str, object]:
    unhealthy = sum(1 for row in rows if row.health in view.UNHEALTHY_HEALTHS)
    sample_only = sum(1 for row in rows if row.health == view.SAMPLE_HEALTH)
    next_moments = [
        moment
        for moment in (
            _next_moment_for(routine, now)
            for routine in found.scheduled
            if not routine.is_paused
        )
        if moment is not None
    ]
    return {
        "total": len(rows),
        "healthy": len(rows) - unhealthy - sample_only,
        "needs_attention": unhealthy,
        "sample_only": sample_only,
        "next_run": (
            view.describe_moment(min(next_moments), now) if next_moments else "any minute"
        ),
    }


def _next_moment_for(routine: discovery.ScheduledRoutine, now: datetime) -> datetime | None:
    if routine.job_schedule is None or routine.state is None:
        return None
    return next_run_at(
        routine.job_schedule, routine.state.last_success, routine.state.last_attempt, now
    )


@app.route("/api/routines/<routine_id>/runs")
def api_runs(routine_id: str) -> Response:
    now = datetime.now()
    ledger.reap_interrupted(DATA_DIR)
    records = ledger.read_runs(routine_id, data_dir=DATA_DIR)
    return jsonify({"runs": [view.run_as_json(record, now) for record in records]})


@app.route("/api/routines/<routine_id>/run", methods=["POST"])
def api_run_now(routine_id: str) -> tuple[Response, int] | Response:
    """Start the routine by hand, through the same recorder the scheduled path
    uses -- which is what makes this run show as "Manual" in the history."""
    routine = _find_scheduled(routine_id)
    if routine is None:
        return jsonify({"error": "That routine is not scheduled here any more."}), 404
    if routine.is_paused:
        return jsonify({"error": "That routine is turned off."}), 409
    if not routine.command:
        return jsonify({"error": "That routine has no command to run."}), 409
    if ledger.read_in_flight(routine_id, DATA_DIR) is not None:
        # Two copies of the same job working the same source at once is how the
        # same thing gets posted twice.
        return jsonify({"error": "It is already running right now."}), 409

    command = [
        str(ENV_WRAPPER),
        "python3",
        str(RECORD_RUN),
        "--job",
        routine_id,
        "--trigger",
        "manual",
        "--update-job-state",
        "--",
        *routine.command,
    ]
    log_path = routine.log_path or (Path("/var/log/supervisor") / f"{routine_id}.log")
    try:
        log_handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 -- owned by the child
    except OSError:
        log_handle = subprocess.DEVNULL

    try:
        # Detached: a pull can take minutes and the page must not block on it.
        subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return jsonify({"error": f"Could not start it: {error}"}), 500

    return jsonify({"started": True})


@app.route("/api/routines/<routine_id>/pause", methods=["POST"])
def api_pause(routine_id: str) -> tuple[Response, int] | Response:
    """Turn a routine off, or back on, by commenting its schedule line out."""
    routine = _find_scheduled(routine_id)
    if routine is None:
        return jsonify({"error": "That routine is not scheduled here any more."}), 404

    payload = request.get_json(silent=True) or {}
    should_pause = bool(payload.get("paused", True))
    try:
        schedule_edit.set_paused(routine, should_pause)
    except schedule_edit.ScheduleEditError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"paused": should_pause})


@app.route("/api/routines/<routine_id>/cadence", methods=["POST"])
def api_cadence(routine_id: str) -> tuple[Response, int] | Response:
    """Change how often a routine runs."""
    routine = _find_scheduled(routine_id)
    if routine is None:
        return jsonify({"error": "That routine is not scheduled here any more."}), 404

    payload = request.get_json(silent=True) or {}
    raw_hour = payload.get("at_hour")
    at_hour = None
    if raw_hour not in (None, ""):
        try:
            at_hour = int(raw_hour)
        except (TypeError, ValueError):
            return jsonify({"error": "That is not a valid time of day."}), 400

    try:
        # The page sends a typed-in number and a unit; a bare cadence token is
        # still accepted so anything scripted against this route keeps working.
        if payload.get("count") not in (None, "") and payload.get("unit"):
            every = schedule_edit.build_cadence(payload.get("count"), payload.get("unit"))
        else:
            every = str(payload.get("every", "")).strip()
        schedule_edit.set_cadence(routine, every, at_hour)
    except schedule_edit.ScheduleEditError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"every": every, "at_hour": at_hour})


@app.route("/api/routines/<routine_id>/diagnose", methods=["POST"])
def api_diagnose(routine_id: str) -> tuple[Response, int] | Response:
    """Open a chat with an agent, briefed on this routine's failure."""
    now = datetime.now()
    routine = _find_scheduled(routine_id)
    if routine is None:
        return jsonify({"error": "That routine is not scheduled here any more."}), 404

    row = view.scheduled_row(
        routine, ledger.latest_run(routine.id, DATA_DIR), now, discovery.app_display_names()
    )
    brief = diagnose.build_brief(row, routine.command, ledger.read_runs(routine.id, data_dir=DATA_DIR))
    try:
        chat_id = diagnose.start_debugging_chat(brief, diagnose.chat_name_for(row), REPO_ROOT)
    except diagnose.DiagnosisRequestError as error:
        return jsonify({"error": str(error)}), 502
    return jsonify({"started": True, "chat_id": chat_id})


@app.route("/api/routines/<routine_id>/log")
def api_log(routine_id: str) -> Response:
    """The routine's own log, unprocessed -- the source behind everything the
    page derives."""
    routine = _find_scheduled(routine_id)
    if routine is not None and routine.log_path is not None:
        path = routine.log_path
    else:
        path = Path("/var/log/supervisor") / f"{routine_id}-stderr.log"

    if not path.exists():
        return Response(
            f"No log file yet at {path}.\n",
            mimetype="text/plain",
        )
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            body = handle.read().decode("utf-8", errors="replace")
    except OSError as error:
        return Response(f"Could not read {path}: {error}\n", mimetype="text/plain")
    return Response(body, mimetype="text/plain")


@app.route("/api/routines/<routine_id>/definition")
def api_definition(routine_id: str) -> Response:
    """The schedule entry exactly as it is written on disk."""
    routine = _find_scheduled(routine_id)
    if routine is None:
        return Response(f"No schedule entry for {routine_id}.\n", mimetype="text/plain")
    try:
        body = routine.source_file.read_text(encoding="utf-8")
    except OSError as error:
        return Response(f"Could not read {routine.source_file}: {error}\n", mimetype="text/plain")
    return Response(f"# {routine.source_file}\n\n{body}", mimetype="text/plain")


def _find_scheduled(routine_id: str) -> discovery.ScheduledRoutine | None:
    for routine in discovery.discover().scheduled:
        if routine.id == routine_id:
            return routine
    return None


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
