"""Temp hygiene (``tests/tmp_hygiene.py``): every temp file of a run under /var/tmp, then gone.

The inner pytest runs load only the plugin and start without TMPDIR, as a plain ``uv run pytest``
from a shell does. Their probe names its files after a random token, so what a run leaves in
/tmp can be told apart from what other, concurrent runs create there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests import tmp_hygiene
from tests.tmp_hygiene import BASE_DIR, PREFIX

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_TMP = Path("/tmp")
TMP_SCAN_DEPTH = 3  # /tmp/pytest-of-<user>/pytest-<n>/<test dir>
INNER_TIMEOUT_S = 60.0
HEADER = re.compile(r"^tempdir: (\S+)", re.MULTILINE)
PRINT_TEMPDIR = "import tempfile; print(tempfile.gettempdir())"
SLEEP = "import time; time.sleep(120)"
UNSET_FOR_INNER_RUNS = (
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DEBUG_TEMPROOT",
)

PROBE = """
import json, os, subprocess, sys, time
from pathlib import Path

CHILD = "import tempfile; print(tempfile.mkstemp(prefix={token!r})[1])"


def test_probe_{token}(tmp_path):
    (tmp_path / "{token}.txt").write_text("in tmp_path")
    child = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True)
    barrier = Path(os.environ["PROBE_BARRIER"])
    (barrier / str(os.getpid())).touch()
    parties, deadline = int(os.environ["PROBE_PARTIES"]), time.monotonic() + 30
    while len(list(barrier.iterdir())) < parties and time.monotonic() < deadline:
        time.sleep(0.05)
    (tmp_path / "after-barrier.txt").write_text("not swept by a concurrent run")
    report = {{
        "tmp_path": str(tmp_path),
        "child_file": child.stdout.strip(),
        "tmpdir": os.environ.get("TMPDIR"),
    }}
    Path(os.environ["PROBE_REPORT"]).write_text(json.dumps(report))
    assert os.environ["PROBE_FAIL"] == "0", "failing on purpose"
"""


@dataclass(frozen=True)
class InnerRun:
    pid: int
    returncode: int
    output: str
    report: dict[str, str]

    @property
    def run_dir(self) -> Path:
        return reported_run_dir(self.output)


class Probe:
    """A generated test file and an inner ``python -m pytest -p tests.tmp_hygiene`` run of it."""

    def __init__(
        self, workdir: Path, *, fail: bool = False, barrier: Path | None = None, parties: int = 1
    ) -> None:
        self.token = f"hygiene{uuid.uuid4().hex[:12]}"
        self.dir = workdir / self.token
        (self.dir / "barrier").mkdir(parents=True)
        self.file = self.dir / f"test_probe_{self.token}.py"
        self.file.write_text(PROBE.format(token=self.token))
        self.report = self.dir / "report.json"
        self.env = {k: v for k, v in os.environ.items() if k not in UNSET_FOR_INNER_RUNS}
        self.env |= {
            "PYTHONPATH": str(ROOT),  # for -p tests.tmp_hygiene
            "PROBE_REPORT": str(self.report),
            "PROBE_BARRIER": str(barrier or self.dir / "barrier"),
            "PROBE_PARTIES": str(parties),
            "PROBE_FAIL": "1" if fail else "0",
        }

    def start(self, *args: str) -> None:
        command = [sys.executable, "-m", "pytest", "-p", "tests.tmp_hygiene"]
        self.process = subprocess.Popen(
            [*command, "-p", "no:cacheprovider", *args, self.file.name],
            cwd=self.dir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def finish(self) -> InnerRun:
        try:
            output, _ = self.process.communicate(timeout=INNER_TIMEOUT_S)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.communicate()
        report = json.loads(self.report.read_text()) if self.report.exists() else {}
        return InnerRun(self.process.pid, self.process.returncode, output, report)

    def run(self, *args: str) -> InnerRun:
        self.start(*args)
        return self.finish()


def reported_run_dir(header: str) -> Path:
    match = HEADER.search(header)
    assert match, f"no 'tempdir:' line in the pytest header:\n{header}"
    return Path(match[1])


def header_of(config: pytest.Config) -> str:
    """The header lines that plugins report for this run."""
    lines: list[str] = []
    for result in config.hook.pytest_report_header(config=config, start_path=config.rootpath):
        lines += [result] if isinstance(result, str) else result
    return "\n".join(lines)


def tmp_traces(token: str, since: float) -> list[str]:
    """Paths under /tmp named after ``token``, searched in our dirs changed since ``since``."""

    def ours_and_changed(entry: os.DirEntry[str]) -> bool:
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        return info.st_uid == os.getuid() and info.st_mtime >= since

    def search(directory: str, depth: int) -> list[str]:
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except FileNotFoundError:  # removed meanwhile, e.g. an old numbered dir of another run
            return []
        found = [entry.path for entry in entries if token in entry.name]
        for entry in entries if depth > 1 else ():
            if entry.is_dir(follow_symlinks=False) and ours_and_changed(entry):
                found += search(entry.path, depth - 1)
        return found

    return search(str(SYSTEM_TMP), TMP_SCAN_DEPTH)


def assert_kept_in_its_run_dir_and_removed(run: InnerRun) -> None:
    run_dir = run.run_dir
    for key in ("tmp_path", "child_file", "tmpdir"):
        assert Path(run.report[key]).is_relative_to(run_dir), (key, run.report, run_dir)
    assert not run_dir.exists(), f"{run_dir} was not removed"


def make_run_dir(base: Path, pid: int, tag: str = "x") -> Path:
    path = base / f"{PREFIX}{pid}-{tag}"
    (path / "tmp").mkdir(parents=True)
    (path / "tmp" / "leftover.txt").write_text("from a killed run")
    return path


@pytest.fixture
def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def live_pid() -> Iterator[int]:
    """The pid of a sleeping helper process, killed afterwards."""
    process = subprocess.Popen([sys.executable, "-c", SLEEP])
    yield process.pid
    process.kill()
    process.wait()


def test_tmp_path_tempfile_and_child_processes_all_use_this_runs_dir_under_var_tmp(
    pytestconfig: pytest.Config, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    run_dir = reported_run_dir(header_of(pytestconfig))
    child = subprocess.run(
        [sys.executable, "-c", PRINT_TEMPDIR],
        capture_output=True,
        text=True,
        check=True,
        timeout=INNER_TIMEOUT_S,
    )

    assert run_dir.is_relative_to(BASE_DIR)
    assert tmp_path.is_relative_to(run_dir)
    assert tmp_path_factory.getbasetemp().is_relative_to(run_dir)
    tempdirs = {Path(os.environ["TMPDIR"]), Path(tempfile.gettempdir()), Path(child.stdout.strip())}
    [tempdir] = tempdirs
    assert tempdir.is_relative_to(run_dir)
    assert tempdir.is_dir()


@pytest.mark.parametrize("fail", [False, True], ids=["passing", "failing"])
def test_a_plain_run_keeps_its_temp_files_in_a_private_run_dir_and_removes_it(
    tmp_path: Path, fail: bool
) -> None:
    since = time.time() - 1  # mtime granularity
    probe = Probe(tmp_path, fail=fail)

    run = probe.run()

    assert run.returncode == (1 if fail else 0), run.output
    assert tmp_traces(probe.token, since) == []
    assert run.run_dir.parent == BASE_DIR
    assert re.fullmatch(rf"{PREFIX}{run.pid}-\w+", run.run_dir.name), run.run_dir
    assert_kept_in_its_run_dir_and_removed(run)


def test_an_explicit_basetemp_becomes_the_run_dir_and_is_removed_too(tmp_path: Path) -> None:
    given = tmp_path / "given"  # does not exist yet

    run = Probe(tmp_path).run("--basetemp", str(given))

    assert run.returncode == 0, run.output
    assert run.run_dir == given
    assert_kept_in_its_run_dir_and_removed(run)


def test_a_symlinked_basetemp_fails_the_run_and_leaves_the_link_target_alone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "not-test-output"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    run = Probe(tmp_path).run("--basetemp", str(link))

    assert run.returncode != 0, run.output
    assert "symbolic link" in run.output
    assert (target / "keep.txt").read_text() == "keep"
    assert link.is_symlink()


def test_a_basetemp_in_a_missing_dir_fails_the_run_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    run = Probe(tmp_path).run("--basetemp", str(missing / "given"))

    assert run.returncode != 0, run.output
    assert not missing.exists()


def test_two_concurrent_runs_both_pass_and_both_clean_up(tmp_path: Path) -> None:
    since = time.time() - 1
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    probes = [Probe(tmp_path, barrier=barrier, parties=2) for _ in range(2)]

    for probe in probes:
        probe.start()
    runs = [probe.finish() for probe in probes]

    assert [run.returncode for run in runs] == [0, 0], [run.output for run in runs]
    assert len(list(barrier.iterdir())) == 2  # both were inside their test at the same time
    assert runs[0].run_dir != runs[1].run_dir
    for probe, run in zip(probes, runs, strict=True):
        assert tmp_traces(probe.token, since) == []
        assert_kept_in_its_run_dir_and_removed(run)


def test_a_run_sweeps_stale_run_dirs_in_var_tmp_and_keeps_live_ones(
    tmp_path: Path, dead_pid: int, live_pid: int
) -> None:
    tag = uuid.uuid4().hex[:12]
    stale = make_run_dir(BASE_DIR, dead_pid, tag)
    live = make_run_dir(BASE_DIR, live_pid, tag)
    try:
        run = Probe(tmp_path).run()

        assert run.returncode == 0, run.output
        assert not stale.exists()
        assert live.exists()
    finally:
        for path in (stale, live):
            shutil.rmtree(path, ignore_errors=True)


def test_the_sweep_removes_only_our_run_dirs_whose_process_is_gone(
    tmp_path: Path, dead_pid: int, live_pid: int
) -> None:
    stale = make_run_dir(tmp_path, dead_pid)
    not_a_dir = tmp_path / f"{PREFIX}{dead_pid}-file"
    not_a_dir.write_text("not a directory")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / f"{PREFIX}{dead_pid}-link"
    link.symlink_to(target, target_is_directory=True)
    no_pid = tmp_path / f"{PREFIX}no-pid"
    no_pid.mkdir()
    kept = [
        make_run_dir(tmp_path, live_pid),  # a concurrent run
        make_run_dir(tmp_path, 1),  # init: alive, and not ours to signal
        not_a_dir,
        link,
        no_pid,
        target,
    ]

    swept = tmp_hygiene.sweep_stale_run_dirs(tmp_path)

    assert swept == [stale]
    assert not stale.exists()
    assert [path for path in kept if not path.exists()] == []
    assert link.is_symlink()


def test_the_sweep_leaves_a_dead_runs_dir_of_another_user_alone(
    tmp_path: Path, dead_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = make_run_dir(tmp_path, dead_pid)
    uid = os.getuid()

    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: uid + 1)
        swept = tmp_hygiene.sweep_stale_run_dirs(tmp_path)

    assert swept == []
    assert foreign.exists()


def test_the_sweep_takes_a_pid_too_large_for_the_os_as_dead_instead_of_crashing(
    tmp_path: Path,
) -> None:
    impossible = 2**31  # beyond pid_t: junk that anyone can leave in /var/tmp
    stale = make_run_dir(tmp_path, impossible)
    junk = tmp_path / f"{PREFIX}{impossible}-file"
    junk.write_text("not a directory")

    assert tmp_hygiene.sweep_stale_run_dirs(tmp_path) == [stale]
    assert junk.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can remove anything")
def test_a_run_dir_that_cannot_be_removed_fails_the_sweep_loudly(
    tmp_path: Path, dead_pid: int
) -> None:
    stale = make_run_dir(tmp_path, dead_pid)
    locked = stale / "tmp"
    locked.chmod(0o500)  # its leftover.txt cannot be unlinked
    try:
        with pytest.raises(PermissionError):
            tmp_hygiene.sweep_stale_run_dirs(tmp_path)
    finally:
        locked.chmod(0o700)


def test_the_sweep_skips_a_run_dir_that_a_concurrent_sweep_removed_first(
    tmp_path: Path, dead_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = make_run_dir(tmp_path, dead_pid, "gone")
    stale = make_run_dir(tmp_path, dead_pid, "stale")

    def alive_while_another_run_sweeps(pid: int) -> bool:
        shutil.rmtree(gone, ignore_errors=True)
        return False

    monkeypatch.setattr(tmp_hygiene, "_alive", alive_while_another_run_sweeps)

    assert tmp_hygiene.sweep_stale_run_dirs(tmp_path) == [stale]
    assert not stale.exists()
