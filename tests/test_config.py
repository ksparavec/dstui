"""Unit tests for dstui.config: command-line parsing and the derived SDK configuration."""

from __future__ import annotations

import dataclasses
import json
import stat
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import DeepSeekHarnessConfig

import dstui.config
from dstui.config import (
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    Settings,
    build_harness_config,
    default_data_dir,
    parse_args,
)

SESSION_LOG_PATCH = [{"id": "session-log-deepseek", "config": {"enabled": False}}]

# --------------------------------------------------------------------------- default_data_dir


def test_default_data_dir_prefers_dstui_home(tmp_path: Path) -> None:
    env = {
        "DSTUI_HOME": str(tmp_path / "custom"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
    }

    assert default_data_dir(env) == tmp_path / "custom"


def test_default_data_dir_falls_back_to_xdg_data_home(tmp_path: Path) -> None:
    env = {"XDG_DATA_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")}

    assert default_data_dir(env) == tmp_path / "xdg" / "dstui"


def test_default_data_dir_falls_back_to_home(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}

    assert default_data_dir(env) == tmp_path / "home" / ".local" / "share" / "dstui"


def test_default_data_dir_treats_empty_dstui_home_as_unset(tmp_path: Path) -> None:
    env = {"DSTUI_HOME": "", "XDG_DATA_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}

    assert default_data_dir(env) == tmp_path / "xdg" / "dstui"


def test_default_data_dir_treats_empty_xdg_data_home_as_unset(tmp_path: Path) -> None:
    env = {"DSTUI_HOME": "", "XDG_DATA_HOME": "", "HOME": str(tmp_path / "home")}

    assert default_data_dir(env) == tmp_path / "home" / ".local" / "share" / "dstui"


@pytest.mark.parametrize("home", [None, ""], ids=["missing", "empty"])
def test_default_data_dir_uses_the_user_home_when_home_is_unset(
    home: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "pw-home"))
    env = {} if home is None else {"HOME": home}

    assert default_data_dir(env) == tmp_path / "pw-home" / ".local" / "share" / "dstui"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DSTUI_HOME": "rel/data"}, Path("rel/data")),
        ({"HOME": "rel/home"}, Path("rel/home/.local/share/dstui")),
    ],
    ids=["dstui-home", "home"],
)
def test_default_data_dir_is_absolute_for_relative_values(
    env: dict[str, str], expected: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = default_data_dir(env)

    assert result.is_absolute()
    assert result == tmp_path / expected


def test_default_data_dir_ignores_a_relative_xdg_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the XDG spec: a relative XDG_DATA_HOME is invalid, skip it
    env = {"XDG_DATA_HOME": "rel/xdg", "HOME": str(tmp_path / "home")}

    assert default_data_dir(env) == tmp_path / "home" / ".local" / "share" / "dstui"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DSTUI_HOME": "~/data"}, Path("data")),
        ({"DSTUI_HOME": "~"}, Path()),
        ({"XDG_DATA_HOME": "~/xdg"}, Path("xdg/dstui")),
    ],
    ids=["dstui-home", "dstui-home-bare", "xdg-data-home"],
)
def test_default_data_dir_expands_a_leading_tilde(
    env: dict[str, str], expected: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))

    assert default_data_dir(env) == home / expected


# --------------------------------------------------------------------------------- parse_args


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def home_env(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home")}


def test_parse_args_defaults(
    workspace: Path, home_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(workspace)

    settings = parse_args([], env=home_env)

    assert settings == Settings(
        workspace=workspace,
        data_dir=tmp_path / "home" / ".local" / "share" / "dstui",
        profile=DEFAULT_PROFILE,
        model=DEFAULT_MODEL,
        reasoning_effort=None,
        max_tokens=None,
        api_key_set=False,
    )
    assert settings.needs_deepseek_key is True  # a property: not part of the equality above


def test_parse_args_accepts_every_long_option(
    workspace: Path, home_env: dict[str, str], tmp_path: Path
) -> None:
    """Wiring only: argparse enforces the choices; test_bridge boots every advertised effort."""
    argv = [
        "--workspace", str(workspace),
        "--profile", "sdk-minimal",
        "--model", "deepseek-v4-pro",
        "--effort", "max",
        "--max-tokens", "4096",
        "--data-dir", str(tmp_path / "data"),
    ]  # fmt: skip

    settings = parse_args(argv, env=home_env)

    assert settings == Settings(
        workspace=workspace,
        data_dir=tmp_path / "data",
        profile="sdk-minimal",
        model="deepseek-v4-pro",
        reasoning_effort="max",
        max_tokens=4096,
        api_key_set=False,
    )


def test_parse_args_accepts_short_options(workspace: Path, home_env: dict[str, str]) -> None:
    settings = parse_args(["-w", str(workspace), "-m", "deepseek-flash"], env=home_env)

    assert settings.workspace == workspace
    assert settings.model == "deepseek-flash"


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [(None, False), ("", False), ("   ", False), ("\t\n", False), ("sk-abc", True), (" sk ", True)],
    ids=["missing", "empty", "spaces", "tab-newline", "key", "padded-key"],
)
def test_parse_args_reports_whether_the_api_key_is_set(
    api_key: str | None, expected: bool, workspace: Path, home_env: dict[str, str]
) -> None:
    env = home_env if api_key is None else {**home_env, "DEEPSEEK_API_KEY": api_key}

    settings = parse_args(["-w", str(workspace)], env=env)

    assert settings.api_key_set is expected


def test_parse_args_resolves_relative_paths(
    tmp_path: Path, home_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)

    settings = parse_args(["-w", "ws", "--data-dir", "state/dstui"], env=home_env)

    assert settings.workspace == tmp_path / "ws"
    assert settings.data_dir == tmp_path / "state" / "dstui"
    assert settings.workspace.is_absolute()
    assert settings.data_dir.is_absolute()


def test_parse_args_expands_a_leading_tilde_in_paths(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "project").mkdir(parents=True)
    (home / "dsh").write_text("#!/bin/sh\n", encoding="utf-8")
    (home / "p.yml").write_text("[]\n", encoding="utf-8")
    monkeypatch.chdir(workspace)  # a literal "~" would land here, inside the workspace
    monkeypatch.setenv("HOME", str(home))
    argv = ["--workspace=~/project", "--data-dir=~/data", "--dsh-bin=~/dsh", "--patch=~/p.yml"]

    # the shell leaves "~" alone after "--opt=", so dstui must expand it
    settings = parse_args(argv, env={"HOME": str(home)})

    assert settings.workspace == home / "project"
    assert settings.data_dir == home / "data"
    assert settings.dsh_bin == home / "dsh"
    assert settings.extra_patches == (home / "p.yml",)
    assert list(workspace.iterdir()) == []


def _parse_error(argv: list[str], env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run parse_args expecting an argparse usage error; return its stderr."""
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv, env=env)
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: dstui")
    return err


def test_parse_args_rejects_a_missing_workspace(
    tmp_path: Path, home_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope"

    err = _parse_error(["-w", str(missing)], home_env, capsys)

    assert "argument -w/--workspace" in err
    assert f"{missing} does not exist" in err


def test_parse_args_rejects_a_workspace_that_is_a_file(
    tmp_path: Path, home_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")

    err = _parse_error(["--workspace", str(a_file)], home_env, capsys)

    assert "argument -w/--workspace" in err
    assert f"{a_file} is not a directory" in err


@pytest.mark.parametrize("value", ["0", "-1", "-4096", "abc", "1.5", ""])
def test_parse_args_rejects_a_non_positive_or_non_integer_max_tokens(
    value: str, workspace: Path, home_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    err = _parse_error(["-w", str(workspace), f"--max-tokens={value}"], home_env, capsys)

    assert "argument --max-tokens" in err
    assert f"must be a positive integer, got {value!r}" in err


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--profile", "sdk-full"),
        ("--model", "gpt-4"),
        ("-m", "deepseek-chat"),
        ("--effort", "medium"),
        ("--effort", "HIGH"),
    ],
)
def test_parse_args_rejects_an_unknown_choice(
    option: str,
    value: str,
    workspace: Path,
    home_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = _parse_error(["-w", str(workspace), option, value], home_env, capsys)

    assert f"invalid choice: {value!r}" in err


@pytest.mark.parametrize(
    ("option", "label"),
    [
        ("--workspace", "argument -w/--workspace"),
        ("--data-dir", "argument --data-dir"),
        ("--dsh-bin", "argument --dsh-bin"),
        ("--patch", "argument --patch"),
    ],
)
def test_parse_args_rejects_an_empty_path(
    option: str,
    label: str,
    workspace: Path,
    home_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = _parse_error(["-w", str(workspace), f"{option}="], home_env, capsys)

    assert f"{label}: path must not be empty" in err


def test_parse_args_rejects_a_data_dir_that_is_a_file(
    workspace: Path, home_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_file = tmp_path / "data.txt"
    a_file.write_text("x")

    err = _parse_error(["-w", str(workspace), "--data-dir", str(a_file)], home_env, capsys)

    assert "argument --data-dir" in err
    assert f"{a_file} is not a directory" in err


def test_parse_args_rejects_a_default_data_dir_that_is_a_file(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_file = tmp_path / "dstui-home.txt"
    a_file.write_text("x")

    err = _parse_error(["-w", str(workspace)], {"DSTUI_HOME": str(a_file)}, capsys)

    assert "argument --data-dir" in err
    assert f"{a_file} is not a directory" in err


def test_parse_args_version_prints_the_package_version_and_exits_0(
    home_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--version"], env=home_env)

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"dstui {version('dstui')}\n"


def test_parse_args_works_without_package_metadata(
    workspace: Path,
    home_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(dstui.config, "version", missing)

    assert parse_args(["-w", str(workspace)], env=home_env).workspace == workspace
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--version"], env=home_env)
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "dstui unknown\n"


def test_parse_args_defaults_to_sys_argv_and_os_environ(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["dstui", "-w", str(workspace), "-m", "deepseek-v4-pro"])
    monkeypatch.setenv("DSTUI_HOME", str(tmp_path / "from-environ"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environ")

    settings = parse_args(None, None)

    assert settings.workspace == workspace
    assert settings.model == "deepseek-v4-pro"
    assert settings.data_dir == tmp_path / "from-environ"
    assert settings.api_key_set is True


def test_parse_args_help_documents_options_defaults_and_the_api_key(
    workspace: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    data_dir = tmp_path / "data-home"

    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"], env={"DSTUI_HOME": str(data_dir)})

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for option in (
        "--workspace", "--profile", "--provider", "--model", "--effort", "--max-tokens",
        "--data-dir", "--dsh-bin", "--patch",
    ):  # fmt: skip
        assert option in out
    assert f"default: {data_dir}" in out
    assert "DEEPSEEK_API_KEY" in out
    assert "sdk = file writes sandboxed to the workspace" in out  # the risk of each profile
    assert "sdk-minimal = NO sandbox (default: sdk)" in out


@pytest.fixture
def patch_files(tmp_path: Path) -> tuple[Path, Path]:
    first, second = tmp_path / "a.yml", tmp_path / "b.yml"
    first.write_text("[]\n", encoding="utf-8")
    second.write_text("[]\n", encoding="utf-8")
    return first, second


def test_parse_args_accepts_another_provider_with_a_free_model_id(
    workspace: Path, home_env: dict[str, str], tmp_path: Path, patch_files: tuple[Path, Path]
) -> None:
    dsh = tmp_path / "dsh"
    dsh.write_text("#!/bin/sh\n", encoding="utf-8")
    argv = [
        "-w", str(workspace),
        "--provider", "router-vllm",
        "-m", "Qwen3-8B-NVFP4::nothink@32768",
        "--dsh-bin", str(dsh),
        "--patch", str(patch_files[0]),
        "--patch", str(patch_files[1]),
    ]  # fmt: skip

    settings = parse_args(argv, env=home_env)

    assert settings.provider == "router-vllm"
    assert settings.model == "Qwen3-8B-NVFP4::nothink@32768"
    assert settings.dsh_bin == dsh
    assert settings.extra_patches == patch_files  # order kept
    assert settings.needs_deepseek_key is False


def test_parse_args_requires_a_model_for_another_provider(
    workspace: Path, home_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    err = _parse_error(["-w", str(workspace), "--provider", "router-vllm"], home_env, capsys)

    assert "required with --provider router-vllm" in err


@pytest.mark.parametrize("option", ["--dsh-bin", "--patch"])
@pytest.mark.parametrize("name", ["nope", ""], ids=["missing", "directory"])
def test_parse_args_rejects_a_path_that_is_not_a_file(
    option: str,
    name: str,
    workspace: Path,
    home_env: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / name  # "" -> tmp_path itself, a directory

    err = _parse_error(["-w", str(workspace), option, str(target)], home_env, capsys)

    assert f"argument {option}: {target} is not a file" in err


# ----------------------------------------------------------------------- build_harness_config


@pytest.fixture
def settings(workspace: Path, tmp_path: Path) -> Settings:
    return Settings(workspace=workspace, data_dir=tmp_path / "state" / "dstui")


def test_build_harness_config_creates_the_data_directories(settings: Settings) -> None:
    assert not settings.data_dir.exists()

    build_harness_config(settings)

    assert settings.data_dir.is_dir()
    assert settings.dsh_home.is_dir()
    assert settings.agents_home.is_dir()
    assert settings.dsh_home.parent == settings.data_dir
    assert settings.agents_home.parent == settings.data_dir


def test_build_harness_config_creates_private_directories(settings: Settings) -> None:
    build_harness_config(settings)

    for directory in (settings.data_dir, settings.dsh_home, settings.agents_home):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_build_harness_config_writes_the_session_log_patch(settings: Settings) -> None:
    build_harness_config(settings)

    assert settings.patch_file.parent == settings.data_dir
    assert json.loads(settings.patch_file.read_text(encoding="utf-8")) == SESSION_LOG_PATCH


def _expected_config(settings: Settings, **env: str) -> DeepSeekHarnessConfig:
    return DeepSeekHarnessConfig(
        provider=settings.provider,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        max_tokens=settings.max_tokens,
        cwd=str(settings.workspace),
        dsh_home=str(settings.data_dir / "dsh-home"),
        profile=settings.profile,
        dsh_bin=None if settings.dsh_bin is None else str(settings.dsh_bin),
        patches=(
            str(settings.data_dir / "dstui-patch.yml"),
            *(str(path) for path in settings.extra_patches),
        ),
        env={
            "DSH_TELEMETRY_DISABLED": "1",
            "DSH_AGENTS_HOME": str(settings.data_dir / "agents"),
            **env,
        },
    )


@pytest.mark.parametrize(
    ("overrides", "env"),
    [
        ({}, {}),
        (
            {
                "profile": "sdk-minimal",
                "model": "deepseek-v4-pro",
                "reasoning_effort": "low",
                "max_tokens": 2048,
                "api_key_set": True,
            },
            {},
        ),
        (
            {
                "provider": "local",
                "model": "my-model",
                "api_key_set": True,
                "dsh_bin": Path("/opt/dsh/bin/dsh"),
                "extra_patches": (Path("/etc/dstui/b.yml"), Path("/etc/dstui/a.yml")),  # in order
            },
            # hidden, or the sdk profile's web_search sends it (and queries) to DeepSeek
            {"DEEPSEEK_API_KEY": ""},
        ),
    ],
    ids=["defaults", "deepseek-options", "other-provider"],
)
def test_build_harness_config_returns_exactly_the_specified_fields(
    overrides: dict[str, Any], env: dict[str, str], settings: Settings
) -> None:
    configured = dataclasses.replace(settings, **overrides)

    config = build_harness_config(configured)

    assert config == _expected_config(configured, **env)
    assert config.api_key is None
    assert config.base_url is None


def test_build_harness_config_is_idempotent(settings: Settings) -> None:
    first = build_harness_config(settings)
    settings.patch_file.write_text("stale: true\n", encoding="utf-8")

    second = build_harness_config(settings)

    assert second == first
    assert json.loads(settings.patch_file.read_text(encoding="utf-8")) == SESSION_LOG_PATCH
