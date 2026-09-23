"""Packaging consistency: the locks, pyproject.toml, .python-version and the Makefile agree."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.helpers_scripts import PYPROJECT, ROOT, VERSION, run_script

RUNTIME_DIST = "deepseek-harness-runtime-bin"


# --------------------------------------------------------------------------------- consistency


def test_python_version_file_matches_the_requires_python_floor() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()

    assert PYPROJECT["project"]["requires-python"] == f">={pinned}"
    assert f"Programming Language :: Python :: {pinned}" in PYPROJECT["project"]["classifiers"]


def test_pyproject_version_is_what_dstui_version_prints(tmp_path: Path) -> None:
    env = {**os.environ, "DSTUI_HOME": str(tmp_path / "home")}

    result = run_script([sys.executable, "-m", "dstui", "--version"], env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"dstui {VERSION}\n"


def pinned(lock: str) -> dict[str, str]:
    text = (ROOT / lock).read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Za-z0-9._-]+)==(\S+)", text, re.MULTILINE))


def test_the_runtime_lock_never_contains_the_embedded_runtime() -> None:
    runtime = pinned("requirements.txt")

    assert RUNTIME_DIST not in runtime
    assert runtime["deepseek-harness-sdk"] == "0.1.5rc1"
    assert {"pydantic", "textual"} <= runtime.keys()


def test_the_dev_lock_carries_the_test_only_embedded_runtime() -> None:
    dev = pinned("requirements-dev.txt")

    assert dev[RUNTIME_DIST] == "0.1.5rc1"
    assert {"pytest", "mypy", "ruff", "bandit"} <= dev.keys()


def test_the_embedded_runtime_is_only_a_dev_dependency() -> None:
    project = PYPROJECT["project"]

    assert not any(RUNTIME_DIST in dep for dep in project["dependencies"])
    assert f"{RUNTIME_DIST}==0.1.5rc1" in project["optional-dependencies"]["dev"]
    assert any(dep.startswith("pydantic") for dep in project["dependencies"])


def makefile_recipe(target: str) -> tuple[str, str]:
    """The prerequisites line and the recipe of ``target`` in the Makefile."""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{target}:(.*)\n((?:\t.*\n)*)", text, re.MULTILINE)
    assert match is not None, f"no {target} target"
    return match.group(1), match.group(2)


def test_makefile_has_the_packaging_and_release_targets() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", text, re.MULTILINE))

    assert targets >= {
        "help", "dev-install", "lock", "test", "test-cov", "lint", "lint-fix",
        "typecheck", "security", "check", "package", "release", "clean",
    }  # fmt: skip
    assert "--no-emit-package deepseek-harness-runtime-bin" in text


def test_make_check_runs_what_the_ci_lint_job_runs() -> None:
    prerequisites, _ = makefile_recipe("check")
    _, security = makefile_recipe("security")

    assert prerequisites.split("##")[0].split() == ["lint", "typecheck", "security"]
    assert "-r src/ -c pyproject.toml" in security


def test_make_dev_install_installs_the_hash_locked_dev_dependencies() -> None:
    _, recipe = makefile_recipe("dev-install")

    sync, install = (line for line in recipe.splitlines() if "uv pip" in line)
    assert "--require-hashes requirements-dev.txt" in sync
    assert {"--no-deps", "--build-constraints", "requirements-build.txt", "-e", "."} <= set(
        install.split()
    )


def test_make_lock_also_pins_the_build_backend() -> None:
    _, recipe = makefile_recipe("lock")

    assert "requirements-build.txt" in recipe
    assert "--generate-hashes" in recipe


def lock_entries(lock: str) -> list[str]:
    """One string per requirement (continuation lines joined)."""
    text = (ROOT / lock).read_text(encoding="utf-8").replace("\\\n", " ")
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize(
    "lock", ["requirements.txt", "requirements-dev.txt", "requirements-build.txt"]
)
def test_every_lock_entry_is_pinned_with_hashes(lock: str) -> None:
    entries = lock_entries(lock)

    assert entries
    for entry in entries:
        assert re.match(r"^[A-Za-z0-9._-]+==\S+ ", entry), entry
        assert "--hash=sha256:" in entry, entry


def test_the_build_lock_pins_the_build_backend() -> None:
    build = pinned("requirements-build.txt")
    backend = PYPROJECT["build-system"]["requires"]

    assert [Requirement(r).name for r in backend] == ["hatchling"]
    assert "hatchling" in build


@pytest.mark.parametrize(
    ("lock", "requirements"),
    [
        ("requirements.txt", PYPROJECT["project"]["dependencies"]),
        (
            "requirements-dev.txt",
            PYPROJECT["project"]["dependencies"]
            + PYPROJECT["project"]["optional-dependencies"]["dev"],
        ),
        ("requirements-build.txt", PYPROJECT["build-system"]["requires"]),
    ],
    ids=["runtime", "dev", "build"],
)
def test_the_locks_satisfy_what_pyproject_declares(lock: str, requirements: list[str]) -> None:
    """A dependency change in pyproject.toml without `make lock` fails here."""
    pins = {canonicalize_name(name): v for name, v in pinned(lock).items()}
    for requirement in map(Requirement, requirements):
        name = canonicalize_name(requirement.name)
        if name == RUNTIME_DIST and lock == "requirements.txt":
            continue  # never in the runtime lock (see above)
        assert name in pins, f"{requirement} is not in {lock}: run make lock"
        assert requirement.specifier.contains(pins[name], prereleases=True), (
            f"{lock} pins {name}=={pins[name]}, pyproject wants {requirement}: run make lock"
        )
