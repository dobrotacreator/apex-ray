#!/usr/bin/env bash
set -euo pipefail

if ! command -v apex-ray >/dev/null 2>&1; then
  echo "apex-ray is not installed; skipping Apex Ray pre-push gate."
  echo "Install Apex Ray to enable local AI review before push."
  exit 0
fi

if ! git fetch --quiet origin +refs/heads/main:refs/remotes/origin/main; then
  echo "Failed to fetch origin/main; refusing to run Apex Ray against a stale base ref." >&2
  exit 1
fi

apex-ray gate pre-push
