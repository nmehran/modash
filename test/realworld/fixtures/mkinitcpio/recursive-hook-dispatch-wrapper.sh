#!/usr/bin/env bash

: "${MODASH_MKINITCPIO_HOOK_ROOT:?}"

msg() {
    printf 'mkinitcpio-recursive-msg:%s\n' "$*"
}

run_hookfunctions_recursive() {
    local fn=$1 desc=$2 hook
    shift 2

    if [ "$#" -eq 0 ]; then
        return 0
    fi

    hook=$1
    shift
    if [ -r "$MODASH_MKINITCPIO_HOOK_ROOT/$hook" ]; then
        unset "$fn"
        . "$MODASH_MKINITCPIO_HOOK_ROOT/$hook"
        if type "$fn" >/dev/null; then
            msg ":: running $desc [$hook]"
            "$fn"
        fi
    fi

    run_hookfunctions_recursive "$fn" "$desc" "$@"
}

run_hookfunctions_recursive 'run_hook' 'hook' consolefont keymap missing-hook
