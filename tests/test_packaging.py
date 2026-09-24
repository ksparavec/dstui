"""Packaging consistency: the locks, pyproject.toml, .python-version and the Makefile agree."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.helpers_scripts import (
    PINNED_PYTHON,
    PYPROJECT,
    ROOT,
    SYSTEM_PATH,
    VERSION,
    run_script,
    write_program,
)

RUNTIME_DIST = "deepseek-harness-runtime-bin"


# --------------------------------------------------------------------------------- consistency


def test_python_version_file_pins_an_exact_cpython_on_the_requires_python_floor() -> None:
    """.python-version is the one exact pin (dev venv, CI, the bundled interpreter); the package
    metadata keeps the X.Y floor."""
    pinned = (ROOT / ".python-version").read_text()

    match = re.fullmatch(r"(3\.\d+)\.\d+\n?", pinned)
    assert match is not None, f".python-version must be a full X.Y.Z, got {pinned!r}"
    minor = match.group(1)
    assert PYPROJECT["project"]["requires-python"] == f">={minor}"
    assert f"Programming Language :: Python :: {minor}" in PYPROJECT["project"]["classifiers"]


def test_pyproject_marks_dstui_private_so_pypi_rejects_an_upload() -> None:
    """The installer is the only distribution; PyPI refuses any 'Private ::' classifier."""
    assert "Private :: Do Not Upload" in PYPROJECT["project"]["classifiers"]


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


def run_make_dev_install(tmp_path: Path, uv_venv: str) -> subprocess.CompletedProcess[str]:
    """``make dev-install`` on a copy of the Makefile and .python-version with a fake uv that logs
    each call to ``uv.log`` and runs the shell code ``uv_venv`` for ``uv venv``."""
    project = tmp_path / "project"
    project.mkdir()
    for name in ("Makefile", ".python-version"):
        shutil.copy2(ROOT / name, project / name)
    uv = write_program(
        tmp_path / "bin" / "uv",
        f'echo "$*" >> "{tmp_path / "uv.log"}"\n[ "$1" != venv ] || {{ {uv_venv}; }}\n',
    )
    return run_script(
        ["make", "-s", "dev-install"], {"PATH": f"{uv.parent}:{SYSTEM_PATH}"}, cwd=project
    )


def uv_calls(tmp_path: Path) -> list[list[str]]:
    return [line.split() for line in (tmp_path / "uv.log").read_text().splitlines()]


def test_make_dev_install_recreates_the_venv_on_exactly_the_pinned_cpython(tmp_path: Path) -> None:
    """A .venv made on another patch (before a pin bump, or on uv's floating 3.X link) must not
    survive: the checks and tests would run on a CPython the installer does not ship."""
    result = run_make_dev_install(tmp_path, uv_venv="exit 0")

    assert result.returncode == 0, result.stderr
    venv, *installs = uv_calls(tmp_path)
    assert venv[0] == "venv"
    assert "--clear" in venv
    assert venv[venv.index("--python") + 1] == PINNED_PYTHON
    assert [call[:2] for call in installs] == [["pip", "sync"], ["pip", "install"]]


def test_make_dev_install_stops_with_uvs_reason_when_it_cannot_make_the_venv(
    tmp_path: Path,
) -> None:
    """E.g. a uv too old to know the pinned CPython, or Python downloads disabled: uv's own error,
    not a later, misleading one from installing into a venv that is not there (or is stale)."""
    error = f"error: No download found for request: cpython-{PINNED_PYTHON}-linux-x86_64-gnu"

    result = run_make_dev_install(tmp_path, uv_venv=f'echo "{error}" >&2; exit 2')

    assert result.returncode != 0
    assert error in result.stderr.splitlines()
    assert [call[0] for call in uv_calls(tmp_path)] == ["venv"]


def test_the_suite_runs_on_exactly_the_pinned_cpython() -> None:
    """The tests run on the CPython the installer ships: CI's setup-python reads .python-version,
    locally ``make dev-install`` makes the venv on it. After a pin bump, re-run dev-install."""
    assert platform.python_version() == PINNED_PYTHON, "stale .venv? run `make dev-install`"


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


# ----------------------------------------------------------------------------------- workflows

WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
ATTEST_ACTION = "actions/attest-build-provenance@"
RELEASE_ASSETS = ["dist/dstui-install.sh", "install.sh"]


def workflow(name: str) -> dict[str, Any]:
    data: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if True in data:  # YAML 1.1 reads a bare `on:` key as the boolean true
        data["on"] = data.pop(True)
    return data


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job["steps"])


def step_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in steps(job) if str(step.get("uses", "")).startswith(action)]


def run_lines(job: dict[str, Any]) -> list[str]:
    return [line.strip() for step in steps(job) for line in str(step.get("run", "")).splitlines()]


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_by_commit_sha_and_checkout_keeps_no_token(name: str) -> None:
    for job in workflow(name)["jobs"].values():
        for step in steps(job):
            uses = step.get("uses")
            if uses is None:
                continue
            assert PINNED_ACTION.match(uses), f"{name}: {uses} is not pinned by a commit SHA"
            if uses.startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


def test_the_release_workflow_runs_on_version_tags_and_proves_the_build_on_prs_and_main() -> None:
    on = workflow("release.yml")["on"]

    assert on["push"]["tags"] == ["v*"]
    assert on["push"]["branches"] == ["main"]
    assert "pull_request" in on


def test_the_release_workflow_builds_a_tag_once() -> None:
    concurrency = workflow("release.yml")["concurrency"]

    assert "github.ref" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.ref_type != 'tag' }}"


def test_only_the_tag_only_publish_job_may_write_sign_and_attest() -> None:
    release = workflow("release.yml")
    jobs = release["jobs"]

    assert release["permissions"] == {"contents": "read"}
    assert set(jobs) == {"package", "publish"}
    assert jobs["package"].get("permissions", {"contents": "read"}) == {"contents": "read"}
    publish = jobs["publish"]
    assert publish["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert publish["needs"] == "package"
    assert publish["if"] == "github.event_name == 'push' && github.ref_type == 'tag'"


def test_the_package_job_builds_the_installer_with_the_full_smoke_test_on_every_run() -> None:
    """PRs and main prove the release build before any tag exists."""
    package = workflow("release.yml")["jobs"]["package"]
    lines = run_lines(package)

    assert "if" not in package
    assert lines.index("make dev-install") < lines.index("make package")
    assert any("apt-get install" in line and "makeself" in line for line in lines)
    (upload,) = step_using(package, "actions/upload-artifact@")
    assert upload["with"]["path"] == "dist/dstui-install.sh"
    assert upload["with"]["if-no-files-found"] == "error"


def test_a_tag_that_does_not_match_the_pyproject_version_fails_before_the_build() -> None:
    package = workflow("release.yml")["jobs"]["package"]
    names = [step.get("name", "") for step in steps(package)]
    check = next(step for step in steps(package) if "GITHUB_REF_NAME" in step.get("run", ""))

    assert check["if"] == "github.ref_type == 'tag'"
    assert "pyproject.toml" in check["run"]
    assert names.index(check["name"]) < next(
        i for i, step in enumerate(steps(package)) if step.get("run") == "make package"
    )


def test_the_publish_job_attests_both_assets_before_releasing_them() -> None:
    publish = workflow("release.yml")["jobs"]["publish"]
    all_steps = steps(publish)

    (attest,) = step_using(publish, ATTEST_ACTION)
    assert attest["with"]["subject-path"].split() == RELEASE_ASSETS
    release = next(step for step in all_steps if "gh release create" in step.get("run", ""))
    assert all_steps.index(attest) < all_steps.index(release)
    assert release["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert (
        'gh release create "$TAG" --verify-tag --title "dstui $TAG" --notes-file "$NOTES" '
        + " ".join(RELEASE_ASSETS)
    ) in " ".join(release["run"].split())
    notes = next(step for step in all_steps if "release-notes.sh" in step.get("run", ""))
    assert all_steps.index(notes) < all_steps.index(release)


def test_no_other_workflow_attests_or_publishes() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        if path.name == "release.yml":
            continue
        text = path.read_text(encoding="utf-8")
        assert ATTEST_ACTION not in text, path.name
        assert "gh release" not in text, path.name
        assert "id-token" not in text, path.name
