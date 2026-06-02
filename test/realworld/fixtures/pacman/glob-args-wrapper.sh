#!/bin/bash

set -- wrapper-positionals
source ./glob-args/*.sh explicit
echo "glob-wrapper:$1:$MODASH_REALWORLD_GLOB_ARGS"
