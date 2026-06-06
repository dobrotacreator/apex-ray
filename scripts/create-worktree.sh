#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/create-worktree.sh <branch> [--base <ref>] [--path <path>] [--no-setup]

Creates a git worktree under the primary checkout's .worktrees/ directory and
runs scripts/setup-worktree.sh inside it by default.
EOF
}

fail() {
  printf 'ERR %s\n' "$1" >&2
  exit 1
}

resolve_path() {
  local base="$1"
  local value="$2"
  python3 - "$base" "$value" <<'PY'
import os
import sys

base, value = sys.argv[1:]
print(os.path.abspath(value if os.path.isabs(value) else os.path.join(base, value)))
PY
}

branch_name=""
base_ref="origin/main"
path_arg=""
run_setup=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --base)
      shift
      [[ $# -gt 0 ]] || fail "--base requires a value"
      base_ref="$1"
      ;;
    --base=*)
      base_ref="${1#--base=}"
      ;;
    --path)
      shift
      [[ $# -gt 0 ]] || fail "--path requires a value"
      path_arg="$1"
      ;;
    --path=*)
      path_arg="${1#--path=}"
      ;;
    --no-setup)
      run_setup=0
      ;;
    -*)
      fail "Unknown option: $1"
      ;;
    *)
      [[ -z "$branch_name" ]] || fail "Unexpected extra argument: $1"
      branch_name="$1"
      ;;
  esac
  shift
done

[[ -n "$branch_name" ]] || fail "Missing branch name"
git check-ref-format --branch "$branch_name" >/dev/null 2>&1 || fail "Invalid branch name: $branch_name"

current_root="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
git_common_dir="$(git -C "$current_root" rev-parse --path-format=absolute --git-common-dir)"
primary_root="$(cd "$(dirname "$git_common_dir")" && pwd -P)"
worktrees_root="${primary_root}/.worktrees"

branch_slug="${branch_name//\//-}"
[[ -n "$path_arg" ]] || path_arg=".worktrees/${branch_slug}"
target_path="$(resolve_path "$primary_root" "$path_arg")"

case "$target_path" in
  "${worktrees_root}/"*) ;;
  *) fail "Worktree path must be under ${worktrees_root}: ${target_path}" ;;
esac

[[ ! -e "$target_path" ]] || fail "Target path already exists: $target_path"
if git -C "$primary_root" rev-parse --verify --quiet "refs/heads/${branch_name}" >/dev/null; then
  fail "Local branch already exists: $branch_name"
fi

case "$base_ref" in
  origin/*)
    remote_branch="${base_ref#origin/}"
    if git check-ref-format "refs/heads/${remote_branch}" >/dev/null 2>&1; then
      git -C "$primary_root" fetch origin "+refs/heads/${remote_branch}:refs/remotes/origin/${remote_branch}"
    fi
    ;;
esac

git -C "$primary_root" rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null \
  || fail "Base ref does not resolve to a commit: $base_ref"

mkdir -p "$(dirname "$target_path")"
git -C "$primary_root" worktree add -b "$branch_name" "$target_path" "$base_ref"

if [[ "$run_setup" == "1" ]]; then
  (cd "$target_path" && WORKTREE_NAME="$(basename "$target_path")" bash scripts/setup-worktree.sh)
fi

printf 'Created worktree: %s\n' "$target_path"
