"""The installer: install.sh, the makeself startup script, check-no-runtime.sh, verify-versions.py.

Hermetic and fast: fake ``uname`` / ``curl`` / ``wget`` / ``zstd`` executables, a plain tar as
the payload and everything else under ``tmp_path``. Nothing reaches GitHub: the one test that runs
the real curl sends it to local servers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

from tests.helpers_scripts import (
    INSTALL_SH,
    ROOT,
    SYSTEM_PATH,
    VERSION,
    run_script,
    write_program,
)

STARTUP_IN = ROOT / "tools" / "package" / "startup.sh.in"
CHECK_NO_RUNTIME = ROOT / "tools" / "package" / "check-no-runtime.sh"
VERIFY_VERSIONS = ROOT / "tools" / "package" / "verify-versions.py"
RELEASES = "https://github.com/ksparavec/dstui/releases"
NODE_SEA_FUSE = b"NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2"


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


def curl_args(tmp_path: Path) -> list[str]:
    return (tmp_path / "curl.log").read_text().splitlines()


def test_install_sh_downloads_with_curl_restricted_to_https(tmp_path: Path, fake_bin: Path) -> None:
    """--proto '=https' holds for every redirect too (GitHub redirects release downloads to its
    CDN), so none can downgrade the download to plain HTTP; as rustup and uv do it."""
    result = run_install(install_env(tmp_path, fake_bin))

    assert result.returncode == 0, result.stderr
    installer = installer_report(result.stdout)["installer"]
    assert curl_args(tmp_path) == [
        "--proto", "=https", "--tlsv1.2", "-fsSL",
        f"{RELEASES}/latest/download/dstui-install.sh", "-o", installer,
    ]  # fmt: skip


def test_install_sh_falls_back_to_wget_without_curl(tmp_path: Path, fake_bin: Path) -> None:
    """A PATH without curl: only the tools install.sh needs, and a fake wget. wget has no option
    that keeps redirects on HTTPS (--https-only only applies to recursive downloads), so none is
    passed as if it did; DSTUI_VERIFY=1 checks what either tool downloaded."""
    bin_dir = tmp_path / "wget-bin"
    bin_dir.mkdir()
    shutil.copy2(fake_bin / "uname", bin_dir / "uname")
    for tool in ("sh", "mktemp", "rm"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or tool)
    write_program(
        bin_dir / "wget",
        'printf "%s\\n" "$@" > "$FAKE_LOG"\n'
        'while [ $# -gt 0 ]; do case "$1" in -*O) out="$2"; shift ;; esac; shift; done\n'
        'printf \'echo "installer=$0"\\n\' > "$out"\n',
    )
    env = install_env(tmp_path, fake_bin) | {"PATH": str(bin_dir)}

    result = run_install(env)

    assert result.returncode == 0, result.stderr
    installer = installer_report(result.stdout)["installer"]
    assert curl_args(tmp_path) == [  # the fake wget logs to the same file
        "-qO", installer, f"{RELEASES}/latest/download/dstui-install.sh"
    ]  # fmt: skip


# ------------------------------------------------ install.sh: the real curl against local servers

RELEASE_PATH = "/ksparavec/dstui/releases/latest/download/dstui-install.sh"
CDN_PATH = "/cdn/dstui-install.sh"
SERVED_INSTALLER = b'echo "installer ran"\n'
OPENSSL = shutil.which("openssl")
CURL = shutil.which("curl", path=SYSTEM_PATH)


def recording_handler(redirects: dict[str, str], hits: list[str]) -> type[BaseHTTPRequestHandler]:
    """Records each GET path in ``hits``; redirects the paths in ``redirects`` (302), serves
    :data:`SERVED_INSTALLER` for any other."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            location = redirects.get(self.path)
            body = b"" if location else SERVED_INSTALLER
            self.send_response(302 if location else 200)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Quiet: the hits are recorded instead."""

    return Handler


@contextlib.contextmanager
def serving(server: ThreadingHTTPServer) -> Iterator[int]:
    """Serve on a daemon thread; yield the port."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def local_github(tmp_path: Path, hits: list[str], redirects: dict[str, str]) -> ThreadingHTTPServer:
    """An HTTPS server with a self-signed certificate for github.com (made by openssl)."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [OPENSSL or "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
         "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1", "-subj", "/CN=github.com",
         "-addext", "subjectAltName=DNS:github.com", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), recording_handler(redirects, hits))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def install_behind_a_redirect(
    tmp_path: Path, fake_bin: Path, scheme: str
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    """install.sh with the real curl, offline: ~/.curlrc sends github.com to a local HTTPS server
    (whose certificate it trusts) that redirects the release download, as GitHub does to its CDN,
    to itself (``https``) or to a local plain HTTP server (``http``). Returns the result and the
    paths each server was asked for."""
    (fake_bin / "curl").unlink()  # the real one, from the system PATH
    github_hits: list[str] = []
    http_hits: list[str] = []
    redirects: dict[str, str] = {}
    github = local_github(tmp_path, github_hits, redirects)
    plain = ThreadingHTTPServer(("127.0.0.1", 0), recording_handler({}, http_hits))
    with serving(github) as github_port, serving(plain) as http_port:
        cdn = "https://github.com" if scheme == "https" else f"http://127.0.0.1:{http_port}"
        redirects[RELEASE_PATH] = cdn + CDN_PATH
        home = tmp_path / "home"
        home.mkdir()
        (home / ".curlrc").write_text(
            f'connect-to = "github.com:443:127.0.0.1:{github_port}"\n'
            f'cacert = "{tmp_path / "cert.pem"}"\n'
        )
        result = run_install(install_env(tmp_path, fake_bin))
    return result, github_hits, http_hits


needs_openssl_and_curl = pytest.mark.skipif(
    OPENSSL is None or CURL is None, reason="needs openssl and curl"
)


@needs_openssl_and_curl
def test_install_sh_lets_curl_follow_a_redirect_that_stays_on_https(
    tmp_path: Path, fake_bin: Path
) -> None:
    """The control for the next test: the local servers and ~/.curlrc work."""
    result, github_hits, _ = install_behind_a_redirect(tmp_path, fake_bin, "https")

    assert result.returncode == 0, result.stderr
    assert "installer ran" in result.stdout
    assert github_hits == [RELEASE_PATH, CDN_PATH]


@needs_openssl_and_curl
def test_install_sh_lets_curl_refuse_a_redirect_to_plain_http(
    tmp_path: Path, fake_bin: Path
) -> None:
    result, github_hits, http_hits = install_behind_a_redirect(tmp_path, fake_bin, "http")

    assert result.returncode == 1
    assert 'Protocol "http"' in result.stderr  # curl's refusal
    assert "download failed" in result.stderr
    assert "installer ran" not in result.stdout
    assert github_hits == [RELEASE_PATH]
    assert http_hits == []  # refused before connecting
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("dstui-install")] == []


def test_install_sh_names_the_repo_once_and_says_dsh_is_separate() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    assert [line for line in code if "ksparavec/dstui" in line] == ['REPO="ksparavec/dstui"']
    assert "@deepseek-ai/dsh" in text  # the header says dsh is a separate install


# ------------------------------------------------------------------ install.sh: DSTUI_VERIFY=1


def write_fake_gh(bin_dir: Path, rc: int = 0) -> None:
    """A gh that logs its arguments, one per line, and keeps a copy of the file it checked."""
    write_program(
        bin_dir / "gh",
        'printf "%s\\n" "$@" > "$FAKE_GH_LOG"\n'
        '[ ! -f "$3" ] || cp "$3" "$FAKE_GH_LOG.subject"\n'
        f"exit {rc}\n",
    )


def gh_env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    return install_env(tmp_path, bin_dir, FAKE_GH_LOG=str(tmp_path / "gh.log"), **extra)


def downloads_left(tmp_path: Path) -> list[str]:
    return [p.name for p in tmp_path.iterdir() if p.name.startswith("dstui-install")]


def test_install_sh_verifies_the_attestation_before_running_the_installer(
    tmp_path: Path, fake_bin: Path
) -> None:
    write_fake_gh(fake_bin)

    result = run_install(gh_env(tmp_path, fake_bin, DSTUI_VERIFY="1"))

    assert result.returncode == 0, result.stderr
    installer = installer_report(result.stdout)["installer"]
    gh_args = (tmp_path / "gh.log").read_text().splitlines()
    assert gh_args == ["attestation", "verify", installer, "--repo", "ksparavec/dstui"]
    assert 'echo "installer=$0"' in (tmp_path / "gh.log.subject").read_text()  # the download
    assert downloads_left(tmp_path) == []


def test_install_sh_does_not_run_an_installer_that_fails_verification(
    tmp_path: Path, fake_bin: Path
) -> None:
    write_fake_gh(fake_bin, rc=1)

    result = run_install(gh_env(tmp_path, fake_bin, DSTUI_VERIFY="1"))

    assert result.returncode == 1
    assert "installer=" not in result.stdout  # never executed
    assert "attestation verification failed" in result.stderr
    assert (tmp_path / "gh.log").exists()
    assert downloads_left(tmp_path) == []  # and removed


def test_install_sh_verify_without_gh_fails_before_downloading(
    tmp_path: Path, fake_bin: Path
) -> None:
    """A PATH with only what install.sh needs (the system dirs may hold a real gh)."""
    bin_dir = tmp_path / "no-gh-bin"
    bin_dir.mkdir()
    for fake in ("uname", "curl"):
        shutil.copy2(fake_bin / fake, bin_dir / fake)
    for tool in ("sh", "mktemp", "rm", "cat"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or tool)
    env = install_env(tmp_path, fake_bin, DSTUI_VERIFY="1") | {"PATH": str(bin_dir)}

    result = run_install(env)

    assert result.returncode == 1
    assert "DSTUI_VERIFY=1 needs the GitHub CLI (gh)" in result.stderr
    assert "installer=" not in result.stdout
    assert not (tmp_path / "curl.log").exists()  # nothing downloaded
    assert downloads_left(tmp_path) == []


@pytest.mark.parametrize("value", [None, "", "0"], ids=["unset", "empty", "zero"])
def test_install_sh_without_dstui_verify_never_calls_gh(
    value: str | None, tmp_path: Path, fake_bin: Path
) -> None:
    write_fake_gh(fake_bin, rc=1)
    extra = {} if value is None else {"DSTUI_VERIFY": value}

    result = run_install(gh_env(tmp_path, fake_bin, **extra))

    assert result.returncode == 0, result.stderr
    assert "installer=" in result.stdout
    assert not (tmp_path / "gh.log").exists()


@pytest.mark.parametrize("value", ["yes", "true", "2"])
def test_install_sh_refuses_an_unknown_dstui_verify_value(
    value: str, tmp_path: Path, fake_bin: Path
) -> None:
    """Fail closed: a typo must not silently skip the verification that was asked for."""
    write_fake_gh(fake_bin)

    result = run_install(gh_env(tmp_path, fake_bin, DSTUI_VERIFY=value))

    assert result.returncode == 1
    assert f"DSTUI_VERIFY must be 1 or 0, got '{value}'" in result.stderr
    assert not (tmp_path / "curl.log").exists()
    assert not (tmp_path / "gh.log").exists()


# ------------------------------------------------------------------------ installer startup

FOREIGN_ID = 4242  # the archive's owner: a uid/gid that means nothing on the installing host
UNSHARE = shutil.which("unshare")


ZSTD_THAT_RUNS = '[ "$1" = --version ] || cat "$2"\n'  # zstd --version, then zstd -dc BUNDLE


def make_extraction_dir(
    tmp_path: Path, python: str = "echo bundled python\n", zstd: str = ZSTD_THAT_RUNS
) -> Path:
    """What makeself extracts: startup.sh, a zstd and bundle.tar.zst (here: a plain tar).

    Like a careless build, the tar keeps a foreign owner and group-writable modes. ``python``
    and ``zstd`` are the bodies of the fake bundled interpreter and zstd (the installer runs
    each once, to see that it can, before it installs anything).
    """
    here = tmp_path / "extracted"
    write_program(here / "zstd", zstd)
    members = {
        "python/": None,
        "python/bin/": None,
        "python/bin/python3.14": f"#!/bin/sh\n{python}",
        "python/bin/dstui": "#!/build/host/dist/.build/python/bin/python3.14\nprint('dstui')\n",
        "python/lib/": None,
        "python/lib/module.pyc": "pyc",
        "doc/": None,
        "doc/README.md": "# dstui\n",
    }
    with tarfile.open(here / "bundle.tar.zst", "w") as bundle:
        for name, content in members.items():
            info = tarfile.TarInfo(name.rstrip("/"))
            info.uid = info.gid = FOREIGN_ID
            if content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o775
                bundle.addfile(info)
                continue
            data = content.encode()
            info.size = len(data)
            info.mode = 0o775 if "/bin/" in name else 0o664
            bundle.addfile(info, io.BytesIO(data))
    baked = f"DSTUI_VERSION={VERSION}\nPYVER=3.14\n"  # as build-binary.sh bakes them in
    write_program(here / "startup.sh", baked + STARTUP_IN.read_text())
    return here


def run_startup(
    here: Path, tmp_path: Path, *args: str, wrap: tuple[str, ...] = (), **env: str
) -> subprocess.CompletedProcess[str]:
    """Run startup.sh as makeself does: from ``here``, under umask 077, via ``sh``."""
    base = {"HOME": str(tmp_path / "home"), "PATH": SYSTEM_PATH, "TMPDIR": str(tmp_path)}
    command = [*wrap, "sh", "-c", 'umask 077 && exec sh ./startup.sh "$@"', "sh", *args]
    return run_script(command, base | env, cwd=here)


@pytest.fixture
def short(tmp_path: Path) -> Iterator[Path]:
    """A short path (a symlink to ``tmp_path`` under /var/tmp) for install prefixes.

    The installer refuses a prefix whose launcher shebang exceeds the kernel's 127 bytes, and
    pytest's temp paths can be longer than that allows.
    """
    holder = Path(tempfile.mkdtemp(prefix="dstui-p.", dir="/var/tmp"))
    link = holder / "t"
    link.symlink_to(tmp_path, target_is_directory=True)
    yield link
    shutil.rmtree(holder)


def assert_installed(prefix: Path) -> None:
    assert sorted(p.name for p in (prefix / "bin").iterdir()) == ["dstui"]
    assert os.readlink(prefix / "bin" / "dstui") == f"{prefix}/lib/dstui/bin/dstui"
    script = (prefix / "lib" / "dstui" / "bin" / "dstui").read_text().splitlines()
    assert script == [f"#!{prefix}/lib/dstui/bin/python3.14 -I", "print('dstui')"]
    assert (prefix / "share" / "doc" / "dstui" / "README.md").is_file()
    assert [p.name for p in prefix.iterdir() if p.name.startswith(".")] == []  # stage removed


def assert_usable_by_everyone_writable_only_by_owner(prefix: Path) -> None:
    for path in [prefix, *prefix.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        assert mode & 0o022 == 0, f"group/other-writable: {path} {oct(mode)}"
        assert mode & 0o004, f"not world-readable: {path} {oct(mode)}"
        if path.is_dir():
            assert mode & 0o001, f"not world-traversable: {path} {oct(mode)}"


def test_startup_installs_into_dstui_prefix_with_only_dstui_on_path(
    tmp_path: Path, short: Path
) -> None:
    prefix = short / "prefix"

    result = run_startup(make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 0, result.stderr
    assert_installed(prefix)


def test_startup_launcher_runs_the_bundled_python_isolated(tmp_path: Path, short: Path) -> None:
    """-I, not just -s: PYTHONPATH/PYTHONHOME and the script's directory never reach sys.path."""
    prefix = short / "prefix"

    run_startup(make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(prefix))

    shebang = (prefix / "lib" / "dstui" / "bin" / "dstui").read_text().splitlines()[0]
    assert shebang.endswith(" -I")


def test_startup_prefix_option_wins_over_dstui_prefix(tmp_path: Path, short: Path) -> None:
    prefix = short / "chosen"

    result = run_startup(
        make_extraction_dir(tmp_path),
        tmp_path,
        "--prefix",
        str(prefix),
        DSTUI_PREFIX=str(short / "ignored"),
    )

    assert result.returncode == 0, result.stderr
    assert_installed(prefix)
    assert not (short / "ignored").exists()


@pytest.mark.parametrize("args", [["--target", "/opt/dstui"], ["--bogus"], ["--prefix"]])
def test_startup_rejects_an_unknown_or_incomplete_option(
    args: list[str], tmp_path: Path, short: Path
) -> None:
    """--target belongs to makeself: plain --target DIR keeps the raw payload in DIR and still
    installs into the default prefix. Only `-- --target` gets here, and it is refused."""
    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, *args, DSTUI_PREFIX=str(short / "prefix")
    )

    assert result.returncode == 2
    assert "Usage: sh dstui-install.sh [-- --prefix DIR]" in result.stderr
    assert not (short / "prefix").exists()


def test_startup_says_that_deepseek_harness_is_a_separate_install(
    tmp_path: Path, short: Path
) -> None:
    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(short / "prefix")
    )

    assert result.returncode == 0, result.stderr
    assert "DeepSeek Harness (dsh)" in result.stdout
    assert "npm install -g @deepseek-ai/dsh" in result.stdout
    assert "Node.js >= 22.19" in result.stdout


def test_startup_refuses_a_prefix_whose_shebang_is_too_long(tmp_path: Path) -> None:
    parent = tmp_path / "new"

    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(parent / ("p" * 120))
    )

    assert result.returncode == 1
    assert "install path too long" in result.stderr
    assert "DSTUI_PREFIX" in result.stderr
    assert not parent.exists()  # refused before anything was created


@pytest.mark.parametrize("blank", [" ", "\t", "\n"], ids=["space", "tab", "newline"])
def test_startup_refuses_a_prefix_with_whitespace(blank: str, tmp_path: Path, short: Path) -> None:
    """The kernel splits a shebang at whitespace: the launcher could never start."""
    parent = short / f"my{blank}tools"

    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(parent / "dstui")
    )

    assert result.returncode == 1
    assert "whitespace" in result.stderr
    assert "DSTUI_PREFIX" in result.stderr
    assert not parent.exists()  # refused before anything was created


@pytest.mark.parametrize("how", ["env", "option"])
def test_startup_resolves_a_relative_prefix_against_the_callers_directory(
    how: str, tmp_path: Path, short: Path
) -> None:
    """makeself runs startup.sh inside its temp dir and deletes it afterwards; the caller's
    directory is $USER_PWD."""
    caller = short / "caller"
    caller.mkdir()
    args, env = (
        (["--prefix", "rel/pfx"], {}) if how == "option" else ([], {"DSTUI_PREFIX": "rel/pfx"})
    )

    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, *args, USER_PWD=str(caller), **env
    )

    assert result.returncode == 0, result.stderr
    assert_installed(caller / "rel" / "pfx")
    assert not (tmp_path / "extracted" / "rel").exists()


def test_startup_expands_a_quoted_tilde_prefix(tmp_path: Path, short: Path) -> None:
    home = short / "home"

    result = run_startup(
        make_extraction_dir(tmp_path), tmp_path, HOME=str(home), DSTUI_PREFIX="~/apps"
    )

    assert result.returncode == 0, result.stderr
    assert_installed(home / "apps")


def test_startup_installs_a_tree_everyone_can_use_but_only_the_owner_can_write(
    tmp_path: Path, short: Path
) -> None:
    """Under makeself's umask 077, from an archive with group-writable modes."""
    prefix = short / "prefix"

    result = run_startup(make_extraction_dir(tmp_path), tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 0, result.stderr
    assert_usable_by_everyone_writable_only_by_owner(prefix)


def userns_available(*flags: str) -> bool:
    if UNSHARE is None:
        return False
    probe = subprocess.run(
        [UNSHARE, "--user", "--map-root-user", *flags, "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


@pytest.mark.skipif(not userns_available(), reason="needs unprivileged user namespaces")
def test_startup_as_root_gives_the_tree_to_root_not_to_the_archived_owner(
    tmp_path: Path, short: Path
) -> None:
    """As root, tar would restore the archive's owner (uid 4242, unmapped in the namespace, so
    chown fails) and its group-writable modes."""
    prefix = short / "prefix"

    result = run_startup(
        make_extraction_dir(tmp_path),
        tmp_path,
        wrap=(str(UNSHARE), "--user", "--map-root-user"),
        DSTUI_PREFIX=str(prefix),
    )

    assert result.returncode == 0, result.stderr
    assert_installed(prefix)
    assert {p.lstat().st_uid for p in [prefix, *prefix.rglob("*")]} == {os.getuid()}
    assert_usable_by_everyone_writable_only_by_owner(prefix)


@pytest.mark.skipif(not userns_available("--mount"), reason="needs user + mount namespaces")
def test_startup_works_when_the_extraction_dir_is_mounted_noexec(
    tmp_path: Path, short: Path
) -> None:
    """CIS-hardened hosts mount /var/tmp (makeself's TMPDIR) noexec: nothing may be executed
    from there, neither startup.sh nor the bundled zstd."""
    here, noexec, prefix = make_extraction_dir(tmp_path), tmp_path / "noexec", short / "prefix"
    noexec.mkdir()
    script = (
        'mount -t tmpfs -o noexec,mode=0700 tmpfs "$1" && cp -p "$2"/* "$1"/ && cd "$1" '
        "&& umask 077 && exec sh ./startup.sh"
    )
    command = [str(UNSHARE), "--user", "--map-root-user", "--mount", "sh", "-c", script]
    env = {"HOME": str(tmp_path / "home"), "PATH": SYSTEM_PATH, "DSTUI_PREFIX": str(prefix)}

    result = run_script([*command, "sh", str(noexec), str(here)], env)

    assert result.returncode == 0, result.stderr
    assert_installed(prefix)


def test_startup_refuses_an_interpreter_that_cannot_run_here_and_keeps_the_old_install(
    tmp_path: Path, short: Path
) -> None:
    """E.g. a musl host (the bundled CPython needs glibc)."""
    prefix = short / "prefix"
    old = prefix / "lib" / "dstui" / "bin" / "dstui"
    write_program(old, "echo old\n")
    here = make_extraction_dir(tmp_path, python="exit 127\n")

    result = run_startup(here, tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 1
    assert "the bundled Python cannot run on this host" in result.stderr
    assert old.read_text() == "#!/bin/sh\necho old\n"
    assert [p.name for p in prefix.iterdir()] == ["lib"]  # no stage left, nothing added


def test_startup_refuses_a_zstd_that_cannot_run_here_and_keeps_the_old_install(
    tmp_path: Path, short: Path
) -> None:
    """Said plainly, not as tar's errors about an empty archive."""
    prefix = short / "prefix"
    old = prefix / "lib" / "dstui" / "bin" / "dstui"
    write_program(old, "echo old\n")
    here = make_extraction_dir(tmp_path, zstd="exit 126\n")

    result = run_startup(here, tmp_path, DSTUI_PREFIX=str(prefix))

    assert result.returncode == 1
    assert "the bundled zstd cannot run on this host" in result.stderr
    assert "tar:" not in result.stderr
    assert old.read_text() == "#!/bin/sh\necho old\n"
    assert [p.name for p in prefix.iterdir()] == ["lib"]  # no stage left, nothing added


@pytest.mark.skipif(not userns_available("--mount"), reason="needs user + mount namespaces")
def test_startup_refuses_a_prefix_mounted_noexec_and_keeps_the_old_install(
    tmp_path: Path, short: Path
) -> None:
    """The stage sits under the prefix, so there the bundled zstd is the first thing that cannot
    run: the installer says why (exit 1) instead of failing inside tar (exit 2)."""
    here, prefix = make_extraction_dir(tmp_path), short / "prefix"
    old = prefix / "lib" / "dstui" / "bin" / "dstui"
    write_program(old, "echo old\n")
    script = (
        'mount --bind "$1" "$1" && mount -o remount,bind,noexec "$1" '
        "&& umask 077 && exec sh ./startup.sh"
    )
    command = [str(UNSHARE), "--user", "--map-root-user", "--mount", "sh", "-c", script]
    env = {"HOME": str(tmp_path / "home"), "PATH": SYSTEM_PATH, "DSTUI_PREFIX": str(prefix)}

    result = run_script([*command, "sh", str(prefix)], env, cwd=here)

    assert result.returncode == 1
    assert "the bundled zstd cannot run on this host" in result.stderr
    assert "not mounted noexec" in result.stderr
    assert "tar:" not in result.stderr
    assert old.read_text() == "#!/bin/sh\necho old\n"
    assert [p.name for p in prefix.iterdir()] == ["lib"]  # no stage left, nothing added


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


# --------------------------------------------------------------------------- verify-versions


def load_verify_versions(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script as the bundle's interpreter runs it: without `packaging` (it is not bundled)."""
    for name in ("packaging", "packaging.version"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # no __pycache__ in tools/package
    spec = importlib.util.spec_from_file_location("verify_versions", VERIFY_VERSIONS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1.0", "1.0.0"),
        ("0.1.5rc1", "0.1.5.rc1"),
        ("2.0.0-rc.2", "2.0.0rc2"),
        ("2026.06.17", "2026.6.17"),
        ("1.0a1", "1.0alpha1"),
        ("1.0b2", "1.0-beta.2"),
        ("1.0c1", "1.0rc1"),
        ("1.0pre1", "1.0rc1"),
        ("1.0-1", "1.0.post1"),
        ("1.0.rev2", "1.0post2"),
        ("1.0.dev0", "1.0dev"),
        ("v1.0", "1.0"),
        ("1.0+Local.01", "1.0+local-1"),
        ("0!1.0", "1.0"),
        (" 8.2.8 ", "8.2.8"),
    ],
)
def test_verify_versions_treats_pep440_equal_versions_as_equal(
    a: str, b: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ver_eq = load_verify_versions(monkeypatch)._ver_eq

    assert ver_eq(a, b)
    assert ver_eq(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1.0", "1.0.1"),
        ("1.0rc1", "1.0"),
        ("1.0rc1", "1.0rc2"),
        ("1.0a1", "1.0b1"),
        ("1.0", "1.0.post0"),
        ("1.0", "1.0.dev0"),
        ("1!1.0", "1.0"),
        ("1.0+a", "1.0"),
        ("not-a-version", "not-a-version-2"),
    ],
)
def test_verify_versions_keeps_different_versions_apart(
    a: str, b: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ver_eq = load_verify_versions(monkeypatch)._ver_eq

    assert not ver_eq(a, b)
    assert not ver_eq(b, a)
