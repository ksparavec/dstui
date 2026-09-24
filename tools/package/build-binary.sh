#!/usr/bin/env bash
#
# build-binary.sh — produce `dist/dstui-install.sh`, a self-extracting
# **makeself** installer carrying a relocatable, sourceless-precompiled CPython
# (exactly the X.Y.Z in .python-version) with dstui + every runtime dependency.
#
# NOT in the bundle: DeepSeek Harness. The SDK's embedded runtime
# (deepseek-harness-runtime-bin: a 275 MB Node single executable + ripgrep
# sidecar + a `dsh` script) is test-only; dstui runs the separately installed
# `dsh` (npm @deepseek-ai/dsh). requirements.txt leaves it out, every dependency
# installs with --no-deps, and tools/package/check-no-runtime.sh fails the build
# if it — or anything Node — gets in anyway.
#
# The interpreter ships without libpython: python-build-standalone links it statically
# into bin/pythonX.Y, and the shared libpythonX.Y.so (32 MB) is only for embedding.
# tools/package/check-python.sh fails the build unless the staged interpreter is
# exactly the pinned version and no libpython, nor any ELF that needs one, is left.
#
# The heavy tree is zstd-compressed and decompressed at install time by a BUNDLED
# static zstd, so target hosts need neither Python nor zstd. `.py` sources are
# dropped (sourceless `.pyc` only). makeself provides SHA256 integrity. x86-64.
# The payload is owned by root:root with modes u+rwX,go+rX,go-w, and carries no
# build-host path (tools/package/check-host-paths.py).
#
# Build deps: uv, makeself, curl, gcc/make (to build the static zstd once), readelf.
# Run `make lock` first (requirements.txt is installed hash-checked, and
# requirements-build.txt pins the build backend that builds the dstui wheel), and
# `make dev-install` (the smoke test runs a real agent turn with the dev extra's
# embedded runtime standing in for the separately installed dsh).
#
# Never /tmp (a small RAM tmpfs on the build hosts): uv/pip unpacking, makeself's
# archive, the static-zstd build and the smoke-test install all live in one
# private directory under /var/tmp, removed on exit. dist/ holds the staging trees.
set -euo pipefail
export PYTHONNOUSERSITE=1   # hermetic build: never satisfy deps from the user site

APP="dstui"
ARCH="$(uname -m)"
[ "$ARCH" = "x86_64" ] || { echo "ERROR: this build targets x86_64 only (got $ARCH)" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# .python-version pins the exact CPython (X.Y.Z); paths and the launcher use X.Y.
PY_VERSION="$(tr -d '[:space:]' < "$ROOT/.python-version")"
[[ "$PY_VERSION" =~ ^3\.[0-9]+\.[0-9]+$ ]] \
    || { echo "ERROR: .python-version must pin an exact CPython X.Y.Z, got '$PY_VERSION'" >&2; exit 1; }
PY_MINOR="${PY_VERSION%.*}"
PY_NODOT="${PY_MINOR/./}"

DIST="$ROOT/dist"
STAGE="$DIST/.build"
MKDIR="$DIST/.mkself"
STARTUP_IN="$ROOT/tools/package/startup.sh.in"
CHECK_NO_RUNTIME="$ROOT/tools/package/check-no-runtime.sh"
CHECK_PYTHON="$ROOT/tools/package/check-python.sh"
CHECK_HOST_PATHS="$ROOT/tools/package/check-host-paths.py"
OUT="$DIST/$APP-install.sh"
REQ="$ROOT/requirements.txt"
BUILD_REQ="$ROOT/requirements-build.txt"
CACHE="$ROOT/.cache/dstui-build"
ZSTD_VERSION="1.5.6"
ZSTD_SHA256="8c29e06cf42aacc1eafc4077ae2ec6c6fcb96a626157e0593d5e82a34fd403c1"
ZSTD_BIN="$CACHE/zstd-$ZSTD_VERSION-static-$ARCH"   # versioned: a bump never reuses the old one
DEV_PY="$ROOT/.venv/bin/python"   # the dev-install venv (has the test-only embedded runtime)
RUNTIME_DIST="deepseek-harness-runtime-bin"

VERSION="$(grep -m1 -E '^version[[:space:]]*=' pyproject.toml | cut -d'"' -f2)"
[ -n "$VERSION" ] || { echo "ERROR: cannot read version from pyproject.toml" >&2; exit 1; }

command -v makeself >/dev/null || { echo "ERROR: makeself not installed (apt-get install makeself)" >&2; exit 1; }
command -v readelf >/dev/null || { echo "ERROR: readelf not installed (apt-get install binutils)" >&2; exit 1; }
[ -f "$REQ" ] || { echo "ERROR: $REQ missing — run 'make lock' first" >&2; exit 1; }
[ -f "$BUILD_REQ" ] || { echo "ERROR: $BUILD_REQ missing — run 'make lock' first" >&2; exit 1; }

# makeself's own header extracts to ${TMPDIR:=/tmp}. Build from a copy defaulting to
# /var/tmp, so running dstui-install.sh directly never unpacks into /tmp (often a small
# RAM tmpfs) either — install.sh already defaults TMPDIR to /var/tmp.
MAKESELF_HEADER=""
for h in "$(dirname "$(readlink -f "$(command -v makeself)")")/makeself-header.sh" \
         /usr/share/makeself/makeself-header.sh /usr/local/share/makeself/makeself-header.sh; do
    if [ -f "$h" ]; then MAKESELF_HEADER="$h"; break; fi
done
[ -n "$MAKESELF_HEADER" ] || { echo "ERROR: cannot find makeself-header.sh" >&2; exit 1; }
if grep -qiE "^$RUNTIME_DIST==" "$REQ"; then
    echo "ERROR: $RUNTIME_DIST in requirements.txt; dstui must not ship the DeepSeek runtime (run 'make lock')" >&2; exit 1
fi

# The smoke test's stand-in for the separately installed dsh: the dev extra's embedded
# runtime executable. It only drives the smoke test; it is never copied into the bundle.
STANDIN="$("$DEV_PY" -s -c 'from deepseek_harness_runtime import bundled_runtime_path as p; print(p())' 2>/dev/null)" \
    || { echo "ERROR: no test-only embedded runtime in .venv for the smoke test — run 'make dev-install'" >&2; exit 1; }

WORK="$(mktemp -d -p /var/tmp dstui-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
export TMPDIR="$WORK/tmp"
mkdir -p "$TMPDIR"

HEADER="$WORK/makeself-header.sh"
# shellcheck disable=SC2016  # the ${TMPDIR:=...} text is matched and written literally
sed 's|^TMPROOT=\\${TMPDIR:=/tmp}$|TMPROOT=\\${TMPDIR:=/var/tmp}|' "$MAKESELF_HEADER" > "$HEADER"
# shellcheck disable=SC2016
grep -qxF 'TMPROOT=\${TMPDIR:=/var/tmp}' "$HEADER" \
    || { echo "ERROR: $MAKESELF_HEADER: no 'TMPROOT=\${TMPDIR:=/tmp}' line to patch" >&2; exit 1; }

echo "==> $APP $VERSION -> makeself installer (CPython $PY_VERSION, sourceless, zstd -19)"

# --- 1. Clean ------------------------------------------------------------
rm -rf "$STAGE" "$MKDIR" build src/*.egg-info
mkdir -p "$STAGE" "$MKDIR" "$CACHE"

# --- 2. Build the dstui wheel -------------------------------------------
# The build backend (hatchling and its dependencies) comes hash-checked from
# requirements-build.txt, not freshly resolved from the index: its code writes the wheel.
echo "==> Building $APP wheel (hash-pinned build backend)"
uv build --wheel --build-constraints "$BUILD_REQ" --require-hashes -o "$DIST" >/dev/null
WHEEL="$(ls "$DIST"/${APP}-${VERSION}-*.whl)"

# --- 3. Stage a standalone, relocatable CPython -------------------------
# --system --managed-python: a uv-managed (python-build-standalone) interpreter, never
# the project's .venv or a distro python — only the former is relocatable. A uv release
# only knows the CPython patches published before it: a bump of .python-version may need
# a newer uv, locally and in CI (setup-uv's `version` in .github/workflows/release.yml).
if ! uv_out="$(uv python install "$PY_VERSION" 2>&1)"; then
    printf '%s\n' "$uv_out" >&2
    echo "ERROR: $(uv --version) cannot install CPython $PY_VERSION (.python-version): update uv" \
         "(in CI: setup-uv's version in .github/workflows/release.yml) or allow uv's Python downloads" >&2
    exit 1
fi
PYBIN="$(uv python find --system --managed-python "$PY_VERSION")"
BASEP="$(cd "$("$PYBIN" -c 'import sys; print(sys.base_prefix)')" && pwd -P)"
case "$BASEP" in
    "$(cd "$(uv python dir)" && pwd -P)"/*) ;;
    *) echo "ERROR: $BASEP is not a uv-managed CPython" >&2; exit 1 ;;
esac
echo "==> Staging interpreter from $BASEP"
cp -a "$BASEP" "$STAGE/python"
PY="$STAGE/python/bin/python${PY_MINOR}"
rm -f "$STAGE/python/lib/python${PY_MINOR}/EXTERNALLY-MANAGED"

# --- 4. Prune unused stdlib (incl. Tcl/Tk v8 AND v9) --------------------
( cd "$STAGE/python/lib/python${PY_MINOR}" && rm -rf \
    test tkinter turtledemo idlelib lib2to3 ensurepip \
    "config-${PY_MINOR}"-*-linux-gnu 2>/dev/null || true )
# tkinter is gone, so Tcl/Tk is dead weight. The standalone ships v9
# (libtcl9tk9.0.so, tcl9/, tk9/) — match v8 AND v9.
( cd "$STAGE/python/lib" && rm -rf \
    tcl8* tk8* tcl9* tk9* Tix* itcl* tdbc* thread* libtcl* libtk* 2>/dev/null || true )

# --- 4b. Reset site-packages to a clean baseline (keep only pip) --------
( cd "$STAGE/python/lib/python${PY_MINOR}/site-packages" && for d in *; do
    case "$d" in pip|pip-*|__pycache__) ;; *) rm -rf "$d" ;; esac
  done )

# --- 5. Install the locked dependencies, then dstui ---------------------
# --require-hashes: exactly the artifacts pinned in requirements.txt. --no-deps: pip adds
# nothing on its own — in particular not the SDK's declared deepseek-harness-runtime-bin.
# --no-cache-dir: never reuse the build host's pip cache — always fetch the current
# artifacts through the configured index/proxy, so a stale or poisoned cached wheel body
# can't ship. --only-binary: never build an sdist here, whose build dependencies pip would
# fetch without hash checks. The bundle's module versions are then verified against the
# lock in step 12.
echo "==> Installing $APP + dependencies (hash-checked wheels, --no-deps, no cache)"
PIP=("$PY" -m pip install --no-input --disable-pip-version-check --no-warn-script-location
     --no-cache-dir --no-compile --no-deps --only-binary :all:)
"${PIP[@]}" --require-hashes -r "$REQ" >/dev/null
"${PIP[@]}" "$WHEEL" >/dev/null

# --no-deps skipped pip's resolver, so prove the lock is complete: the one requirement
# allowed to stay unmet is the embedded runtime the SDK declares and dstui never ships.
unmet="$("$PY" -m pip check 2>&1 | grep -vE \
    "^(deepseek-harness-sdk [^ ]+ requires $RUNTIME_DIST, which is not installed\.|No broken requirements found\.)$" \
    || true)"
[ -z "$unmet" ] || { echo "ERROR: requirements.txt is incomplete (pip check):" >&2; echo "$unmet" >&2; exit 1; }

# The DeepSeek runtime — or anything Node — must not have got in.
bash "$CHECK_NO_RUNTIME" "$STAGE/python"

SP="$STAGE/python/lib/python${PY_MINOR}/site-packages"
( cd "$SP" && rm -rf pip setuptools wheel pkg_resources _distutils_hack 2>/dev/null || true )
find "$SP" -name direct_url.json -delete 2>/dev/null || true

# --- 5b. Strip dead weight ---------------------------------------------
echo "==> Stripping dead weight (dep CLIs, headers, libpython, dep tests)"
( cd "$STAGE/python/bin" && for f in *; do
    case "$f" in "python${PY_MINOR}"|python3|python|"${APP}") ;; *) rm -f "$f" ;; esac
  done )
rm -rf "$STAGE/python/include" "$STAGE/python/share" "$STAGE/python/lib/pkgconfig"
# libpythonX.Y.so*, libpython3.so and any static libpython: for embedding only. bin/pythonX.Y
# has libpython linked in statically, and no extension module links against it (gated next).
rm -f "$STAGE/python/lib"/libpython*
find "$SP" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

# Exactly the pinned CPython, no libpython left, and no ELF that NEEDs one.
bash "$CHECK_PYTHON" "$STAGE/python" "$PY_VERSION"

# --- 5c. No build-host paths in the payload -----------------------------
# uv rewrote the interpreter's sysconfig data from python-build-standalone's neutral
# /install to its install path in the maintainer's home; pip stamped the console script
# with the staging path. Put /install back (sysconfig derives the real paths from
# sys.prefix at run time) and give the launcher a placeholder shebang, which the
# installer rewrites to the bundled interpreter anyway.
"$PY" -I - "$BASEP" "$STAGE/python/lib/python${PY_MINOR}"/_sysconfigdata_*.py <<'PYEOF'
import sys
base, *files = sys.argv[1:]
for name in files:
    with open(name, encoding="utf-8") as f:
        text = f.read()
    with open(name, "w", encoding="utf-8") as f:
        f.write(text.replace(base, "/install"))
PYEOF
LAUNCHER="$STAGE/python/bin/$APP"
case "$(head -n1 "$LAUNCHER")" in
    '#!'*/python*) ;;
    *) echo "ERROR: unexpected first line in pip's $APP launcher: $(head -n1 "$LAUNCHER")" >&2; exit 1 ;;
esac
sed -i "1s|.*|#!/install/bin/python${PY_MINOR}|" "$LAUNCHER"

# --- 6. Sanity-check the staged interpreter ----------------------------
# NB: do NOT `strip` the PBS ELF files: that corrupted libpython's symbol-version tables, and
# bin/python carries the same (statically linked) code.
"$PY" -c "import sqlite3, ssl, ctypes" \
    || { echo "ERROR: staged interpreter is not functional" >&2; exit 1; }

# --- 7. Precompile EVERYTHING (unchecked-hash, relative paths) ----------
echo "==> Precompiling all modules"
"$PY" -m compileall -q -f -j 0 -s "$STAGE/python" --invalidation-mode unchecked-hash "$STAGE/python" >/dev/null 2>&1 || true

# --- 7b. Drop .py sources: relocate to sourceless .pyc, delete .py ------
echo "==> Dropping .py sources (sourceless .pyc)"
"$PY" -s - "$STAGE/python" "$PY_NODOT" <<'PYEOF'
import os, sys
root, tag = sys.argv[1], sys.argv[2]
suffix = f".cpython-{tag}.pyc"
for dp, _dn, fn in os.walk(root):
    if os.path.basename(dp) != "__pycache__":
        continue
    parent = os.path.dirname(dp)
    for f in fn:
        if f.endswith(suffix):
            os.replace(os.path.join(dp, f), os.path.join(parent, f[:-len(suffix)] + ".pyc"))
for dp, _dn, fn in os.walk(root, topdown=False):
    for f in fn:
        if f.endswith(".py"):
            os.remove(os.path.join(dp, f))
    if os.path.basename(dp) == "__pycache__":
        try:
            os.rmdir(dp)
        except OSError:
            pass
PYEOF

# --- 7c. Sanity on the sourceless tree ---------------------------------
"$PY" -I -c "import dstui, dstui.app, textual, deepseek_harness" \
    || { echo "ERROR: sourceless bundle not importable" >&2; exit 1; }

# The payload must not disclose the build host (account name, directory layout). Files
# verified byte for byte against the RECORD of a wheel pinned in requirements.txt are upstream
# content and exempt: pydantic_core's SBOM names pydantic's CI checkout, /home/runner/work/...,
# which is under $HOME/ on a GitHub runner. Whatever the build or pip wrote stays scanned,
# the dstui wheel built from the checkout included.
leak_patterns=("$BASEP" "$ROOT/")
case "${HOME:-/}" in /) ;; *) leak_patterns+=("$HOME/") ;; esac
"$PY" -I "$CHECK_HOST_PATHS" "$REQ" "$STAGE" "${leak_patterns[@]}"

# --- 8. Obtain a static zstd (cached across builds) --------------------
# Reused only while it is the pinned version and static (no program interpreter);
# otherwise rebuilt from the checksummed source.
zstd_ok() {
    local out
    out="$("$1" --version 2>/dev/null)" || return 1
    case "$out" in *" v$ZSTD_VERSION,"*) ;; *) return 1 ;; esac
    readelf -h "$1" >/dev/null 2>&1 || return 1
    ! readelf -lW "$1" 2>/dev/null | grep -q 'Requesting program interpreter'
}
if ! zstd_ok "$ZSTD_BIN"; then
    echo "==> Building static zstd $ZSTD_VERSION (cached at $ZSTD_BIN)"
    ztmp="$(mktemp -d -p "$WORK")"
    curl -fsSL -o "$ztmp/z.tgz" \
        "https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz"
    echo "$ZSTD_SHA256  $ztmp/z.tgz" | sha256sum -c - >/dev/null \
        || { echo "ERROR: zstd source checksum mismatch" >&2; exit 1; }
    tar -C "$ztmp" -xzf "$ztmp/z.tgz"
    make -C "$ztmp/zstd-${ZSTD_VERSION}/programs" zstd \
        HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0 ZSTD_LEGACY_SUPPORT=0 \
        LDFLAGS=-static -j"$(nproc)" >/dev/null 2>&1
    strip "$ztmp/zstd-${ZSTD_VERSION}/programs/zstd"
    cp "$ztmp/zstd-${ZSTD_VERSION}/programs/zstd" "$ZSTD_BIN"
    rm -rf "$ztmp"
    zstd_ok "$ZSTD_BIN" || { echo "ERROR: $ZSTD_BIN is not a static zstd $ZSTD_VERSION" >&2; exit 1; }
fi
cp "$ZSTD_BIN" "$MKDIR/zstd"
chmod +x "$MKDIR/zstd"

# --- 9. Compress the heavy payload (zstd -19, multithreaded) -----------
mkdir -p "$STAGE/doc"
cp -p README.md LICENSE CHANGELOG.md "$STAGE/doc/" 2>/dev/null || true
echo "==> Compressing payload (zstd -19 -T0)"
# root:root and no group/other write bits: tar run as root on the target restores both,
# and the installer must not hand the tree to whichever local account has the build
# user's uid (see also startup.sh.in).
tar --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
    -C "$STAGE" -cf - python doc | "$MKDIR/zstd" -19 -T0 -q -o "$MKDIR/bundle.tar.zst"
foreign="$("$MKDIR/zstd" -dc "$MKDIR/bundle.tar.zst" | tar --numeric-owner -tvf - \
    | awk '$2 != "0/0" || substr($1, 6, 1) == "w" || substr($1, 9, 1) == "w"')"
[ -z "$foreign" ] || { echo "ERROR: payload entries not root-owned or group/other-writable:" >&2
                       printf '%s\n' "$foreign" | sed 5q >&2; exit 1; }

# --- 10. Generate the makeself startup script (baked-in version/py) ----
{
    printf '%s\n' '#!/bin/sh'
    printf 'DSTUI_VERSION=%s\n' "$VERSION"
    printf 'PYVER=%s\n' "$PY_MINOR"
    cat "$STARTUP_IN"
} > "$MKDIR/startup.sh"
chmod +x "$MKDIR/startup.sh"

# --- 11. Assemble the self-extracting installer with makeself ----------
# --nox11: run the startup script inline (no xterm). --nocomp: the payload is
# already zstd-compressed. --sha256: integrity check on extraction. --header: the
# /var/tmp-defaulting copy made above. `sh ./startup.sh`, not ./startup.sh: makeself's
# temp dir may be mounted noexec (CIS-hardened /var/tmp).
echo "==> Assembling makeself installer"
rm -f "$OUT"
chmod -R u+rwX,go+rX,go-w "$MKDIR"   # like the payload: nobody else may write what root runs
makeself --nox11 --nocomp --sha256 --tar-quietly --header "$HEADER" \
    --tar-extra "--owner=0 --group=0 --numeric-owner" \
    "$MKDIR" "$OUT" "dstui $VERSION installer" sh ./startup.sh >/dev/null
rm -rf "$STAGE" "$MKDIR"
# shellcheck disable=SC2016
[ "$(grep -m1 -a '^TMPROOT=' "$OUT")" = 'TMPROOT=${TMPDIR:=/var/tmp}' ] \
    || { echo "ERROR: $OUT does not default its extraction dir to /var/tmp" >&2; exit 1; }

# --- 12. Smoke test: install to a temp prefix + run --------------------
echo "==> Smoke test (install to a temp prefix under /var/tmp + run)"
TPREFIX="$WORK/prefix"
DSTUI_PREFIX="$TPREFIX" sh "$OUT" >/dev/null
BUNDLE_PY="$TPREFIX/lib/$APP/bin/python${PY_MINOR}"

# Version audit: confirm every installed module matches requirements.txt, at both
# the dist-info metadata AND the imported-code (__version__) level.
echo "==> Verifying bundled module versions against requirements.txt"
"$BUNDLE_PY" -I "$ROOT/tools/package/verify-versions.py" "$REQ" \
    || { echo "ERROR: bundled module versions do not match requirements.txt (stale build?)" >&2; exit 1; }

# What the installer laid down carries no DeepSeek runtime and nothing Node either.
bash "$CHECK_NO_RUNTIME" "$TPREFIX"

# $PREFIX/bin must contain ONLY `dstui`: the bundled interpreter on PATH would shadow
# the host's own python$PY_MINOR (see startup.sh.in).
onpath="$(ls "$TPREFIX/bin")"
if [ "$onpath" != "$APP" ]; then
    echo "ERROR: \$PREFIX/bin must expose only '$APP', got:" >&2
    printf '  %s\n' $onpath >&2
    exit 1
fi

# Capture outputs (do NOT pipe into grep -q: under `set -o pipefail` its early exit
# SIGPIPEs the producer and fails the pipeline).
help_out="$("$TPREFIX/bin/$APP" --help)" || { echo "ERROR: '$APP --help' failed" >&2; exit 1; }
case "$help_out" in
    "usage: $APP"*--dsh-bin*) ;;
    *) echo "ERROR: '$APP --help' did not render the usage" >&2; exit 1 ;;
esac
version_out="$("$TPREFIX/bin/$APP" --version)"
[ "$version_out" = "$APP $VERSION" ] \
    || { echo "ERROR: '$APP --version' printed '$version_out', want '$APP $VERSION'" >&2; exit 1; }

# Hermetic (-I): a host PYTHONPATH with its own pydantic, a module in the cwd reached
# through an empty PYTHONPATH entry, and a stray PYTHONHOME must all be ignored.
HOSTILE="$WORK/hostile"
mkdir -p "$HOSTILE/pydantic"
echo 'raise ImportError("a host pydantic shadowed the bundled one")' > "$HOSTILE/pydantic/__init__.py"
echo 'raise ImportError("a module from the cwd was imported")' > "$HOSTILE/textual.py"
hostile_out="$(cd "$HOSTILE" && PYTHONPATH="$HOSTILE:" PYTHONHOME=/nonexistent \
    "$TPREFIX/bin/$APP" --version 2>&1)" \
    || { echo "ERROR: '$APP --version' fails with PYTHONPATH/PYTHONHOME set: $hostile_out" >&2; exit 1; }
[ "$hostile_out" = "$APP $VERSION" ] \
    || { echo "ERROR: with PYTHONPATH/PYTHONHOME set '$APP --version' printed '$hostile_out'" >&2; exit 1; }

# Usable by every user, writable only by the installing one.
bad_modes="$(find "$TPREFIX" ! -type l \( -perm /022 -o ! -perm -004 -o \( -type d ! -perm -005 \) \))"
[ -z "$bad_modes" ] || { echo "ERROR: installed files with wrong modes:" >&2
                         printf '%s\n' "$bad_modes" | sed 5q >&2; exit 1; }

# No dsh anywhere (empty PATH, and the bundle has no embedded runtime): a clear error and
# exit 1 before the TUI starts or anything is created.
mkdir -p "$WORK/no-dsh-bin" "$WORK/no-dsh-home" "$WORK/ws"
nodsh_err="$(env -i HOME="$WORK/no-dsh-home" PATH="$WORK/no-dsh-bin" \
    "$TPREFIX/bin/$APP" -w "$WORK/ws" 2>&1 >/dev/null)" && nodsh_rc=0 || nodsh_rc=$?
case "$nodsh_rc:$nodsh_err" in
    "1:dstui: DeepSeek Harness (dsh) not found:"*) ;;
    *) echo "ERROR: without dsh '$APP' must exit 1 with the not-found message; got $nodsh_rc: $nodsh_err" >&2
       exit 1 ;;
esac
[ -z "$(ls -A "$WORK/no-dsh-home")" ] \
    || { echo "ERROR: '$APP' without dsh created files in \$HOME" >&2; exit 1; }

# End to end, run by the BUNDLED interpreter from an on-disk probe file: dstui's own
# config + AgentBridge complete one real agent turn against tests/fake_deepseek.py
# (stdlib only, loaded from the source tree), with the stand-in as --dsh-bin; then the
# sourceless Textual app mounts headless.
PROBE="$WORK/bundle-probe.py"
cat > "$PROBE" <<'PYEOF'
import asyncio
import dataclasses
import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

fake_path, standin, scratch = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("fake_deepseek", fake_path)
fake_deepseek = importlib.util.module_from_spec(spec)
sys.modules["fake_deepseek"] = fake_deepseek
spec.loader.exec_module(fake_deepseek)

from dstui.app import DsTuiApp
from dstui.bridge import AgentBridge, TurnOutcome
from dstui.config import build_harness_config, parse_args
from dstui.events import AssistantText

assert importlib.util.find_spec("deepseek_harness_runtime") is None, "embedded runtime bundled"

REPLY = "Hello from the fake DeepSeek API."
workspace, data = Path(scratch, "ws"), Path(scratch, "data")
workspace.mkdir(parents=True)
argv = ["-w", str(workspace), "--data-dir", str(data), "--profile", "sdk-minimal",
        "--dsh-bin", standin]
settings = parse_args(argv, env={})


def runtime_pids():
    marker = f"DSH_HOME={data}".encode()
    pids = []
    for entry in Path("/proc").iterdir():
        try:
            environ = (entry / "environ").read_bytes() if entry.name.isdigit() else b""
        except OSError:
            continue
        if any(item.startswith(marker) for item in environ.split(b"\0")):
            pids.append(int(entry.name))
    return pids


with fake_deepseek.FakeDeepSeek() as fake:
    fake.enqueue(fake_deepseek.text_reply(REPLY))
    config = dataclasses.replace(
        build_harness_config(settings), api_key="sk-fake", base_url=fake.url
    )
    bridge = AgentBridge(config)
    events = []
    try:
        outcome = bridge.send("hello", events.append)
    finally:
        bridge.close()
assert outcome == TurnOutcome("completed"), outcome
assert AssistantText(REPLY) in events, events

deadline = time.monotonic() + 5.0  # the runtime's children exit asynchronously
while (left := runtime_pids()) and time.monotonic() < deadline:
    time.sleep(0.05)
for pid in left:
    os.kill(pid, signal.SIGKILL)
assert not left, f"runtime processes left running: {left}"


class NoAgent:
    def start(self): pass
    def send(self, text, on_event): return TurnOutcome("completed")
    def cancel(self): pass
    def new_conversation(self): pass
    def close(self): pass


async def mount():
    app = DsTuiApp(NoAgent(), settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt")


asyncio.run(mount())
print("probe ok")
PYEOF
if ! "$BUNDLE_PY" -I "$PROBE" "$ROOT/tests/fake_deepseek.py" "$STANDIN" "$WORK/probe" >/dev/null; then
    echo "ERROR: bundle probe failed (agent turn via --dsh-bin, runtime cleanup, or TUI mount)" >&2
    exit 1
fi
echo "    ok (versions, no runtime/Node, bin=dstui, --help, --version, hermetic, modes, no-dsh exit 1, agent turn, TUI mount)"

# --- 13. Report --------------------------------------------------------
SIZE="$(du -h --apparent-size "$OUT" | cut -f1)"   # not the blocks XFS preallocated
echo ""
echo "Built installer:"
echo "  $OUT  ($SIZE)"
echo ""
echo "Install on any linux-x86_64 glibc host (no Python, no zstd required):"
echo "  sh ./dstui-install.sh                                # -> ~/.local"
echo "  sh ./dstui-install.sh -- --prefix DIR                # or DSTUI_PREFIX=DIR"
echo "  sudo DSTUI_PREFIX=/usr/local sh ./dstui-install.sh   # system install"
echo "  dstui --help"
echo "Requires DeepSeek Harness, installed separately: npm install -g @deepseek-ai/dsh (Node >= 22.19)"
