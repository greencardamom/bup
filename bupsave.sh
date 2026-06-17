#!/bin/bash
#
# bupsave.sh - deploy helper for the bup (BooksUp) tool.
#
# Pipeline (run top-to-bottom):
#   1. commit local changes   (in /home/greenc/repos/gh/bup)
#   2. push to GitHub         (origin/main)
#   3. git pull on Toolforge  (/data/project/bup/www, via deploy key)
#   4. webservice restart      (ON by default)
#
# Usage:
#   ./bupsave.sh ["msg"]              # full cycle: commit + push + pull + restart
#   ./bupsave.sh --push-only ["msg"]  # commit + push to GitHub ONLY (no Toolforge)
#   ./bupsave.sh --pull-only          # pull GitHub -> Toolforge ONLY (+ restart)
#   ./bupsave.sh --no-restart ["msg"] # full cycle but DO NOT restart the webservice
#
# Flags compose: e.g. `--pull-only --no-restart` just fast-forwards Toolforge.
# --push-only and --pull-only are mutually exclusive. An optional commit message
# is the last argument (auto-generated if omitted).
#
set -euo pipefail

REPO="/home/greenc/repos/gh/bup"
TOOL="bup"
TF_WWW="/data/project/${TOOL}/www"
BRANCH="main"
WSTYPE="python3.11"     # webservice type (see `webservice status`)

PUSH=1                  # commit + push to GitHub
PULL=1                  # pull on Toolforge
RESTART=1               # restart webservice after the pull
MSG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --push-only)  PULL=0 ;;
    --pull-only)  PUSH=0 ;;
    --no-restart) RESTART=0 ;;
    -h|--help)    sed -n '3,19p' "$0"; exit 0 ;;
    --)           shift; MSG="$*"; break ;;
    -*)           echo "bupsave: unknown option: $1" >&2; exit 2 ;;
    *)            MSG="$*"; break ;;   # first non-flag arg (+ rest) = commit msg
  esac
  shift
done

if [ "$PUSH" -eq 0 ] && [ "$PULL" -eq 0 ]; then
  echo "bupsave: --push-only and --pull-only are mutually exclusive" >&2
  exit 2
fi

MSG="${MSG:-bup update $(date '+%Y-%m-%d %H:%M:%S')}"

# --- 1+2. commit + push (acre -> GitHub) ----------------------------------
if [ "$PUSH" -eq 1 ]; then
  echo "==> commit + push to GitHub"
  cd "$REPO"
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "$MSG"
  else
    echo "    (no local changes to commit)"
  fi
  git push origin "$BRANCH"
else
  echo "==> --pull-only: skipping local commit/push"
fi

# --- 3+4. pull (+ restart) on Toolforge -----------------------------------
if [ "$PULL" -eq 1 ]; then
  if [ "$RESTART" -eq 1 ]; then
    echo "==> pull on Toolforge + webservice restart"
    RESTART_CMD="webservice restart || webservice ${WSTYPE} start"
  else
    echo "==> pull on Toolforge (--no-restart)"
    RESTART_CMD='echo "    (skipping webservice restart)"'
  fi
  # `become <tool> bash -s` runs the heredoc as the tool account. The heredoc is
  # UNquoted so $TF_WWW / $BRANCH / $RESTART_CMD expand here, before being sent.
  ssh -o BatchMode=yes -o ConnectTimeout=30 tools "become ${TOOL} bash -s" <<REMOTE
set -e
cd "${TF_WWW}"
echo "    pulling origin ${BRANCH}..."
git pull --ff-only origin ${BRANCH}
${RESTART_CMD}
REMOTE
else
  echo "==> --push-only: skipping Toolforge pull/restart"
fi

echo "==> done"
