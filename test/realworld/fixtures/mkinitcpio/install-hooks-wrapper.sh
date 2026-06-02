. ../install/base
base_help=$(help | sed -n '1p')
base_build=$(declare -F build >/dev/null && printf build)

. ../install/modconf
modconf_help=$(help | sed -n '1p')
modconf_build=$(declare -F build >/dev/null && printf build)

printf 'mkinitcpio:hooks:%s:%s:%s:%s\n' "$base_build" "$modconf_build" "$base_help" "$modconf_help"
