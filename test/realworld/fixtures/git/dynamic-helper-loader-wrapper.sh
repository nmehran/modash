#!/usr/bin/env bash

: "${MODASH_GIT_HELPER:?}"
: "${MODASH_GIT_MERGETOOLS_DIR:?}"

load_mergetool() {
    . "$1"
}

"$MODASH_GIT_HELPER" "$MODASH_GIT_MERGETOOLS_DIR/bc"
"$MODASH_GIT_HELPER" "$MODASH_GIT_MERGETOOLS_DIR/vimdiff"
