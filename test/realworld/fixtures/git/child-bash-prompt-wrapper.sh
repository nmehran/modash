#!/usr/bin/env bash
set -u

: "${MODASH_GIT_COMPLETION_DIR:?}"

bash -c '. "$1"; type __git_ps1 >/dev/null && printf "prompt=ready\n"' bash "$MODASH_GIT_COMPLETION_DIR/git-prompt.sh"
