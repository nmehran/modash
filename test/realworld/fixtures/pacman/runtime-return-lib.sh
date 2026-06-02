MODASH_RETURN_VALUE=before
printf 'return-lib:%s\n' "$MODASH_RETURN_VALUE"
return 4
MODASH_RETURN_VALUE=after
printf 'unreachable\n'
