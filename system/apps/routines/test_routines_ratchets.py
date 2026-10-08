from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(0))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    # All three are in bin/record_run.py, and all deliberate. That script is the
    # cron-side run wrapper: two of them relay the command's own stdout and
    # stderr onward so the routine's log file still receives everything, and the
    # third is its single _warn helper, since it runs from cron with no app
    # environment and imports nothing beyond the standard library -- a logger is
    # not available to it. Everything in src/routines/ uses loguru; keep this
    # budget pinned to that one file.
    rc.check_bare_print(_DIR, snapshot(3))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))

