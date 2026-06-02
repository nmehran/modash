#!/usr/bin/env bash

: "${MODASH_MKINITCPIO_HOOK_ROOT:?}"

msg() {
    printf 'mkinitcpio-msg:%s\n' "$*"
}

run_hookfunctions() {
    local hook fn=$1 desc=$2

    shift 2
    for hook in "$@"; do
        [ -r "$MODASH_MKINITCPIO_HOOK_ROOT/$hook" ] || continue

        unset "$fn"
        . "$MODASH_MKINITCPIO_HOOK_ROOT/$hook"
        type "$fn" >/dev/null || continue

        msg ":: running $desc [$hook]"
        "$fn"
    done
}

run_hookfunctions 'run_hook' 'hook' consolefont keymap missing-hook
