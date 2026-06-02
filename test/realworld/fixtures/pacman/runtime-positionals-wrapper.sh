#!/bin/bash

set -- wrapper-one wrapper-two
source ./runtime-positionals-lib.sh "arg one" arg-two
status=$?
echo "positionals-wrapper:$1:$2:$#:$status:$MODASH_REALWORLD_POSITIONALS"
