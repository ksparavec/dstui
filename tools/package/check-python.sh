#!/usr/bin/env bash
#
# check-python.sh — gate the staged interpreter tree: exactly the pinned CPython, no libpython.
#
#     bash tools/package/check-python.sh PYTHON_DIR X.Y.Z
#
# PYTHON_DIR is a staged python-build-standalone tree (bin/pythonX.Y, lib/...), X.Y.Z the
# version .python-version pins. It fails if
#   - bin/pythonX.Y does not run, or reports any version but X.Y.Z
#   - any libpython* is left in the tree (file or symlink)
#   - any ELF file in the tree NEEDs a libpython (readelf -d)
# python-build-standalone links libpython statically into bin/pythonX.Y; the shared
# libpythonX.Y.so (and the libpython3.so stable-ABI shim on top of it) are only for
# programs that embed Python, so the build drops them. This gate proves that nothing
# shipped needs them.
# Exit status: 0 clean, 1 with the problems on stderr, 2 on bad usage.
set -euo pipefail
export LC_ALL=C   # byte-wise `read -N` for the ELF magic

usage() { echo "usage: check-python.sh PYTHON_DIR X.Y.Z" >&2; exit 2; }
[ $# -eq 2 ] || usage
DIR="$1" VERSION="$2"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
[ -d "$DIR" ] || { echo "check-python: not a directory: $DIR" >&2; exit 2; }
command -v readelf >/dev/null || { echo "ERROR: readelf not installed (apt-get install binutils)" >&2; exit 1; }
MINOR="${VERSION%.*}"

# --- the exact pinned version ---------------------------------------------
PY="$DIR/bin/python$MINOR"
staged="$("$PY" -I -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
    || { echo "ERROR: the staged interpreter $PY cannot run" >&2; exit 1; }
[ "$staged" = "$VERSION" ] || {
    echo "ERROR: the staged interpreter is CPython $staged, .python-version pins $VERSION" >&2
    exit 1
}

# --- no libpython, and nothing that needs one ------------------------------
is_elf() {
    local magic=""
    IFS= read -r -N 4 -d '' magic < "$1" 2>/dev/null || true
    [ "$magic" = $'\177ELF' ]
}

offenders=()
while IFS= read -r -d '' path; do
    offenders+=("$path")
done < <(find "$DIR" -name 'libpython*' -print0)
while IFS= read -r -d '' path; do
    is_elf "$path" || continue
    dynamic="$(readelf -dW "$path" 2>&1)" \
        || { offenders+=("$path: readelf failed: ${dynamic%%$'\n'*}"); continue; }
    needed="$(printf '%s\n' "$dynamic" | sed -n 's/.*(NEEDED).*\[\(.*libpython[^]]*\)\].*/\1/p')"
    [ -z "$needed" ] || offenders+=("$path: ${needed//$'\n'/, }")
done < <(find "$DIR" -type f -print0)

if [ ${#offenders[@]} -gt 0 ]; then
    echo "ERROR: libpython in the bundle, or an ELF that needs it:" >&2
    printf '  %s\n' "${offenders[@]}" >&2
    exit 1
fi
echo "    ok: CPython $VERSION, no libpython and no ELF that needs one"
