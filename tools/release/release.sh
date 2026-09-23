#!/usr/bin/env bash
#
# release.sh — cut a release of dstui.
#
# Flow: guard the repo state -> promote the CHANGELOG's [Unreleased] section to
# the current version -> rebuild the self-contained installer (which bundles the
# freshly-promoted CHANGELOG) -> commit -> annotated tag -> push -> publish a
# GitHub release with `dist/dstui-install.sh` as an asset.
#
# The version is read from pyproject.toml (the single source of truth, same as
# tools/package/build-binary.sh). Release the *current* version; bump the version
# and fill in CHANGELOG entries before running this.
#
# Guards (all must pass before anything mutates): on `main`, clean working tree
# (untracked files and assume-unchanged / skip-worktree entries included),
# local == origin/main, and neither the tag nor the GitHub release exists yet.
#
# Non-interactive use (CI): set DSTUI_RELEASE_ASSUME_YES=1 to skip the prompt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BRANCH="main"
CHANGELOG="CHANGELOG.md"
INSTALLER="dist/dstui-install.sh"
# The uv-style `curl … | install.sh | sh` bootstrap; shipped as a release asset
# so `…/releases/latest/download/install.sh` resolves.
BOOTSTRAP="install.sh"

# `|| true` keeps a no-match grep (exit 1) from tripping `set -e`/pipefail before
# the explicit emptiness check below can emit a friendly diagnostic.
VERSION="$(grep -m1 -E '^version[[:space:]]*=' pyproject.toml | cut -d'"' -f2 || true)"
[ -n "$VERSION" ] || { echo "ERROR: cannot read version from pyproject.toml" >&2; exit 1; }
TAG="v$VERSION"

# --- cleanup / abort safety --------------------------------------------------
# Before the release commit lands, any failure should leave the tree pristine:
# revert the CHANGELOG promotion and remove the awk-rewrite temp file.
CHANGELOG_PROMOTED=0
COMMITTED=0
NOTES_FILE=""
cleanup() {
    rm -f "$CHANGELOG.tmp"
    if [ "$CHANGELOG_PROMOTED" = "1" ] && [ "$COMMITTED" != "1" ]; then
        # Restore from HEAD (not the index) so the promotion is reverted even
        # after it was `git add`ed; `git checkout HEAD --` also unstages it.
        git checkout HEAD -- "$CHANGELOG" 2>/dev/null || true
        echo "==> Reverted $CHANGELOG (release aborted before commit)" >&2
    fi
    return 0
}
trap cleanup EXIT

# --- 0. Tooling --------------------------------------------------------------
command -v gh  >/dev/null || { echo "ERROR: gh CLI not installed" >&2; exit 1; }
command -v git >/dev/null || { echo "ERROR: git not installed" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh is not authenticated (run: gh auth login)" >&2; exit 1; }

# --- 1. Guards ---------------------------------------------------------------
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" = "$BRANCH" ] \
    || { echo "ERROR: not on '$BRANCH' (currently on '$CURRENT_BRANCH')" >&2; exit 1; }

# --untracked-files=all overrides a status.showUntrackedFiles=no config: the wheel would
# pick up an untracked module under src/ that is not in the tagged commit.
[ -z "$(git status --porcelain --untracked-files=all)" ] \
    || { echo "ERROR: working tree is not clean; commit or stash first" >&2; exit 1; }
# git status cannot see edits to files marked assume-unchanged (lower-case tag in
# `git ls-files -v`) or skip-worktree (S).
HIDDEN="$(git ls-files -v | grep -E '^([a-z]|S) ' || true)"
[ -z "$HIDDEN" ] || {
    echo "ERROR: files hidden from git status (assume-unchanged / skip-worktree):" >&2
    printf '%s\n' "$HIDDEN" | cut -c3- | sed 's/^/  /' >&2
    echo "       clear with: git update-index --no-assume-unchanged --no-skip-worktree FILE" >&2
    exit 1
}

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "ERROR: tag $TAG already exists locally" >&2; exit 1
fi
if git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag $TAG already exists on origin" >&2; exit 1
fi
if gh release view "$TAG" >/dev/null 2>&1; then
    echo "ERROR: a GitHub release for $TAG already exists" >&2; exit 1
fi

echo "==> Fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse "origin/$BRANCH")"
[ "$LOCAL_HEAD" = "$REMOTE_HEAD" ] \
    || { echo "ERROR: local $BRANCH differs from origin/$BRANCH; push or pull first" >&2; exit 1; }

# --- 2. Extract + verify the release notes from [Unreleased] -----------------
# Everything between '## [Unreleased]' and the next '## [' header, with leading
# and trailing blank lines trimmed.
NOTES="$(awk '
    /^## \[Unreleased\]/ { grab=1; next }
    /^## \[/ && grab     { exit }
    grab                 { buf = buf $0 ORS }
    END { sub(/^\n+/, "", buf); sub(/\n+$/, "", buf); printf "%s", buf }
' "$CHANGELOG")"
[ -n "${NOTES//[[:space:]]/}" ] \
    || { echo "ERROR: the '## [Unreleased]' section in $CHANGELOG is empty; nothing to release" >&2; exit 1; }

# --- 3. Confirm --------------------------------------------------------------
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo '?')"
cat <<EOF

About to release:
  repo    : $REPO
  version : $VERSION
  tag     : $TAG
  branch  : $BRANCH  ($(git rev-parse --short HEAD))
  asset   : $INSTALLER  (rebuilt fresh)

This promotes the CHANGELOG, rebuilds the installer, commits, tags, pushes, and
publishes a GitHub release. It is outward-facing and hard to undo.
EOF
if [ "${DSTUI_RELEASE_ASSUME_YES:-0}" != "1" ]; then
    if [ -t 0 ]; then
        printf 'Continue? [y/N] '; read -r reply
    # The /dev/tty node always exists; without a controlling terminal opening it
    # fails (and set -e would abort without a word), so probe it instead.
    elif { : > /dev/tty; } 2>/dev/null; then
        printf 'Continue? [y/N] ' > /dev/tty; read -r reply < /dev/tty
    else
        echo "ERROR: not a TTY; set DSTUI_RELEASE_ASSUME_YES=1 to proceed non-interactively" >&2
        exit 1
    fi
    case "$reply" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

# --- 4. Promote the CHANGELOG ------------------------------------------------
# Keep a fresh empty '## [Unreleased]', move its content under '## [VERSION] - DATE'.
RELEASE_DATE="$(date +%F)"
awk -v ver="$VERSION" -v date="$RELEASE_DATE" '
    !done && /^## \[Unreleased\]/ {
        print "## [Unreleased]"; print ""; print "## [" ver "] - " date
        done=1; next
    }
    { print }
' "$CHANGELOG" > "$CHANGELOG.tmp"
mv "$CHANGELOG.tmp" "$CHANGELOG"
CHANGELOG_PROMOTED=1
echo "==> Promoted $CHANGELOG: [Unreleased] -> [$VERSION] - $RELEASE_DATE"

# --- 5. Rebuild the installer (bundles the promoted CHANGELOG) ----------------
echo "==> Rebuilding installer"
bash tools/package/build-binary.sh
[ -f "$INSTALLER" ] || { echo "ERROR: installer not produced at $INSTALLER" >&2; exit 1; }
[ -f "$BOOTSTRAP" ] || { echo "ERROR: bootstrap $BOOTSTRAP missing from repo root" >&2; exit 1; }

# --- 6. Commit + annotated tag (local only) ----------------------------------
# Nothing is on the remote yet: the recovery for a failure here is to undo the
# local commit (which also restores the promoted CHANGELOG) and start over.
git add "$CHANGELOG"
git commit -m "chore: release $TAG" >/dev/null
COMMITTED=1
if ! git tag -a "$TAG" -m "dstui $TAG"; then
    echo "ERROR: creating tag $TAG failed. The release commit is local-only (not pushed)." >&2
    echo "       Undo it and retry from a clean state:" >&2
    echo "         git reset --hard HEAD~1" >&2
    exit 1
fi
echo "==> Committed and tagged $TAG"

# Persist the notes to a gitignored path (dist/ is ignored) so the recovery
# hints below reference a real file, and so a manual retry has notes on hand.
NOTES_FILE="dist/RELEASE_NOTES-$TAG.md"
printf '%s\n' "$NOTES" > "$NOTES_FILE"

# --- 7. Push commit + tag together -------------------------------------------
echo "==> Pushing $BRANCH and $TAG to origin"
if ! git push --atomic origin "$BRANCH" "$TAG"; then
    cat >&2 <<EOF
ERROR: push failed. The release commit and tag $TAG exist locally, but nothing
       was published. Choose one:
       (a) Abort and undo everything local, then start over:
             git tag -d $TAG && git reset --hard HEAD~1
       (b) Fix the remote state and finish the release manually:
             git push --atomic origin $BRANCH $TAG
             gh release create $TAG --title 'dstui $TAG' --notes-file $NOTES_FILE $INSTALLER $BOOTSTRAP
EOF
    exit 1
fi

# --- 8. Publish the GitHub release with the installer + bootstrap assets ------
# Past this point $TAG is pushed; `make release` can't resume (the tag guard
# blocks it), so recovery is the single manual gh command below.
echo "==> Creating GitHub release $TAG"
if ! gh release create "$TAG" \
        --title "dstui $TAG" \
        --notes-file "$NOTES_FILE" \
        "$INSTALLER" "$BOOTSTRAP"; then
    echo "ERROR: gh release create failed, but $TAG is already pushed." >&2
    echo "       Retry the release step only:" >&2
    echo "         gh release create $TAG --title 'dstui $TAG' --notes-file $NOTES_FILE $INSTALLER $BOOTSTRAP" >&2
    exit 1
fi

echo ""
echo "Released dstui $TAG"
gh release view "$TAG" --json url -q .url 2>/dev/null || true
