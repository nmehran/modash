runtime_guarded_if=unset
if awk 'BEGIN { exit ENVIRON["MODASH_REALWORLD_RUNTIME_IF"] == "1" ? 0 : 1 }'; then
  source ./runtime-guarded-if-lib.sh
else
  runtime_guarded_if=disabled
  echo "runtime-if-disabled"
fi
echo "runtime-if=${runtime_guarded_if}"
