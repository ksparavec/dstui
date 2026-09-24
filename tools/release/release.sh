#!/usr/bin/env bash
#
# release.sh — cut a release of dstui: tag it, and let CI build, attest and publish it.
#
# Flow: guard the repo state -> promote the CHANGELOG's [Unreleased] section to the current
# version -> commit "chore: release vX.Y.Z" -> annotated tag -> push commit and tag atomically.
# Nothing is built here and no release is created here: the pushed tag triggers
# .github/workflows/release.yml, which builds the installer (full smoke test), attests
# dist/dstui-install.sh and install.sh (GitHub artifact attestations can only be made inside
# GitHub Actions) and publishes the GitHub release. This script then follows that run with
# `gh run watch` and prints the release URL.
#
# The version is read from pyproject.toml (the single source of truth, same as
# tools/package/build-binary.sh). Release the *current* version; bump the version and fill in
# CHANGELOG entries before running this.
#
# Guards (all must pass before anything mutates): on `main`, clean working tree (untracked
# files and assume-unchanged / skip-worktree entries included), local == origin/main, and
# neither the tag nor the GitHub release exists yet.
#
# Non-interactive use: set DSTUI_RELEASE_ASSUME_YES=1 to skip the prompt.
# DSTUI_RELEASE_WATCH_WAIT: how many seconds to wait for the tag's workflow run to show up
# (default 60); without one, the script says where to follow it and still succeeds.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BRANCH="main"
CHANGELOG="CHANGELOG.md"
WORKFLOW="release.yml"
WATCH_WAIT="${DSTUI_RELEASE_WATCH_WAIT:-60}"
WATCH_POLL=3

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

# --untracked-files=all overrides a status.showUntrackedFiles=no config: what was tested
# locally must be exactly what the tag points at.
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

# --- 2. The [Unreleased] section must have something to release --------------
bash tools/release/release-notes.sh Unreleased "$CHANGELOG" >/dev/null \
    || { echo "ERROR: nothing to release" >&2; exit 1; }

# --- 3. Confirm --------------------------------------------------------------
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo '?')"
cat <<EOF

About to release:
  repo    : $REPO
  version : $VERSION
  tag     : $TAG
  branch  : $BRANCH  ($(git rev-parse --short HEAD))

This promotes the CHANGELOG, commits, tags and pushes. The pushed tag starts the
$WORKFLOW workflow, which builds, attests and publishes the GitHub release.
It is outward-facing and hard to undo.
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

# --- 5. Commit + annotated tag (local only) ----------------------------------
# Nothing is on the remote yet: the recovery for a failure here is to undo the
# local commit (which also restores the promoted CHANGELOG) and start over.
git add "$CHANGELOG"
git commit -q -m "chore: release $TAG" || { echo "ERROR: git commit failed" >&2; exit 1; }
COMMITTED=1
if ! git tag -a "$TAG" -m "dstui $TAG"; then
    echo "ERROR: creating tag $TAG failed. The release commit is local-only (not pushed)." >&2
    echo "       Undo it and retry from a clean state:" >&2
    echo "         git reset --hard HEAD~1" >&2
    exit 1
fi
echo "==> Committed and tagged $TAG"

# --- 6. Push commit + tag together -------------------------------------------
# --atomic: origin takes both or neither, so main never carries a release commit without
# its tag (and the release workflow never runs for a tag main does not have).
echo "==> Pushing $BRANCH and $TAG to origin"
if ! git push --atomic origin "$BRANCH" "$TAG"; then
    cat >&2 <<EOF
ERROR: push failed. The release commit and tag $TAG exist locally, but nothing
       was pushed. Choose one:
       (a) Abort and undo everything local, then start over:
             git tag -d $TAG && git reset --hard HEAD~1
       (b) Fix the remote state and push again; the tag starts the release workflow:
             git push --atomic origin $BRANCH $TAG
EOF
    exit 1
fi

# --- 7. Follow the release workflow the tag started --------------------------
# Past this point $TAG is pushed and the release is in CI's hands; `make release` cannot
# resume (the tag guard blocks it). GitHub creates the run a few seconds after the push.
echo "==> $TAG pushed; the $WORKFLOW workflow builds, attests and publishes it"
RELEASE_COMMIT="$(git rev-parse HEAD)"
run_id=""
waited=0
while :; do
    run_id="$(gh run list --workflow "$WORKFLOW" --branch "$TAG" --commit "$RELEASE_COMMIT" \
        --event push --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null \
        || true)"
    case "$run_id" in
        ''|*[!0-9]*) run_id="" ;;
        *) break ;;
    esac
    [ "$waited" -lt "$WATCH_WAIT" ] 2>/dev/null || break
    sleep "$WATCH_POLL"
    waited=$((waited + WATCH_POLL))
done

if [ -z "$run_id" ]; then
    echo "==> No $WORKFLOW run for $TAG showed up yet. Follow it at"
    echo "      https://github.com/$REPO/actions/workflows/$WORKFLOW"
    echo "    The release appears at https://github.com/$REPO/releases/tag/$TAG when it succeeds."
    exit 0
fi

echo "==> Watching run $run_id (Ctrl-C stops watching, not the release)"
if ! gh run watch "$run_id" --exit-status; then
    cat >&2 <<EOF
ERROR: the release workflow (run $run_id) failed: $TAG is pushed but not published.
       See why:           gh run view $run_id --log-failed
       Transient failure: gh run rerun $run_id --failed
       Needs a code fix:  drop the tag (git push origin :refs/tags/$TAG && git tag -d $TAG),
                          push the fix to $BRANCH, then tag it again and push the tag:
                            git tag -a $TAG -m 'dstui $TAG' && git push origin $TAG
                          (make release would refuse: [Unreleased] is empty now)
EOF
    exit 1
fi

echo ""
echo "Released dstui $TAG"
gh release view "$TAG" --json url -q .url 2>/dev/null || true
