#!/usr/bin/env bash
# Rebase this repo's modifications onto a specific upstream usestrix/strix
# release tag. See UPDATING.md for the policy this implements: updates only
# ever come from a tagged release, never from upstream's branches/commits.
set -euo pipefail

usage() {
    echo "Usage: $0 <upstream-tag>   (e.g. $0 v1.8.0)" >&2
    exit 2
}

[ "$#" -eq 1 ] || usage
TAG="$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "error: working tree has uncommitted changes — commit or stash first" >&2
    exit 1
fi

if ! git remote get-url upstream >/dev/null 2>&1; then
    echo "error: no 'upstream' remote configured. Run:" >&2
    echo "  git remote add upstream https://github.com/usestrix/strix.git" >&2
    exit 1
fi

echo "Fetching tags from upstream..."
git fetch upstream --tags --quiet

if ! git rev-parse --verify --quiet "refs/tags/${TAG}^{commit}" >/dev/null; then
    echo "error: tag '${TAG}' not found on upstream after fetching." >&2
    echo "Check https://github.com/usestrix/strix/releases for the exact tag name." >&2
    exit 1
fi

echo "Rebasing onto upstream tag ${TAG}..."
if git rebase "refs/tags/${TAG}^{commit}"; then
    cat <<EOF

Rebase onto ${TAG} completed cleanly.

Before pushing:
  1. uv lock                          # re-sync the lockfile if pyproject.toml changed upstream
  2. make viewer                      # rebuild the viewer bundle if its frontend changed upstream
  3. make check-all && uv run pytest  # confirm nothing broke
  4. Review CHANGES.md: does any file listed there need a Claude-subscription
     touch-up for what changed in ${TAG}?

Then push:
  git push --force-with-lease origin main
EOF
else
    cat <<EOF

Rebase stopped with conflicts. This is expected when upstream's ${TAG} also
touched a file this project modifies (see CHANGES.md for that list) — resolve
each conflict by hand, keeping the Claude-subscription behavior while picking
up upstream's change, then:

  git add <resolved files>
  git rebase --continue

Repeat until the rebase finishes, then follow the post-rebase checklist from
this script's success message (re-run it, or see UPDATING.md).
EOF
    exit 1
fi
