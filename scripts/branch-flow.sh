#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"
DEV_BRANCH="${DEV_BRANCH:-dev-bing}"

usage() {
  cat <<EOF
Usage:
  ./scripts/branch-flow.sh status
  ./scripts/branch-flow.sh sync-main
  ./scripts/branch-flow.sh sync-dev [--push]

Defaults:
  UPSTREAM_REMOTE=${UPSTREAM_REMOTE}
  ORIGIN_REMOTE=${ORIGIN_REMOTE}
  MAIN_BRANCH=${MAIN_BRANCH}
  DEV_BRANCH=${DEV_BRANCH}

Commands:
  status      Show current branch and tracking status.
  sync-main   Fetch ${UPSTREAM_REMOTE} and fast-forward ${MAIN_BRANCH} to ${UPSTREAM_REMOTE}/${MAIN_BRANCH}.
  sync-dev    Sync ${MAIN_BRANCH}, then merge it into ${DEV_BRANCH}. Use --push to push ${DEV_BRANCH} to ${ORIGIN_REMOTE}.

Examples:
  ./scripts/branch-flow.sh status
  ./scripts/branch-flow.sh sync-main
  ./scripts/branch-flow.sh sync-dev --push
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

run_git() {
  git -C "$REPO_ROOT" "$@"
}

require_remote() {
  local remote="$1"
  run_git remote get-url "$remote" >/dev/null 2>&1 || die "remote '$remote' does not exist"
}

require_branch() {
  local branch="$1"
  run_git show-ref --verify --quiet "refs/heads/$branch" || die "local branch '$branch' does not exist"
}

require_clean_tree() {
  local status
  status="$(run_git status --porcelain)"
  [[ -z "$status" ]] || die "working tree is not clean; commit or stash changes first"
}

print_status() {
  echo "Repository: $REPO_ROOT"
  echo "Current branch: $(run_git branch --show-current)"
  echo ""
  run_git branch -vv
  echo ""
  run_git status -sb
}

sync_main() {
  require_clean_tree
  require_remote "$UPSTREAM_REMOTE"
  require_branch "$MAIN_BRANCH"

  echo "Fetching ${UPSTREAM_REMOTE}..."
  run_git fetch "$UPSTREAM_REMOTE"

  echo "Checking out ${MAIN_BRANCH}..."
  run_git checkout "$MAIN_BRANCH"

  echo "Fast-forwarding ${MAIN_BRANCH} -> ${UPSTREAM_REMOTE}/${MAIN_BRANCH}..."
  run_git merge --ff-only "${UPSTREAM_REMOTE}/${MAIN_BRANCH}"
}

sync_dev() {
  local push_after="${1:-}"

  require_clean_tree
  require_remote "$UPSTREAM_REMOTE"
  require_remote "$ORIGIN_REMOTE"
  require_branch "$MAIN_BRANCH"
  require_branch "$DEV_BRANCH"

  sync_main

  echo "Checking out ${DEV_BRANCH}..."
  run_git checkout "$DEV_BRANCH"

  echo "Merging ${MAIN_BRANCH} into ${DEV_BRANCH}..."
  run_git merge "$MAIN_BRANCH"

  if [[ "$push_after" == "--push" ]]; then
    echo "Pushing ${DEV_BRANCH} to ${ORIGIN_REMOTE}..."
    run_git push "$ORIGIN_REMOTE" "$DEV_BRANCH"
  elif [[ -n "$push_after" ]]; then
    die "unknown option: $push_after"
  fi
}

main() {
  local command="${1:-}"
  shift || true

  case "$command" in
    status)
      print_status
      ;;
    sync-main)
      [[ $# -eq 0 ]] || die "sync-main does not accept extra arguments"
      sync_main
      ;;
    sync-dev)
      [[ $# -le 1 ]] || die "sync-dev accepts at most one option: --push"
      sync_dev "${1:-}"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      [[ -n "$command" ]] && exit 1
      ;;
  esac
}

main "$@"
