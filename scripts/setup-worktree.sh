#!/usr/bin/env bash
set -euo pipefail

info() { printf '-> %s\n' "$1"; }
warn() { printf 'WARN %s\n' "$1" >&2; }

current_root="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
cd "$current_root"
git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
primary_root="$(cd "$(dirname "$git_common_dir")" && pwd -P)"

copy_local_file_if_missing() {
  local relative_path="$1"
  local source="${primary_root}/${relative_path}"
  local target="${current_root}/${relative_path}"

  [[ "$primary_root" != "$current_root" ]] || return 0
  [[ -f "$source" ]] || return 0
  [[ ! -e "$target" ]] || return 0
  if ! git check-ignore -q -- "$relative_path"; then
    warn "Skipped ${relative_path}; target path is not ignored"
    return 0
  fi

  mkdir -p "$(dirname "$target")"
  python3 - "$source" "$target" "$primary_root" "$current_root" <<'PY'
from pathlib import Path
import sys

source, target, source_root, current_root = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8").replace(source_root, current_root)
Path(target).write_text(text, encoding="utf-8")
PY
  chmod go-rwx "$target" 2>/dev/null || true
  info "Copied ${relative_path} from primary worktree"
}

copy_local_file_if_missing ".apex-ray/config.local.yml"
copy_local_file_if_missing ".mcp.json"
copy_local_file_if_missing ".env"
copy_local_file_if_missing ".env.local"

if [[ -f "pyproject.toml" ]]; then
  info "Installing Python dependencies"
  uv sync --all-groups
fi

if [[ -f "analyzer-runtimes/typescript/package-lock.json" ]]; then
  info "Installing TypeScript analyzer dependencies"
  npm --prefix analyzer-runtimes/typescript ci
  npm --prefix analyzer-runtimes/typescript run build
fi

if [[ -f "lefthook.yml" ]] && command -v lefthook >/dev/null 2>&1; then
  info "Installing Lefthook hooks"
  lefthook install
elif [[ -f "lefthook.yml" ]]; then
  warn "lefthook is not installed; skipping hook installation"
fi
