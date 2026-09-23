"""release.sh against throwaway git repositories, a bare 'origin' and a fake gh.

Everything lives under ``tmp_path``; nothing reaches GitHub.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers_scripts import (
    INSTALL_SH,
    ROOT,
    SYSTEM_PATH,
    VERSION,
    run_script,
    write_program,
)

RELEASE_SH = ROOT / "tools" / "release" / "release.sh"


# ---------------------------------------------------------------------------------- release.sh

RELEASED_ENTRY = "### Added\n- The first thing.\n- The second thing."


def git(repo: Path, *args: str, env: dict[str, str]) -> str:
    result = run_script(["git", *args], env, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def release_env(tmp_path: Path) -> dict[str, str]:
    """PATH with a fake gh (authenticated; no release exists; logs every call), isolated git."""
    bin_dir = tmp_path / "gh-bin"
    write_program(
        bin_dir / "gh",
        'echo "$*" >> "$FAKE_GH_LOG"\n'
        'case "$1 $2" in\n'
        '  "auth status") exit 0 ;;\n'
        '  "release view") exit 1 ;;\n'
        '  "repo view") echo ksparavec/dstui ;;\n'
        '  "release create") exit 0 ;;\n'
        "esac\n",
    )
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{SYSTEM_PATH}",
        "TMPDIR": str(tmp_path),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


FAILING_BUILD = "cp CHANGELOG.md ../changelog-at-build.md\nexit 1\n"  # nothing gets pushed
BUILD = "mkdir -p dist\necho installer > dist/dstui-install.sh\n"


def make_release_repo(
    tmp_path: Path, env: dict[str, str], unreleased: str, build: str = FAILING_BUILD
) -> Path:
    """A repo on main, in sync with a bare 'origin' under tmp_path, carrying the real release.sh
    and a fake tools/package/build-binary.sh running ``build``."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    (repo / "tools" / "release").mkdir(parents=True)
    shutil.copy2(RELEASE_SH, repo / "tools" / "release" / "release.sh")
    shutil.copy2(INSTALL_SH, repo / "install.sh")
    write_program(repo / "tools" / "package" / "build-binary.sh", build)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "dstui"\nversion = "{VERSION}"\n')
    (repo / ".gitignore").write_text("dist/\n")
    (repo / "CHANGELOG.md").write_text(changelog(unreleased))
    run_script(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env)
    git(repo.parent, "init", "-q", "-b", "main", str(repo), env=env)
    git(repo, "add", ".", env=env)
    git(repo, "commit", "-q", "-m", "initial", env=env)
    git(repo, "remote", "add", "origin", str(origin), env=env)
    git(repo, "push", "-q", "origin", "main", env=env)
    return repo


def changelog(unreleased: str) -> str:
    return (
        "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n"
        f"{unreleased}\n\n## [0.0.1] - 2026-01-01\n\n- Old.\n"
    )


def run_release(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_script(
        ["bash", "tools/release/release.sh"], env | {"DSTUI_RELEASE_ASSUME_YES": "1"}, cwd=repo
    )


def assert_nothing_released(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    assert git(repo, "tag", "--list", env=env) == ""
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert git(repo, "log", "--format=%s", env=env) == "initial"
    origin = tmp_path / "origin.git"
    assert git(origin, "log", "--format=%s", "main", env=env) == "initial"
    assert git(origin, "tag", "--list", env=env) == ""
    gh_calls = (tmp_path / "gh.log").read_text() if (tmp_path / "gh.log").exists() else ""
    assert "release create" not in gh_calls


def test_release_refuses_to_run_off_main(tmp_path: Path, release_env: dict[str, str]) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    git(repo, "checkout", "-q", "-b", "feature", env=release_env)

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "not on 'main' (currently on 'feature')" in result.stderr
    git(repo, "checkout", "-q", "main", env=release_env)
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_refuses_a_dirty_tree(tmp_path: Path, release_env: dict[str, str]) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    (repo / "install.sh").write_text("# edited\n")

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr
    git(repo, "checkout", "--", "install.sh", env=release_env)
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_refuses_untracked_files_that_git_status_is_told_to_hide(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    """The wheel would pick up an untracked module that is not in the tagged commit."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    git(repo, "config", "status.showUntrackedFiles", "no", env=release_env)
    (repo / "tools" / "extra.py").write_text("print('not committed')\n")

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr
    (repo / "tools" / "extra.py").unlink()
    assert_nothing_released(repo, tmp_path, release_env)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_release_refuses_local_edits_hidden_from_git_status(
    flag: str, tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    git(repo, "update-index", flag, "install.sh", env=release_env)
    (repo / "install.sh").write_text("# edited\n")

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "hidden from git status" in result.stderr
    assert "install.sh" in result.stderr


def test_release_refuses_when_main_is_not_in_sync_with_origin(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    git(repo, "commit", "-q", "--allow-empty", "-m", "unpushed", env=release_env)

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "local main differs from origin/main" in result.stderr
    assert git(repo, "tag", "--list", env=release_env) == ""
    assert "release create" not in (tmp_path / "gh.log").read_text()


@pytest.mark.parametrize("where", ["local", "origin", "github"])
def test_release_refuses_a_version_that_is_already_tagged_or_released(
    where: str, tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    tag = f"v{VERSION}"
    if where in ("local", "origin"):
        git(repo, "tag", tag, env=release_env)
    if where == "origin":
        git(repo, "push", "-q", "origin", tag, env=release_env)
        git(repo, "tag", "-d", tag, env=release_env)
    if where == "github":
        gh = Path(release_env["PATH"].split(":")[0]) / "gh"
        gh.write_text(gh.read_text().replace('"release view") exit 1', '"release view") exit 0'))

    result = run_release(repo, release_env)

    expected = {
        "local": f"tag {tag} already exists locally",
        "origin": f"tag {tag} already exists on origin",
        "github": f"a GitHub release for {tag} already exists",
    }[where]
    assert result.returncode == 1
    assert expected in result.stderr
    assert git(repo, "log", "--format=%s", env=release_env) == "initial"
    assert "release create" not in (tmp_path / "gh.log").read_text()


def test_release_refuses_an_empty_unreleased_section(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, "")

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "'## [Unreleased]' section in CHANGELOG.md is empty" in result.stderr
    assert (repo / "CHANGELOG.md").read_text() == changelog("")
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_refuses_without_confirmation_off_a_tty(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)

    result = run_script(["bash", "tools/release/release.sh"], release_env, cwd=repo)

    assert result.returncode == 1
    assert "DSTUI_RELEASE_ASSUME_YES=1" in result.stderr
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_promotes_unreleased_before_the_build_and_reverts_on_failure(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    before = datetime.date.today().isoformat()

    result = run_release(repo, release_env)

    after = datetime.date.today().isoformat()
    assert result.returncode == 1  # the fake build fails after capturing the CHANGELOG
    promoted = (tmp_path / "changelog-at-build.md").read_text()
    expected = {
        changelog(RELEASED_ENTRY).replace(
            "## [Unreleased]\n", f"## [Unreleased]\n\n## [{VERSION}] - {day}\n"
        )
        for day in (before, after)
    }
    assert promoted in expected
    assert "Reverted CHANGELOG.md" in result.stderr
    assert (repo / "CHANGELOG.md").read_text() == changelog(RELEASED_ENTRY)
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_commits_tags_pushes_and_publishes_both_assets(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    """The whole flow, against the throwaway bare 'origin' under tmp_path and the fake gh."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY, build=BUILD)
    tag = f"v{VERSION}"

    result = run_release(repo, release_env)

    assert result.returncode == 0, result.stderr
    assert git(repo, "log", "-1", "--format=%s", env=release_env) == f"chore: release {tag}"
    assert f"## [{VERSION}] - " in (repo / "CHANGELOG.md").read_text()
    assert git(repo, "cat-file", "-t", tag, env=release_env) == "tag"  # annotated
    assert git(repo, "tag", "-l", "--format=%(contents:subject)", tag, env=release_env) == (
        f"dstui {tag}"
    )
    origin = tmp_path / "origin.git"
    head = git(repo, "rev-parse", "HEAD", env=release_env)
    assert git(origin, "rev-parse", "main", f"{tag}^{{commit}}", env=release_env).split() == [
        head,
        head,
    ]
    notes = f"dist/RELEASE_NOTES-{tag}.md"
    assert (repo / notes).read_text() == f"{RELEASED_ENTRY}\n"
    assert (
        f"release create {tag} --title dstui {tag} --notes-file {notes} "
        "dist/dstui-install.sh install.sh"
    ) in (tmp_path / "gh.log").read_text().splitlines()
