#!/bin/bash

MAKEPKG_LIBRARY=${MAKEPKG_LIBRARY:-./scripts/libmakepkg}

source "$MAKEPKG_LIBRARY/util/util.sh"
set -- wrapper-positionals
source_safe ./source-safe-args-target.sh "arg one" arg-two
echo "wrapper-args:$1:$MODASH_REALWORLD_SOURCE_SAFE_ARGS"
