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
#   DSTUI_VERIFY   1: before running the downloaded installer, verify its GitHub
#                  artifact attestation (the release workflow built it from this
#                  repository) with `gh attestation verify`. Needs the GitHub CLI
#                  (gh) on PATH, logged in. Any failure, or no gh, stops here: the
#                  installer is not run. Default 0 (no check).
#
# Custom prefix, or verification, with the pipe form:
#   curl -fsSL .../install.sh | DSTUI_PREFIX=/usr/local sh
#   curl -fsSL .../install.sh | DSTUI_VERIFY=1 sh
set -eu

REPO="ksparavec/dstui"
ASSET="dstui-install.sh"
VERSION="${DSTUI_VERSION:-latest}"
VERIFY="${DSTUI_VERIFY:-0}"
TMPDIR="${TMPDIR:-/var/tmp}"
export TMPDIR   # the makeself installer unpacks its payload under $TMPDIR too

# Fail closed: a verification that was asked for is never skipped quietly.
case "$VERIFY" in
    0|1) ;;
    *) echo "dstui: DSTUI_VERIFY must be 1 or 0, got '${VERIFY}'" >&2; exit 1 ;;
esac
if [ "$VERIFY" = 1 ] && ! command -v gh >/dev/null 2>&1; then
    echo "dstui: DSTUI_VERIFY=1 needs the GitHub CLI (gh) on PATH: https://cli.github.com" >&2
    exit 1
fi

# The bundle is a linux-x86_64 build; fail fast anywhere else.
OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" != "Linux" ] || [ "$ARCH" != "x86_64" ]; then
    echo "dstui: unsupported platform ${OS}/${ARCH}; only Linux x86_64 is supported" >&2
    exit 1
fi

# HTTPS only, redirects included (GitHub redirects release downloads to its CDN).
if command -v curl >/dev/null 2>&1; then
    dl() { curl --proto '=https' --tlsv1.2 -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    dl() { wget --https-only -qO "$2" "$1"; }
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

if [ "$VERIFY" = 1 ]; then
    echo "dstui: verifying the build provenance attestation (gh attestation verify)" >&2
    if ! gh attestation verify "$TMP" --repo "$REPO" >&2; then
        echo "dstui: attestation verification failed; the installer was not run" >&2
        exit 1
    fi
fi

echo "dstui: running installer" >&2
# </dev/null: the makeself installer is non-interactive; detach it from the
# (already-consumed) pipe stdin used when this bootstrap is run via `curl | sh`.
sh "$TMP" </dev/null
