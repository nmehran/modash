#!/usr/bin/env bash

bash -c 'source ./child-bash-trace-target.sh; printf "child:%s\n" "$CHILD_VALUE"'
printf "parent:%s\n" "${CHILD_VALUE-unset}"
