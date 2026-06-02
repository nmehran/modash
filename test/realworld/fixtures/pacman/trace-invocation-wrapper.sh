#!/bin/bash

builtin source ./trace-invocation-target.sh builtin-source
builtin . ./trace-invocation-target.sh builtin-dot
command source ./trace-invocation-target.sh command-source
command . ./trace-invocation-target.sh command-dot

echo "invocation:$MODASH_TRACE_INVOCATION"
