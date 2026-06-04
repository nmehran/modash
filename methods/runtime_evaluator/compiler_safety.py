from __future__ import annotations

import re
from dataclasses import dataclass

from methods.runtime_evaluator.compiler_model import RuntimeObservedCompileError, _CompilePlan
from methods.runtime_evaluator.scanners import function_context_sensitive_top_level_lines
from methods.shell.line import get_commands
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
}
INSTRUMENTATION_VARIABLES = {"BASH_ENV", "BASH_XTRACEFD", "PS4", "SHELLOPTS", *TRACE_VARIABLES}
DECLARATION_COMMANDS = {"declare", "typeset", "local", "export", "readonly"}
DYNAMIC_STATE_COMMANDS = {"unset", "read", *DECLARATION_COMMANDS}


@dataclass(frozen=True)
class _Command:
    name: str
    words: tuple[str, ...]
    raw_words: tuple[str, ...]


def validate_runtime_compile_safety(plan: _CompilePlan) -> None:
    for unit in plan.file_units.values():
        _validate_unit_text(unit.physical_path or unit.logical_path, unit.content)
    for process_plan in plan.process_plans.values():
        for unit in process_plan.units.values():
            if unit.physical_path is None:
                _validate_unit_text(unit.logical_path, unit.content)


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


def _validate_unit_text(label: str, content: str) -> None:
    if RESERVED_RUNTIME_PREFIX in content:
        raise RuntimeObservedCompileError(
            f"runtime graph compiler input uses reserved generated namespace {RESERVED_RUNTIME_PREFIX!r}: {label}",
            code="runtime.compile.reserved_namespace",
        )

    shopt_restore_vars = _safe_shopt_restore_variables(content)
    has_exit = False
    for line_number, line in _code_lines(content):
        for command in get_commands(line):
            parsed = _parse_command(command)
            name = parsed.name
            if _instrumentation_sensitive(parsed):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input observes instrumentation-sensitive shell state at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            if name == "alias":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses aliases under trace instrumentation at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            if name == "eval" and not _is_shopt_restore_eval(parsed.words, shopt_restore_vars):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic eval at {label}:{line_number}",
                    code="runtime.compile.dynamic_eval",
                )
            if name == "exec":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses exec, which can bypass replay validation: {label}:{line_number}",
                    code="runtime.compile.exec",
                )
            if name == "trap" and _trap_may_target_exit(parsed.words):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input manipulates EXIT traps: {label}:{line_number}",
                    code="runtime.compile.exit_trap",
                )
            if _dynamic_state_mutation(parsed):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic shell state mutation at {label}:{line_number}",
                    code="runtime.compile.dynamic_state",
                )
            if name == "exit":
                has_exit = True

    if has_exit:
        # Explicit exit is safe only while the generated EXIT trap remains intact.
        # EXIT trap manipulation is rejected above.
        return


def _code_lines(content: str):
    active_heredocs = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        yield line_number, line
        active_heredocs.extend(extract_heredoc_delimiters(line))


def _safe_shopt_restore_variables(content: str) -> set[str]:
    restore_counts: dict[str, int] = {}
    other_assignments: set[str] = set()
    for _line_number, line in _code_lines(content):
        for command in get_commands(line):
            parsed = _parse_command(command)
            for raw_word, word in zip(parsed.raw_words, parsed.words):
                name = _assignment_name(word)
                if name is None:
                    continue
                if _is_shopt_restore_assignment(raw_word, word):
                    restore_counts[name] = restore_counts.get(name, 0) + 1
                else:
                    other_assignments.add(name)
    return {
        name
        for name, count in restore_counts.items()
        if count == 1 and name not in other_assignments
    }


def _assignment_name(word: str) -> str | None:
    if not ASSIGNMENT_WORD_PATTERN.match(word):
        return None
    return word.split("=", 1)[0]


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


def _variable_reference_name(word: str) -> str | None:
    match = re.fullmatch(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})", word)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _is_simple_parameter_reference(word: str) -> bool:
    return _variable_reference_name(word) is not None or bool(re.fullmatch(r"\$(?:[0-9]|\{[0-9]+\})", word))


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
    if command.name in {"set", "shopt"} and any(word in {"-o", "+o", "-p"} for word in words[1:]):
        if "xtrace" in words or "expand_aliases" in words:
            return True
    return False


def _uses_instrumentation_variable(words: tuple[str, ...], raw_words: tuple[str, ...]) -> bool:
    for raw_word, word in zip(raw_words, words):
        if _is_single_quoted_word(raw_word):
            continue
        if _word_is_assignment(word):
            continue
        if word in INSTRUMENTATION_VARIABLES:
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
    if command.name == "set" and any(word in {"-o", "+o"} for word in words[1:]):
        return True
    if command.name in {"[[", "[", "test"} and "-v" in words:
        index = words.index("-v")
        if index + 1 < len(words):
            target = words[index + 1]
            if target == "BASH_ENV" or _has_dynamic_shell_expansion(target):
                return True
    return False


def _option_test_mentions(words: tuple[str, ...], option_name: str) -> bool:
    return any(
        word == "-o" and index + 1 < len(words) and words[index + 1] == option_name
        for index, word in enumerate(words)
    )


def _dynamic_state_mutation(command: _Command) -> bool:
    if command.name == "printf":
        return _printf_v_target_is_dynamic(command.words)
    if command.name not in DYNAMIC_STATE_COMMANDS:
        return False
    if command.name in DECLARATION_COMMANDS and "-n" in command.words[1:]:
        # Namerefs can target generated replay state indirectly. Literal
        # makepkg-style local nameref helpers are left to runtime behavior unless
        # the declared target itself is dynamic.
        return any(_declaration_target_is_dynamic(word) for word in command.words[2:])
    if command.name == "read":
        return any(_read_target_can_mutate_replay_state(word) for word in _read_targets(command.words))
    if command.name == "unset":
        return _unset_can_mutate_replay_state(command.words)
    if command.name in DECLARATION_COMMANDS:
        return any(_declaration_target_is_dynamic(word) for word in command.words[1:])
    return False


def _printf_v_target_is_dynamic(words: tuple[str, ...]) -> bool:
    for index, word in enumerate(words[1:], start=1):
        if word == "-v" and index + 1 < len(words):
            return _has_dynamic_shell_expansion(words[index + 1])
    return False


def _read_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    targets = []
    index = 1
    while index < len(words):
        word = words[index]
        if word in {"-a", "-d", "-n", "-N", "-p", "-t", "-u"}:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        targets.extend(word for word in words[index:] if not word.startswith(("<", ">")))
        break
    return tuple(targets)


def _read_target_can_mutate_replay_state(word: str) -> bool:
    return _has_dynamic_shell_expansion(word) and not _is_simple_parameter_reference(word)


def _unset_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(word for word in words[1:] if not word.startswith("-"))


def _unset_can_mutate_replay_state(words: tuple[str, ...]) -> bool:
    if any(word in {"-f", "-v"} for word in words[1:]):
        return any(_has_dynamic_shell_expansion(word) for word in _unset_targets(words))
    return any(
        _has_dynamic_shell_expansion(word) and not _is_simple_parameter_reference(word)
        for word in _unset_targets(words)
    )


def _declaration_target_is_dynamic(word: str) -> bool:
    if word.startswith("-"):
        return False
    name = word.split("=", 1)[0]
    return _has_dynamic_shell_expansion(name)


def _word_is_assignment(word: str) -> bool:
    return ASSIGNMENT_WORD_PATTERN.match(word) is not None


def _is_single_quoted_word(word: str) -> bool:
    return len(word) >= 2 and word[0] == "'" and word[-1] == "'"


def _has_dynamic_shell_expansion(word: str) -> bool:
    return bool(re.search(r"\$(?:\{|[A-Za-z_@*#?$!0-9-])|`|\$\(", word))


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
