#!/usr/bin/env bash
# Commit the current working-tree change onto a fixed bot branch and open (or
# update) its PR. Shared by the jobs in .github/workflows/bump-image-inputs.yml,
# which differ entirely in how they DETECT a new version but not at all in what
# they do about it.
#
# One fixed branch per bumper, force-updated: a version-suffixed branch would
# leave a stale PR behind every time a newer release landed mid-review, so this
# keeps exactly zero or one open PR per bumper, always at the newest version.
#
# Usage:  GH_TOKEN=<pat> scripts/open-bump-pr.sh <branch> <title> <commit-subject>
#         ...with the PR body on stdin.
set -euo pipefail

branch="${1:?usage: open-bump-pr.sh <branch> <title> <commit-subject>}"
title="${2:?missing title}"
subject="${3:?missing commit subject}"
body="$(cat)"

# GITHUB_TOKEN would be silently useless here: GitHub suppresses push and
# pull_request events caused by it, so CI would never run on the PR and its
# required contexts would never report, leaving it unmergeable. Fail loudly
# instead of opening a PR nobody can merge.
if [ -z "${GH_TOKEN:-}" ]; then
  echo "::error::GH_TOKEN is empty — set the BUMP_PR_TOKEN secret (fine-grained PAT, Contents + Pull requests: write)"
  exit 1
fi
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"

git config user.name "husk-bot"
git config user.email "husk-bot@users.noreply.github.com"
git switch -c "$branch"
git commit -am "$subject"

# Token as a push-time argument only, never persisted into .git/config. Actions
# masks the secret in logs, but there is no reason to leave it on disk.
git push --force \
  "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
  "HEAD:refs/heads/${branch}"

existing="$(gh pr list --head "$branch" --state open --json number -q '.[0].number // empty')"
if [ -n "$existing" ]; then
  gh pr edit "$existing" --title "$title" --body "$body"
  echo "updated PR #${existing}"
else
  gh pr create --base main --head "$branch" --title "$title" --body "$body"
fi
