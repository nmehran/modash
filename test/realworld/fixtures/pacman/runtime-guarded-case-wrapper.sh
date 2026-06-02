case "$MODASH_REALWORLD_CASE_MODE" in
  prod) source ./runtime-guarded-case-prod.sh ;;
  dev) source ./runtime-guarded-case-dev.sh ;;
  *) runtime_guarded_case=fallback; echo "runtime-case-fallback" ;;
esac
echo "runtime-case=${runtime_guarded_case:-unset}"
