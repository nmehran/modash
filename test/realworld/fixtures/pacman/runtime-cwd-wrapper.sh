#!/bin/bash

printf 'start:%s\n' "${PWD##*/}"
source ./runtime-cwd-lib.sh
printf 'after:%s:%s:%s\n' "${PWD##*/}" "$MODASH_CWD_NESTED" "$MODASH_CWD_STATUS"
