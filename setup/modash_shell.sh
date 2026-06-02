#!/bin/bash

ALLOWED_COMMANDS=(
    "ls"
    "cd"
    "pwd"
    "echo"
    "cat"
    "grep"
    "head"
    "tail"
    "whoami"
    "date"
    "df"
    "du"
    "ps"
    "dirname"
    "basename"
    "realpath"
    "readlink"
    "stat"
    "which"
    "type"
    "id"
    "printenv"
    "cut"
    "tr"
    "sed"
    "sort"
    "uniq"
    "wc"
    "whereis"
    "source"
    "."
    "printf"
    "test"
    "["
    "[["
    "true"
    "false"
    ":"
    "return"
    "exit"
    "set"
    "shift"
    "local"
    "declare"
    "export"
    "readonly"
    "unset"
)

declare -A ALLOWED_COMMAND_MAP=()
for allowed_cmd in "${ALLOWED_COMMANDS[@]}"; do
    ALLOWED_COMMAND_MAP["$allowed_cmd"]=1
done

RESTRICTED_PATH=""
NEXT_WORD=""
NEXT_REST=""

cleanup_restricted_path() {
    trap - DEBUG
    if [[ -n "$RESTRICTED_PATH" ]]; then
        /bin/rm -rf "$RESTRICTED_PATH"
    fi
}

command_is_allowed() {
    local cmd=$1
    local cmd_type

    if [[ -n "${ALLOWED_COMMAND_MAP[$cmd]+allowed}" ]]; then
        return 0
    fi

    cmd_type=$(type -t -- "$cmd" 2>/dev/null || true)
    [[ "$cmd_type" == "function" || "$cmd_type" == "keyword" ]]
}

execute_command() {
    local cmd=$1
    shift

    if command_is_allowed "$cmd"; then
        "$cmd" "$@"
    else
        echo "Command not allowed: $cmd" >&2
        return 126
    fi
}

create_restricted_path() {
    local restricted_path
    local allowed_cmd
    local command_path

    restricted_path=$(mktemp -d "${TMPDIR:-/tmp}/modash-path.XXXXXX")
    for allowed_cmd in "${ALLOWED_COMMANDS[@]}"; do
        command_path=$(type -P -- "$allowed_cmd" 2>/dev/null || true)
        if [[ -n "$command_path" ]]; then
            ln -s "$command_path" "$restricted_path/$allowed_cmd"
        fi
    done

    printf '%s\n' "$restricted_path"
}

read_next_word() {
    local line=$1
    local index=0
    local length=${#line}
    local char
    local quote=""
    local escaped=0
    local word
    local backslash=$'\\'

    NEXT_WORD=""
    NEXT_REST=""

    while (( index < length )); do
        char=${line:index:1}
        if [[ ! "$char" =~ [[:space:]] ]]; then
            break
        fi
        ((index++))
    done

    while (( index < length )); do
        char=${line:index:1}

        if (( escaped )); then
            word+="$char"
            escaped=0
            ((index++))
            continue
        fi

        if [[ "$char" == "$backslash" && "$quote" != "'" ]]; then
            word+="$char"
            escaped=1
            ((index++))
            continue
        fi

        if [[ -z "$quote" ]]; then
            case "$char" in
                "'" | '"')
                    quote=$char
                    word+="$char"
                    ;;
                ' ' | $'\t' | ';' | '|' | '&' | '<' | '>')
                    break
                    ;;
                *)
                    word+="$char"
                    ;;
            esac
        elif [[ "$char" == "$quote" ]]; then
            quote=""
            word+="$char"
        else
            word+="$char"
        fi

        ((index++))
    done

    NEXT_WORD=$word
    NEXT_REST=${line:index}
}

is_assignment_word() {
    local word=$1
    [[ "$word" =~ ^[A-Za-z_][A-Za-z0-9_]*(\[[^]]+\])?(\+)?= ]]
}

extract_command_name() {
    local line=$1

    if [[ "$line" =~ ^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*(\[[^]]+\])?(\+)?=\( ]]; then
        return 1
    fi

    while true; do
        read_next_word "$line"
        if [[ -z "$NEXT_WORD" ]]; then
            return 1
        fi

        if ! is_assignment_word "$NEXT_WORD"; then
            printf '%s\n' "$NEXT_WORD"
            return 0
        fi

        line=$NEXT_REST
    done
}

restrict_command() {
    local cmd

    cmd=$(extract_command_name "$BASH_COMMAND" || true)
    if [[ -z "$cmd" ]]; then
        return 0
    fi

    if [[ "$cmd" == */* ]] || ! command_is_allowed "$cmd"; then
        echo "Command not allowed: $cmd" >&2
        if [[ "$BASHPID" -ne "$$" ]]; then
            kill -USR1 "$$"
        fi
        exit 126
    fi
}

run_script() {
    local script_path=$1
    shift

    if [[ ! -f "$script_path" ]]; then
        echo "Error: The script does not exist at '$script_path'." >&2
        return 1
    fi

    RESTRICTED_PATH=$(create_restricted_path)
    trap cleanup_restricted_path EXIT
    trap 'exit 126' USR1

    PATH="$RESTRICTED_PATH"
    readonly PATH
    shopt -s extdebug
    trap restrict_command DEBUG

    # shellcheck source=/dev/null
    source "$script_path" "$@"
}

run_interactive() {
    local words

    RESTRICTED_PATH=$(create_restricted_path)
    trap cleanup_restricted_path EXIT
    PATH="$RESTRICTED_PATH"
    readonly PATH

    while read -r -a words -p 'restricted$ '; do
        if [[ ${#words[@]} -eq 0 ]]; then
            continue
        fi
        execute_command "${words[0]}" "${words[@]:1}"
    done
}

if [[ $# -gt 0 ]]; then
    run_script "$@"
else
    run_interactive
fi
