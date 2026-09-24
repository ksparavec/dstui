"""check-python.sh: the build gate on the staged interpreter (exact version, no libpython).

python-build-standalone links libpython statically into ``bin/python3.X``; the shared
``libpython3.X.so`` next to it (32 MB) is for embedding only, so the build drops it and this gate
fails if the version is not exactly ``.python-version``, if a libpython file is still there, or if
any ELF in the tree NEEDs one. The ELF files here are minimal hand-made shared objects with just a
dynamic section, which is all ``readelf -d`` reads.

Before that, build-binary.sh has uv install exactly that CPython; when uv cannot (too old to know
a newly pinned patch, or Python downloads disabled) the build stops with uv's own reason. After
it, the build precompiles the staged tree and deletes every ``.py``, so a module that does not
compile fails the build instead of going missing from the bundle.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers_scripts import PINNED_PYTHON as PINNED
from tests.helpers_scripts import ROOT, SYSTEM_PATH, VERSION, run_script, write_program

CHECK_PYTHON = ROOT / "tools" / "package" / "check-python.sh"
MINOR = PINNED.rsplit(".", 1)[0]

DT_NULL, DT_NEEDED, DT_STRTAB, DT_STRSZ = 0, 1, 5, 10
PT_LOAD, PT_DYNAMIC = 1, 2
EHDR_SIZE, PHDR_SIZE = 64, 56


def minimal_elf(*needed: str) -> bytes:
    """An x86-64 ELF shared object whose dynamic section NEEDs ``needed``."""
    strtab, offsets = b"\0", []
    for lib in needed:
        offsets.append(len(strtab))
        strtab += lib.encode() + b"\0"
    strtab_at = EHDR_SIZE + 2 * PHDR_SIZE
    dynamic_at = -(-(strtab_at + len(strtab)) // 8) * 8
    entries = [(DT_NEEDED, off) for off in offsets]
    entries += [(DT_STRTAB, strtab_at), (DT_STRSZ, len(strtab)), (DT_NULL, 0)]
    dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries)
    size = dynamic_at + len(dynamic)
    ident = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)  # 64-bit, little endian, version 1
    header = ident + struct.pack(
        "<HHIQQQIHHHHHH", 3, 62, 1, 0, EHDR_SIZE, 0, 0, EHDR_SIZE, PHDR_SIZE, 2, 64, 0, 0
    )
    load = struct.pack("<IIQQQQQQ", PT_LOAD, 4, 0, 0, 0, size, size, 0x1000)
    dyn = struct.pack(
        "<IIQQQQQQ", PT_DYNAMIC, 6, dynamic_at, dynamic_at, dynamic_at, *[len(dynamic)] * 2, 8
    )
    body = header + load + dyn + strtab
    return body + bytes(dynamic_at - len(body)) + dynamic


def make_tree(root: Path, version: str = PINNED) -> Path:
    """A staged interpreter tree like the build's, minus libpython: a fake ``bin/python3.X``
    printing ``version`` and extension modules that need only libc."""
    write_program(root / "bin" / f"python{MINOR}", f'echo "{version}"\n')
    ext = root / "lib" / f"python{MINOR}" / "site-packages" / "pydantic_core" / "_core.so"
    ext.parent.mkdir(parents=True)
    ext.write_bytes(minimal_elf("libgcc_s.so.1", "libc.so.6"))
    dynload = root / "lib" / f"python{MINOR}" / "lib-dynload" / "_dbm.so"
    dynload.parent.mkdir(parents=True)
    dynload.write_bytes(minimal_elf("libc.so.6"))
    (root / "lib" / f"python{MINOR}" / "os.pyc").write_bytes(b"\xf3\r\r\n" + bytes(12))
    return root


def run_check(tree: Path, version: str = PINNED) -> subprocess.CompletedProcess[str]:
    return run_script(["bash", str(CHECK_PYTHON), str(tree), version], {"PATH": SYSTEM_PATH})


def test_check_python_accepts_the_pinned_version_without_libpython(tmp_path: Path) -> None:
    result = run_check(make_tree(tmp_path / "python"))

    assert result.returncode == 0, result.stderr
    assert f"CPython {PINNED}" in result.stdout
    assert "no libpython" in result.stdout


@pytest.mark.parametrize("staged", [f"{MINOR}.0", f"{PINNED}1", "3.13.9"])
def test_check_python_refuses_any_other_interpreter_version(staged: str, tmp_path: Path) -> None:
    result = run_check(make_tree(tmp_path / "python", version=staged))

    assert result.returncode == 1
    assert f"staged interpreter is CPython {staged}, .python-version pins {PINNED}" in (
        result.stderr
    )


def test_check_python_refuses_an_interpreter_that_does_not_run(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "python")
    write_program(tree / "bin" / f"python{MINOR}", "exit 127\n")

    result = run_check(tree)

    assert result.returncode == 1
    assert "cannot run" in result.stderr


@pytest.mark.parametrize(
    "name", [f"libpython{MINOR}.so.1.0", f"libpython{MINOR}.so", "libpython3.so"]
)
def test_check_python_refuses_a_libpython_left_in_the_tree(name: str, tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "python")
    (tree / "lib" / name).write_bytes(minimal_elf("libc.so.6"))

    result = run_check(tree)

    assert result.returncode == 1
    assert f"  {tree / 'lib' / name}" in result.stderr.splitlines()


def test_check_python_refuses_a_dangling_libpython_symlink(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "python")
    (tree / "lib" / f"libpython{MINOR}.so").symlink_to(f"libpython{MINOR}.so.1.0")

    result = run_check(tree)

    assert result.returncode == 1
    assert f"  {tree / 'lib' / f'libpython{MINOR}.so'}" in result.stderr.splitlines()


@pytest.mark.parametrize("renamed", [False, True], ids=["extension", "any-name"])
def test_check_python_refuses_an_elf_that_needs_libpython(renamed: bool, tmp_path: Path) -> None:
    """Found by the ELF magic, not by the file name."""
    tree = make_tree(tmp_path / "python")
    offender = tree / "lib" / f"python{MINOR}" / ("plugin.data" if renamed else "_embed.so")
    offender.write_bytes(minimal_elf("libc.so.6", f"libpython{MINOR}.so.1.0"))

    result = run_check(tree)

    assert result.returncode == 1
    assert f"  {offender}: libpython{MINOR}.so.1.0" in result.stderr.splitlines()


@pytest.mark.parametrize(
    "args", [[], ["only-one"], ["dir", "3.14"], ["dir", "3.14.7", "extra"]], ids=str
)
def test_check_python_rejects_bad_usage(args: list[str]) -> None:
    result = run_script(["bash", str(CHECK_PYTHON), *args], {"PATH": SYSTEM_PATH})

    assert result.returncode == 2
    assert "usage: check-python.sh" in result.stderr


# ------------------------------------------------------------------ build-binary.sh, faked around

BUILD_BINARY = ROOT / "tools" / "package" / "build-binary.sh"
BUILD_GATES = ["check-no-runtime.sh", "check-python.sh", "check-host-paths.py"]
UV_CANNOT_DOWNLOAD = f"error: No download found for request: cpython-{PINNED}-linux-x86_64-gnu"
NO_NETWORK = "curl: (6) no network in the tests"


def managed_python(tmp_path: Path) -> Path:
    """The fake uv-managed CPython of :func:`fake_build_project`."""
    return tmp_path / "uv-python" / f"cpython-{PINNED}-linux-x86_64-gnu"


def fake_build_project(tmp_path: Path, uv_has_the_pin: bool = False) -> tuple[Path, dict[str, str]]:
    """A project copy with the real build-binary.sh and its gates, and just enough around it
    (locks, dev venv, makeself, readelf, x86_64):

    - uv logs every call to ``uv.log`` and builds a wheel. Unless ``uv_has_the_pin`` it cannot
      install the pinned CPython, like uv 0.12.17 asked for one it has no download for; else it
      installs and finds :func:`managed_python`.
    - that CPython (a stdlib module ``fine.py`` and its sysconfig data) fakes pip, whose wheel
      install writes the dstui launcher, and the import checks of the staged tree. Everything
      else (compileall, the sourceless step, the gates) goes to the interpreter running the
      tests, which is exactly the pinned CPython.
    - curl has no network, and no static zstd is cached: the build stops there.
    """
    project = tmp_path / "project"
    (project / "tools" / "package").mkdir(parents=True)
    for script in [BUILD_BINARY.name, *BUILD_GATES]:
        shutil.copy2(ROOT / "tools" / "package" / script, project / "tools" / "package" / script)
    shutil.copy2(ROOT / ".python-version", project / ".python-version")
    (project / "pyproject.toml").write_text(f'[project]\nname = "dstui"\nversion = "{VERSION}"\n')
    (project / "requirements.txt").write_text("textual==1.0\n")
    (project / "requirements-build.txt").write_text("hatchling==1.0\n")
    write_program(project / ".venv" / "bin" / "python", "echo /nonexistent/dsh\n")
    python = managed_python(tmp_path)
    write_program(
        python / "bin" / f"python{MINOR}",
        f"""here="$(cd "$(dirname "$0")/.." && pwd)"
case "$*" in
    *sys.base_prefix*) echo "$here" ;;
    "-m pip check") echo "No broken requirements found." ;;
    "-m pip install "*.whl) printf '#!%s\\nprint("dstui")\\n' "$0" > "$here/bin/dstui" ;;
    "-m pip install "*) ;;
    *"import dstui, dstui.app"*) ;;
    *) exec "{sys.executable}" "$@" ;;
esac
""",
    )
    stdlib = python / "lib" / f"python{MINOR}"
    (stdlib / "site-packages").mkdir(parents=True)
    (stdlib / "fine.py").write_text("ANSWER = 42\n")
    (stdlib / "_sysconfigdata__linux_x86_64-linux-gnu.py").write_text(
        f"build_time_vars = {{'prefix': '{python}'}}\n"
    )
    bin_dir = tmp_path / "bin"
    write_program(bin_dir / "uname", "echo x86_64\n")
    write_program(bin_dir / "readelf", "exit 0\n")
    write_program(bin_dir / "makeself", "exit 0\n")
    write_program(bin_dir / "curl", f'echo "{NO_NETWORK}" >&2; exit 6\n')
    (bin_dir / "makeself-header.sh").write_text("TMPROOT=\\${TMPDIR:=/tmp}\n")
    wheel = f"dstui-{VERSION}-py3-none-any.whl"
    install, find = (
        ("exit 0", f'echo "{python / "bin" / f"python{MINOR}"}"')
        if uv_has_the_pin
        else (
            f'echo "{UV_CANNOT_DOWNLOAD}" >&2; exit 2',
            'echo "error: No interpreter found for Python $5 in managed installations" >&2; exit 2',
        )
    )
    write_program(
        bin_dir / "uv",
        f'''echo "$*" >> "{tmp_path / "uv.log"}"
case "$1 $2" in
    "--version ") echo "uv 0.0.1" ;;
    "build --wheel") while [ $# -gt 1 ]; do [ "$1" != -o ] || : > "$2/{wheel}"; shift; done ;;
    "python install") {install} ;;
    "python find") {find} ;;
    "python dir") echo "{python.parent}" ;;
esac
''',
    )
    return project, {"PATH": f"{bin_dir}:{SYSTEM_PATH}", "HOME": str(tmp_path / "home")}


def run_build(project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_script(["bash", str(project / "tools" / "package" / "build-binary.sh")], env)


def test_the_build_stops_with_uvs_reason_when_uv_cannot_install_the_pinned_cpython(
    tmp_path: Path,
) -> None:
    """A patch bump of .python-version needs a uv that knows the new CPython, in CI too (the
    setup-uv version in release.yml): the error says so instead of a bare 'No interpreter found'."""
    project, env = fake_build_project(tmp_path)

    result = run_build(project, env)

    assert result.returncode == 1
    assert UV_CANNOT_DOWNLOAD in result.stderr.splitlines()
    assert f"ERROR: uv 0.0.1 cannot install CPython {PINNED} (.python-version)" in result.stderr
    assert ".github/workflows/release.yml" in result.stderr
    calls = (tmp_path / "uv.log").read_text().splitlines()
    assert f"python install {PINNED}" in calls
    assert not [call for call in calls if call.startswith("python find")]


# ------------------------------------------------------ build-binary.sh: precompiling, sourceless


def staged_stdlib(project: Path) -> Path:
    return project / "dist" / ".build" / "python" / "lib" / f"python{MINOR}"


def test_the_build_compiles_every_module_quietly_and_then_drops_the_sources(
    tmp_path: Path,
) -> None:
    """compileall says nothing when every module compiles."""
    project, env = fake_build_project(tmp_path, uv_has_the_pin=True)

    result = run_build(project, env)

    assert result.returncode == 6, result.stderr  # no network for the static zstd, as faked
    assert result.stderr.splitlines()[-1] == NO_NETWORK
    assert "==> Precompiling all modules\n==> Dropping .py sources (sourceless .pyc)\n" in (
        result.stdout
    )
    assert "compil" not in result.stderr.lower()
    stdlib = staged_stdlib(project)
    assert (stdlib / "fine.pyc").is_file()
    assert not list(stdlib.rglob("*.py"))


def test_a_module_that_does_not_compile_fails_the_build_with_the_compiler_error(
    tmp_path: Path,
) -> None:
    """The sourceless step deletes every .py next, so a module that did not compile would just be
    missing from the bundle; the smoke test imports only a few."""
    project, env = fake_build_project(tmp_path, uv_has_the_pin=True)
    (managed_python(tmp_path) / "lib" / f"python{MINOR}" / "broken.py").write_text("def (:\n")

    result = run_build(project, env)

    assert result.returncode == 1
    assert "*** Error compiling" in result.stderr
    assert "broken.py" in result.stderr
    assert "SyntaxError" in result.stderr
    assert "ERROR: a module in the bundle does not compile" in result.stderr
    assert "==> Dropping .py sources" not in result.stdout
    assert (staged_stdlib(project) / "broken.py").is_file()  # stopped before dropping it
