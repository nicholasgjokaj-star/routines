"""Finding every routine in this workspace, live, from the files that define them.

The design rule here is that this module owns **no list of routines**. It reads
the same sources the scheduler and the process supervisor read, on every
request:

- ``/etc/cron.d/`` -- the live drop-ins cron actually obeys. A job added,
  re-timed, paused or removed through the ``manage-scheduled-tasks`` skill
  changes a file here, so it changes what this app shows with no edit to this
  app. ``data/.state/cron.d/`` is read too, to catch a durable entry that has
  not been installed live yet.
- ``data/.state/jobs/<id>/`` -- ``run_job.sh``'s own state for each job
  (``last_attempt``, ``last_success``, ``failures``).
- ``supervisorctl status`` -- what the long-running services are doing.

Only the *wording* comes from ``catalog.toml``, and only as an override: a
routine with no entry there is still discovered and still shown, labelled from
its id and tagged as undescribed. The single exception is a service whose
refresh cadence lives inside its own process: nothing on disk states that, so
the catalog is where it is declared.
"""

import http.client
import os
import re
import socket
import subprocess
import tomllib
import xmlrpc.client
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.frozen_model import FrozenModel

from routines.schedules import CronSchedule
from routines.schedules import JobSchedule
from routines.schedules import parse_duration_seconds

LIVE_CRON_DIR: Final[Path] = Path(os.environ.get("ROUTINES_CRON_DIR", "/etc/cron.d"))
DURABLE_CRON_DIR: Final[Path] = Path(
    os.environ.get("ROUTINES_DURABLE_CRON_DIR", "data/.state/cron.d")
)
JOB_STATE_DIR: Final[Path] = Path(os.environ.get("ROUTINES_JOB_STATE_DIR", "data/.state/jobs"))
LOG_DIR: Final[Path] = Path(os.environ.get("ROUTINES_LOG_DIR", "/var/log/supervisor"))
APP_REGISTRY: Final[Path] = Path(os.environ.get("ROUTINES_APP_REGISTRY", "data/.state/apps.toml"))

CATALOG_PATH: Final[Path] = Path(__file__).parent / "catalog.toml"

_RUN_JOB_MARKER: Final[str] = "run_job.sh"
_RECORD_RUN_MARKER: Final[str] = "record_run.py"
_ENV_WRAPPER_MARKER: Final[str] = "with_agent_env.sh"

# `supervisorctl status` prints "<name> <STATE> <description...>".
_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\S+)\s+(\S+)(?:\s+(.*))?$")

_SUPERVISORCTL_TIMEOUT_SECONDS: Final[float] = 10.0

# supervisord's own RPC endpoint, declared in system/supervisord.conf.
SUPERVISOR_SOCKET: Final[str] = os.environ.get(
    "ROUTINES_SUPERVISOR_SOCKET", "/var/run/supervisor.sock"
)

_SUPERVISOR_RPC_TIMEOUT: Final[float] = 5.0

# `pid 13242, uptime 5:36:22` -- supervisorctl's own wording.
_PID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bpid\s+(\d+)")

# What counts as "on" for a service's sample-data env var.
_TRUTHY_ENV_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


class ProcessStatus(FrozenModel):
    """What supervisord currently reports about one program."""

    state: str
    detail: str
    pid: int | None = None


class CatalogEntry(FrozenModel):
    """Human wording for one routine, plus the one fact only a human can supply
    (a service's internal refresh cadence)."""

    id: str
    title: str | None = None
    what: str | None = None
    app: str | None = None
    built_in: bool = False
    every: str | None = None
    every_seconds: int | None = None
    only_while_open: bool = False
    # Some routines exist to repair the workspace and are not the user's to
    # drive: no Run now, no pause, no change of cadence.
    locked: bool = False
    # An env var that, when set on the running process, means this service is
    # serving sample data rather than doing the real thing. Named per service
    # because only the service knows its own switch -- but checked live, so a
    # service taken out of demo mode starts reporting honestly on its own.
    demo_env: str | None = None


class Catalog(FrozenModel):
    jobs: dict[str, CatalogEntry]
    services: dict[str, CatalogEntry]


class JobState(FrozenModel):
    """``run_job.sh``'s record of a job, read fresh from its state directory."""

    last_attempt: datetime | None
    last_success: datetime | None
    consecutive_failures: int


class ScheduledRoutine(FrozenModel):
    """One cron entry: either a ``run_job.sh`` job or a plain cron line."""

    id: str
    command: tuple[str, ...]
    log_path: Path | None
    source_file: Path
    is_paused: bool
    is_installed_live: bool
    job_schedule: JobSchedule | None = None
    cron_schedule: CronSchedule | None = None
    state: JobState | None = None
    catalog: CatalogEntry | None = None


class ServiceRoutine(FrozenModel):
    """One always-running supervisord program that refreshes on its own clock."""

    id: str
    supervisor_state: str
    supervisor_detail: str
    catalog: CatalogEntry
    # True when the process is running in its own sample-data mode, so its
    # refresh loop is turning over fixed fixtures rather than fetching anything.
    is_serving_sample_data: bool = False


class Discovered(FrozenModel):
    scheduled: tuple[ScheduledRoutine, ...] = ()
    services: tuple[ServiceRoutine, ...] = ()


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    if not path.exists():
        return Catalog(jobs={}, services={})
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return Catalog(
        jobs={entry["id"]: _catalog_entry(entry) for entry in raw.get("job", [])},
        services={entry["id"]: _catalog_entry(entry) for entry in raw.get("service", [])},
    )


def _catalog_entry(raw: dict[str, Any]) -> CatalogEntry:
    return CatalogEntry(
        id=raw["id"],
        title=raw.get("title"),
        what=raw.get("what"),
        app=raw.get("app"),
        built_in=bool(raw.get("built_in", False)),
        every=raw.get("every"),
        every_seconds=raw.get("every_seconds"),
        only_while_open=bool(raw.get("only_while_open", False)),
        locked=bool(raw.get("locked", False)),
        demo_env=raw.get("demo_env"),
    )


def app_display_names(registry_path: Path = APP_REGISTRY) -> dict[str, str]:
    """``{app name: what the user sees it called}`` from the live app registry.

    The registry is what the workspace shell itself labels windows from, so a
    routine's app tag reads the same as the app's own window title rather than
    the internal name used in a cron entry.
    """
    try:
        raw = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {
        row["name"]: row.get("display_name") or row["name"]
        for row in raw.get("apps", [])
        if isinstance(row, dict) and "name" in row and not row.get("internal")
    }


def read_job_state(job_id: str, state_dir: Path = JOB_STATE_DIR) -> JobState:
    directory = state_dir / job_id
    return JobState(
        last_attempt=_read_epoch(directory / "last_attempt"),
        last_success=_read_epoch(directory / "last_success"),
        consecutive_failures=_read_int(directory / "failures"),
    )


def _read_epoch(path: Path) -> datetime | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return datetime.fromtimestamp(int(text))
    except ValueError:
        return None


def _read_int(path: Path) -> int:
    text = _read_text(path)
    if text is None:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _read_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def discover(
    live_cron_dir: Path = LIVE_CRON_DIR,
    durable_cron_dir: Path = DURABLE_CRON_DIR,
    job_state_dir: Path = JOB_STATE_DIR,
    catalog: Catalog | None = None,
) -> Discovered:
    """Read every source of truth and return what is currently scheduled and
    what is currently running."""
    resolved_catalog = catalog if catalog is not None else load_catalog()
    scheduled = _discover_scheduled(
        live_cron_dir, durable_cron_dir, job_state_dir, resolved_catalog
    )
    return Discovered(
        scheduled=tuple(scheduled), services=tuple(_discover_services(resolved_catalog))
    )


def _discover_scheduled(
    live_cron_dir: Path,
    durable_cron_dir: Path,
    job_state_dir: Path,
    catalog: Catalog,
) -> list[ScheduledRoutine]:
    """Every cron entry, live ones first. A durable entry that was never
    installed is still surfaced -- it is a routine someone intended to exist,
    and hiding it would make a broken install look like no install."""
    by_id: dict[str, ScheduledRoutine] = {}

    for directory, is_live in ((live_cron_dir, True), (durable_cron_dir, False)):
        for entry_file in _cron_entry_files(directory):
            for routine in _parse_cron_file(entry_file, is_live, job_state_dir, catalog):
                # The live copy wins: it is what cron is actually obeying.
                if routine.id not in by_id or is_live:
                    by_id[routine.id] = routine

    return sorted(by_id.values(), key=lambda routine: routine.id)


def _cron_entry_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    # cron itself ignores names containing a dot, so neither should this.
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and "." not in path.name and not path.name.startswith(".")
    )


def _parse_cron_file(
    path: Path,
    is_live: bool,
    job_state_dir: Path,
    catalog: Catalog,
) -> list[ScheduledRoutine]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    routines: list[ScheduledRoutine] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # A commented-out line is the documented way to pause a job without
        # losing its definition, so it is shown as paused rather than dropped.
        is_paused = stripped.startswith("#")
        candidate = stripped.lstrip("#").strip() if is_paused else stripped
        if not candidate or "=" in candidate.split()[0]:
            continue
        routine = _parse_cron_line(candidate, path, is_live, is_paused, job_state_dir, catalog)
        if routine is not None:
            routines.append(routine)
    return routines


def _parse_cron_line(
    line: str,
    source_file: Path,
    is_live: bool,
    is_paused: bool,
    job_state_dir: Path,
    catalog: Catalog,
) -> ScheduledRoutine | None:
    tokens = line.split()
    # Five schedule fields, then the user, then the command.
    if len(tokens) < 7:
        return None
    schedule_fields = " ".join(tokens[:5])
    command_tokens = tokens[6:]
    command_tokens, log_path = _split_off_redirect(command_tokens)

    if command_tokens and command_tokens[0].endswith(_ENV_WRAPPER_MARKER):
        command_tokens = command_tokens[1:]

    job_index = _index_of_suffix(command_tokens, _RUN_JOB_MARKER)
    if job_index is not None:
        return _parse_run_job(
            command_tokens[job_index + 1 :],
            source_file,
            is_live,
            is_paused,
            log_path,
            job_state_dir,
            catalog,
        )

    # A plain cron line: the five fields are the whole schedule, and the entry
    # file's name is the only identifier it has.
    return ScheduledRoutine(
        id=source_file.name,
        command=tuple(_strip_run_recorder(command_tokens)),
        log_path=log_path,
        source_file=source_file,
        is_paused=is_paused,
        is_installed_live=is_live,
        cron_schedule=CronSchedule(fields=schedule_fields),
        catalog=catalog.jobs.get(source_file.name),
    )


def _parse_run_job(
    tokens: list[str],
    source_file: Path,
    is_live: bool,
    is_paused: bool,
    log_path: Path | None,
    job_state_dir: Path,
    catalog: Catalog,
) -> ScheduledRoutine | None:
    """``<job-id> --every N [--at H] [--retry-after N] <command...>``."""
    if not tokens:
        return None
    job_id = tokens[0]
    rest = tokens[1:]

    every_seconds: int | None = None
    at_hour: int | None = None
    retry_seconds: int | None = None

    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--every" and index + 1 < len(rest):
            every_seconds = parse_duration_seconds(rest[index + 1])
            index += 2
        elif token == "--at" and index + 1 < len(rest):
            at_hour = _parse_hour(rest[index + 1])
            index += 2
        elif token == "--retry-after" and index + 1 < len(rest):
            retry_seconds = parse_duration_seconds(rest[index + 1])
            index += 2
        else:
            break

    if every_seconds is None:
        return None

    schedule = JobSchedule(
        every_seconds=every_seconds,
        at_hour=at_hour,
        **({"retry_after_seconds": retry_seconds} if retry_seconds is not None else {}),
    )
    return ScheduledRoutine(
        id=job_id,
        command=tuple(_strip_run_recorder(rest[index:])),
        log_path=log_path,
        source_file=source_file,
        is_paused=is_paused,
        is_installed_live=is_live,
        job_schedule=schedule,
        state=read_job_state(job_id, job_state_dir),
        catalog=catalog.jobs.get(job_id),
    )


def _strip_run_recorder(tokens: list[str]) -> list[str]:
    """Unwrap ``record_run.py --job X --trigger scheduled -- <real command>``.

    The recorder is this app's own wrapper for logging each run; the user cares
    about the command underneath it, not the bookkeeping around it.
    """
    index = _index_of_suffix(tokens, _RECORD_RUN_MARKER)
    if index is None:
        return tokens
    if "--" in tokens[index:]:
        return tokens[index + tokens[index:].index("--") + 1 :]
    return tokens


def _index_of_suffix(tokens: list[str], suffix: str) -> int | None:
    for index, token in enumerate(tokens):
        if token.endswith(suffix):
            return index
    return None


def _split_off_redirect(tokens: list[str]) -> tuple[list[str], Path | None]:
    """Peel ``>> /var/log/... 2>&1`` off the end of a cron command."""
    log_path: Path | None = None
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in (">>", ">") and index + 1 < len(tokens):
            log_path = Path(tokens[index + 1])
            index += 2
            continue
        if token == "2>&1" or token.startswith("2>"):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned, log_path


def _parse_hour(text: str) -> int | None:
    try:
        hour = int(text)
    except ValueError:
        return None
    return hour if 0 <= hour <= 23 else None


class _UnixSocketTransport(xmlrpc.client.Transport):
    """XML-RPC over supervisord's unix socket.

    Asking for status by shelling out to ``supervisorctl`` costs ~1.6s, almost
    all of it a second Python interpreter starting up -- which the user feels
    as a page that sits blank. Talking to the same RPC interface directly over
    the socket is milliseconds.
    """

    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def make_connection(self, host: str) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("localhost", timeout=_SUPERVISOR_RPC_TIMEOUT)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(_SUPERVISOR_RPC_TIMEOUT)
        connection.sock.connect(self._socket_path)
        return connection


def supervisor_status() -> dict[str, ProcessStatus]:
    """``{program: status}`` from supervisord, or empty if it cannot be reached
    (the app still renders -- a missing supervisor is not a reason to show the
    user a blank page)."""
    statuses = _supervisor_status_over_socket()
    return statuses if statuses is not None else _supervisor_status_over_cli()


def _supervisor_status_over_socket() -> dict[str, ProcessStatus] | None:
    """None (rather than empty) when the socket is unusable, so the caller can
    tell "supervisord says nothing is running" apart from "could not ask"."""
    if not Path(SUPERVISOR_SOCKET).exists():
        return None
    try:
        server = xmlrpc.client.ServerProxy(
            "http://localhost", transport=_UnixSocketTransport(SUPERVISOR_SOCKET)
        )
        processes = server.supervisor.getAllProcessInfo()
    except (OSError, xmlrpc.client.Error) as error:
        # Worth a log line: silently degrading to the slow path would hide a
        # permissions problem behind nothing but a sluggish page.
        logger.warning("supervisord RPC unavailable ({}); falling back to the CLI", error)
        return None

    return {
        process["name"]: ProcessStatus(
            state=process["statename"],
            detail=process.get("description", ""),
            pid=process.get("pid") or None,
        )
        for process in processes
        if isinstance(process, dict) and "name" in process
    }


def _supervisor_status_over_cli() -> dict[str, ProcessStatus]:
    try:
        completed = subprocess.run(
            ["supervisorctl", "status"],
            capture_output=True,
            text=True,
            timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    statuses: dict[str, ProcessStatus] = {}
    for line in completed.stdout.splitlines():
        match = _STATUS_PATTERN.match(line.strip())
        if match is None:
            continue
        detail = (match.group(3) or "").strip()
        statuses[match.group(1)] = ProcessStatus(
            state=match.group(2), detail=detail, pid=_pid_in(detail)
        )
    return statuses


def _pid_in(detail: str) -> int | None:
    """``pid 13242, uptime 5:36:22`` -> 13242. The RPC path reports the pid as
    a field; the CLI only prints it inside this sentence."""
    match = _PID_PATTERN.search(detail)
    return int(match.group(1)) if match else None


def is_serving_sample_data(pid: int | None, demo_env: str | None) -> bool:
    """Whether the running process has its own sample-data switch turned on.

    Read from the live process rather than from any config file: the switch can
    come from the program block, an export, or a hand restart, and the only
    thing that settles it is what the process is actually running with. A
    service flipped out of demo mode therefore starts reporting honestly on the
    next page load, with no edit here.
    """
    if not demo_env or not pid:
        return False
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        # No permission or the process just exited; claiming "real data" on a
        # failed read would be the misleading direction, but so would claiming
        # "sample data", and the service's own state is the weaker claim.
        return False
    for item in raw.split(b"\0"):
        name, separator, value = item.decode("utf-8", errors="replace").partition("=")
        if separator and name == demo_env:
            return value.strip().lower() in _TRUTHY_ENV_VALUES
    return False


def _discover_services(catalog: Catalog) -> list[ServiceRoutine]:
    """The declared self-refreshing services, with their live supervisord state.

    Unlike scheduled jobs these cannot be discovered -- a refresh loop inside a
    process leaves no trace on disk -- so the catalog is the source, and a
    declared service that no longer has a program is dropped rather than shown
    as broken.
    """
    statuses = supervisor_status()
    services: list[ServiceRoutine] = []
    for entry in catalog.services.values():
        status = statuses.get(entry.id)
        if status is None:
            continue
        services.append(
            ServiceRoutine(
                id=entry.id,
                supervisor_state=status.state,
                supervisor_detail=status.detail,
                catalog=entry,
                is_serving_sample_data=is_serving_sample_data(status.pid, entry.demo_env),
            )
        )
    return sorted(services, key=lambda service: service.id)
