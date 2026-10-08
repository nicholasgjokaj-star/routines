"""Handing a broken routine to an agent, with everything it needs to start.

The Fix button exists because the failure and the evidence are already on this
page: the app knows the cadence, the exact command, how long it has been
failing, and what the last run printed. Making the user retype any of that into
a chat -- or worse, making the agent rediscover it -- wastes the one advantage
this view has.

So the button writes the brief itself and opens a chat on it, through
``system/scripts/message_chat.py --create``, which makes the chat, has the
shell surface its window (``auto_open=true``), and sends the brief as its first
message. The brief is addressed to the ``/assist`` skill, the workspace's own
diagnose-confirm-fix flow, so the agent arrives with a procedure rather than
just a complaint.
"""

import json
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from routines.ledger import RunRecord
from routines.view import Row

MESSAGE_CHAT_SCRIPT: Final[Path] = Path("system/scripts/message_chat.py")

# The create makes an agent and waits on the chat app; a few seconds is normal.
CREATE_TIMEOUT_SECONDS: Final[float] = 120.0

# How many recent runs the brief summarizes. Enough to show whether the routine
# has been failing all along or only since a particular run.
RUNS_IN_BRIEF: Final[int] = 8

# Chat names must stay short enough to read in a tab.
MAX_CHAT_NAME_CHARS: Final[int] = 40


class DiagnosisRequestError(RuntimeError):
    """Raised when the debugging chat could not be started."""


def build_brief(row: Row, command: tuple[str, ...], runs: list[RunRecord]) -> str:
    """The first message of the debugging chat.

    Front-loads the evidence the agent would otherwise spend its first several
    tool calls gathering, and names the files rather than describing them, so
    it can go straight to reproducing the failure.
    """
    lines = [
        f"/assist The scheduled routine \"{row.title}\" is failing and I would like it fixed.",
        "",
        "This brief was written by the Routines app, which watches every scheduled job",
        "in this workspace. Everything below is read from the live state, not remembered.",
        "",
        "## What it is",
        "",
        f"- Shows in Routines as: {row.title}",
        f"- Job id: `{row.id}`",
        f"- What it is supposed to do: {row.what}",
    ]
    if row.app_label:
        lines.append(f"- Belongs to the app: {row.app_label}")
    lines += [
        f"- Schedule: {row.schedule}",
        f"- Command it runs: `{' '.join(command)}`",
        "",
        "## What is wrong",
        "",
        f"- Current state: {row.health_label}",
    ]
    if row.health_detail:
        lines.append(f"- {row.health_detail}")
    lines += [
        "",
        "## Where to look",
        "",
        f"- Its schedule entry: `{row.raw_source}`",
    ]
    if row.log_path:
        lines.append(f"- Its log: `{row.log_path}`")
    lines += [
        f"- The runner's state for it: `data/.state/jobs/{row.id}/`"
        " (`last_attempt`, `last_success`, `failures`)",
        "- The run log, one JSON line per run: `data/.apps/routines/runs.jsonl`",
        "- How the scheduling works: the `manage-scheduled-tasks` skill;"
        " the runner is `system/libs/automations/run_job.sh`",
        "",
        _runs_section(runs),
        "",
        "## What I would like",
        "",
        "Reproduce the failure by running the command above yourself, find the real cause,",
        "and fix it. Two things to keep in mind for this particular kind of problem:",
        "",
        "- The routine is still retrying on its schedule while you work, so a fix can be",
        "  confirmed by watching the next run succeed rather than only by reasoning.",
        "- If the cause turns out to be an expired or missing third-party connection rather",
        "  than a bug, say so plainly -- that needs the user to reconnect it, not a code change.",
        "",
        "When you are done, tell me in plain terms what was wrong and whether it is fixed.",
    ]
    return "\n".join(lines)


def _runs_section(runs: list[RunRecord]) -> str:
    if not runs:
        return (
            "## Recent runs\n\n"
            "None recorded yet. The run log only covers runs since the Routines app was\n"
            "built, so this routine has not run through it -- the log file above is the\n"
            "evidence to use instead."
        )

    lines = ["## Recent runs", "", "Newest first."]
    for record in runs[:RUNS_IN_BRIEF]:
        verdict = "worked" if record.did_succeed else f"FAILED (exit {record.exit_code})"
        lines.append(
            f"\n- {record.started_at:%b %-d %H:%M} · {record.trigger} · {verdict}"
        )
        if record.output.strip():
            lines.append("\n  ```\n" + _indent(record.output.strip()) + "\n  ```")
    return "\n".join(lines)


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


def chat_name_for(row: Row) -> str:
    """A tab label the user can recognize among their other chats."""
    name = f"Fix {row.title}"
    return name[:MAX_CHAT_NAME_CHARS]


def start_debugging_chat(brief: str, name: str, repo_root: Path) -> str:
    """Create the chat, surface it, and send the brief. Returns its chat id.

    ``--label auto_open=true`` is what makes the window appear on the user's
    screen: pressing Fix and being taken nowhere would read as the button not
    working.
    """
    script = repo_root / MESSAGE_CHAT_SCRIPT
    try:
        completed = subprocess.run(
            [
                "python3",
                str(script),
                "--create",
                "--name",
                name,
                "--label",
                "auto_open=true",
                "-m",
                brief,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=CREATE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiagnosisRequestError(f"could not start a chat: {error}") from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        logger.error("starting a debugging chat failed (rc={}): {}", completed.returncode, detail)
        raise DiagnosisRequestError(detail or "the chat could not be created")

    return _chat_id_from(completed.stdout)


def _chat_id_from(stdout: str) -> str:
    """The create prints one JSON line naming the chat. An unreadable line is
    not a failure -- the chat exists and its window is opening either way, so
    the caller just cannot link to it."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "chat_id" in parsed:
            return str(parsed["chat_id"])
    return ""
