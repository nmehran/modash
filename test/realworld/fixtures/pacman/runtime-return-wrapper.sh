#!/bin/bash

source ./runtime-return-lib.sh
status=$?
printf 'return-status:%s\n' "$status"
printf 'return-value:%s\n' "$MODASH_RETURN_VALUE"
