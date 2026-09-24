"""check-host-paths.py: the build gate that keeps build-host paths out of the payload.

build-binary.sh searches the staged tree for the build's own paths: the uv-managed CPython it
copied, the project checkout and ``$HOME/``. On a GitHub runner ``$HOME`` is /home/runner, and
pydantic_core's CycloneDX SBOM names pydantic's own CI checkout (/home/runner/work/pydantic/...):
upstream bytes that pip installed from a hash-pinned wheel, which cannot disclose this build host.
So a file listed in a ``*.dist-info/RECORD`` with a sha256 that matches it is exempt. Everything the
build or pip writes stays scanned: the compiled .pyc, _sysconfigdata, the launcher, the installer
metadata pip rehashes into RECORD, and every file that is in no RECORD or differs from it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers_scripts import PINNED_PYTHON, ROOT, SYSTEM_PATH, run_script

CHECK_HOST_PATHS = ROOT / "tools" / "package" / "check-host-paths.py"
MINOR = PINNED_PYTHON.rsplit(".", 1)[0]

# What build-binary.sh passes on a GitHub runner: the uv-managed CPython, the checkout, $HOME/.
CI_BASEP = f"/home/runner/.local/share/uv/python/cpython-{PINNED_PYTHON}-linux-x86_64-gnu"
CI_ROOT = "/home/runner/work/dstui/dstui/"
CI_HOME = "/home/runner/"
CI_PATTERNS = (CI_BASEP, CI_ROOT, CI_HOME)

SBOM = "pydantic_core-2.46.5.dist-info/sboms/pydantic-core.cyclonedx.json"
SBOM_TEXT = '{"bom-ref": "path+file:///home/runner/work/pydantic/pydantic/pydantic-core#2.46.5"}\n'
LAUNCHER = "#!/home/runner/work/dstui/dstui/dist/.build/python/bin/python3.14\nimport dstui\n"


def sha256_field(data: bytes) -> str:
    """RECORD's hash field: sha256=<urlsafe base64 without padding>."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode()}"


def site_packages(stage: Path) -> Path:
    return stage / "python" / "lib" / f"python{MINOR}" / "site-packages"


def install(stage: Path, dist: str, files: dict[str, str], record: list[str]) -> None:
    """Lay ``files`` (paths relative to site-packages) down and append ``record`` rows to the
    RECORD of the ``dist`` dist-info (which lists itself without a hash, as pip writes it)."""
    sp = site_packages(stage)
    for rel, text in files.items():
        path = sp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    record_path = sp / f"{dist}.dist-info" / "RECORD"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [*record, f"{dist}.dist-info/RECORD,,"]
    record_path.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")


def verbatim_row(rel: str, text: str) -> str:
    data = text.encode()
    return f"{rel},{sha256_field(data)},{len(data)}"


def make_stage(root: Path) -> Path:
    """A clean staged tree as build-binary.sh leaves it before the gate: sourceless (the RECORD
    still lists the deleted .py), the launcher and sysconfig data rewritten to /install, and
    pydantic_core's upstream SBOM verbatim from its wheel."""
    stage = root / "stage"
    lib = stage / "python" / "lib" / f"python{MINOR}"
    lib.mkdir(parents=True)
    (lib / "_sysconfigdata__linux_x86_64-linux-gnu.pyc").write_bytes(b"\xf3\r\r\n/install/lib")
    launcher = stage / "python" / "bin" / "dstui"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f"#!/install/bin/python{MINOR}\nimport dstui\n", encoding="utf-8")
    (stage / "python" / "bin" / "python3").symlink_to(f"python{MINOR}")
    source = "from ._pydantic_core import *\n"
    install(
        stage,
        "pydantic_core-2.46.5",
        {SBOM: SBOM_TEXT, "pydantic_core/__init__.pyc": "compiled"},
        [verbatim_row(SBOM, SBOM_TEXT), verbatim_row("pydantic_core/__init__.py", source)],
    )
    return stage


def run_gate(stage: Path, *patterns: str) -> subprocess.CompletedProcess[str]:
    return run_script(
        [sys.executable, "-I", str(CHECK_HOST_PATHS), str(stage), *patterns],
        {"PATH": SYSTEM_PATH},
    )


def leak_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    lines = result.stderr.splitlines()
    assert lines[0] == "ERROR: build-host paths in the payload:", result.stderr
    return lines[1:]


def test_an_upstream_file_verified_against_its_wheel_record_passes_the_gate(
    tmp_path: Path,
) -> None:
    """The CI failure: pydantic_core's SBOM contains /home/runner/work/pydantic/..., and $HOME/
    is /home/runner/ on the runner. The file is the wheel's own bytes, so it is only noted."""
    stage = make_stage(tmp_path)

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    sbom = site_packages(stage) / SBOM
    assert f"    upstream, verbatim from its wheel's RECORD: {sbom}: {CI_HOME}" in (
        result.stdout.splitlines()
    )
    assert (
        "ok: no build-host paths (upstream files verified against their RECORD: 1)" in result.stdout
    )


def test_a_file_that_differs_from_its_record_hash_is_scanned(tmp_path: Path) -> None:
    stage = make_stage(tmp_path)
    sbom = site_packages(stage) / SBOM
    sbom.write_text(SBOM_TEXT.replace("#2.46.5", "#2.46.6"), encoding="utf-8")

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {sbom}: {CI_HOME}"]


@pytest.mark.parametrize(
    "rel",
    [
        f"python/lib/python{MINOR}/site-packages/pydantic_core/__init__.pyc",
        f"python/lib/python{MINOR}/_sysconfigdata__linux_x86_64-linux-gnu.pyc",
        "python/bin/dstui",
    ],
    ids=["compiled-pyc", "sysconfigdata", "launcher"],
)
def test_a_file_the_build_writes_is_scanned_and_names_every_pattern_it_contains(
    rel: str, tmp_path: Path
) -> None:
    """Not in any RECORD (the RECORD lists the deleted .py, not the .pyc compiled from it). The
    checkout is under $HOME on the runner, so a checkout path matches both patterns."""
    stage = make_stage(tmp_path)
    (stage / rel).write_bytes(b"\xf3\r\r\n" + f"{CI_ROOT}src/dstui/app.py".encode() + b"\0")

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {stage / rel}: {CI_ROOT}, {CI_HOME}"]


def test_the_uv_python_path_is_caught(tmp_path: Path) -> None:
    stage = make_stage(tmp_path)
    sysconfig = stage / "python" / "lib" / f"python{MINOR}" / "_sysconfigdata_x.pyc"
    sysconfig.write_text(f"'prefix': '{CI_BASEP}'", encoding="utf-8")

    result = run_gate(stage, CI_BASEP, CI_ROOT)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {sysconfig}: {CI_BASEP}"]


def test_a_record_entry_without_a_hash_exempts_nothing(tmp_path: Path) -> None:
    stage = make_stage(tmp_path)
    notice = "pydantic_core/NOTICE"
    install(stage, "other-1.0", {notice: SBOM_TEXT}, [f"{notice},,"])

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {site_packages(stage) / notice}: {CI_HOME}"]


def test_a_script_record_entry_resolves_outside_site_packages_and_is_scanned(
    tmp_path: Path,
) -> None:
    """pip lists the console-script launcher it generates as ../../../bin/dstui with a hash of
    what it wrote: the staging interpreter's path. The build must rewrite it, so a launcher that
    still matches its RECORD row is a leak, not upstream content."""
    stage = make_stage(tmp_path)
    launcher = stage / "python" / "bin" / "dstui"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    install(stage, "dstui-0.1.0", {}, [verbatim_row("../../../bin/dstui", LAUNCHER)])

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {launcher}: {CI_ROOT}, {CI_HOME}"]


@pytest.mark.parametrize("lister", ["dstui-0.1.0", "other-1.0"], ids=["own-record", "other-record"])
@pytest.mark.parametrize("name", ["direct_url.json", "INSTALLER", "REQUESTED"])
def test_installer_metadata_pip_rehashes_into_record_is_scanned(
    name: str, lister: str, tmp_path: Path
) -> None:
    """pip writes these at install time (direct_url.json names the wheel it installed, on the
    build host) and records their hashes: they match, but are not what the wheel shipped. That
    holds whichever RECORD lists them."""
    stage = make_stage(tmp_path)
    rel = f"dstui-0.1.0.dist-info/{name}"
    text = f'{{"url": "file://{CI_ROOT}dist/dstui-0.1.0-py3-none-any.whl"}}'
    install(stage, lister, {rel: text}, [verbatim_row(rel, text)])

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {site_packages(stage) / rel}: {CI_ROOT}, {CI_HOME}"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory anyway")
def test_a_directory_the_gate_cannot_read_fails_it(tmp_path: Path) -> None:
    """A tree the gate could not search is not a clean tree."""
    stage = make_stage(tmp_path)
    hidden = stage / "python" / "lib" / "hidden"
    hidden.mkdir()
    (hidden / "leak.pyc").write_text(CI_ROOT, encoding="utf-8")
    hidden.chmod(0)
    try:
        result = run_gate(stage, *CI_PATTERNS)
    finally:
        hidden.chmod(0o755)

    assert result.returncode == 1
    assert result.stderr == (
        f"ERROR: cannot search the payload for build-host paths: [Errno 13] Permission denied:"
        f" '{hidden}'\n"
    )
    assert "ok: no build-host paths" not in result.stdout


def test_a_symlink_to_a_build_host_path_is_caught(tmp_path: Path) -> None:
    """The link target itself is payload (tar stores it); it would dangle on every other host."""
    stage = make_stage(tmp_path)
    link = stage / "python" / "lib" / "cert.pem"
    link.symlink_to(f"{CI_BASEP}/ssl/cert.pem")

    result = run_gate(stage, *CI_PATTERNS)

    assert result.returncode == 1
    assert leak_lines(result) == [f"  {link} -> {CI_BASEP}/ssl/cert.pem: {CI_BASEP}, {CI_HOME}"]


def test_a_stage_without_any_match_passes(tmp_path: Path) -> None:
    stage = make_stage(tmp_path)

    result = run_gate(stage, "/home/sparavec/")

    assert result.returncode == 0, result.stderr
    assert (
        "ok: no build-host paths (upstream files verified against their RECORD: 1)" in result.stdout
    )
    assert "upstream, verbatim" not in result.stdout


@pytest.mark.parametrize(
    "args",
    [[], ["stage"], ["stage", ""], ["stage", CI_HOME, ""], ["missing", CI_HOME]],
    ids=["none", "no-pattern", "empty-pattern", "one-empty-pattern", "not-a-directory"],
)
def test_check_host_paths_rejects_bad_usage(args: list[str], tmp_path: Path) -> None:
    make_stage(tmp_path)
    argv = [str(tmp_path / arg) if arg in {"stage", "missing"} else arg for arg in args]

    result = run_script([sys.executable, "-I", str(CHECK_HOST_PATHS), *argv], {"PATH": SYSTEM_PATH})

    assert result.returncode == 2
    assert "check-host-paths.py" in result.stderr
