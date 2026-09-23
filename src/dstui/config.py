"""Command-line settings and the SDK configuration derived from them."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from deepseek_harness import DeepSeekHarnessConfig

PROFILES = ("sdk", "sdk-minimal")
MODELS = ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-flash", "deepseek-v4-flash-vision-exp")
EFFORTS = ("off", "low", "high", "max")
DEFAULT_PROFILE = "sdk"
DEFAULT_MODEL = "deepseek-v4-flash"

_PROVIDER = "deepseek-official"  # the only provider the runtime's initialize accepts
# Runtime patch disabling the per-request session log (JSON is valid YAML).
_SESSION_LOG_PATCH = '[{"id":"session-log-deepseek","config":{"enabled":false}}]\n'


@dataclass(frozen=True, slots=True)
class Settings:
    workspace: Path  # the agent's working directory (sandbox root for the "sdk" profile)
    data_dir: Path  # dstui's own state: dsh home, agents home, patch file
    profile: str = DEFAULT_PROFILE
    model: str = DEFAULT_MODEL
    reasoning_effort: str | None = None  # None -> runtime default ("high")
    max_tokens: int | None = None  # None -> runtime default
    api_key_set: bool = False  # DEEPSEEK_API_KEY present and non-empty

    @property
    def dsh_home(self) -> Path:
        return self.data_dir / "dsh-home"

    @property
    def agents_home(self) -> Path:
        return self.data_dir / "agents"

    @property
    def patch_file(self) -> Path:
        return self.data_dir / "dstui-patch.yml"


def default_data_dir(env: Mapping[str, str]) -> Path:
    """``$DSTUI_HOME``, else ``$XDG_DATA_HOME/dstui``, else ``~/.local/share/dstui``.

    A leading ``~`` is expanded; a relative ``$XDG_DATA_HOME`` is ignored (XDG spec).
    """
    xdg_data_home = _expand_user(env.get("XDG_DATA_HOME", ""))
    if dstui_home := env.get("DSTUI_HOME"):
        data_dir = _expand_user(dstui_home)
    elif xdg_data_home.is_absolute():
        data_dir = xdg_data_home / "dstui"
    else:
        data_dir = Path(env.get("HOME") or Path.home()) / ".local" / "share" / "dstui"
    return data_dir.resolve()


def _expand_user(value: str) -> Path:
    """``value`` with a leading ``~`` expanded (the shell skips ``--opt=~/x`` and env files).

    ``os.path.expanduser`` never raises: an unknown ``~user`` is left as it is.
    """
    return Path(os.path.expanduser(value))


def parse_args(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Parse the command line (``sys.argv[1:]`` / ``os.environ`` when None)."""
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser(default_data_dir(env))
    args = parser.parse_args(argv)
    return Settings(
        workspace=args.workspace,
        data_dir=args.data_dir,
        profile=args.profile,
        model=args.model,
        reasoning_effort=args.effort,
        max_tokens=args.max_tokens,
        api_key_set=bool(env.get("DEEPSEEK_API_KEY", "").strip()),
    )


def _build_parser(default_data: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dstui",
        description="Chat with a DeepSeek Harness agent in the terminal.",
        epilog="The API key is read from DEEPSEEK_API_KEY (and DEEPSEEK_BASE_URL, if set).",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        type=_existing_dir,
        default=".",
        metavar="PATH",
        help="the agent's working directory (default: current directory)",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=DEFAULT_PROFILE,
        help="agent profile: sdk = file writes sandboxed to the workspace, "
        "sdk-minimal = NO sandbox (default: %(default)s)",
    )
    parser.add_argument(
        "-m", "--model", choices=MODELS, default=DEFAULT_MODEL, help="model (default: %(default)s)"
    )
    parser.add_argument(
        "--effort", choices=EFFORTS, default=None, help="reasoning effort (default: runtime)"
    )
    parser.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum output tokens per model request (default: runtime)",
    )
    parser.add_argument(
        "--data-dir",
        type=_dir_or_missing,
        default=str(default_data),  # a string default goes through the type check too
        metavar="PATH",
        help="dstui state directory (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def _version() -> str:
    try:
        return version("dstui")
    except PackageNotFoundError:
        return "unknown"


def _absolute_path(value: str) -> Path:
    if not value:
        raise argparse.ArgumentTypeError("path must not be empty")
    return _expand_user(value).resolve()


def _dir_or_missing(value: str) -> Path:
    path = _absolute_path(value)
    if path.exists() and not path.is_dir():
        raise argparse.ArgumentTypeError(f"{path} is not a directory")
    return path


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return number


def _existing_dir(value: str) -> Path:
    path = _absolute_path(value)
    if not path.exists():
        raise argparse.ArgumentTypeError(f"{path} does not exist")
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"{path} is not a directory")
    return path


def build_harness_config(settings: Settings) -> DeepSeekHarnessConfig:
    """Create dstui's data directories and patch file, then return the SDK config."""
    for directory in (settings.data_dir, settings.dsh_home, settings.agents_home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)  # private when newly created
    settings.patch_file.write_text(_SESSION_LOG_PATCH, encoding="utf-8")
    return DeepSeekHarnessConfig(
        provider=_PROVIDER,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        max_tokens=settings.max_tokens,
        cwd=str(settings.workspace),
        dsh_home=str(settings.dsh_home),
        profile=settings.profile,
        patches=(str(settings.patch_file),),
        env={"DSH_TELEMETRY_DISABLED": "1", "DSH_AGENTS_HOME": str(settings.agents_home)},
    )
