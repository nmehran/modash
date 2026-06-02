#!/usr/bin/env bash
set -u

: "${MODASH_GIT_MERGETOOLS_DIR:?}"

loaded=0
for tool in vimdiff bc missing-tool; do
    [ -f "$MODASH_GIT_MERGETOOLS_DIR/$tool" ] || continue
    . "$MODASH_GIT_MERGETOOLS_DIR/$tool"
    loaded=$((loaded + 1))
done

printf 'loaded=%s\n' "$loaded"
type diff_cmd >/dev/null && printf 'diff_cmd=ready\n'
type merge_cmd >/dev/null && printf 'merge_cmd=ready\n'
list_tool_variants | while IFS= read -r variant; do
    printf 'variant=%s\n' "$variant"
done
