#!/usr/bin/env bash
#
# check-no-runtime.sh — fail if a tree carries the DeepSeek Harness runtime or anything Node.
#
#     bash tools/package/check-no-runtime.sh DIR [DIR ...]
#
# dstui's installer must NOT contain the SDK's embedded runtime
# (deepseek-harness-runtime-bin: a Node single executable + ripgrep sidecar + a
# `dsh` console script): DeepSeek Harness is installed separately. The build runs
# this on the staged bundle and again on the smoke-test install. It flags:
#   - the runtime's module (deepseek_harness_runtime) or its dist-info
#   - a `dsh` executable, `node`, a node_modules tree, Node native addons (*.node)
#   - any file carrying the Node single-executable-application (SEA) fuse,
#     i.e. a Node runtime under any name
# Exit status: 0 clean, 1 with the offending paths on stderr, 2 on bad usage.
set -euo pipefail

[ $# -gt 0 ] || { echo "usage: check-no-runtime.sh DIR [DIR ...]" >&2; exit 2; }

# Every Node SEA binary embeds this sentinel (the fuse flipped when the app blob is injected).
SEA_FUSE="NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2"

offenders=()
for dir in "$@"; do
    [ -d "$dir" ] || { echo "check-no-runtime: not a directory: $dir" >&2; exit 2; }
    while IFS= read -r -d '' path; do
        offenders+=("$path")
    done < <(find "$dir" \( \
            -name 'deepseek_harness_runtime' -o -iname '*runtime_bin*' -o -iname '*runtime-bin*' \
            -o -name dsh -o -name node -o -name node_modules -o -name '*.node' \
        \) -print0 -prune)
    while IFS= read -r -d '' path; do
        offenders+=("$path")
    done < <(grep -rlZaF -- "$SEA_FUSE" "$dir" || true)
done

if [ ${#offenders[@]} -gt 0 ]; then
    echo "ERROR: the DeepSeek Harness runtime or Node.js is in the bundle (dsh is a separate install):" >&2
    printf '  %s\n' "${offenders[@]}" | sort -u >&2
    exit 1
fi
