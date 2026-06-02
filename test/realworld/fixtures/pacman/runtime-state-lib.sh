MODASH_STATE_VALUE="${MODASH_STATE_VALUE:-seed}:lib"
export MODASH_EXPORTED_VALUE=exported-from-lib

modash_fixture_function() {
	printf 'function:%s:%s\n' "$MODASH_STATE_VALUE" "$MODASH_EXPORTED_VALUE"
}
