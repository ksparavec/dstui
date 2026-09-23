"""Packaging and release: install.sh, the installer's startup script, release.sh, the locks.

Hermetic and fast: fake ``uname`` / ``curl`` / ``gh`` / ``zstd`` executables, throwaway git
repositories and everything else under ``tmp_path``. Nothing reaches GitHub.
"""

from __future__ import annotations

import datetime
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
RELEASE_SH = ROOT / "tools" / "release" / "release.sh"
STARTUP_IN = ROOT / "tools" / "package" / "startup.sh.in"
CHECK_NO_RUNTIME = ROOT / "tools" / "package" / "check-no-runtime.sh"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = PYPROJECT["project"]["version"]
RUNTIME_DIST = "deepseek-harness-runtime-bin"
SCRIPT_TIMEOUT_S = 60.0
SYSTEM_PATH = "/usr/bin:/bin"
RELEASES = "https://github.com/ksparavec/dstui/releases"
NODE_SEA_FUSE = b"NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2"


def write_program(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def run_script(
    command: list[str], env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run without a terminal (no stdin, no controlling tty), as in CI."""
    return subprocess.run(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_S,
        start_new_session=True,
    )


# ----------------------------------------------------------------------------------- install.sh


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """Fake uname (Linux x86_64) and curl: curl logs its arguments and 'downloads' an installer
    that reports its own path, DSTUI_PREFIX and TMPDIR."""
    bin_dir = tmp_path / "fake-bin"
    write_program(
        bin_dir / "uname",
        'case "$1" in -s) echo "${FAKE_OS:-Linux}" ;; -m) echo "${FAKE_ARCH:-x86_64}" ;; esac\n',
    )
    write_program(
        bin_dir / "curl",
        'printf "%s\\n" "$@" > "$FAKE_LOG"\n'
        '[ -z "$FAKE_CURL_FAIL" ] || exit 22\n'
        'while [ $# -gt 1 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        "cat > \"$out\" <<'EOF'\n"
        'echo "installer=$0"\n'
        'echo "prefix=$DSTUI_PREFIX"\n'
        'echo "tmpdir=$TMPDIR"\n'
        "EOF\n",
    )
    return bin_dir


def install_env(tmp_path: Path, fake_bin: Path, **extra: str) -> dict[str, str]:
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:{SYSTEM_PATH}",
        "FAKE_LOG": str(tmp_path / "curl.log"),
        "TMPDIR": str(tmp_path),
    }
    return env | extra


def run_install(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_script(["sh", str(INSTALL_SH)], env)


def installer_report(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


@pytest.mark.parametrize(
    ("os_name", "arch"), [("Darwin", "arm64"), ("Linux", "aarch64"), ("FreeBSD", "x86_64")]
)
def test_install_sh_refuses_an_unsupported_platform(
    os_name: str, arch: str, tmp_path: Path, fake_bin: Path
) -> None:
    result = run_install(install_env(tmp_path, fake_bin, FAKE_OS=os_name, FAKE_ARCH=arch))

    assert result.returncode == 1
    assert f"unsupported platform {os_name}/{arch}" in result.stderr
    assert not (tmp_path / "curl.log").exists()  # nothing downloaded


@pytest.mark.parametrize(
    ("version", "url"),
    [
        (None, f"{RELEASES}/latest/download/dstui-install.sh"),
        ("v0.1.0", f"{RELEASES}/download/v0.1.0/dstui-install.sh"),
    ],
    ids=["latest", "pinned"],
)
def test_install_sh_downloads_the_release_asset_and_runs_it(
    version: str | None, url: str, tmp_path: Path, fake_bin: Path
) -> None:
    extra = {"DSTUI_PREFIX": str(tmp_path / "prefix")}
    if version is not None:
        extra["DSTUI_VERSION"] = version

    result = run_install(install_env(tmp_path, fake_bin, **extra))

    assert result.returncode == 0, result.stderr
    assert url in (tmp_path / "curl.log").read_text().splitlines()
    report = installer_report(result.stdout)
    assert report["prefix"] == str(tmp_path / "prefix")
    assert not Path(report["installer"]).exists()  # the downloaded installer is removed


def test_install_sh_fails_when_the_download_fails(tmp_path: Path, fake_bin: Path) -> None:
    result = run_install(install_env(tmp_path, fake_bin, FAKE_CURL_FAIL="1"))

    assert result.returncode == 1
    assert "download failed" in result.stderr
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("dstui-install")] == []


def test_install_sh_stages_the_download_in_tmpdir(tmp_path: Path, fake_bin: Path) -> None:
    result = run_install(install_env(tmp_path, fake_bin))

    assert result.returncode == 0, result.stderr
    report = installer_report(result.stdout)
    assert Path(report["installer"]).parent == tmp_path
    assert report["tmpdir"] == str(tmp_path)


def test_install_sh_stages_in_var_tmp_never_tmp_without_tmpdir(
    tmp_path: Path, fake_bin: Path
) -> None:
    env = install_env(tmp_path, fake_bin)
    del env["TMPDIR"]

    result = run_install(env)

    assert result.returncode == 0, result.stderr
    report = installer_report(result.stdout)
    assert Path(report["installer"]).parent == Path("/var/tmp")
    assert report["tmpdir"] == "/var/tmp"  # the installer extracts there too, not in /tmp
    assert not Path(report["installer"]).exists()


def test_install_sh_names_the_repo_once_and_says_dsh_is_separate() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert [line for line in code if "ksparavec/dstui" in line] == ['REPO="ksparavec/dstui"']
    assert "@deepseek-ai/dsh" in text  # the header says dsh is a separate install


# ------------------------------------------------------------------------ installer startup


def make_extraction_dir(tmp_path: Path) -> Path:
    """What makeself extracts: startup.sh, a zstd and bundle.tar.zst (here: a plain tar)."""
    here = tmp_path / "extracted"
    write_program(here / "zstd", 'cat "$2"\n')  # called as: zstd -dc bundle.tar.zst
    members = {
        "python/bin/python3.14": "#!/bin/sh\necho bundled python\n",
        "python/bin/dstui": "#!/build/host/dist/.build/python/bin/python3.14\nprint('dstui')\n",
        "doc/README.md": "# dstui\n",
    }
    with tarfile.open(here / "bundle.tar.zst", "w") as bundle:
        for name, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            bundle.addfile(info, io.BytesIO(data))
    baked = f"DSTUI_VERSION={VERSION}\nPYVER=3.14\n"  # as build-binary.sh bakes them in
    write_program(here / "startup.sh", baked + STARTUP_IN.read_text())
    return here


def run_startup(
    here: Path, tmp_path: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    base = {"HOME": str(tmp_path / "home"), "PATH": SYSTEM_PATH, "TMPDIR": str(tmp_path)}
    return run_script(["sh", "./startup.sh", *args], base | env, cwd=here)


def test_startup_installs_into_dstui_prefix_with_only_dstui_on_path(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"

    result = run_startup(make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (prefix / "bin").iterdir()) == ["dstui"]
    assert (prefix / "bin" / "dstui").resolve() == prefix / "lib" / "dstui" / "bin" / "dstui"
    script = (prefix / "lib" / "dstui" / "bin" / "dstui").read_text().splitlines()
    assert script == [f"#!{prefix}/lib/dstui/bin/python3.14 -s", "print('dstui')"]
    assert (prefix / "share" / "doc" / "dstui" / "README.md").is_file()
    assert [p.name for p in prefix.iterdir() if p.name.startswith(".")] == []  # stage removed


def test_startup_target_option_wins_over_dstui_prefix(tmp_path: Path) -> None:
    target = tmp_path / "target"

    result = run_startup(
        make_extraction_dir(tmp_path),
        tmp_path,
        "--target",
        str(target),
        DSTUI_PREFIX=str(tmp_path / "ignored"),
    )

    assert result.returncode == 0, result.stderr
    assert (target / "bin" / "dstui").is_symlink()
    assert not (tmp_path / "ignored").exists()


def test_startup_says_that_deepseek_harness_is_a_separate_install(tmp_path: Path) -> None:
    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(tmp_path / "prefix")
    )

    assert result.returncode == 0, result.stderr
    assert "DeepSeek Harness (dsh)" in result.stdout
    assert "npm install -g @deepseek-ai/dsh" in result.stdout
    assert "Node.js >= 22.19" in result.stdout


def test_startup_refuses_a_prefix_whose_shebang_is_too_long(tmp_path: Path) -> None:
    prefix = tmp_path / ("p" * 120)

    result = run_startup(make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 1
    assert "install path too long" in result.stderr
    assert list(prefix.iterdir()) == []  # refused before anything was unpacked


# --------------------------------------------------------------------------- check-no-runtime


def run_check(*trees: Path) -> subprocess.CompletedProcess[str]:
    return run_script(["bash", str(CHECK_NO_RUNTIME), *map(str, trees)], {"PATH": SYSTEM_PATH})


def test_check_no_runtime_accepts_a_clean_tree(tmp_path: Path) -> None:
    for name in ("bin/dstui", "lib/python3.14/site-packages/deepseek_harness/client.pyc"):
        write_program(tmp_path / "tree" / name, "true\n")

    result = run_check(tmp_path / "tree")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("offender", "reported"),
    [
        ("sp/deepseek_harness_runtime/__init__.pyc", "sp/deepseek_harness_runtime"),
        (
            "sp/deepseek_harness_runtime_bin-0.1.5rc1.dist-info/RECORD",
            "sp/deepseek_harness_runtime_bin-0.1.5rc1.dist-info",
        ),
        ("bin/dsh", "bin/dsh"),
        ("lib/node_modules/@deepseek-ai/dsh/lib/bin.js", "lib/node_modules"),
        ("lib/addon.node", "lib/addon.node"),
        ("bin/node", "bin/node"),
        ("lib/renamed", "lib/renamed"),  # a Node single executable, found by its fuse
    ],
)
def test_check_no_runtime_rejects_the_runtime_and_anything_node(
    offender: str, reported: str, tmp_path: Path
) -> None:
    clean, tree = tmp_path / "clean", tmp_path / "tree"
    write_program(clean / "bin" / "dstui", "true\n")
    path = tree / offender
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x7fELF\0" + NODE_SEA_FUSE + b":0\0" if offender == "lib/renamed" else b"x")

    result = run_check(clean, tree)

    assert result.returncode == 1
    lines = result.stderr.splitlines()
    assert f"  {tree / reported}" in lines
    assert not any(str(clean) in line for line in lines)


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


def test_makefile_has_the_packaging_and_release_targets() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z][a-z-]*):", text, re.MULTILINE))

    assert targets >= {
        "help", "dev-install", "lock", "test", "test-cov", "lint", "lint-fix",
        "typecheck", "check", "package", "release", "clean",
    }  # fmt: skip
    assert "--no-emit-package deepseek-harness-runtime-bin" in text
