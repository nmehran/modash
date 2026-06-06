
exec 19>>"$MODASH_TRACE_FILE" || exit 125
exec 18>>"$MODASH_TRACE_XTRACE_FILE" || exit 125
BASH_XTRACEFD=18
PS4=$'+MODASH_XTRACE\x1f${BASHPID}\x1f${PWD}\x1f${BASH_SOURCE[0]}\x1f${LINENO}\x1f${FUNCNAME[0]-}\x1f '

__modash_source_stack=()
declare -A __modash_source_file_map=()
declare -A __modash_function_file_map=()
declare -A __modash_function_metadata_map=()
declare -A __modash_source_positional_mutation_cache=()
declare -A __modash_source_positional_assignment_cache=()
__modash_caller_positionals=()
__modash_source_call_args=()
__modash_caller_positionals_captured=0

__modash_trace_abort() {
  local code=$1 message=$2
  {
    printf '%s\n' "$code"
    printf '%s\n' "$message"
  } > "$MODASH_TRACE_FAILURE_FILE"
  printf '%s\n' "$message" >&2
  exit 125
}

__modash_emit_process_event() {
  local pid=$1 parent_pid=$2 cwd=$3 entrypoint=$4 command=$5
  shift 5
  printf '%s\0' \
    'MODASH_PROCESS_EVENT' \
    "$pid" \
    "$parent_pid" \
    "$cwd" \
    "$entrypoint" \
    "$command" \
    "$#" \
    "$@" >&19
}

__modash_source_function_stack() {
  local index name
  __modash_function_stack=()
  for ((index = 2; index < ${#FUNCNAME[@]}; index++)); do
    name=${FUNCNAME[$index]}
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    __modash_function_stack+=("$name")
  done
}

__modash_emit_source_event_with_stack() {
  local index=$1 pid=$2 kind=$3 caller_file=$4 caller_line=$5 cwd=$6 source_path=$7 resolved_path=$8 source_size=$9 source_mtime_ns=${10} source_sha256=${11} status=${12} source_entry_status=${13}
  shift 13
  __modash_source_function_stack
  printf '%s\0' \
    'MODASH_SOURCE_EVENT' \
    "$index" \
    "$pid" \
    "$kind" \
    "$caller_file" \
    "$caller_line" \
    "$cwd" \
    "$source_path" \
    "$resolved_path" \
    "$source_size" \
    "$source_mtime_ns" \
    "$source_sha256" \
    "$status" \
    "$source_entry_status" \
    "${#__modash_function_stack[@]}" \
    "${__modash_function_stack[@]}" \
    "$#" \
    "$@" >&19
}

__modash_next_source_index() {
  local lock_path="${MODASH_TRACE_COUNTER_FILE}.lock"
  local index
  while ! "$MODASH_TRACE_MKDIR" "$lock_path" 2>/dev/null; do
    "$MODASH_TRACE_SLEEP" 0.001
  done
  if [[ -r $MODASH_TRACE_COUNTER_FILE ]]; then
    IFS= read -r index < "$MODASH_TRACE_COUNTER_FILE"
  else
    index=0
  fi
  printf '%s\n' "$((index + 1))" > "$MODASH_TRACE_COUNTER_FILE"
  "$MODASH_TRACE_RMDIR" "$lock_path"
  printf '%s' "$index"
}

__modash_source_fingerprint=()

__modash_fingerprint_file() {
  local path=${1-}
  __modash_source_fingerprint=("" "" "")
  if [[ -z $path || ! -f $path ]]; then
    return 1
  fi
  mapfile -t __modash_source_fingerprint < <("$MODASH_TRACE_PYTHON" -S "$MODASH_TRACE_FINGERPRINT_SCANNER" "$path") || return 1
  ((${#__modash_source_fingerprint[@]} == 3))
}

__modash_physical_pwd() {
  builtin pwd -P 2>/dev/null || printf '%s' "$PWD"
}

__modash_resolve_source_path() {
  local source_path=${1-}
  local directory cwd
  cwd=$(__modash_physical_pwd)

  if [[ -z $source_path ]]; then
    printf '%s/' "$cwd"
    return
  fi

  if [[ $source_path == */* ]]; then
    if [[ $source_path == /* ]]; then
      printf '%s' "$source_path"
    else
      printf '%s/%s' "$cwd" "$source_path"
    fi
    return
  fi

  if ! shopt -q sourcepath; then
    printf '%s/%s' "$cwd" "$source_path"
    return
  fi

  local old_ifs=$IFS
  IFS=:
  for directory in $PATH; do
    if [[ -z $directory ]]; then
      directory=.
    fi
    if [[ -f $directory/$source_path ]]; then
      if [[ $directory == /* ]]; then
        printf '%s/%s' "$directory" "$source_path"
      else
        printf '%s/%s/%s' "$cwd" "$directory" "$source_path"
      fi
      IFS=$old_ifs
      return
    fi
  done
  IFS=$old_ifs
  printf '%s/%s' "$cwd" "$source_path"
}

__modash_source_file_from_bash_source() {
  local bash_source=${1-}
  local depth=${#__modash_source_stack[@]}

  if [[ -n $bash_source && -n ${__modash_source_file_map[$bash_source]+set} ]]; then
    printf '%s' "${__modash_source_file_map[$bash_source]}"
  elif [[ -n $bash_source && $bash_source == /* ]]; then
    printf '%s' "$bash_source"
  elif [[ -n $bash_source && $bash_source != "$0" ]]; then
    printf '%s/%s' "$MODASH_TRACE_INITIAL_CWD" "$bash_source"
  elif (( depth > 0 )); then
    printf '%s' "${__modash_source_stack[$((depth - 1))]}"
  else
    printf '%s' "$__modash_trace_process_entrypoint"
  fi
}

__modash_current_source_file() {
  local bash_source=${1-} function_name=${2-}
  local mapped_file source_file current_metadata

  if [[ -n $function_name && -n ${__modash_function_file_map[$function_name]+set} ]]; then
    mapped_file=${__modash_function_file_map[$function_name]}
    current_metadata=$(__modash_function_definition_metadata "$function_name" || true)
    if [[ -z $current_metadata || ${__modash_function_metadata_map[$function_name]-} != "$current_metadata" ]]; then
      source_file=$(__modash_source_file_from_bash_source "$bash_source")
      unset "__modash_function_file_map[$function_name]"
      unset "__modash_function_metadata_map[$function_name]"
      printf '%s' "$source_file"
      return
    fi
    printf '%s' "$mapped_file"
    return
  fi

  __modash_source_file_from_bash_source "$bash_source"
}

__modash_function_definition_metadata() {
  local name=$1 definition line file
  definition=$(shopt -s extdebug; declare -F "$name") || return 1
  read -r _ line file <<< "$definition"
  [[ -n $line && -n $file ]] || return 1
  printf '%s\t%s' "$line" "$file"
}

__modash_record_sourced_functions() {
  local resolved_path=$1 name declared_name declared_line current_metadata definition_file definition_line definition_path
  local scanner_status function_records function_record_status function_records_loaded=0
  local -A live_function_map=()
  local -A unknown_function_map=()
  local -A unknown_function_line_map=()

  while IFS= read -r name; do
    [[ -n $name ]] || continue
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    current_metadata=$(__modash_function_definition_metadata "$name") || continue
    definition_line=${current_metadata%%$'\t'*}
    definition_file=${current_metadata#*$'\t'}
    definition_path=$(__modash_current_source_file "$definition_file" "")
    [[ $definition_path == "$resolved_path" ]] || continue
    if ((function_records_loaded == 0)); then
      function_records_loaded=1
      function_records=$("$MODASH_TRACE_PYTHON" -S "$MODASH_TRACE_FUNCTION_SCANNER" "$resolved_path")
      scanner_status=$?
      case "$scanner_status" in
        0)
          while IFS=$'\t' read -r function_record_status declared_name declared_line; do
            [[ -n $declared_name ]] || continue
            case "$function_record_status" in
              live)
                live_function_map["$declared_name"]=1
                ;;
              unknown)
                if [[ $declared_name == "*" && -n $declared_line ]]; then
                  unknown_function_line_map["$declared_line"]=1
                else
                  unknown_function_map["$declared_name"]=1
                fi
                ;;
            esac
          done <<< "$function_records"
          ;;
        1)
          ;;
        *)
          __modash_trace_abort \
            "runtime.trace.function-scanner" \
            "modash: runtime trace could not scan sourced function definitions: ${resolved_path}"
          ;;
      esac
    fi
    if [[ -n ${unknown_function_map[$name]+set} || -n ${unknown_function_line_map[$definition_line]+set} ]]; then
      __modash_trace_abort \
        "runtime.trace.ambiguous-function-provenance" \
        "modash: runtime trace cannot disambiguate branch-dependent function provenance: ${name} in ${resolved_path}"
    fi
    if [[ -z ${live_function_map[$name]+set} ]]; then
      continue
    fi
    __modash_function_file_map["$name"]=$resolved_path
    __modash_function_metadata_map["$name"]=$current_metadata
  done < <(compgen -A function)
}

__modash_forget_deleted_sourced_functions() {
  local name
  local -n definitions_before_ref=$1

  for name in "${!definitions_before_ref[@]}"; do
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    if [[ -n ${__modash_function_file_map[$name]+set} ]] && ! declare -F "$name" >/dev/null; then
      unset "__modash_function_file_map[$name]"
      unset "__modash_function_metadata_map[$name]"
    fi
  done
}

__modash_process_entrypoint() {
  local shell_entrypoint=${1-}
  if [[ -z $shell_entrypoint ]]; then
    printf '%s' "$MODASH_TRACE_ENTRYPOINT"
  elif [[ $shell_entrypoint == */* ]]; then
    if [[ $shell_entrypoint == /* ]]; then
      printf '%s' "$shell_entrypoint"
    else
      printf '%s/%s' "$(__modash_physical_pwd)" "$shell_entrypoint"
    fi
  else
    __modash_resolve_source_path "$shell_entrypoint"
  fi
}

__modash_trace_process_entrypoint=$(__modash_process_entrypoint "$0")
__modash_trace_process_command=${BASH_EXECUTION_STRING:-$__modash_trace_process_entrypoint}
__modash_emit_process_event \
  "$BASHPID" "$PPID" "$PWD" "$__modash_trace_process_entrypoint" "$__modash_trace_process_command" "$@"

__modash_capture_source_call() {
  local caller_count=${1:-0}
  shift

  __modash_caller_positionals=()
  __modash_source_call_args=()
  __modash_caller_positionals_captured=1

  local index
  for ((index = 0; index < caller_count; index++)); do
    __modash_caller_positionals+=("${1-}")
    shift
  done

  if [[ ${1-} == -- ]]; then
    shift
  fi
  __modash_source_call_args=("$@")
}

__modash_source_may_mutate_positionals() {
  local path=${1-} scanner_status
  if [[ -z $path || ! -r $path ]]; then
    return 1
  fi

  if [[ -n ${__modash_source_positional_mutation_cache[$path]+set} ]]; then
    return "${__modash_source_positional_mutation_cache[$path]}"
  fi

  "$MODASH_TRACE_PYTHON" -S "$MODASH_TRACE_POSITIONAL_SCANNER" "$path"
  scanner_status=$?
  if ((scanner_status == 0)); then
    __modash_source_positional_mutation_cache["$path"]=0
    return 0
  fi
  if ((scanner_status == 1)); then
    __modash_source_positional_mutation_cache["$path"]=1
    return 1
  fi
  __modash_source_positional_mutation_cache["$path"]=0
  return 0
}

__modash_source_may_assign_positionals() {
  local path=${1-} scanner_status
  if [[ -z $path || ! -r $path ]]; then
    return 1
  fi

  if [[ -n ${__modash_source_positional_assignment_cache[$path]+set} ]]; then
    return "${__modash_source_positional_assignment_cache[$path]}"
  fi

  "$MODASH_TRACE_PYTHON" -S "$MODASH_TRACE_POSITIONAL_SCANNER" positional-assignments "$path"
  scanner_status=$?
  if ((scanner_status == 0)); then
    __modash_source_positional_assignment_cache["$path"]=0
    return 0
  fi
  if ((scanner_status == 1)); then
    __modash_source_positional_assignment_cache["$path"]=1
    return 1
  fi
  __modash_source_positional_assignment_cache["$path"]=0
  return 0
}

__modash_source_builtin_enabled() {
  local line
  case "$1" in
    source)
      while IFS= read -r line; do
        [[ $line == "enable source" ]] && return 0
      done < <(enable -p)
      return 1
      ;;
    .)
      while IFS= read -r line; do
        [[ $line == "enable ." ]] && return 0
      done < <(enable -p)
      return 1
      ;;
    *)
      return 1
      ;;
  esac
}

__modash_builtin_source_command_index() {
  __modash_source_command_index=-1
  local index=0
  if [[ ${__modash_source_call_args[0]-} == -- ]]; then
    index=1
  fi
  case "${__modash_source_call_args[$index]-}" in
    source|.)
      __modash_source_command_index=$index
      return 0
      ;;
  esac
  return 1
}

__modash_command_source_command_index() {
  __modash_source_command_index=-1
  local index=0 option letters
  while ((index < ${#__modash_source_call_args[@]})); do
    option=${__modash_source_call_args[$index]}
    if [[ $option == -- ]]; then
      ((index++))
      break
    fi
    if [[ $option != -* ]]; then
      break
    fi
    letters=${option#-}
    if [[ -z $letters || $letters == *v* || $letters == *V* || ${letters//p/} != "" ]]; then
      return 1
    fi
    ((index++))
  done
  case "${__modash_source_call_args[$index]-}" in
    source|.)
      __modash_source_command_index=$index
      return 0
      ;;
  esac
  return 1
}

__modash_trace_source_common() {
  local prior_status=$1 kind=$2 builtin_name=$3
  shift 3

  local event_index
  event_index=$(__modash_next_source_index)

  local caller_file caller_line cwd source_path resolved_path status source_arg_count track_functions
  local source_size="" source_mtime_ns="" source_sha256=""
  local after_source_size="" after_source_mtime_ns="" after_source_sha256=""
  local function_name function_definition
  local -a source_args explicit_source_args
  local -A function_definitions_before
  source_args=("$@")
  source_arg_count=${#source_args[@]}
  caller_file=$(__modash_current_source_file "${BASH_SOURCE[2]:-}" "${FUNCNAME[2]:-}")
  caller_line=${BASH_LINENO[1]:-1}
  cwd=$PWD
  source_path=${source_args[0]-}
  resolved_path=$(__modash_resolve_source_path "$source_path")
  track_functions=0

  if ! __modash_source_builtin_enabled "$builtin_name"; then
    __modash_trace_abort \
      "runtime.trace.disabled-source-builtin" \
      "modash: runtime trace cannot observe disabled ${builtin_name} builtin"
  fi

  if ! [[ $caller_line =~ ^[0-9]+$ ]] || ((caller_line < 1)); then
    __modash_trace_abort \
      "runtime.trace.untrusted-call-site" \
      "modash: runtime trace cannot identify a stable source call site for ${source_path}"
  fi

  if ((source_arg_count == 1)); then
    if ((__modash_caller_positionals_captured == 0)); then
      __modash_trace_abort \
        "runtime.trace.nontransparent-source" \
        "modash: runtime trace cannot transparently observe source after its tracing alias was removed: ${source_path}"
    fi
    if __modash_source_may_mutate_positionals "$resolved_path"; then
      __modash_trace_abort \
        "runtime.trace.nontransparent-source-positionals" \
        "modash: runtime trace cannot transparently observe no-argument source of a file that may mutate caller positionals: ${resolved_path}"
    fi
  elif __modash_source_may_assign_positionals "$resolved_path"; then
    __modash_trace_abort \
      "runtime.trace.nontransparent-source-positionals" \
      "modash: runtime trace cannot transparently observe explicit-argument source of a file that may assign caller positionals: ${resolved_path}"
  fi

  if [[ -n $source_path && -r $resolved_path ]]; then
    track_functions=1
    if [[ -f $resolved_path ]]; then
      if ! __modash_fingerprint_file "$resolved_path"; then
        __modash_trace_abort \
          "runtime.trace.fingerprint-source" \
          "modash: runtime trace could not fingerprint sourced file: ${resolved_path}"
      fi
      source_size=${__modash_source_fingerprint[0]}
      source_mtime_ns=${__modash_source_fingerprint[1]}
      source_sha256=${__modash_source_fingerprint[2]}
    fi
    while IFS= read -r function_name; do
      [[ -n $function_name ]] || continue
      function_definition=$(declare -f "$function_name")
      function_definitions_before["$function_name"]=$function_definition
    done < <(compgen -A function)
  fi

  if [[ -n $source_path ]]; then
    __modash_source_file_map["$source_path"]=$resolved_path
    __modash_source_file_map["$resolved_path"]=$resolved_path
    __modash_source_stack+=("$resolved_path")
  fi

  if [[ -n $source_path && ! -e $resolved_path && ! -L $resolved_path ]]; then
    builtin printf '%s: line %s: %s: No such file or directory\n' "$caller_file" "$caller_line" "$source_path" >&2
    status=1
  elif ((source_arg_count == 0)); then
    builtin printf '%s: line %s: %s: filename argument required\n' "$caller_file" "$caller_line" "$builtin_name" >&2
    builtin printf '%s: usage: %s filename [arguments]\n' "$builtin_name" "$builtin_name" >&2
    status=2
  elif [[ -n $source_path && -d $resolved_path ]]; then
    builtin printf '%s: line %s: %s: %s: is a directory\n' "$caller_file" "$caller_line" "$builtin_name" "$source_path" >&2
    status=1
  elif [[ -n $source_path && -e $resolved_path && ! -r $resolved_path ]]; then
    builtin printf '%s: line %s: %s: Permission denied\n' "$caller_file" "$caller_line" "$source_path" >&2
    status=1
  elif ((source_arg_count == 1)); then
    if ((${#__modash_caller_positionals[@]} > 0)); then
      if ((prior_status == 0)); then
        builtin "$builtin_name" "$source_path" "${__modash_caller_positionals[@]}"
      else
        ( exit "$prior_status" ) || builtin "$builtin_name" "$source_path" "${__modash_caller_positionals[@]}"
      fi
    else
      set --
      if ((prior_status == 0)); then
        builtin "$builtin_name" "$source_path"
      else
        ( exit "$prior_status" ) || builtin "$builtin_name" "$source_path"
      fi
    fi
    status=$?
  else
    if ((prior_status == 0)); then
      builtin "$builtin_name" "${source_args[@]}"
    else
      ( exit "$prior_status" ) || builtin "$builtin_name" "${source_args[@]}"
    fi
    status=$?
  fi

  if [[ -n $source_sha256 ]]; then
    if ! __modash_fingerprint_file "$resolved_path"; then
      __modash_trace_abort \
        "runtime.trace.mutated-source" \
        "modash: runtime trace source changed or disappeared while being sourced: ${resolved_path}"
    fi
    after_source_size=${__modash_source_fingerprint[0]}
    after_source_mtime_ns=${__modash_source_fingerprint[1]}
    after_source_sha256=${__modash_source_fingerprint[2]}
    if [[ $source_size != "$after_source_size" || $source_mtime_ns != "$after_source_mtime_ns" || $source_sha256 != "$after_source_sha256" ]]; then
      __modash_trace_abort \
        "runtime.trace.mutated-source" \
        "modash: runtime trace source changed while being sourced: ${resolved_path}"
    fi
  fi

  if ((track_functions)); then
    __modash_forget_deleted_sourced_functions function_definitions_before
    __modash_record_sourced_functions "$resolved_path"
  fi

  if [[ -n $source_path ]]; then
    unset '__modash_source_stack[-1]'
  fi

  if ((source_arg_count > 0)) || ((status == 2)); then
    if ((source_arg_count > 1)); then
      explicit_source_args=("${source_args[@]:1}")
      __modash_emit_source_event_with_stack \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$source_size" "$source_mtime_ns" "$source_sha256" "$status" "$prior_status" \
        "${explicit_source_args[@]}"
    else
      __modash_emit_source_event_with_stack \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$source_size" "$source_mtime_ns" "$source_sha256" "$status" "$prior_status"
    fi
  fi

  return "$status"
}

__modash_trace_source_alias() {
  local prior_status=$?
  local kind=$1 builtin_name=$2 caller_count=$3
  shift 3
  __modash_capture_source_call "$caller_count" "$@"
  __modash_trace_source_common "$prior_status" "$kind" "$builtin_name" "${__modash_source_call_args[@]}"
}

source() {
  local prior_status=$?
  __modash_caller_positionals=()
  __modash_caller_positionals_captured=0
  __modash_trace_source_common "$prior_status" source source "$@"
}

__modash_trace_builtin() {
  local prior_status=$?
  local caller_count=$1
  shift
  __modash_capture_source_call "$caller_count" "$@"
  local builtin_name
  if __modash_builtin_source_command_index; then
    builtin_name=${__modash_source_call_args[$__modash_source_command_index]}
    case "$builtin_name" in
      source)
        __modash_trace_source_common "$prior_status" source source "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
      .)
        __modash_trace_source_common "$prior_status" dot . "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
    esac
    return
  fi
  builtin_name=${__modash_source_call_args[0]-}
  case "$builtin_name" in
    source)
      __modash_trace_source_common "$prior_status" source source "${__modash_source_call_args[@]:1}"
      ;;
    .)
      __modash_trace_source_common "$prior_status" dot . "${__modash_source_call_args[@]:1}"
      ;;
    *)
      builtin "${__modash_source_call_args[@]}"
      ;;
  esac
}

__modash_trace_command() {
  local prior_status=$?
  local caller_count=$1
  shift
  __modash_capture_source_call "$caller_count" "$@"
  local command_name
  if __modash_command_source_command_index; then
    command_name=${__modash_source_call_args[$__modash_source_command_index]}
    case "$command_name" in
      source)
        __modash_trace_source_common "$prior_status" source source "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
      .)
        __modash_trace_source_common "$prior_status" dot . "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
    esac
    return
  fi
  command_name=${__modash_source_call_args[0]-}
  case "$command_name" in
    source)
      __modash_trace_source_common "$prior_status" source source "${__modash_source_call_args[@]:1}"
      ;;
    .)
      __modash_trace_source_common "$prior_status" dot . "${__modash_source_call_args[@]:1}"
      ;;
    bash|/bin/bash|/usr/bin/bash)
      __modash_trace_run_child_bash "${__modash_source_call_args[@]}"
      ;;
    *)
      command "${__modash_source_call_args[@]}"
      ;;
  esac
}

__modash_trace_run_child_bash() {
  BASH_ENV=$BASH_ENV \
  MODASH_TRACE_ENTRYPOINT=$MODASH_TRACE_ENTRYPOINT \
  MODASH_TRACE_INITIAL_CWD=$MODASH_TRACE_INITIAL_CWD \
  MODASH_TRACE_FILE=$MODASH_TRACE_FILE \
  MODASH_TRACE_COUNTER_FILE=$MODASH_TRACE_COUNTER_FILE \
  MODASH_TRACE_XTRACE_FILE=$MODASH_TRACE_XTRACE_FILE \
  MODASH_TRACE_FAILURE_FILE=$MODASH_TRACE_FAILURE_FILE \
  MODASH_TRACE_POSITIONAL_SCANNER=$MODASH_TRACE_POSITIONAL_SCANNER \
  MODASH_TRACE_FUNCTION_SCANNER=$MODASH_TRACE_FUNCTION_SCANNER \
  MODASH_TRACE_FINGERPRINT_SCANNER=$MODASH_TRACE_FINGERPRINT_SCANNER \
  MODASH_TRACE_PYTHON=$MODASH_TRACE_PYTHON \
  MODASH_TRACE_MKDIR=$MODASH_TRACE_MKDIR \
  MODASH_TRACE_RMDIR=$MODASH_TRACE_RMDIR \
  MODASH_TRACE_SLEEP=$MODASH_TRACE_SLEEP \
  command "$@"
}

bash() {
  local prior_status=$?
  ( exit "$prior_status" )
  __modash_trace_run_child_bash bash "$@"
}

__modash_env_launches_child_bash() {
  local index=0 word
  local -a args=("$@")
  while ((index < ${#args[@]})); do
    word=${args[$index]}
    case "$word" in
      -u|--unset)
        ((index += 2))
        continue
        ;;
      --unset=*)
        ((index++))
        continue
        ;;
      -i|--ignore-environment|-0|--null)
        ((index++))
        continue
        ;;
      -*)
        return 1
        ;;
      *=*)
        ((index++))
        continue
        ;;
    esac
    case "$word" in
      bash|/bin/bash|/usr/bin/bash)
        return 0
        ;;
    esac
    return 1
  done
  return 1
}

env() {
  local prior_status=$?
  ( exit "$prior_status" )
  if __modash_env_launches_child_bash "$@"; then
    __modash_trace_run_child_bash env "$@"
    return
  fi
  command env "$@"
}

export -n \
  BASH_ENV \
  MODASH_TRACE_ENTRYPOINT \
  MODASH_TRACE_INITIAL_CWD \
  MODASH_TRACE_FILE \
  MODASH_TRACE_COUNTER_FILE \
  MODASH_TRACE_XTRACE_FILE \
  MODASH_TRACE_FAILURE_FILE \
  MODASH_TRACE_POSITIONAL_SCANNER \
  MODASH_TRACE_FUNCTION_SCANNER \
  MODASH_TRACE_FINGERPRINT_SCANNER \
  MODASH_TRACE_PYTHON \
  MODASH_TRACE_MKDIR \
  MODASH_TRACE_RMDIR \
  MODASH_TRACE_SLEEP

alias source='__modash_trace_source_alias source source "$#" "$@" --'
alias .='__modash_trace_source_alias dot . "$#" "$@" --'
alias builtin='__modash_trace_builtin "$#" "$@" --'
alias command='__modash_trace_command "$#" "$@" --'
shopt -s expand_aliases
set -x
