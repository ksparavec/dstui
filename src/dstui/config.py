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
# DeepSeek's own API. Any other provider must be declared to the runtime by a
# --patch file (e.g. an `llm-pi-ai` entry for an OpenAI-compatible endpoint).
DEFAULT_PROVIDER = "deepseek-official"
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
    provider: str = DEFAULT_PROVIDER
    dsh_bin: Path | None = None  # None -> the SDK's bundled runtime
    extra_patches: tuple[Path, ...] = ()  # applied after dstui's own patch, in order

    @property
    def needs_deepseek_key(self) -> bool:
        """Whether this provider reads DEEPSEEK_API_KEY (only DeepSeek's own API does)."""
        return self.provider == DEFAULT_PROVIDER

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
    if args.provider == DEFAULT_PROVIDER:
        model = DEFAULT_MODEL if args.model is None else args.model
        if model not in MODELS:
            choices = ", ".join(repr(m) for m in MODELS)
            parser.error(f"argument -m/--model: invalid choice: {model!r} (choose from {choices})")
    elif args.model is None:
        parser.error(f"argument -m/--model: required with --provider {args.provider}")
    else:
        model = args.model
    return Settings(
        workspace=args.workspace,
        data_dir=args.data_dir,
        profile=args.profile,
        model=model,
        reasoning_effort=args.effort,
        max_tokens=args.max_tokens,
        api_key_set=bool(env.get("DEEPSEEK_API_KEY", "").strip()),
        provider=args.provider,
        dsh_bin=args.dsh_bin,
        extra_patches=tuple(args.patch),
    )


def _build_parser(default_data: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dstui",
        description="Chat with a DeepSeek Harness agent in the terminal.",
        epilog="With the default provider the API key is read from DEEPSEEK_API_KEY (and "
        "DEEPSEEK_BASE_URL, if set). Another provider is declared by a --patch file, which "
        "also names where its key comes from.",
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
        "--provider",
        default=DEFAULT_PROVIDER,
        metavar="ID",
        help="model provider id; anything but the default must be declared by a --patch file "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        metavar="MODEL",
        help=f"model; with the default provider one of {', '.join(MODELS)} "
        f"(default: {DEFAULT_MODEL}); required with any other provider",
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
    parser.add_argument(
        "--dsh-bin",
        type=_existing_file,
        default=None,
        metavar="PATH",
        help="run this DeepSeek Harness executable instead of the SDK's bundled runtime",
    )
    parser.add_argument(
        "--patch",
        type=_existing_file,
        action="append",
        default=[],
        metavar="PATH",
        help="extra runtime patch file, applied after dstui's own (repeatable)",
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


def _existing_file(value: str) -> Path:
    path = _absolute_path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"{path} is not a file")
    return path


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
        provider=settings.provider,
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        max_tokens=settings.max_tokens,
        cwd=str(settings.workspace),
        dsh_home=str(settings.dsh_home),
        profile=settings.profile,
        dsh_bin=None if settings.dsh_bin is None else str(settings.dsh_bin),
        patches=(str(settings.patch_file), *(str(p) for p in settings.extra_patches)),
        env={"DSH_TELEMETRY_DISABLED": "1", "DSH_AGENTS_HOME": str(settings.agents_home)},
    )
