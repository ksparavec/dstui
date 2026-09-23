#!/usr/bin/env bash
#
# build-binary.sh — produce `dist/dstui-install.sh`, a self-extracting
# **makeself** installer carrying a relocatable, sourceless-precompiled CPython
# 3.x with dstui + every runtime dependency.
#
# NOT in the bundle: DeepSeek Harness. The SDK's embedded runtime
# (deepseek-harness-runtime-bin: a 275 MB Node single executable + ripgrep
# sidecar + a `dsh` script) is test-only; dstui runs the separately installed
# `dsh` (npm @deepseek-ai/dsh). requirements.txt leaves it out, every dependency
# installs with --no-deps, and tools/package/check-no-runtime.sh fails the build
# if it — or anything Node — gets in anyway.
#
# The heavy tree is zstd-compressed and decompressed at install time by a BUNDLED
# static zstd, so target hosts need neither Python nor zstd. `.py` sources are
# dropped (sourceless `.pyc` only). makeself provides SHA256 integrity. x86-64.
#
# Build deps: uv, makeself, curl, gcc/make (to build the static zstd once).
# Run `make lock` first (requirements.txt is installed hash-checked), and
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

PY_VERSION="$(tr -d '[:space:]' < "$ROOT/.python-version")"
[ -n "$PY_VERSION" ] || { echo "ERROR: cannot read .python-version" >&2; exit 1; }
PY_NODOT="${PY_VERSION/./}"

DIST="$ROOT/dist"
STAGE="$DIST/.build"
MKDIR="$DIST/.mkself"
STARTUP_IN="$ROOT/tools/package/startup.sh.in"
CHECK_NO_RUNTIME="$ROOT/tools/package/check-no-runtime.sh"
OUT="$DIST/$APP-install.sh"
REQ="$ROOT/requirements.txt"
CACHE="$ROOT/.cache/dstui-build"
ZSTD_BIN="$CACHE/zstd-static-$ARCH"
ZSTD_VERSION="1.5.6"
ZSTD_SHA256="8c29e06cf42aacc1eafc4077ae2ec6c6fcb96a626157e0593d5e82a34fd403c1"
DEV_PY="$ROOT/.venv/bin/python"   # the dev-install venv (has the test-only embedded runtime)
RUNTIME_DIST="deepseek-harness-runtime-bin"

VERSION="$(grep -m1 -E '^version[[:space:]]*=' pyproject.toml | cut -d'"' -f2)"
[ -n "$VERSION" ] || { echo "ERROR: cannot read version from pyproject.toml" >&2; exit 1; }

command -v makeself >/dev/null || { echo "ERROR: makeself not installed (apt-get install makeself)" >&2; exit 1; }
[ -f "$REQ" ] || { echo "ERROR: $REQ missing — run 'make lock' first" >&2; exit 1; }
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

echo "==> $APP $VERSION -> makeself installer (CPython $PY_VERSION, sourceless, zstd -19)"

# --- 1. Clean ------------------------------------------------------------
rm -rf "$STAGE" "$MKDIR" build src/*.egg-info
mkdir -p "$STAGE" "$MKDIR" "$CACHE"

# --- 2. Build the dstui wheel -------------------------------------------
echo "==> Building $APP wheel"
uv build --wheel -o "$DIST" >/dev/null
WHEEL="$(ls "$DIST"/${APP}-${VERSION}-*.whl)"

# --- 3. Stage a standalone, relocatable CPython -------------------------
# --system --managed-python: a uv-managed (python-build-standalone) interpreter, never
# the project's .venv or a distro python — only the former is relocatable.
uv python install "$PY_VERSION" >/dev/null 2>&1 || true
PYBIN="$(uv python find --system --managed-python "$PY_VERSION")"
BASEP="$(cd "$("$PYBIN" -c 'import sys; print(sys.base_prefix)')" && pwd -P)"
case "$BASEP" in
    "$(cd "$(uv python dir)" && pwd -P)"/*) ;;
    *) echo "ERROR: $BASEP is not a uv-managed CPython" >&2; exit 1 ;;
esac
echo "==> Staging interpreter from $BASEP"
cp -a "$BASEP" "$STAGE/python"
PY="$STAGE/python/bin/python${PY_VERSION}"
rm -f "$STAGE/python/lib/python${PY_VERSION}/EXTERNALLY-MANAGED"

# --- 4. Prune unused stdlib (incl. Tcl/Tk v8 AND v9) --------------------
( cd "$STAGE/python/lib/python${PY_VERSION}" && rm -rf \
    test tkinter turtledemo idlelib lib2to3 ensurepip \
    config-${PY_VERSION}-*-linux-gnu 2>/dev/null || true )
# tkinter is gone, so Tcl/Tk is dead weight. The standalone ships v9
# (libtcl9tk9.0.so, tcl9/, tk9/) — match v8 AND v9.
( cd "$STAGE/python/lib" && rm -rf \
    tcl8* tk8* tcl9* tk9* Tix* itcl* tdbc* thread* libtcl* libtk* 2>/dev/null || true )

# --- 4b. Reset site-packages to a clean baseline (keep only pip) --------
( cd "$STAGE/python/lib/python${PY_VERSION}/site-packages" && for d in *; do
    case "$d" in pip|pip-*|__pycache__) ;; *) rm -rf "$d" ;; esac
  done )

# --- 5. Install the locked dependencies, then dstui ---------------------
# --require-hashes: exactly the artifacts pinned in requirements.txt. --no-deps: pip adds
# nothing on its own — in particular not the SDK's declared deepseek-harness-runtime-bin.
# --no-cache-dir: never reuse the build host's pip cache — always fetch the current
# artifacts through the configured index/proxy, so a stale or poisoned cached wheel body
# can't ship. The bundle's module versions are then verified against the lock in step 12.
echo "==> Installing $APP + dependencies (hash-checked, --no-deps, no cache)"
PIP=("$PY" -m pip install --no-input --disable-pip-version-check --no-warn-script-location
     --no-cache-dir --no-compile --no-deps)
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

SP="$STAGE/python/lib/python${PY_VERSION}/site-packages"
( cd "$SP" && rm -rf pip setuptools wheel pkg_resources _distutils_hack 2>/dev/null || true )
find "$SP" -name direct_url.json -delete 2>/dev/null || true

# --- 5b. Strip dead weight ---------------------------------------------
echo "==> Stripping dead weight (dep CLIs, headers, dep tests)"
( cd "$STAGE/python/bin" && for f in *; do
    case "$f" in python${PY_VERSION}|python3|python|${APP}) ;; *) rm -f "$f" ;; esac
  done )
rm -rf "$STAGE/python/include" "$STAGE/python/share"
rm -f "$STAGE/python/lib"/libpython*.a
find "$SP" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

# --- 6. Sanity-check the staged interpreter ----------------------------
# NB: do NOT `strip` libpython — it corrupts PBS symbol-version tables.
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
"$PY" -s -c "import dstui, dstui.app, textual, deepseek_harness" \
    || { echo "ERROR: sourceless bundle not importable" >&2; exit 1; }

# --- 8. Obtain a static zstd (cached across builds) --------------------
if ! "$ZSTD_BIN" --version >/dev/null 2>&1; then
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
fi
cp "$ZSTD_BIN" "$MKDIR/zstd"
chmod +x "$MKDIR/zstd"

# --- 9. Compress the heavy payload (zstd -19, multithreaded) -----------
mkdir -p "$STAGE/doc"
cp -p README.md LICENSE CHANGELOG.md "$STAGE/doc/" 2>/dev/null || true
echo "==> Compressing payload (zstd -19 -T0)"
tar -C "$STAGE" -cf - python doc | "$MKDIR/zstd" -19 -T0 -q -o "$MKDIR/bundle.tar.zst"

# --- 10. Generate the makeself startup script (baked-in version/py) ----
{
    printf '%s\n' '#!/bin/sh'
    printf 'DSTUI_VERSION=%s\n' "$VERSION"
    printf 'PYVER=%s\n' "$PY_VERSION"
    cat "$STARTUP_IN"
} > "$MKDIR/startup.sh"
chmod +x "$MKDIR/startup.sh"

# --- 11. Assemble the self-extracting installer with makeself ----------
# --nox11: run the startup script inline (no xterm). --nocomp: the payload is
# already zstd-compressed. --sha256: integrity check on extraction.
echo "==> Assembling makeself installer"
rm -f "$OUT"
makeself --nox11 --nocomp --sha256 --tar-quietly \
    "$MKDIR" "$OUT" "dstui $VERSION installer" ./startup.sh >/dev/null
rm -rf "$STAGE" "$MKDIR"

# --- 12. Smoke test: install to a temp prefix + run --------------------
echo "==> Smoke test (install to a temp prefix under /var/tmp + run)"
TPREFIX="$WORK/prefix"
DSTUI_PREFIX="$TPREFIX" sh "$OUT" >/dev/null
BUNDLE_PY="$TPREFIX/lib/$APP/bin/python${PY_VERSION}"

# Version audit: confirm every installed module matches requirements.txt, at both
# the dist-info metadata AND the imported-code (__version__) level.
echo "==> Verifying bundled module versions against requirements.txt"
"$BUNDLE_PY" -s "$ROOT/tools/package/verify-versions.py" "$REQ" \
    || { echo "ERROR: bundled module versions do not match requirements.txt (stale build?)" >&2; exit 1; }

# What the installer laid down carries no DeepSeek runtime and nothing Node either.
bash "$CHECK_NO_RUNTIME" "$TPREFIX"

# $PREFIX/bin must contain ONLY `dstui`: the bundled interpreter on PATH would shadow
# the host's own python$PY_VERSION (see startup.sh.in).
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
if ! "$BUNDLE_PY" -s "$PROBE" "$ROOT/tests/fake_deepseek.py" "$STANDIN" "$WORK/probe" >/dev/null; then
    echo "ERROR: bundle probe failed (agent turn via --dsh-bin, runtime cleanup, or TUI mount)" >&2
    exit 1
fi
echo "    ok (versions, no runtime/Node, bin=dstui, --help, --version, no-dsh exit 1, agent turn, TUI mount)"

# --- 13. Report --------------------------------------------------------
SIZE="$(du -h "$OUT" | cut -f1)"
echo ""
echo "Built installer:"
echo "  $OUT  ($SIZE)"
echo ""
echo "Install on any linux-x86_64 host (no Python, no zstd required):"
echo "  ./dstui-install.sh                           # -> ~/.local"
echo "  DSTUI_PREFIX=/usr/local ./dstui-install.sh   # system install"
echo "  dstui --help"
echo "Requires DeepSeek Harness, installed separately: npm install -g @deepseek-ai/dsh (Node >= 22.19)"
