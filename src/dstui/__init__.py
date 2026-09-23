"""dstui: a Textual chat TUI for a DeepSeek Harness agent."""

from __future__ import annotations

import dataclasses
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from dstui.app import DsTuiApp
from dstui.bridge import AgentBridge
from dstui.config import DshNotFoundError, build_harness_config, parse_args, resolve_dsh_bin

__all__ = ["main"]

_LOG_FILE = "dstui.log"  # in the data dir: Textual swallows stderr while the app runs
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the TUI with settings from ``argv`` (``sys.argv[1:]`` if None); return the exit code."""
    settings = parse_args(argv)
    try:
        settings = dataclasses.replace(settings, dsh_bin=resolve_dsh_bin(settings.dsh_bin))
    except DshNotFoundError as error:
        sys.stderr.write(f"dstui: {error}\n")
        return 1
    try:
        config = build_harness_config(settings)
        log_handler = _warnings_log(settings.data_dir / _LOG_FILE)
    except OSError as error:
        sys.stderr.write(f"dstui: cannot prepare the data directory {settings.data_dir}: {error}\n")
        return 1
    logger = logging.getLogger("dstui")  # not the root logger: leave other handlers alone
    logger.addHandler(log_handler)
    bridge = AgentBridge(config)
    app = DsTuiApp(bridge, settings)
    try:
        app.run()
    finally:
        bridge.close()  # the app closes it on unmount; this also covers a crashed driver
        logger.removeHandler(log_handler)
        log_handler.close()
    return app.return_code or 0


def _warnings_log(path: Path) -> logging.Handler:
    """A timestamped WARNING-level file handler (appends; raises OSError if unwritable)."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    return handler
