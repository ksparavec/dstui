"""The ``dstui`` entry point: command line -> SDK config -> AgentBridge -> DsTuiApp."""

from __future__ import annotations

import logging
import os
import re
import runpy
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest
from deepseek_harness import DeepSeekHarnessConfig

import dstui
from dstui.app import DsTuiApp
from dstui.bridge import AgentBridge
from dstui.config import build_harness_config, parse_args
from tests.conftest import runtime_pids_under

CLI_TIMEOUT_S = 60.0
CONSOLE_SCRIPT = Path(sys.executable).with_name("dstui")


class RunSpy:
    """Replaces ``DsTuiApp.run``: records each app and call instead of starting a terminal UI."""

    def __init__(
        self,
        *,
        exit_code: int | None = None,
        error: Exception | None = None,
        during_run: Callable[[], None] | None = None,
    ) -> None:
        self.apps: list[DsTuiApp] = []
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.exit_code = exit_code
        self.error = error
        self.during_run = during_run

    def __call__(self, app: DsTuiApp, *args: object, **kwargs: object) -> None:
        self.apps.append(app)
        self.calls.append((args, kwargs))
        if self.during_run is not None:
            self.during_run()
        if self.exit_code is not None:
            app.exit(return_code=self.exit_code)
        if self.error is not None:
            raise self.error


class SpyBridge(AgentBridge):
    """A real AgentBridge that records the config it was built with and its close() calls."""

    instances: list[SpyBridge]

    def __init__(self, config: DeepSeekHarnessConfig) -> None:
        super().__init__(config)
        self.config = config
        self.closed = 0
        SpyBridge.instances.append(self)

    def close(self) -> None:
        self.closed += 1
        super().close()


@pytest.fixture
def spy_bridge(monkeypatch: pytest.MonkeyPatch) -> type[SpyBridge]:
    SpyBridge.instances = []
    monkeypatch.setattr(dstui, "AgentBridge", SpyBridge)
    return SpyBridge


def install_run_spy(monkeypatch: pytest.MonkeyPatch, spy: RunSpy) -> RunSpy:
    def run(app: DsTuiApp, *args: object, **kwargs: object) -> None:  # bound like App.run
        spy(app, *args, **kwargs)

    monkeypatch.setattr(DsTuiApp, "run", run)
    return spy


def cli_args(tmp_path: Path, *extra: str) -> list[str]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return ["-w", str(workspace), "--data-dir", str(tmp_path / "data"), *extra]


def cli_env(tmp_path: Path) -> dict[str, str]:
    """The current environment without an API key, with dstui's state under ``tmp_path``."""
    env = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"}
    env["DSTUI_HOME"] = str(tmp_path / "home")
    return env


def run_cli(command: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, env=env, cwd=cwd, capture_output=True, text=True, timeout=CLI_TIMEOUT_S
    )


def dstui_file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger("dstui").handlers if isinstance(h, logging.FileHandler)]


def test_main_wires_settings_config_bridge_and_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    spy = install_run_spy(monkeypatch, RunSpy())
    argv = cli_args(
        tmp_path, "--profile", "sdk-minimal", "-m", "deepseek-v4-pro", "--effort", "low"
    )

    code = dstui.main([*argv, "--max-tokens", "123"])

    assert code == 0
    expected = parse_args([*argv, "--max-tokens", "123"])
    assert expected.api_key_set is True
    [app] = spy.apps
    assert spy.calls == [((), {})]  # full screen on the real terminal driver
    assert app.settings == expected
    [bridge] = spy_bridge.instances
    assert app.backend is bridge
    assert bridge.config == build_harness_config(expected)
    assert expected.dsh_home.is_dir()  # build_harness_config ran for real
    assert expected.patch_file.is_file()
    assert runtime_pids_under(tmp_path) == []  # nothing boots before the app runs


def test_main_reads_sys_argv_when_no_argv_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    spy = install_run_spy(monkeypatch, RunSpy())
    argv = cli_args(tmp_path, "--profile", "sdk-minimal")
    monkeypatch.setattr(sys, "argv", ["dstui", *argv])

    assert dstui.main() == 0

    [app] = spy.apps
    assert app.settings == parse_args(argv)
    assert app.settings.profile == "sdk-minimal"
    assert app.settings.api_key_set is False


def test_main_closes_the_bridge_after_the_app_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    install_run_spy(monkeypatch, RunSpy())

    dstui.main(cli_args(tmp_path))

    [bridge] = spy_bridge.instances
    assert bridge.closed >= 1


def test_main_closes_the_bridge_when_the_app_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    install_run_spy(monkeypatch, RunSpy(error=RuntimeError("terminal gone")))

    with pytest.raises(RuntimeError, match="terminal gone"):
        dstui.main(cli_args(tmp_path))

    [bridge] = spy_bridge.instances
    assert bridge.closed >= 1
    assert dstui_file_handlers() == []


def test_main_logs_warnings_and_errors_to_the_data_dir_while_the_app_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    def log_from_the_app() -> None:
        logging.getLogger("dstui.app").error("boom")
        logging.getLogger("dstui.bridge").warning("careful")

    install_run_spy(monkeypatch, RunSpy(during_run=log_from_the_app))

    assert dstui.main(cli_args(tmp_path)) == 0

    lines = (tmp_path / "data" / "dstui.log").read_text(encoding="utf-8").splitlines()
    stamp = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"
    assert len(lines) == 2
    assert re.fullmatch(rf"{stamp} ERROR dstui\.app: boom", lines[0])
    assert re.fullmatch(rf"{stamp} WARNING dstui\.bridge: careful", lines[1])
    assert dstui_file_handlers() == []  # detached (and closed) when the app is gone
    logging.getLogger("dstui.app").error("after the app")
    assert "after the app" not in (tmp_path / "data" / "dstui.log").read_text(encoding="utf-8")


def test_main_reports_an_unwritable_log_file_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spy_bridge: type[SpyBridge],
) -> None:
    spy = install_run_spy(monkeypatch, RunSpy())
    (tmp_path / "data" / "dstui.log").mkdir(parents=True)  # a directory cannot be opened

    code = dstui.main(cli_args(tmp_path))

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("dstui: cannot prepare the data directory")
    assert "Traceback" not in err
    assert spy.apps == []
    assert spy_bridge.instances == []
    assert dstui_file_handlers() == []


def test_main_returns_the_app_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_bridge: type[SpyBridge]
) -> None:
    install_run_spy(monkeypatch, RunSpy(exit_code=3))

    assert dstui.main(cli_args(tmp_path)) == 3


def test_main_reports_an_unusable_data_dir_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spy_bridge: type[SpyBridge],
) -> None:
    spy = install_run_spy(monkeypatch, RunSpy())
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    data_dir = blocker / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    code = dstui.main(["-w", str(workspace), "--data-dir", str(data_dir)])

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("dstui: cannot prepare the data directory")
    assert str(data_dir) in err
    assert "Traceback" not in err
    assert spy.apps == []
    assert spy_bridge.instances == []


def test_main_exits_2_with_a_usage_error_for_bad_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spy = install_run_spy(monkeypatch, RunSpy())

    with pytest.raises(SystemExit) as raised:
        dstui.main(cli_args(tmp_path, "--profile", "nope"))

    assert raised.value.code == 2
    assert "invalid choice: 'nope'" in capsys.readouterr().err
    assert spy.apps == []


def test_python_m_dstui_exits_with_the_code_main_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    argv = cli_args(tmp_path)
    argv[argv.index("--data-dir") + 1] = str(blocker / "data")
    monkeypatch.setattr(sys, "argv", ["dstui", *argv])

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("dstui", run_name="__main__")

    assert raised.value.code == 1
    assert "cannot prepare the data directory" in capsys.readouterr().err


def test_python_m_dstui_prints_the_version(tmp_path: Path) -> None:
    result = run_cli([sys.executable, "-m", "dstui", "--version"], cli_env(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"dstui {version('dstui')}"


def test_console_script_prints_help(tmp_path: Path) -> None:
    assert CONSOLE_SCRIPT.is_file(), "run `uv sync` to install the dstui console script"

    result = run_cli([str(CONSOLE_SCRIPT), "--help"], cli_env(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: dstui")
    for option in ("--workspace", "--profile", "--model", "--effort", "--max-tokens", "--data-dir"):
        assert option in result.stdout
    assert not (tmp_path / "home").exists()  # --help creates nothing


def test_console_script_rejects_bad_arguments(tmp_path: Path) -> None:
    result = run_cli([str(CONSOLE_SCRIPT), "--max-tokens", "0"], cli_env(tmp_path), tmp_path)

    assert result.returncode == 2
    assert "must be a positive integer" in result.stderr
