from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from methods.runtime_evaluator.compiler_model import RuntimeObservedCompileError, _CompilePlan
from methods.runtime_evaluator.scanners import function_context_sensitive_top_level_lines, inert_function_body_lines
from methods.shell.line import get_commands
from methods.shell.scan import is_array_assignment_paren, read_backtick_body, read_balanced_body
from methods.source_commands import source_command_invocation
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    UnsupportedSourceError,
    extract_heredoc_delimiters,
    is_heredoc_end,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

RESERVED_RUNTIME_PREFIX = "__modash_"
SHELL_COMMAND_PREFIXES = {"if", "then", "elif", "else", "do", "while", "until", "for", "time", "!"}
TRACE_VARIABLES = {
    "MODASH_TRACE_FILE",
    "MODASH_TRACE_COUNTER_FILE",
    "MODASH_TRACE_XTRACE_FILE",
    "MODASH_TRACE_FAILURE_FILE",
    "MODASH_TRACE_POSITIONAL_SCANNER",
    "MODASH_TRACE_FUNCTION_SCANNER",
    "MODASH_TRACE_FINGERPRINT_SCANNER",
    "MODASH_TRACE_PYTHON",
    "MODASH_TRACE_MKDIR",
    "MODASH_TRACE_RMDIR",
    "MODASH_TRACE_SLEEP",
    "BASH_EXECUTION_STRING",
}
INSTRUMENTATION_VARIABLES = {"BASH_ENV", "BASH_XTRACEFD", "PS4", "SHELLOPTS", "BASHOPTS", "BASH_ALIASES", *TRACE_VARIABLES}
DECLARATION_COMMANDS = {"declare", "typeset", "local", "export", "readonly"}
DYNAMIC_STATE_COMMANDS = {"unset", "read", "mapfile", "readarray", "let", "((", *DECLARATION_COMMANDS}
REPLAY_CRITICAL_FUNCTIONS = {"source", ".", "eval", "exec", "trap", "kill", "command", "builtin", "shopt", "env", "exit", "enable"}
SHELL_C_COMMANDS = {"bash", "sh", "dash", "ksh", "ksh93", "mksh", "zsh", "ash", "busybox"}
EXTERNAL_ENV_PROBE_COMMANDS = {"python", "python3", "perl", "ruby", "node", "php", "lua"}
SAFE_LITERAL_EVAL_COMMANDS = {"echo", ":"}


@dataclass(frozen=True)
class _Command:
    name: str
    words: tuple[str, ...]
    raw_words: tuple[str, ...]


def validate_runtime_compile_safety(plan: _CompilePlan) -> None:
    exact_environment = _exact_environment_values(plan)
    inert_source_paths = _inert_source_file_paths(plan)
    trusted_source_lines = _trusted_source_lines(plan)
    for unit in plan.file_units.values():
        _validate_unit_text(
            unit.physical_path or unit.logical_path,
            unit.content,
            exact_environment,
            skip_inert_function_bodies=unit.physical_path in inert_source_paths,
            trusted_source_lines=trusted_source_lines.get(unit.physical_path or "", frozenset()),
        )
    for process_plan in plan.process_plans.values():
        for unit in process_plan.units.values():
            if unit.physical_path is None:
                _validate_unit_text(unit.logical_path, unit.content, exact_environment)


def validate_graph_source_file_safety(graph: dict) -> None:
    source_paths = {
        fingerprint["path"]
        for fingerprint in graph["files"]
        if "source" in fingerprint["roles"]
    }
    for path in sorted(source_paths):
        lines = function_context_sensitive_top_level_lines(path)
        if not lines:
            continue
        raise RuntimeObservedCompileError(
            "runtime source graph cannot trust a sourced file with top-level "
            f"function-context-sensitive Bash at {path}:{lines[0]}",
            code="runtime.graph.function_context_sensitive",
        )


def _exact_environment_values(plan: _CompilePlan) -> dict[str, str]:
    values = plan.graph.get("environment", {}).get("values", {})
    if not isinstance(values, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in values.items()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key))
    }


def _inert_source_file_paths(plan: _CompilePlan) -> set[str]:
    paths: set[str] = set()
    for fingerprint in plan.graph.get("files", ()):
        roles = set(fingerprint.get("roles", ()))
        if "source" in roles:
            paths.add(str(fingerprint.get("path", "")))
    return paths


def _trusted_source_lines(plan: _CompilePlan) -> dict[str, frozenset[int]]:
    lines_by_path: dict[str, set[int]] = {}
    for edge in plan.graph.get("edges", ()):
        call_site = edge.get("call_site", {})
        path = call_site.get("file")
        line = call_site.get("line")
        if isinstance(path, str) and isinstance(line, int):
            lines_by_path.setdefault(path, set()).add(line)
    return {path: frozenset(lines) for path, lines in lines_by_path.items()}


def _validate_unit_text(
    label: str,
    content: str,
    exact_environment: dict[str, str] | None = None,
    *,
    skip_inert_function_bodies: bool = False,
    trusted_source_lines: frozenset[int] = frozenset(),
) -> None:
    if RESERVED_RUNTIME_PREFIX in content:
        raise RuntimeObservedCompileError(
            f"runtime graph compiler input uses reserved generated namespace {RESERVED_RUNTIME_PREFIX!r}: {label}",
            code="runtime.compile.reserved_namespace",
        )

    shopt_restore_scopes: list[set[str]] = [set()]
    exact_variable_scopes: list[dict[str, str]] = [dict(exact_environment or {})]
    has_exit = False
    for line_number, line, inert_function_body in _code_lines(
        content,
        label,
        skip_inert_function_bodies=skip_inert_function_bodies,
        trusted_source_lines=trusted_source_lines,
    ):
        if _contains_coproc_command(line):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler input uses unsupported coproc execution at {label}:{line_number}",
                code="runtime.compile.dynamic_command",
            )
        if _contains_mapfile_callback(line):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler input uses mapfile/readarray callbacks at {label}:{line_number}",
                code="runtime.compile.dynamic_state",
            )
        if _defines_replay_critical_function(line):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler input redefines replay-critical shell functions at {label}:{line_number}",
                code="runtime.compile.dynamic_state",
            )
        if skip_inert_function_bodies:
            one_line_body = _one_line_function_body(line)
            one_line_body_code = (
                _inert_function_body_bypass_code(one_line_body)
                if one_line_body is not None else None
            )
            if one_line_body_code is not None:
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses replay-critical function body dispatch at {label}:{line_number}",
                    code=one_line_body_code,
                )
        if inert_function_body:
            inert_body_code = _inert_function_body_bypass_code(line)
            if inert_body_code is not None:
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses replay-critical function body dispatch at {label}:{line_number}",
                    code=inert_body_code,
                )
            continue
        if _opens_simple_scope(line):
            shopt_restore_scopes.append(set())
            exact_variable_scopes.append({})
        for command in get_commands(line):
            parsed = _parse_command(command)
            name = parsed.name
            if _closes_simple_scope(name):
                if len(shopt_restore_scopes) > 1:
                    shopt_restore_scopes.pop()
                if len(exact_variable_scopes) > 1:
                    exact_variable_scopes.pop()
                continue
            _update_safe_shopt_restore_scopes(parsed, shopt_restore_scopes)
            _update_exact_variable_scopes(parsed, exact_variable_scopes)
            exact_variables = _merged_exact_variables(exact_variable_scopes)
            effective = _command_with_exact_dynamic_name(parsed, exact_variables)
            if _instrumentation_sensitive(effective):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input observes instrumentation-sensitive shell state at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            if effective.name == "alias":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses aliases under trace instrumentation at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            if (
                effective.name == "eval"
                and not _is_shopt_restore_eval(effective.words, shopt_restore_scopes[-1])
                and not _is_safe_literal_eval(effective.words, effective.raw_words)
            ):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic eval at {label}:{line_number}",
                    code="runtime.compile.dynamic_eval",
                )
            if (
                source_command_invocation(command, stop_at_shell_control=True) is None
                and (
                    _dynamic_validation_bypass_command(parsed, exact_variables)
                    or _dynamic_validation_bypass_text(command, exact_variables)
                )
            ):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic validation-bypassing command dispatch at {label}:{line_number}",
                    code="runtime.compile.dynamic_command",
                )
            if effective.name == "exec" and _words_have_source_bearing_shell_payload(command):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses exec with a source-bearing shell payload: {label}:{line_number}",
                    code="runtime.compile.dynamic_command",
                )
            if effective.name == "trap":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input manipulates shell traps: {label}:{line_number}",
                    code="runtime.compile.exit_trap",
                )
            if effective.name == "enable":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input manipulates shell builtins: {label}:{line_number}",
                    code="runtime.compile.dynamic_state",
                )
            if _kill_bypasses_generated_guard(parsed.words):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input can bypass replay validation with unguarded kill: {label}:{line_number}",
                    code="runtime.compile.dynamic_command",
                )
            if _dynamic_state_mutation(effective, in_simple_scope=len(exact_variable_scopes) > 1):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic shell state mutation at {label}:{line_number}",
                    code="runtime.compile.dynamic_state",
                )
            if name == "exit":
                has_exit = True
            if _opens_control_scope(parsed.words):
                shopt_restore_scopes.append(set())
                exact_variable_scopes.append({})

    if has_exit:
        # Explicit exit is safe only while the generated EXIT trap remains intact.
        # EXIT trap manipulation is rejected above.
        return


def _code_lines(
    content: str,
    label: str,
    *,
    skip_inert_function_bodies: bool = False,
    trusted_source_lines: frozenset[int] = frozenset(),
):
    inert_body_lines = (
        inert_function_body_lines(label, content, active_lines=trusted_source_lines)
        if skip_inert_function_bodies else frozenset()
    )
    active_heredocs = []
    inert_heredocs = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if inert_heredocs:
            if is_heredoc_end(line, inert_heredocs[0]):
                inert_heredocs.pop(0)
            continue
        if active_heredocs:
            delimiter, scan_body = active_heredocs[0]
            if is_heredoc_end(line, delimiter):
                active_heredocs.pop(0)
            elif scan_body and _heredoc_body_introspects_instrumented_environment(line):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input observes instrumentation-sensitive shell state at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            elif not delimiter.quoted and _scan_shell_fragments_for_dynamic_bypass(line, {}, depth=0):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic validation-bypassing heredoc expansion at {label}:{line_number}",
                    code="runtime.compile.dynamic_command",
                )
            continue
        inert_body_line = line_number in inert_body_lines
        yield line_number, line, inert_body_line
        if not inert_body_line:
            scan_body = _line_has_external_interpreter_heredoc(line)
            active_heredocs.extend((delimiter, scan_body) for delimiter in extract_heredoc_delimiters(line))
        else:
            inert_heredocs.extend(extract_heredoc_delimiters(line))


def _update_safe_shopt_restore_scopes(command: _Command, scopes: list[set[str]]) -> None:
    persistent_restore_names = _persistent_shopt_restore_assignment_names(command)
    for raw_word, word in zip(command.raw_words, command.words):
        name = _assignment_name(word)
        if name is None:
            continue
        if name in persistent_restore_names:
            scopes[-1].add(name)
        else:
            for safe_variables in scopes:
                safe_variables.discard(name)


def _update_exact_variable_scopes(command: _Command, scopes: list[dict[str, str]]) -> None:
    for raw_word, word in zip(command.raw_words, command.words):
        name = _assignment_name(word)
        if name is None:
            continue
        value = word.split("=", 1)[1]
        raw_value = raw_word.split("=", 1)[1] if "=" in raw_word else ""
        if _exact_assignment_value_is_safe(value) and (raw_word == word or _quoted_literal_assignment_value_is_safe(raw_value)):
            scopes[-1][name] = value
        else:
            for variables in scopes:
                variables.pop(name, None)


def _merged_exact_variables(scopes: list[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for scope in scopes:
        merged.update(scope)
    return merged


def _exact_assignment_value_is_safe(value: str) -> bool:
    return bool(value) and not _has_dynamic_shell_expansion(value) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)


def _quoted_literal_assignment_value_is_safe(raw_value: str) -> bool:
    if _has_dynamic_shell_expansion(raw_value):
        return False
    return bool(re.fullmatch(r"'[^']*'|\"[^\"]*\"", raw_value))


def _opens_simple_scope(line: str) -> bool:
    return bool(re.search(r"(^|[;&|])\s*(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*(?:\(\s*\))?\s*\{", line))


def _one_line_function_body(line: str) -> str | None:
    if not _opens_simple_scope(line):
        return None
    start = line.find("{")
    end = line.rfind("}")
    if start < 0 or end <= start:
        return None
    return line[start + 1:end]


def _contains_coproc_command(line: str) -> bool:
    return bool(re.search(r"(^|[;&|({]|\bthen\b|\bdo\b)\s*coproc\b", line))


def _contains_mapfile_callback(line: str) -> bool:
    for command in get_commands(line):
        parsed = _parse_command(command)
        for index, word in enumerate(parsed.words):
            if word in {"mapfile", "readarray"} and _mapfile_has_callback(parsed.words[index:]):
                return True
    return False


def _opens_control_scope(words: tuple[str, ...]) -> bool:
    return bool(words) and words[0] in {"if", "for", "while", "until", "case"}


def _closes_simple_scope(name: str) -> bool:
    return name in {"}", "fi", "done", "esac"}


def _assignment_name(word: str) -> str | None:
    if not ASSIGNMENT_WORD_PATTERN.match(word):
        return None
    return word.split("=", 1)[0]


def _persistent_shopt_restore_assignment_names(command: _Command) -> set[str]:
    words = command.words
    raw_words = command.raw_words
    if not words:
        return set()
    if command.name == "":
        candidates = zip(raw_words, words)
    elif command.name in {"local", "declare", "typeset"}:
        candidates = (
            (raw_word, word)
            for raw_word, word in zip(raw_words[1:], words[1:])
            if not word.startswith("-")
        )
    else:
        return set()
    names = set()
    for raw_word, word in candidates:
        if _is_shopt_restore_assignment(raw_word, word):
            names.add(word.split("=", 1)[0])
    return names


def _is_shopt_restore_assignment(raw_word: str, word: str) -> bool:
    name = _assignment_name(word)
    if name is None:
        return False
    value = word.split("=", 1)[1]
    return bool(re.fullmatch(r"\$\(shopt\s+-p\b[^)]*\)", value)) and raw_word == word


def _is_shopt_restore_eval(words: tuple[str, ...], variables: set[str]) -> bool:
    if len(words) != 2:
        return False
    variable = _variable_reference_name(words[1])
    return variable in variables


def _is_safe_literal_eval(words: tuple[str, ...], raw_words: tuple[str, ...] | None = None) -> bool:
    raw_words = raw_words or words
    command_index = _effective_command_index_without_unwrap(words)
    if command_index is None or words[command_index] != "eval":
        return False
    if len(words) != command_index + 2:
        if command_index + 1 >= len(words):
            return False
        evaluated_command = words[command_index + 1]
        if evaluated_command not in SAFE_LITERAL_EVAL_COMMANDS or _has_dynamic_shell_expansion(evaluated_command):
            return False
        return all(
            _literal_eval_argument_is_safe(raw_word, word)
            for raw_word, word in zip(raw_words[command_index + 2:], words[command_index + 2:])
        )
    evaluated_command = words[command_index + 1]
    if evaluated_command not in SAFE_LITERAL_EVAL_COMMANDS:
        return False
    return not _has_dynamic_shell_expansion(evaluated_command)


def _literal_eval_argument_is_safe(raw_word: str, word: str) -> bool:
    if re.search(r"[;&|<>`]", raw_word) or "$(" in raw_word or "<(" in raw_word or ">(" in raw_word:
        return False
    if raw_word.startswith(r"\${") and raw_word.endswith("}"):
        inner = raw_word[3:-1]
        if any(operator in inner for operator in ("@", "!", ":", "#", "%", "/", "^", ",", "[", "]")):
            return False
        return True
    return not _has_dynamic_shell_expansion(raw_word) and not _has_dynamic_shell_expansion(word)


def _variable_reference_name(word: str) -> str | None:
    match = re.fullmatch(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})", word)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _is_simple_parameter_reference(word: str) -> bool:
    return _variable_reference_name(word) is not None or bool(re.fullmatch(r"\$(?:[0-9]|\{[0-9]+\})", word))


def _is_positional_parameter_reference(word: str) -> bool:
    return bool(re.fullmatch(r"\$(?:[0-9]|\{[0-9]+\})", word))


def _parse_command(command: str) -> _Command:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return _Command("", (), ())
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in SHELL_COMMAND_PREFIXES:
        index += 1
    if index < len(words) and words[index] in {"command", "builtin"}:
        index = _unwrap_command_or_builtin(words, index)
    name = words[index] if index < len(words) else ""
    return _Command(name, words, tuple(raw_words))


def _defines_replay_critical_function(line: str) -> bool:
    names = r"(?:source|\.|eval|exec|trap|kill|command|builtin|shopt|env|exit|enable)"
    return bool(
        re.search(rf"(^|[;&|])\s*(?:function\s+)?{names}\s*(?:\(\s*\))?\s*\{{", line)
        or re.search(rf"(^|[;&|])\s*{names}\s*\(\s*\)\s*\{{", line)
    )


def _inert_function_body_can_bypass_replay(line: str) -> bool:
    return _inert_function_body_bypass_code(line) is not None


def _inert_function_body_bypass_code(line: str) -> str | None:
    if re.search(r"(^|[;&|{])\s*(?:builtin|command)\s+(?:source|\.)\b", line):
        return "runtime.compile.dynamic_command"
    if _words_have_source_bearing_shell_payload(line):
        return "runtime.compile.dynamic_command"
    if _inert_function_literal_mentions_generated_replay(line):
        return "runtime.compile.dynamic_command"
    for command in get_commands(line):
        parsed = _parse_command(command)
        if not parsed.words:
            continue
        command_index = _effective_command_index_without_unwrap(parsed.words)
        if command_index is None:
            continue
        if parsed.name in {"builtin", "command", "eval", "exec", "trap", "enable"}:
            return "runtime.compile.dynamic_command"
        if _dynamic_state_mutation(parsed, in_simple_scope=True):
            return "runtime.compile.dynamic_state"
        if _contains_mapfile_callback(command):
            return "runtime.compile.dynamic_state"
        if _dynamic_inert_command_can_become_source(parsed.words):
            return "runtime.compile.dynamic_command"
    return None


def _dynamic_inert_command_can_become_source(words: tuple[str, ...]) -> bool:
    command_index = _effective_command_index_without_unwrap(words)
    if command_index is None:
        return False
    name = words[command_index]
    if not _has_dynamic_shell_expansion(name):
        return False
    tail = words[command_index + 1:]
    if any(_dynamic_helper_literal_can_bypass_replay(word) for word in tail):
        return True
    return _dynamic_tail_can_name_source_command(tail)


def _dynamic_tail_can_name_source_command(words: tuple[str, ...]) -> bool:
    for index, word in enumerate(words):
        if word in {"|", "||", "&&", ";", ")", "}", "then", "do", "fi", "done"}:
            break
        if not _has_dynamic_shell_expansion(word):
            continue
        name = _variable_reference_name(word)
        if name and re.search(r"(?:^|_)(?:CMD|COMMAND|SOURCE|BUILTIN|EVAL)(?:$|_)", name):
            return True
        if index + 1 < len(words) and _looks_like_source_path(words[index + 1]):
            return True
    return False


def _looks_like_source_path(word: str) -> bool:
    return bool(re.search(r"(?:^|/|\./|\.\./)[^;&|<>]*\.(?:sh|bash|inc|conf|env)(?:$|[^\w.-])", word))


def _inert_function_literal_mentions_generated_replay(text: str) -> bool:
    if "__modash" in text or "__entrypoint__" in text or "__process_command__" in text:
        return True
    if re.search(r"p[0-9]+:[^| \t;]+:[0-9]+:[0-9]+(?:\|[0-9]+)?", text):
        return True
    return False


def _unwrap_command_or_builtin(words: tuple[str, ...], index: int) -> int:
    command_name = words[index]
    index += 1
    if command_name == "builtin":
        return index
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1
        if word == "-p":
            index += 1
            continue
        if word.startswith("-"):
            return len(words)
        return index
    return index


def _instrumentation_sensitive(command: _Command) -> bool:
    words = command.words
    raw_words = command.raw_words
    if _uses_instrumentation_variable(words, raw_words):
        return True
    if _tests_xtrace_or_expand_aliases(command):
        return True
    if _introspects_instrumented_environment(command):
        return True
    if _introspects_replay_critical_command_resolution(command):
        return True
    if command.name in {"set", "shopt"} and any(word in {"-o", "+o", "-p"} for word in words[1:]):
        if "xtrace" in words or "expand_aliases" in words:
            return True
    return False


def _uses_instrumentation_variable(words: tuple[str, ...], raw_words: tuple[str, ...]) -> bool:
    for raw_word, word in zip(raw_words, words):
        if _is_single_quoted_word(raw_word):
            continue
        if (
            _mentions_proc_environment(word)
            or _mentions_trace_fd(raw_word)
            or _mentions_trace_fd(word)
            or _mentions_dynamic_fd_redirection(raw_word)
            or _mentions_dynamic_fd_redirection(word)
        ):
            return True
        assignment_name = _assignment_name(word)
        if assignment_name is not None:
            if assignment_name in INSTRUMENTATION_VARIABLES:
                return True
            continue
        if _uses_unsafe_indirect_expansion(word):
            return True
        if word in INSTRUMENTATION_VARIABLES:
            return True
        if any(variable in word for variable in INSTRUMENTATION_VARIABLES):
            return True
        if re.search(r"\$\-|\$\{\-\}", word):
            return True
        for variable in INSTRUMENTATION_VARIABLES:
            if re.search(rf"\$(?:{re.escape(variable)}|\{{{re.escape(variable)}\}})", word):
                return True
    return False


def _tests_xtrace_or_expand_aliases(command: _Command) -> bool:
    words = command.words
    if command.name in {"[[", "[", "test"} and _option_test_mentions(words, "xtrace"):
        return True
    if command.name == "shopt" and (
        ("expand_aliases" in words and any(word in {"-q", "-p"} for word in words[1:]))
        or ("xtrace" in words and "-o" in words)
    ):
        return True
    if command.name == "shopt" and any(word in {"-q", "-p"} for word in words[1:]):
        return any(_has_dynamic_shell_expansion(word) for word in words[1:] if not word.startswith("-"))
    if command.name == "set":
        for index, word in enumerate(words[1:], start=1):
            if word in {"-o", "+o"}:
                if index + 1 >= len(words):
                    return True
                option = words[index + 1]
                if option in {"|", "||", "&&", ";"}:
                    return True
                if option == "xtrace" or _has_dynamic_shell_expansion(option):
                    return True
    if command.name in {"[[", "[", "test"} and "-v" in words:
        index = words.index("-v")
        if index + 1 < len(words):
            target = words[index + 1]
            if target == "BASH_ENV" or _has_dynamic_shell_expansion(target):
                return True
    return False


def _introspects_instrumented_environment(command: _Command) -> bool:
    words = command.words
    if command.name in {"printenv", "/usr/bin/printenv", "/bin/printenv"}:
        return (
            len(words) == 1
            or "|" in words
            or any(_has_dynamic_shell_expansion(word) or word in INSTRUMENTATION_VARIABLES for word in words[1:])
        )
    if command.name in {"env", "/usr/bin/env", "/bin/env"} and (len(words) == 1 or "|" in words):
        return True
    if command.name in DECLARATION_COMMANDS and any(word in {"-p", "-F", "-f"} for word in words[1:]):
        return True
    if command.name == "compgen" and "-v" in words[1:]:
        return True
    if command.name == "set" and (len(words) == 1 or "|" in words):
        return True
    if command.name in EXTERNAL_ENV_PROBE_COMMANDS:
        if "-c" in words[1:] and any(_external_interpreter_payload_can_probe_environment(word) for word in words[1:]):
            return True
    return False


def _heredoc_body_introspects_instrumented_environment(line: str) -> bool:
    if any(variable in line for variable in INSTRUMENTATION_VARIABLES):
        return True
    return _external_interpreter_payload_can_probe_environment(line)


def _line_has_external_interpreter_heredoc(line: str) -> bool:
    for command in get_commands(line):
        parsed = _parse_command(command)
        if parsed.name in EXTERNAL_ENV_PROBE_COMMANDS and any(word.startswith("<<") for word in parsed.words[1:]):
            return True
    return False


def _external_interpreter_payload_can_probe_environment(word: str) -> bool:
    collapsed = re.sub(r"[\s\"'+]", "", word)
    return bool(
        re.search(r"/proc/(?:self|\$\$|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/(?:environ|cmdline|fd)(?:/|$)", word)
        or re.search(r"listdir\([^)]*[\"']/proc/(?:self|\$\$|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/fd[\"']", word)
        or "BASH_" in word
        or "MODASH_TRACE_" in word
        or "/proc/self/fd" in collapsed
        or "/proc/self/environ" in collapsed
        or "/proc/self/cmdline" in collapsed
        or "__modash_child_replay_marker" in collapsed
        or "__modash_child_replay_token" in collapsed
        or "child_replay_marker" in collapsed
        or "child_replay_token" in collapsed
    )


def _introspects_replay_critical_command_resolution(command: _Command) -> bool:
    words = command.words
    if not words:
        return False
    start = _effective_command_index_without_unwrap(words)
    if start is None:
        return False
    if words[start] == "type":
        return _resolution_probe_targets_replay_critical(words[start + 1:])
    if words[start] == "command":
        index = start + 1
        saw_resolution = False
        while index < len(words):
            word = words[index]
            if word == "--":
                index += 1
                break
            if word in {"-v", "-V"}:
                saw_resolution = True
                index += 1
                continue
            if word.startswith("-") and ("v" in word or "V" in word):
                saw_resolution = True
                index += 1
                continue
            if word.startswith("-"):
                index += 1
                continue
            break
        return saw_resolution and _resolution_probe_targets_replay_critical(words[index:])
    if words[start] in DECLARATION_COMMANDS and any(word in {"-F", "-f"} for word in words[start + 1:]):
        return len(words) == start + 1 or _resolution_probe_targets_replay_critical(words[start + 1:])
    if words[start] == "compgen" and "-A" in words[start + 1:]:
        try:
            index = words.index("-A", start + 1)
        except ValueError:
            return False
        if index + 1 < len(words) and words[index + 1] in {"function", "alias"}:
            return len(words) <= index + 2 or _resolution_probe_targets_replay_critical(words[index + 2:])
    return False


def _resolution_probe_targets_replay_critical(words: tuple[str, ...]) -> bool:
    targets = [word for word in words if word not in {"-a", "-t", "-p", "-P", "--"} and not word.startswith("-")]
    if not targets:
        return True
    return any(target in REPLAY_CRITICAL_FUNCTIONS for target in targets)


def _mentions_proc_environment(word: str) -> bool:
    return bool(re.search(r"(?:^|[<>=:])/?proc/(?:self|\$\$|\$\{?\$?\}?|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/(?:environ|cmdline)(?:$|[\s;&|])", word))


def _mentions_trace_fd(word: str) -> bool:
    return bool(
        re.search(r"(?:^|[<>]&?|/dev/fd/|/proc/(?:self|\$\$|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/fd/)(?:18|19)(?:$|[^\d])", word)
    )


def _mentions_dynamic_fd_redirection(word: str) -> bool:
    return bool(re.search(r"[<>]&\$|\{[A-Za-z_][A-Za-z0-9_]*\}[<>]", word))


def _option_test_mentions(words: tuple[str, ...], option_name: str) -> bool:
    return any(
        word == "-o" and index + 1 < len(words) and words[index + 1] == option_name
        for index, word in enumerate(words)
    )


def _dynamic_state_mutation(command: _Command, *, in_simple_scope: bool = False) -> bool:
    if command.name == "printf":
        return _printf_v_target_is_dynamic(command.words)
    if command.name not in DYNAMIC_STATE_COMMANDS:
        return False
    if command.name in DECLARATION_COMMANDS and _declaration_has_unsafe_nameref(command.words):
        return not (in_simple_scope and command.name == "local" and _declaration_has_function_local_nameref(command.words))
    if command.name == "let":
        return any(_has_dynamic_shell_expansion(word) for word in command.words[1:])
    if command.name == "((":
        return _arithmetic_words_can_mutate_dynamic_target(command.words[1:])
    if command.name == "read":
        return any(_read_target_can_mutate_replay_state(word) for word in _read_targets(command.words))
    if command.name in {"mapfile", "readarray"}:
        if _mapfile_has_callback(command.words):
            return True
        return any(_has_dynamic_shell_expansion(word) for word in _mapfile_targets(command.words))
    if command.name == "unset":
        return _unset_can_mutate_replay_state(command.words)
    if command.name in DECLARATION_COMMANDS:
        return any(_declaration_target_is_dynamic(word) for word in command.words[1:])
    return False


def _printf_v_target_is_dynamic(words: tuple[str, ...]) -> bool:
    for index, word in enumerate(words[1:], start=1):
        if word == "-v" and index + 1 < len(words):
            return _has_dynamic_shell_expansion(words[index + 1])
        if word.startswith("-v") and word != "-v":
            return _has_dynamic_shell_expansion(word[2:])
    return False


def _arithmetic_words_can_mutate_dynamic_target(words: tuple[str, ...]) -> bool:
    operators = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}
    inner = tuple(word for word in words if word != "))")
    for index, word in enumerate(inner):
        if not _has_dynamic_shell_expansion(word):
            continue
        if "++" in word or "--" in word:
            return True
        if re.search(r"(^|[^=!<>])=(?!=)", word):
            return True
        if index + 1 < len(inner) and inner[index + 1] in operators:
            return True
    return False


def _read_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    targets = []
    index = 1
    while index < len(words):
        word = words[index]
        if word == "-a":
            if index + 1 < len(words):
                targets.append(words[index + 1])
            index += 2
            continue
        if word in {"-d", "-n", "-N", "-p", "-t", "-u"}:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        targets.extend(word for word in words[index:] if not word.startswith(("<", ">")))
        break
    return tuple(targets)


def _mapfile_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    index = 1
    while index < len(words):
        word = words[index]
        if word in {"-C", "-c", "-d", "-n", "-O", "-s", "-u"}:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return (word,)
    return ()


def _mapfile_has_callback(words: tuple[str, ...]) -> bool:
    return any(word == "-C" or word.startswith("-C") for word in words[1:])


def _read_target_can_mutate_replay_state(word: str) -> bool:
    return _has_dynamic_shell_expansion(word) and not _is_positional_parameter_reference(word)


def _unset_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(word for word in words[1:] if not word.startswith("-"))


def _unset_can_mutate_replay_state(words: tuple[str, ...]) -> bool:
    if "-f" in words[1:]:
        return any(
            target in REPLAY_CRITICAL_FUNCTIONS
            or _has_dynamic_shell_expansion(target)
            or "{" in target
            or "}" in target
            for target in _unset_targets(words)
        )
    if "-v" in words[1:]:
        return any(_has_dynamic_shell_expansion(word) for word in _unset_targets(words))
    return any(
        _has_dynamic_shell_expansion(word) and not _is_simple_parameter_reference(word)
        for word in _unset_targets(words)
    )


def _dynamic_validation_bypass_command(command: _Command, exact_variables: dict[str, str]) -> bool:
    return _dynamic_words_can_bypass_source_validation(command.words, exact_variables)


def _dynamic_words_can_bypass_source_validation(words: tuple[str, ...], exact_variables: dict[str, str]) -> bool:
    if not words:
        return False
    command_index = _effective_command_index_without_unwrap(words)
    if command_index is None:
        return False
    name = words[command_index]
    if "=" in name or name.startswith("(("):
        return False
    if name in {"command", "builtin"}:
        target_index = _command_or_builtin_target_index(words, command_index)
        return target_index is not None and _has_dynamic_shell_expansion(words[target_index])
    if _has_dynamic_shell_expansion(name):
        if _dynamic_command_word_can_expand_multiple_words(name):
            return True
        exact_name = _exact_dynamic_command_name(name, exact_variables)
        if exact_name is None:
            if "|" in words[command_index + 1:]:
                return True
            return not _unknown_dynamic_command_tail_is_safe_helper_call(words[command_index + 1:])
        if exact_name in {"source", ".", "builtin", "command", "exec", "trap"}:
            return True
        if exact_name in SHELL_C_COMMANDS and _shell_c_tail_is_dynamic(words[command_index + 1:]):
            return True
        return _dynamic_tail_can_bypass_source_validation(words[command_index + 1:], exact_variables)
    return False


def _dynamic_command_word_can_expand_multiple_words(word: str) -> bool:
    return bool(re.search(r"\$(?:[@*]|\{[@*]\}|\{[^}]+(?:\[@\]|\[\*\])\})", word))


def _unknown_dynamic_command_tail_is_safe_helper_call(words: tuple[str, ...]) -> bool:
    args = []
    for word in words:
        if word in {"|", "||", "&&", ";", ")", "}", "then", "do", "fi", "done"}:
            break
        if word.startswith(("<", ">")):
            return False
        if _has_dynamic_shell_expansion(word):
            return False
        if _dynamic_helper_literal_can_bypass_replay(word):
            return False
        args.append(word)
    return True


def _dynamic_helper_literal_can_bypass_replay(word: str) -> bool:
    if word in {"source", ".", "builtin", "command", "eval", "exec", "trap", "read", "mapfile", "readarray", "printf"}:
        return True
    if re.search(r"(^|[\s;&|({])(?:builtin|command)\s+(?:source|\.)\b", word):
        return True
    if re.search(r"(^|[\s;&|({])(?:source|\.)\s+[^;&|]+", word):
        return True
    if _words_have_source_bearing_shell_payload(word):
        return True
    if word == "-v":
        return True
    if "__modash" in word or "__entrypoint__" in word or "__process_command__" in word:
        return True
    if re.fullmatch(r"p[0-9]+:[^|]+:[0-9]+:[0-9]+(?:\|[0-9]+)?", word):
        return True
    return False


def _words_have_source_bearing_shell_payload(command: str) -> bool:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return False
    words = [strip_shell_word_quotes(word) for word in raw_words]
    words = _expand_env_split_words(words)
    return (
        _shell_payload_contains_source(words)
        or _direct_non_bash_shell_payload_contains_source(words)
        or _busybox_shell_payload_contains_source(words)
        or _exec_style_shell_payload_contains_source(words)
    )


def _shell_payload_contains_source(words: list[str]) -> bool:
    index = _shell_command_index_after_wrappers(words)
    if index is None or _path_basename(words[index]) not in SHELL_C_COMMANDS:
        return False
    payload_index = _shell_c_payload_index(words, index + 1)
    return payload_index is not None and _payload_contains_source(words[payload_index])


def _expand_env_split_words(words: list[str]) -> list[str]:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    if index < len(words) and words[index] == "command":
        index += 1
        while index < len(words):
            if words[index] == "--":
                index += 1
                break
            if words[index] == "-p":
                index += 1
                continue
            if words[index].startswith("-"):
                return words
            break
    if index >= len(words) or _path_basename(words[index]) != "env":
        return words
    env_index = index
    index += 1
    split_words: list[str] | None = None
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word in {"-i", "--ignore-environment"}:
            index += 1
            continue
        if word == "-u":
            index += 2
            continue
        if word.startswith("--unset=") or ASSIGNMENT_WORD_PATTERN.match(word):
            index += 1
            continue
        if word == "-S":
            if index + 1 >= len(words):
                return words[:env_index] + ["sh", "-c", "source __modash_env_split_parse_error__"]
            split_words = _split_env_s(words[index + 1])
            index += 2
            break
        if word.startswith("-S") and word != "-S":
            split_words = _split_env_s(word[2:])
            index += 1
            break
        if word == "--split-string":
            if index + 1 >= len(words):
                return words[:env_index] + ["sh", "-c", "source __modash_env_split_parse_error__"]
            split_words = _split_env_s(words[index + 1])
            index += 2
            break
        if word.startswith("--split-string="):
            split_words = _split_env_s(word.split("=", 1)[1])
            index += 1
            break
        if word.startswith("-"):
            return words
        break
    if split_words is None:
        return words
    return words[:env_index] + split_words + words[index:]


def _split_env_s(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return ["sh", "-c", "source __modash_env_split_parse_error__"]


def _direct_non_bash_shell_payload_contains_source(words: list[str]) -> bool:
    index = _shell_command_index_after_wrappers(words)
    if index is None or not _is_non_bash_shell_name(words[index]):
        return False
    payload_index = _shell_c_payload_index(words, index + 1)
    return payload_index is not None and _payload_contains_source(words[payload_index])


def _busybox_shell_payload_contains_source(words: list[str]) -> bool:
    index = _shell_command_index_after_wrappers(words)
    if index is None or _path_basename(words[index]) != "busybox":
        return False
    if index + 1 >= len(words) or not _is_non_bash_shell_name(words[index + 1]):
        return False
    payload_index = _shell_c_payload_index(words, index + 2)
    return payload_index is not None and _payload_contains_source(words[payload_index])


def _exec_style_shell_payload_contains_source(words: list[str]) -> bool:
    for index, word in enumerate(words):
        if word not in {"-exec", "-execdir"} and _path_basename(word) != "xargs":
            continue
        for shell_index in range(index + 1, len(words)):
            if _is_non_bash_shell_name(words[shell_index]):
                payload_index = _shell_c_payload_index(words, shell_index + 1)
                return payload_index is not None and _payload_contains_source(words[payload_index])
            if words[shell_index] in {";", r"\;", "&&", "||", "|"}:
                break
    return False


def _shell_command_index_after_wrappers(words: list[str]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words):
        if words[index] == "!":
            index += 1
            continue
        if words[index] == "time":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            continue
        break
    if index < len(words) and words[index] == "command":
        index += 1
        while index < len(words):
            if words[index] == "--":
                return index + 1 if index + 1 < len(words) else None
            if words[index] == "-p":
                index += 1
                continue
            if words[index].startswith("-"):
                return None
            break
    if index < len(words) and words[index] == "env":
        index += 1
        while index < len(words):
            word = words[index]
            if word == "--":
                return index + 1 if index + 1 < len(words) else None
            if word in {"-i", "--ignore-environment"}:
                index += 1
                continue
            if word == "-u":
                index += 2
                continue
            if word.startswith("--unset=") or ASSIGNMENT_WORD_PATTERN.match(word):
                index += 1
                continue
            if word.startswith("-"):
                return None
            break
    if index < len(words) and words[index] == "exec":
        index += 1
    while index < len(words):
        wrapper = _path_basename(words[index])
        if wrapper == "nice":
            index = _command_tail_after_nice(words, index + 1)
        elif wrapper == "timeout":
            index = _command_tail_after_timeout(words, index + 1)
        elif wrapper == "setsid":
            index = _command_tail_after_option_wrapper(words, index + 1)
        elif wrapper == "nohup":
            index += 1
        elif wrapper == "stdbuf":
            index = _command_tail_after_stdbuf(words, index + 1)
        else:
            break
        if index is None:
            return None
    return index if index < len(words) else None


def _command_tail_after_nice(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = words[index]
        if word in {"-n", "--adjustment"}:
            index += 2
            continue
        if word.startswith("--adjustment="):
            index += 1
            continue
        if re.fullmatch(r"-[0-9]+", word) or re.fullmatch(r"-n[0-9]+", word):
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _command_tail_after_timeout(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = words[index]
        if word in {"-k", "--kill-after", "-s", "--signal"}:
            index += 2
            continue
        if word.startswith("--kill-after=") or word.startswith("--signal="):
            index += 1
            continue
        if word in {"--preserve-status", "--foreground", "-v", "--verbose"}:
            index += 1
            continue
        if word.startswith("-"):
            return None
        index += 1
        return index if index < len(words) else None
    return None


def _command_tail_after_option_wrapper(words: list[str], index: int) -> int | None:
    while index < len(words):
        if words[index] == "--":
            return index + 1 if index + 1 < len(words) else None
        if words[index].startswith("-"):
            index += 1
            continue
        return index
    return None


def _command_tail_after_stdbuf(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = words[index]
        if word in {"-i", "-o", "-e", "--input", "--output", "--error"}:
            index += 2
            continue
        if re.match(r"^(?:-[ioe]|--(?:input|output|error)=)", word):
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _is_non_bash_shell_name(word: str) -> bool:
    return _path_basename(word) in {"sh", "dash", "ksh", "ksh93", "mksh", "zsh", "ash"}


def _path_basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _kill_bypasses_generated_guard(words: tuple[str, ...]) -> bool:
    if not words:
        return False
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"(", "{", "time", "!"}:
        index += 1
    while index < len(words) and words[index] in SHELL_COMMAND_PREFIXES:
        index += 1
    if index >= len(words):
        return False
    command = words[index]
    if command == "builtin":
        target = _command_or_builtin_target_index(words, index)
        return target is not None and target < len(words) and words[target] == "kill"
    target = _shell_command_index_after_wrappers(list(words[index:]))
    if target is None or index + target >= len(words):
        return False
    target_word = words[index + target]
    if _path_basename(target_word) != "kill":
        return False
    return target != 0 or target_word != "kill"
    return False


def _shell_c_payload_index(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = words[index]
        if word == "-c":
            return index + 1 if index + 1 < len(words) else None
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return index + 1 if index + 1 < len(words) else None
        if word in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("+"):
            index += 1
            continue
        return None
    return None


def _payload_contains_source(payload: str) -> bool:
    return bool(re.search(r"(^|[\s;&|({])(?:source|\.)\s+|(^|[\s;&|({])(?:builtin|command)\s+(?:source|\.)\b", payload))


def _shell_c_tail_is_dynamic(words: tuple[str, ...]) -> bool:
    index = 0
    while index < len(words):
        word = words[index]
        if word == "-c":
            return index + 1 >= len(words) or _has_dynamic_shell_expansion(words[index + 1])
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return index + 1 >= len(words) or _has_dynamic_shell_expansion(words[index + 1])
        if word in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("+"):
            index += 1
            continue
        return False
    return False


def _dynamic_tail_can_bypass_source_validation(words: tuple[str, ...], exact_variables: dict[str, str]) -> bool:
    saw_dynamic = False
    for word in words:
        if word in {"|", "||", "&&", ";", ")", "}", "then", "do", "fi", "done"}:
            break
        exact_name = _exact_dynamic_command_name(word, exact_variables)
        if word in {"source", ".", "command", "builtin"} or exact_name in {"source", ".", "command", "builtin"}:
            return True
        if _has_dynamic_shell_expansion(word):
            if _exact_expand_dynamic_word(word, exact_variables) is not None:
                continue
            saw_dynamic = True
        elif saw_dynamic and _source_like_dynamic_dispatch_argument(word):
            return True
    return False


def _dynamic_validation_bypass_text(text: str, exact_variables: dict[str, str]) -> bool:
    return _scan_shell_fragments_for_dynamic_bypass(text, exact_variables, depth=0)


def _scan_shell_fragments_for_dynamic_bypass(text: str, exact_variables: dict[str, str], *, depth: int) -> bool:
    if depth > 8:
        return True
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if in_single_quote:
            index += 1
            continue
        if char == "`":
            body, end_index = read_backtick_body(text, index + 1)
            if body is None:
                index += 1
                continue
            if _shell_body_has_dynamic_bypass(body, exact_variables, depth + 1):
                return True
            index = end_index + 1
            continue
        if text.startswith("$((", index):
            body, end_index = read_balanced_body(text, index + 3)
            if body is None:
                return True
            index = end_index + 1
            continue
        if not in_double_quote and text.startswith("((", index):
            end_index = _arithmetic_command_end(text, index + 2)
            if end_index is None:
                return True
            index = end_index + 2
            continue
        if text.startswith("$(", index):
            body, end_index = read_balanced_body(text, index + 2)
            if body is None:
                index += 2
                continue
            if _shell_body_has_dynamic_bypass(body, exact_variables, depth + 1):
                return True
            index = end_index + 1
            continue
        if not in_double_quote and char == "(" and not is_array_assignment_paren(text, index):
            body, end_index = read_balanced_body(text, index + 1)
            if body is None:
                index += 1
                continue
            if _shell_body_has_dynamic_bypass(body, exact_variables, depth + 1):
                return True
            index = end_index + 1
            continue
        index += 1
    return False


def _arithmetic_command_end(text: str, start_index: int) -> int | None:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = start_index
    while index < len(text) - 1:
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if not in_single_quote and not in_double_quote and text.startswith("))", index):
            return index
        index += 1
    return None


def _shell_body_has_dynamic_bypass(body: str, exact_variables: dict[str, str], depth: int) -> bool:
    for line in body.splitlines() or [body]:
        for command in get_commands(line):
            parsed = _parse_command(command)
            if parsed.name == "eval" and not _is_safe_literal_eval(parsed.words, parsed.raw_words):
                return True
            if (
                _dynamic_words_can_bypass_source_validation(parsed.words, exact_variables)
                or _unknown_nested_dynamic_command(parsed.words, exact_variables)
            ):
                return True
            if _scan_shell_fragments_for_dynamic_bypass(command, exact_variables, depth=depth):
                return True
    return False


def _unknown_nested_dynamic_command(words: tuple[str, ...], exact_variables: dict[str, str]) -> bool:
    command_index = _effective_command_index_without_unwrap(words)
    if command_index is None:
        return False
    name = words[command_index]
    if "=" in name or name.startswith("(("):
        return False
    if not _has_dynamic_shell_expansion(name):
        return False
    exact_name = _exact_dynamic_command_name(name, exact_variables)
    return exact_name is None


def _command_with_exact_dynamic_name(command: _Command, exact_variables: dict[str, str]) -> _Command:
    command_index = _effective_command_index_without_unwrap(command.words)
    if command_index is None:
        return command
    name = command.words[command_index]
    if not _has_dynamic_shell_expansion(name):
        return command
    exact_name = _exact_dynamic_command_name(name, exact_variables)
    if exact_name is None:
        return command
    words = list(command.words)
    words[command_index] = exact_name
    return _Command(exact_name, tuple(words), command.raw_words)


def _exact_dynamic_command_name(word: str, exact_variables: dict[str, str]) -> str | None:
    variable = _variable_reference_name(word)
    if variable is None:
        return None
    return exact_variables.get(variable)


def _exact_expand_dynamic_word(word: str, exact_variables: dict[str, str]) -> str | None:
    if "`" in word or "$(" in word:
        return None

    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in exact_variables:
            raise KeyError
        return exact_variables[name]

    try:
        return re.sub(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)})", replace, word)
    except KeyError:
        return None


def _effective_command_index_without_unwrap(words: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"(", "{", "time", "!", "coproc"}:
        index += 1
        if index < len(words) and words[index - 1] == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
        if index < len(words) and words[index - 1] == "coproc" and words[index] not in {"{", "source", ".", "builtin", "command", "time", "!"}:
            index += 1
    while index < len(words) and words[index] in SHELL_COMMAND_PREFIXES:
        index += 1
    return index if index < len(words) else None


def _command_or_builtin_target_index(words: tuple[str, ...], index: int) -> int | None:
    if words[index] == "builtin":
        return index + 1 if index + 1 < len(words) else None
    index += 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word == "-p":
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _declaration_target_is_dynamic(word: str) -> bool:
    if word.startswith("-"):
        return False
    name = word.split("=", 1)[0]
    return _has_dynamic_shell_expansion(name)


def _declaration_has_unsafe_nameref(words: tuple[str, ...]) -> bool:
    if "-n" not in words[1:]:
        return False
    declarations = _declaration_operands(words)
    if not declarations:
        return True
    for word in declarations:
        if "=" not in word:
            return True
        name, value = word.split("=", 1)
        if _has_dynamic_shell_expansion(name):
            return True
        if _nameref_target_is_safe(value):
            continue
        return True
    return False


def _declaration_has_function_local_nameref(words: tuple[str, ...]) -> bool:
    if "-n" not in words[1:]:
        return False
    declarations = _declaration_operands(words)
    if not declarations:
        return False
    for word in declarations:
        if "=" not in word:
            return False
        name, value = word.split("=", 1)
        if _has_dynamic_shell_expansion(name):
            return False
        if _nameref_target_is_safe(value) or _function_local_nameref_target_is_safe(value):
            continue
        return False
    return True


def _declaration_operands(words: tuple[str, ...]) -> list[str]:
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word.startswith("-"):
            index += 1
            continue
        break
    return [word for word in words[index:] if not word.startswith(("<", ">"))]


def _nameref_target_is_safe(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _function_local_nameref_target_is_safe(value: str) -> bool:
    return bool(re.fullmatch(r"\$(?:[0-9]|\{[0-9]+(?::-[A-Za-z_][A-Za-z0-9_]*)?\})", value))


def _source_like_dynamic_dispatch_argument(word: str) -> bool:
    return (
        word.startswith(("./", "../", "/"))
        or word.endswith((".sh", ".bash"))
        or "/" in word
    )


def _dynamic_tail_has_source_like_literal(words: tuple[str, ...]) -> bool:
    return any(_source_like_dynamic_dispatch_argument(word) for word in words if not _has_dynamic_shell_expansion(word))


def _word_is_assignment(word: str) -> bool:
    return ASSIGNMENT_WORD_PATTERN.match(word) is not None


def _is_single_quoted_word(word: str) -> bool:
    return len(word) >= 2 and word[0] == "'" and word[-1] == "'"


def _has_dynamic_shell_expansion(word: str) -> bool:
    return bool(re.search(r"\$(?:\{|[A-Za-z_@*#?$!0-9-])|`|\$\(", word))


def _uses_unsafe_indirect_expansion(word: str) -> bool:
    for match in re.finditer(r"\$\{!([^}]*)}", word):
        expression = match.group(1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*@a", expression):
            continue
        array_index_match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(?:\*|@)]", expression)
        if array_index_match is not None:
            name = array_index_match.group(1)
            if (
                name not in INSTRUMENTATION_VARIABLES
                and not name.startswith(("BASH", "MODASH_TRACE", "__modash_"))
            ):
                continue
        return True
    return False


def _trap_may_target_exit(words: tuple[str, ...]) -> bool:
    if len(words) <= 1:
        return False
    if words[1] in {"-", "--"} and len(words) == 2:
        return False
    for word in words[2:] if words[1] in {"-", ""} else words[1:]:
        if _has_dynamic_shell_expansion(word):
            return True
        if word in {"0", "EXIT"}:
            return True
    return False
