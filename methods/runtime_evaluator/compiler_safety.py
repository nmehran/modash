from __future__ import annotations

import re

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
INSTRUMENTATION_SENSITIVE_PATTERN = re.compile(
    r"(?<![\w])(?:"
    r"\$\-|\$\{\-\}|"
    r"SHELLOPTS|BASH_XTRACEFD|PS4|\$BASH_ENV|\$\{BASH_ENV\}|"
    r"MODASH_TRACE_(?:FILE|COUNTER_FILE|XTRACE_FILE|FAILURE_FILE|POSITIONAL_SCANNER|"
    r"FUNCTION_SCANNER|FINGERPRINT_SCANNER|PYTHON)"
    r")(?![\w])"
)


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

    shopt_restore_vars = _shopt_restore_variables(content)
    has_exit = False
    has_exit_trap = False
    for line_number, line in _code_lines(content):
        if INSTRUMENTATION_SENSITIVE_PATTERN.search(line):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler input observes instrumentation-sensitive shell state at {label}:{line_number}",
                code="runtime.compile.instrumentation_sensitive",
            )
        for command in get_commands(line):
            name, words = _command_name_and_words(command)
            if name == "alias":
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses aliases under trace instrumentation at {label}:{line_number}",
                    code="runtime.compile.instrumentation_sensitive",
                )
            if name == "eval" and not _is_shopt_restore_eval(words, shopt_restore_vars):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler input uses dynamic eval at {label}:{line_number}",
                    code="runtime.compile.dynamic_eval",
                )
            if name == "exit":
                has_exit = True
            if name == "trap" and _trap_targets_exit(words):
                has_exit_trap = True

    if has_exit and has_exit_trap:
        raise RuntimeObservedCompileError(
            f"runtime graph compiler input combines explicit exit with EXIT trap manipulation: {label}",
            code="runtime.compile.exit_trap",
        )


def _code_lines(content: str):
    active_heredocs = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        yield line_number, line
        active_heredocs.extend(extract_heredoc_delimiters(line))


def _shopt_restore_variables(content: str) -> set[str]:
    variables: set[str] = set()
    pattern = re.compile(
        r"(?:^|[\s;])(?:local\s+)?([A-Za-z_][A-Za-z0-9_]*)=\$\(shopt\s+-p\b[^)]*\)"
    )
    for _line_number, line in _code_lines(content):
        for match in pattern.finditer(line):
            variables.add(match.group(1))
    return variables


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


def _command_name_and_words(command: str) -> tuple[str, tuple[str, ...]]:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return "", ()
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in SHELL_COMMAND_PREFIXES:
        index += 1
    return (words[index], words) if index < len(words) else ("", words)


def _trap_targets_exit(words: tuple[str, ...]) -> bool:
    if len(words) <= 1:
        return False
    if words[1] in {"-", "--"} and len(words) == 2:
        return False
    for word in words[2:] if words[1] in {"-", ""} else words[1:]:
        if word in {"0", "EXIT"}:
            return True
    return False
