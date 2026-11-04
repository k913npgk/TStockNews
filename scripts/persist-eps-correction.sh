#!/usr/bin/env bash
set -euo pipefail

for path in "$CORRECTION_DATA" "$CORRECTION_REPORTS"; do
  mkdir -p "state-checkout/$path"
  cp -a "$path"/. "state-checkout/$path/"
done
if [[ ! -e "$CORRECTION_DATA/error.json" ]]; then
  rm -f "state-checkout/$CORRECTION_DATA/error.json"
fi
git -C state-checkout add "$CORRECTION_DATA" "$CORRECTION_REPORTS"
if ! git -C state-checkout diff --cached --quiet; then
  git -C state-checkout -c user.name='github-actions[bot]' \
    -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
    commit -m 'Save formal EPS rollover corrections'
  git -C state-checkout push origin HEAD:refs/heads/screener-data
fi
