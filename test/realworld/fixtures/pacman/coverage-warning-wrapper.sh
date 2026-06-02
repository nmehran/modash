if [[ "${MODASH_RUNTIME_BRANCH:-prod}" == prod ]]; then
  source ./coverage-prod.sh
else
  source ./coverage-dev.sh
fi

printf 'branch:%s\n' "${MODASH_RUNTIME_BRANCH:-prod}"
