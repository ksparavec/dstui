"""Deterministic control over where dstui finds the DeepSeek Harness runtime (``dsh``)."""

from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

RUNTIME_MODULE = "deepseek_harness_runtime"  # the SDK's embedded runtime: dev/test installs only
DSH_NOT_FOUND = (
    "DeepSeek Harness (dsh) not found: install @deepseek-ai/dsh (npm, Node >= 22.19) "
    "or pass --dsh-bin PATH"
)


def set_runtime_importable(monkeypatch: pytest.MonkeyPatch, importable: bool) -> None:
    """Make ``deepseek_harness_runtime`` importable (a stub) or not, whatever is installed."""
    module = importlib.util.module_from_spec(ModuleSpec(RUNTIME_MODULE, None))
    monkeypatch.setitem(sys.modules, RUNTIME_MODULE, module if importable else None)


def empty_search_path(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create ``directory`` and make it the whole PATH."""
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


def put_on_path(directory: Path, name: str = "dsh", *, executable: bool = True) -> Path:
    program = directory / name
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755 if executable else 0o644)
    return program
