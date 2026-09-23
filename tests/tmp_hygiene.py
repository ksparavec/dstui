"""pytest plugin: keep every temp file of a test run under /var/tmp and remove it afterwards.

/tmp is a RAM tmpfs with about 1M inodes shared by the whole machine (see CLAUDE.md), and a full
e2e run creates about 100k files. So each run gets a private directory
``/var/tmp/dstui-pytest-<pid>-<random>`` holding pytest's basetemp (``tmp_path``) and the
``TMPDIR`` of this process and its children: the runtime, ``python -m dstui``, the agent's shell.
The directory is removed when the session ends, pass or fail. Directories of killed runs (dead
pid) are removed when the next run starts; those of live, concurrent runs are left alone. An
explicit ``--basetemp`` becomes the run directory instead (pytest treats it as disposable too).

No project imports, so a test can load it on its own: ``pytest -p tests.tmp_hygiene``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

BASE_DIR = Path("/var/tmp")
PREFIX = "dstui-pytest-"
_OWNER_PID = re.compile(rf"{re.escape(PREFIX)}(\d+)-")
_RUN_DIR = pytest.StashKey[Path]()


@pytest.hookimpl(tryfirst=True)  # before pytest's tmpdir plugin reads --basetemp
def pytest_configure(config: pytest.Config) -> None:
    sweep_stale_run_dirs()
    run_dir = _make_run_dir(config.option.basetemp)
    config.add_cleanup(lambda: _rm_rf(run_dir))
    config.stash[_RUN_DIR] = run_dir
    tmp = run_dir / "tmp"  # beside basetemp, not in it: pytest empties basetemp on first use
    tmp.mkdir()
    patch = pytest.MonkeyPatch()  # undone before the removal: cleanups run last in, first out
    config.add_cleanup(patch.undo)
    patch.setattr(config.option, "basetemp", str(run_dir / "basetemp"))
    patch.setenv("TMPDIR", str(tmp))  # children inherit it
    patch.setattr(tempfile, "tempdir", None)  # gettempdir() reads TMPDIR again


def pytest_report_header(config: pytest.Config) -> str:
    return f"tempdir: {config.stash[_RUN_DIR]} (tmp_path and TMPDIR, removed at exit)"


def sweep_stale_run_dirs(base: Path = BASE_DIR) -> list[Path]:
    """Remove our run directories in ``base`` whose pytest process is gone; return them."""
    swept = []
    for path in sorted(base.glob(f"{PREFIX}*")):
        owner = _OWNER_PID.match(path.name)
        if owner is None or _alive(int(owner[1])) or not _own_dir(path):
            continue
        _rm_rf(path)
        swept.append(path)
    return swept


def _make_run_dir(given_basetemp: str | None) -> Path:
    if given_basetemp is None:
        return Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-", dir=BASE_DIR))
    run_dir = Path(given_basetemp).resolve()
    _rm_rf(run_dir)  # as pytest does with a given basetemp
    run_dir.mkdir(mode=0o700, parents=True)
    return run_dir


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, but another user's
        return True
    return True


def _own_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:  # a concurrent run swept it first
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def _rm_rf(path: Path) -> None:
    shutil.rmtree(path, onexc=_ignore_missing)


def _ignore_missing(function: Callable[..., object], name: str, error: BaseException) -> None:
    if not isinstance(error, FileNotFoundError):  # gone already is fine; anything else is not
        raise error
