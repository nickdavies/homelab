#!/usr/bin/env bash
# Post a CI comment on a PR, or edit the one this workflow posted before.
#
# Usage: pr-comment.sh <pr number> <body file> [--update-only]
#
# Comments are matched by their first line (e.g. "### Chart Diff"), so each
# check keeps one comment however many checks post. With --update-only, a
# body is posted only if the PR already has that comment: a check with
# nothing to say stays quiet unless it said something earlier.
#
# Needs gh, GH_TOKEN and GITHUB_REPOSITORY.
set -euo pipefail

pr=$1
body=$2
update_only=${3:-}

heading=$(head -n1 "$body")
id=$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$pr/comments" \
  --jq ".[] | select(.user.login == \"github-actions[bot]\") | select(.body | split(\"\n\")[0] == $(jq -Rn --arg h "$heading" '$h')) | .id" \
  | tail -n1)

if [ -n "$id" ]; then
  gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$id" -F "body=@$body" >/dev/null
elif [ "$update_only" != "--update-only" ]; then
  gh pr comment "$pr" --body-file "$body"
fi
