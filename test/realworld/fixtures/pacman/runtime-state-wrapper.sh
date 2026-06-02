#!/bin/bash

source ./runtime-state-lib.sh
printf 'value:%s\n' "$MODASH_STATE_VALUE"
export -p | grep ' MODASH_EXPORTED_VALUE='
modash_fixture_function
