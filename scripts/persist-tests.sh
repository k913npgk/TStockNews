#!/usr/bin/env bash
set -euo pipefail
# Dedicated branch: a test can never push the production screener-data branch.
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
if [ ! -e test-state/.git ]; then
  if git show-ref --verify --quiet refs/remotes/origin/screener-tests; then
    git worktree add --detach test-state origin/screener-tests
  else
    git worktree add --detach test-state HEAD
    git -C test-state switch --orphan screener-tests
  fi
fi
mkdir -p test-state/test-runs
cp -a "test-runs/$TEST_ID" test-state/test-runs/
git -C test-state add --force "test-runs/$TEST_ID"
if ! git -C test-state diff --cached --quiet; then
  git -C test-state commit -m "Save isolated test $TEST_ID"
  git -C test-state push origin HEAD:refs/heads/screener-tests
fi
