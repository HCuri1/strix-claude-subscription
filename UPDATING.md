# Updating from upstream

This repository shares real git history with the upstream
[Strix](https://github.com/usestrix/strix) project, starting at its `v1.7.0`
release tag. The Claude-subscription modification (see
[CHANGES.md](CHANGES.md)) lives as commits on top of that point.

## Policy

Updates to this repository only ever come from an upstream **release tag**
(`vX.Y.Z`) — never from upstream's default branch or any other commit. This
keeps every update tied to something upstream actually shipped and tagged,
and keeps the update itself a single, reviewable `git rebase`.

## How to update

```bash
./scripts/update-from-upstream.sh v1.8.0   # the tag you want to update to
```

This fetches upstream's tags and rebases this repo's commits onto the given
tag. Two outcomes:

- **Clean rebase**: the script prints a short checklist — regenerate
  `uv.lock` (`uv lock`) and the viewer bundle (`make viewer`) if upstream
  touched those, run `make check-all && uv run pytest`, check whether
  anything in `CHANGES.md`'s file list also changed upstream in this release
  and needs a touch-up — then push with
  `git push --force-with-lease origin main`.
- **Conflicts**: expected whenever upstream's release also touched a file
  this project modifies (`CHANGES.md` has that list). Resolve each
  conflicted file by hand — keep the Claude-subscription behavior while
  picking up whatever upstream changed — then `git add` the resolved files
  and `git rebase --continue`. Repeat until it finishes, then follow the
  checklist above.

## One-time setup (already done on this clone)

If you ever clone this repository fresh and the `upstream` remote isn't
configured:

```bash
git remote add upstream https://github.com/usestrix/strix.git
```

No push access is needed or configured for `upstream` — it's fetch-only, used
solely to pull tags for rebasing.
