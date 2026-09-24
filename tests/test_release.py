"""release.sh and release-notes.sh against throwaway git repos, a bare origin and a fake gh.

release.sh only tags: it promotes the CHANGELOG, commits, tags and pushes both atomically. The
pushed tag triggers .github/workflows/release.yml, which builds, attests and publishes; release.sh
then just follows that run. Everything lives under ``tmp_path``; nothing reaches GitHub.
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
RELEASE_NOTES_SH = ROOT / "tools" / "release" / "release-notes.sh"
TAG = f"v{VERSION}"
RUN_ID = "4242"
REPO_URL = "https://github.com/ksparavec/dstui"

# A gh that is authenticated, logs every call and knows no release. `run list` answers
# $FAKE_GH_RUN_ID from its (FAKE_GH_RUN_AFTER + 1)th call on; `run watch` exits FAKE_GH_WATCH_RC.
# Anything else (such as `release create`) is not expected from release.sh and fails.
FAKE_GH = """\
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "auth status") ;;
  "repo view") echo ksparavec/dstui ;;
  "release view")
    case "$*" in
      *--json*) echo "https://github.com/ksparavec/dstui/releases/tag/$3" ;;
      *) [ -n "$FAKE_GH_RELEASE_EXISTS" ] || exit 1 ;;
    esac ;;
  "run list")
    calls=$(cat "$FAKE_GH_LOG.runs" 2>/dev/null || echo 0)
    echo $((calls + 1)) > "$FAKE_GH_LOG.runs"
    if [ "$calls" -ge "${FAKE_GH_RUN_AFTER:-0}" ]; then echo "$FAKE_GH_RUN_ID"; fi ;;
  "run watch") exit "${FAKE_GH_WATCH_RC:-0}" ;;
  *) exit 64 ;;
esac
"""


# ---------------------------------------------------------------------------------- release.sh

RELEASED_ENTRY = "### Added\n- The first thing.\n- The second thing."


def git(repo: Path, *args: str, env: dict[str, str]) -> str:
    result = run_script(["git", *args], env, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def release_env(tmp_path: Path) -> dict[str, str]:
    """PATH with the fake gh, isolated git, and no waiting for a workflow run to show up."""
    write_program(tmp_path / "gh-bin" / "gh", FAKE_GH)
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tmp_path / 'gh-bin'}:{SYSTEM_PATH}",
        "TMPDIR": str(tmp_path),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_RUN_ID": RUN_ID,
        "DSTUI_RELEASE_WATCH_WAIT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def make_release_repo(tmp_path: Path, env: dict[str, str], unreleased: str) -> Path:
    """A repo on main, in sync with a bare 'origin' under tmp_path, carrying the real release
    scripts and a tools/package/build-binary.sh that only leaves a marker (CI builds now)."""
    repo, origin = tmp_path / "repo", tmp_path / "origin.git"
    (repo / "tools" / "release").mkdir(parents=True)
    shutil.copy2(RELEASE_SH, repo / "tools" / "release" / "release.sh")
    shutil.copy2(RELEASE_NOTES_SH, repo / "tools" / "release" / "release-notes.sh")
    shutil.copy2(INSTALL_SH, repo / "install.sh")
    write_program(repo / "tools" / "package" / "build-binary.sh", "touch ../build-ran\n")
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


def promoted_changelog(day: str) -> str:
    return changelog(RELEASED_ENTRY).replace(
        "## [Unreleased]\n", f"## [Unreleased]\n\n## [{VERSION}] - {day}\n"
    )


def run_release(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_script(
        ["bash", "tools/release/release.sh"], env | {"DSTUI_RELEASE_ASSUME_YES": "1"}, cwd=repo
    )


def gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh.log"
    return log.read_text().splitlines() if log.exists() else []


def assert_nothing_built_or_published(tmp_path: Path) -> None:
    assert not (tmp_path / "build-ran").exists()  # the release workflow builds, not release.sh
    assert not [call for call in gh_calls(tmp_path) if call.startswith("release create")]


def assert_nothing_released(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    assert git(repo, "tag", "--list", env=env) == ""
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert git(repo, "log", "--format=%s", env=env) == "initial"
    origin = tmp_path / "origin.git"
    assert git(origin, "log", "--format=%s", "main", env=env) == "initial"
    assert git(origin, "tag", "--list", env=env) == ""
    assert_nothing_built_or_published(tmp_path)
    assert not [call for call in gh_calls(tmp_path) if call.startswith("run ")]


def assert_released_at_origin(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    """The release commit and the annotated tag, both on origin, both at HEAD."""
    assert git(repo, "log", "-1", "--format=%s", env=env) == f"chore: release {TAG}"
    assert git(repo, "cat-file", "-t", TAG, env=env) == "tag"  # annotated
    assert git(repo, "tag", "-l", "--format=%(contents:subject)", TAG, env=env) == f"dstui {TAG}"
    head = git(repo, "rev-parse", "HEAD", env=env)
    origin = tmp_path / "origin.git"
    assert git(origin, "rev-parse", "main", f"{TAG}^{{commit}}", env=env).split() == [head, head]


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
    """The tagged commit would not contain what was tested locally."""
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
    assert_nothing_built_or_published(tmp_path)


def test_release_refuses_when_main_is_not_in_sync_with_origin(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    git(repo, "commit", "-q", "--allow-empty", "-m", "unpushed", env=release_env)

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "local main differs from origin/main" in result.stderr
    assert git(repo, "tag", "--list", env=release_env) == ""
    assert_nothing_built_or_published(tmp_path)


@pytest.mark.parametrize("where", ["local", "origin", "github"])
def test_release_refuses_a_version_that_is_already_tagged_or_released(
    where: str, tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    if where in ("local", "origin"):
        git(repo, "tag", TAG, env=release_env)
    if where == "origin":
        git(repo, "push", "-q", "origin", TAG, env=release_env)
        git(repo, "tag", "-d", TAG, env=release_env)
    env = release_env | ({"FAKE_GH_RELEASE_EXISTS": "1"} if where == "github" else {})

    result = run_release(repo, env)

    expected = {
        "local": f"tag {TAG} already exists locally",
        "origin": f"tag {TAG} already exists on origin",
        "github": f"a GitHub release for {TAG} already exists",
    }[where]
    assert result.returncode == 1
    assert expected in result.stderr
    assert git(repo, "log", "--format=%s", env=release_env) == "initial"
    assert_nothing_built_or_published(tmp_path)


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


def test_release_promotes_unreleased_and_reverts_it_when_the_commit_fails(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    write_program(
        repo / ".git" / "hooks" / "pre-commit",
        "cp CHANGELOG.md ../changelog-at-commit.md\nexit 1\n",
    )
    before = datetime.date.today().isoformat()

    result = run_release(repo, release_env)

    after = datetime.date.today().isoformat()
    assert result.returncode == 1
    promoted = (tmp_path / "changelog-at-commit.md").read_text()
    assert promoted in {promoted_changelog(day) for day in (before, after)}
    assert "Reverted CHANGELOG.md" in result.stderr
    assert (repo / "CHANGELOG.md").read_text() == changelog(RELEASED_ENTRY)
    assert_nothing_released(repo, tmp_path, release_env)


def test_release_commits_tags_and_pushes_without_building_or_publishing(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    """The tag push is the release: CI builds, attests and publishes. release.sh follows the
    workflow run the tag triggered and prints the release URL."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)

    result = run_release(repo, release_env)

    assert result.returncode == 0, result.stderr
    assert f"## [{VERSION}] - " in (repo / "CHANGELOG.md").read_text()
    assert_released_at_origin(repo, tmp_path, release_env)
    assert_nothing_built_or_published(tmp_path)
    head = git(repo, "rev-parse", "HEAD", env=release_env)
    (run_list,) = [call for call in gh_calls(tmp_path) if call.startswith("run list")]
    for option in ("--workflow release.yml", f"--branch {TAG}", f"--commit {head}", "--event push"):
        assert option in run_list
    assert f"run watch {RUN_ID} --exit-status" in gh_calls(tmp_path)
    assert f"{REPO_URL}/releases/tag/{TAG}" in result.stdout


def test_release_waits_for_the_workflow_run_to_show_up(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    """GitHub creates the run a few seconds after the push."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    env = release_env | {"FAKE_GH_RUN_AFTER": "1", "DSTUI_RELEASE_WATCH_WAIT": "30"}

    result = run_release(repo, env)

    assert result.returncode == 0, result.stderr
    assert len([call for call in gh_calls(tmp_path) if call.startswith("run list")]) == 2
    assert f"run watch {RUN_ID} --exit-status" in gh_calls(tmp_path)


@pytest.mark.parametrize("answer", ["", "null"], ids=["empty", "null"])
def test_release_succeeds_when_no_workflow_run_shows_up(
    answer: str, tmp_path: Path, release_env: dict[str, str]
) -> None:
    """The tag is pushed and the workflow will run; only following it is not possible."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)

    result = run_release(repo, release_env | {"FAKE_GH_RUN_ID": answer})

    assert result.returncode == 0, result.stderr
    assert_released_at_origin(repo, tmp_path, release_env)
    assert f"{REPO_URL}/actions/workflows/release.yml" in result.stdout
    assert not [call for call in gh_calls(tmp_path) if call.startswith("run watch")]


def test_release_reports_a_failed_workflow_run_with_its_recovery(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)

    result = run_release(repo, release_env | {"FAKE_GH_WATCH_RC": "1"})

    assert result.returncode == 1
    assert_released_at_origin(repo, tmp_path, release_env)  # the tag stays pushed
    assert f"{TAG} is pushed but not published" in result.stderr
    assert f"gh run view {RUN_ID} --log-failed" in result.stderr
    assert f"gh run rerun {RUN_ID} --failed" in result.stderr
    assert f"git push origin :refs/tags/{TAG}" in result.stderr


def test_release_pushes_commit_and_tag_atomically(
    tmp_path: Path, release_env: dict[str, str]
) -> None:
    """origin refuses the tag: main must not move either, or a half-release would sit on main."""
    repo = make_release_repo(tmp_path, release_env, RELEASED_ENTRY)
    write_program(
        tmp_path / "origin.git" / "hooks" / "update",
        'case "$1" in refs/tags/*) echo "tags are frozen" >&2; exit 1 ;; esac\n',
    )

    result = run_release(repo, release_env)

    assert result.returncode == 1
    assert "push failed" in result.stderr
    origin = tmp_path / "origin.git"
    assert git(origin, "log", "--format=%s", "main", env=release_env) == "initial"
    assert git(origin, "tag", "--list", env=release_env) == ""
    assert git(repo, "log", "-1", "--format=%s", env=release_env) == f"chore: release {TAG}"
    assert f"git tag -d {TAG} && git reset --hard HEAD~1" in result.stderr
    assert f"git push --atomic origin main {TAG}" in result.stderr
    assert "gh release create" not in result.stderr
    assert_nothing_built_or_published(tmp_path)
    assert not [call for call in gh_calls(tmp_path) if call.startswith("run ")]


# --------------------------------------------------------------------------- release-notes.sh

NOTES_CHANGELOG = """\
# Changelog

## [Unreleased]

## [0.2.0] - 2026-09-24

### Fixed
- Two.

## [0.1.10] - 2026-05-01

- Ten.

## [0.1.0]

### Added
- One.
"""


def run_notes(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(NOTES_CHANGELOG)
    return run_script(["bash", str(RELEASE_NOTES_SH), *args, str(path)], {"PATH": SYSTEM_PATH})


@pytest.mark.parametrize(
    ("version", "notes"),
    [
        ("0.2.0", "### Fixed\n- Two.\n"),
        ("0.1.10", "- Ten.\n"),
        ("0.1.0", "### Added\n- One.\n"),  # the last section, without a date
    ],
)
def test_release_notes_prints_exactly_the_versions_section(
    version: str, notes: str, tmp_path: Path
) -> None:
    result = run_notes(tmp_path, version)

    assert result.returncode == 0, result.stderr
    assert result.stdout == notes


@pytest.mark.parametrize("version", ["0.1", "0.1.1", "0.3.0"])
def test_release_notes_refuses_a_version_without_a_section(version: str, tmp_path: Path) -> None:
    result = run_notes(tmp_path, version)

    assert result.returncode == 1
    assert f"no '## [{version}]' section in" in result.stderr
    assert result.stdout == ""


def test_release_notes_refuses_an_empty_section(tmp_path: Path) -> None:
    result = run_notes(tmp_path, "Unreleased")

    assert result.returncode == 1
    assert "'## [Unreleased]' section in" in result.stderr
    assert "is empty" in result.stderr


def test_release_notes_reads_the_projects_changelog_by_default(tmp_path: Path) -> None:
    """Whatever [Unreleased] holds right now (it is empty right after a release)."""
    explicit = run_script(
        ["bash", str(RELEASE_NOTES_SH), "Unreleased", str(ROOT / "CHANGELOG.md")],
        {"PATH": SYSTEM_PATH},
        cwd=tmp_path,
    )

    default = run_script(
        ["bash", str(RELEASE_NOTES_SH), "Unreleased"], {"PATH": SYSTEM_PATH}, cwd=tmp_path
    )

    assert (default.returncode, default.stdout, default.stderr) == (
        explicit.returncode,
        explicit.stdout,
        explicit.stderr,
    )
    assert default.returncode in (0, 1)
    assert default.stdout or "is empty" in default.stderr


@pytest.mark.parametrize("args", [[], ["1.0.0", "CHANGELOG.md", "extra"]], ids=["none", "three"])
def test_release_notes_rejects_bad_usage(args: list[str]) -> None:
    result = run_script(["bash", str(RELEASE_NOTES_SH), *args], {"PATH": SYSTEM_PATH})

    assert result.returncode == 2
    assert "usage: release-notes.sh VERSION [CHANGELOG]" in result.stderr
