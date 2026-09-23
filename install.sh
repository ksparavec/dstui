#!/bin/sh
# dstui bootstrap installer.
#
# The shipped installer (dstui-install.sh) is a makeself self-extracting
# archive: it finds its embedded payload by seeking within its own file, so it
# CANNOT be piped straight into a shell. This tiny bootstrap downloads it to a
# temp file and runs it — which is what makes the one-liner work:
#
#     curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | sh
#
# dstui needs DeepSeek Harness (`dsh`), which is NOT part of dstui and is not
# installed by this script. Install it separately (Node.js >= 22.19):
#
#     npm install -g @deepseek-ai/dsh
#
# Environment:
#   DSTUI_PREFIX   install prefix (default ~/.local); honored by the installer
#   DSTUI_VERSION  release tag to install (default: latest), e.g. v0.1.0
#   TMPDIR         where the installer is downloaded and unpacked (default /var/tmp,
#                  never /tmp: that is often a small RAM-backed tmpfs)
#
# Custom prefix with the pipe form:
#   curl -fsSL .../install.sh | DSTUI_PREFIX=/usr/local sh
set -eu

REPO="ksparavec/dstui"
ASSET="dstui-install.sh"
VERSION="${DSTUI_VERSION:-latest}"
TMPDIR="${TMPDIR:-/var/tmp}"
export TMPDIR   # the makeself installer unpacks its payload under $TMPDIR too

# The bundle is a linux-x86_64 build; fail fast anywhere else.
OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" != "Linux" ] || [ "$ARCH" != "x86_64" ]; then
    echo "dstui: unsupported platform ${OS}/${ARCH}; only Linux x86_64 is supported" >&2
    exit 1
fi

if command -v curl >/dev/null 2>&1; then
    dl() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    dl() { wget -qO "$2" "$1"; }
else
    echo "dstui: need curl or wget to download the installer" >&2
    exit 1
fi

if [ "$VERSION" = "latest" ]; then
    URL="https://github.com/${REPO}/releases/latest/download/${ASSET}"
else
    URL="https://github.com/${REPO}/releases/download/${VERSION}/${ASSET}"
fi

TMP="$(mktemp -p "$TMPDIR" dstui-install.XXXXXX)"
trap 'rm -f "$TMP"' EXIT INT TERM

echo "dstui: downloading ${URL}" >&2
if ! dl "$URL" "$TMP"; then
    echo "dstui: download failed (${URL})" >&2
    exit 1
fi

echo "dstui: running installer" >&2
# </dev/null: the makeself installer is non-interactive; detach it from the
# (already-consumed) pipe stdin used when this bootstrap is run via `curl | sh`.
sh "$TMP" </dev/null
