#!/usr/bin/env bash
set -u

: "${MODASH_COMPLETION_ROOT:?}"

loaded=0
for completion in cd alias missing-completion; do
    [ -r "$MODASH_COMPLETION_ROOT/$completion" ] || continue
    . "$MODASH_COMPLETION_ROOT/$completion"
    loaded=$((loaded + 1))
done

printf 'loaded=%s\n' "$loaded"
type _comp_cmd_cd >/dev/null && printf 'cd=ready\n'
type _comp_cmd_alias >/dev/null && printf 'alias=ready\n'
