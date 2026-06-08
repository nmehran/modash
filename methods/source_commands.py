"""Shared parsing helpers for Bash source command forms."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from methods.shell_commands import create_command_pattern
from methods.shell.line import get_commands
from methods.shell.scan import is_array_assignment_paren, read_backtick_body, read_balanced_body
from methods.source_errors import UnsupportedSourceError
from methods.source_words import (
    ASSIGNMENT_WORD_PATTERN,
    parse_shell_words,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

SOURCE_PATTERN = create_command_pattern(command=r'\bsource\b|\.', regex=True)
SOURCE_COMMAND_NAMES = frozenset({"source", "."})
TRACE_SOURCE_ALIAS = "__modash_trace_source_alias"
TRACE_DOT_ALIAS = "__modash_trace_dot_source"
TRACE_BUILTIN_WRAPPER = "__modash_trace_builtin"
TRACE_COMMAND_WRAPPER = "__modash_trace_command"
SOURCE_LIKE_PREFIXES = (
    "source",
    ".",
    "builtin source",
    "builtin .",
    "command source",
    "command .",
    TRACE_SOURCE_ALIAS,
    TRACE_DOT_ALIAS,
    f"{TRACE_BUILTIN_WRAPPER} source",
    f"{TRACE_BUILTIN_WRAPPER} .",
    f"{TRACE_COMMAND_WRAPPER} source",
    f"{TRACE_COMMAND_WRAPPER} .",
)
SHELL_CONTROL_WORDS = frozenset({"then", "do", "else", "fi", "done", "}"})
SHELL_CONTROL_OPERATORS = frozenset({"&&", "||", "|"})
SOURCE_COMMAND_PREFIX_WORDS = frozenset({"time", "coproc"})


@dataclass(frozen=True)
class SourceCommandInvocation:
    command_name: str
    source_expression: str
    source_site: str
    source_site_column_offset: int
    source_column_offset: int
    command_start_index: int
    source_index: int
    source_path_index: int
    source_end_index: int
    source_path: str
    arguments: tuple[str, ...] = ()
    source_argument_indexes: tuple[int, ...] = ()
    words: tuple[str, ...] = ()
    wrapped: bool = False
    option_terminator: bool = False
    invalid_option: str | None = None


def clean_shell_word(word: str):
    return strip_shell_word_quotes(strip_trailing_shell_punctuation(word))


def strip_trailing_shell_punctuation(word: str):
    while word.endswith(";"):
        word = word[:-1]
    return word


def source_command_index(command_or_words):
    if isinstance(command_or_words, str):
        try:
            words = parse_shell_words(command_or_words)
        except UnsupportedSourceError:
            return 0 if SOURCE_PATTERN.findall(command_or_words) else None
        return source_command_word_index(words)
    return source_command_word_index(command_or_words)


def source_command_word_index(words: Sequence[str]):
    position = source_command_position(words)
    return position[1] if position else None


def source_command_position(words: Sequence[str]):
    command_start = 0
    command_start = _skip_leading_redirections(words, command_start)
    command_start = _skip_negation_and_assignments(words, command_start)
    effective_start = _source_effective_command_start(words, command_start)
    effective_command_start = _source_command_start_for_effective_start(words, command_start, effective_start)

    for index, word in enumerate(words):
        if word not in SOURCE_COMMAND_NAMES:
            continue

        if index == effective_start:
            return effective_command_start, index

        first_word = words[effective_start] if effective_start < len(words) else ""
        previous_word = words[index - 1]
        if first_word == "builtin":
            command_index = _builtin_source_command_index(words, effective_start)
            if index == command_index:
                return command_start, index
        if first_word == "command":
            command_index = _command_source_command_index(words, effective_start)
            if index == command_index:
                return command_start, index
        if first_word in {"if", "while", "until", "then", "elif", "else", "do"}:
            branch_index = effective_start + 1
            branch_index = _skip_negation_and_assignments(words, branch_index)
            branch_index = _source_effective_command_start(words, branch_index)
            if index == branch_index:
                return command_start, index
            branch_word = words[branch_index] if branch_index < len(words) else ""
            if branch_word == "builtin":
                command_index = _builtin_source_command_index(words, branch_index)
                if index == command_index:
                    return command_start, index
            if branch_word == "command":
                command_index = _command_source_command_index(words, branch_index)
                if index == command_index:
                    return command_start, index
        brace_command_start = _brace_group_command_start(words, command_start, index)
        if brace_command_start is not None:
            return brace_command_start, index
        if any(candidate.endswith(")") for candidate in words[command_start:index]):
            return command_start, index

    return None


def _skip_negation_and_assignments(words: Sequence[str], index: int):
    while index < len(words):
        start = index
        while index < len(words) and words[index] == "!":
            index += 1
        index = _skip_leading_redirections(words, index)
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        index = _skip_leading_redirections(words, index)
        if index == start:
            break
    return index


def _skip_leading_redirections(words: Sequence[str], index: int):
    while index < len(words):
        redirection = _redirection_token_kind(words[index])
        if redirection is None:
            break
        index += 1
        if redirection == "separate-target" and index < len(words):
            index += 1
    return index


def _source_command_start_for_effective_start(words: Sequence[str], command_start: int, effective_start: int):
    if command_start < len(words) and words[command_start] == "{" and effective_start > command_start:
        return _assignment_prefix_start(words, command_start + 1, effective_start)
    return command_start


def _brace_group_command_start(words: Sequence[str], command_start: int, source_index: int):
    index = source_index - 1
    while index >= command_start and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index -= 1
    while index >= command_start and words[index] == "!":
        index -= 1
    if index >= command_start and (words[index] == "{" or words[index].endswith("{")):
        return _assignment_prefix_start(words, index + 1, source_index)
    return None


def _assignment_prefix_start(words: Sequence[str], start: int, source_index: int):
    index = start
    while index < source_index and words[index] == "!":
        index += 1
    return index


def _source_effective_command_start(words: Sequence[str], index: int):
    while index < len(words) and words[index] == "!":
        index += 1
    index = _skip_leading_redirections(words, index)
    while index < len(words) and words[index] in SOURCE_COMMAND_PREFIX_WORDS:
        word = words[index]
        index += 1
        if word == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
            while index < len(words) and words[index] == "!":
                index += 1
            continue
        if word == "coproc":
            while index < len(words) and words[index] == "!":
                index += 1
            if index < len(words) and words[index] == "{":
                index += 1
                continue
            if index < len(words) and words[index] not in {"{", "source", ".", "builtin", "command", "time"}:
                index += 1
                if index < len(words) and words[index] == "{":
                    index += 1
            continue
    if index < len(words) and words[index] == "{":
        index += 1
        index = _skip_negation_and_assignments(words, index)
    return index


def source_command_invocation(
    command: str,
    *,
    normalize_trace_wrappers: bool = False,
    stop_at_shell_control: bool = False,
):
    try:
        quoted_words = parse_shell_words_preserving_quotes(command)
        words = tuple(clean_shell_word(word) for word in quoted_words)
    except UnsupportedSourceError:
        return None
    if not words:
        return None

    normalized_words = normalized_trace_wrapper_words(words) if normalize_trace_wrappers else None
    if normalized_words is not None:
        return _source_command_invocation_from_words(
            command,
            tuple(normalized_words),
            tuple(normalized_words),
            normalized=True,
            stop_at_shell_control=stop_at_shell_control,
        )
    return _source_command_invocation_from_words(
        command,
        tuple(quoted_words),
        words,
        normalized=False,
        stop_at_shell_control=stop_at_shell_control,
    )


def source_invocation_from_command(command: str, *, normalize_trace_wrappers: bool = True):
    return source_command_invocation(command, normalize_trace_wrappers=normalize_trace_wrappers)


def _source_command_invocation_from_words(
    command: str,
    quoted_words: tuple[str, ...],
    words: tuple[str, ...],
    *,
    normalized: bool,
    stop_at_shell_control: bool,
):
    position = source_command_position(words)
    if position is None:
        return None
    command_start, source_index = position
    if source_index >= len(words):
        return None

    source_words = _source_path_and_arguments(words, source_index, stop_at_shell_control)
    if source_words is None:
        return None
    source_path, arguments, source_path_index, argument_indexes, source_end_index, option_terminator, invalid_option = source_words

    wrapped = source_index != command_start
    command_name = words[source_index]
    if command_name not in SOURCE_COMMAND_NAMES:
        return None

    if normalized:
        if source_path:
            source_expression_words = (words[source_path_index], *[words[index] for index in argument_indexes])
        else:
            source_expression_words = ()
        source_expression = " ".join(source_expression_words)
        source_site = " ".join(words)
        token_start = 0
        source_site_column_offset = 0
    else:
        token_start = shell_word_start(command, quoted_words, source_index)
        if token_start is None:
            return None
        if stop_at_shell_control:
            if source_path:
                source_expression_words = (quoted_words[source_path_index], *[quoted_words[index] for index in argument_indexes])
            else:
                source_expression_words = ()
            source_expression = " ".join(source_expression_words)
        else:
            if source_path:
                expression_start = shell_word_start(command, quoted_words, source_path_index)
                source_expression = command[expression_start:].strip() if expression_start is not None else command[token_start + len(quoted_words[source_index]):].strip()
            else:
                source_expression = ""
        if wrapped:
            site_start_index = 0 if _has_simple_command_prefix(words, command_start) else command_start
            command_start_token = shell_word_start(command, quoted_words, site_start_index)
            if command_start_token is None:
                return None
            source_site = command[command_start_token:].strip()
            source_site_column_offset = command_start_token
        elif source_index > 0:
            command_start_token = shell_word_start(command, quoted_words, 0)
            if command_start_token is None:
                return None
            source_site = command[command_start_token:].strip()
            source_site_column_offset = command_start_token
        else:
            source_site = command[token_start:].strip()
            source_site_column_offset = token_start

    return SourceCommandInvocation(
        command_name=command_name,
        source_expression=source_expression,
        source_site=source_site,
        source_site_column_offset=source_site_column_offset,
        source_column_offset=token_start,
        command_start_index=command_start,
        source_index=source_index,
        source_path_index=source_path_index,
        source_end_index=source_end_index,
        source_path=source_path,
        arguments=arguments,
        source_argument_indexes=argument_indexes,
        words=words,
        wrapped=wrapped,
        option_terminator=option_terminator,
        invalid_option=invalid_option,
    )


def _source_path_and_arguments(words: Sequence[str], source_index: int, stop_at_shell_control: bool):
    index = source_index + 1
    argv_entries: list[tuple[int, str]] = []
    source_end_index = index

    while index < len(words):
        cleaned = clean_shell_word(words[index])
        if _is_source_word_boundary(cleaned, stop_at_shell_control):
            break
        redirection = _redirection_token_kind(words[index])
        if redirection is not None:
            index += 1
            if redirection == "separate-target" and index < len(words):
                index += 1
            source_end_index = index
            continue
        argv_entries.append((index, cleaned))
        index += 1
        source_end_index = index

    option_terminator = False
    if argv_entries and argv_entries[0][1] == "--":
        option_terminator = True
        argv_entries = argv_entries[1:]

    source_path_index = len(words)
    source_path = ""
    argument_indexes: list[int] = []
    arguments: list[str] = []
    if argv_entries:
        source_path_index, source_path = argv_entries[0]
        for argument_index, argument in argv_entries[1:]:
            argument_indexes.append(argument_index)
            arguments.append(argument)

    invalid_option = None
    if source_path and not option_terminator and _invalid_source_option_word(source_path):
        invalid_option = _source_option_diagnostic_word(source_path)
    return source_path, tuple(arguments), source_path_index, tuple(argument_indexes), source_end_index, option_terminator, invalid_option


def _has_simple_command_prefix(words: Sequence[str], command_start: int):
    if command_start <= 0:
        return False
    index = 0
    while index < command_start:
        if words[index] == "!" or ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
            continue
        redirection = _redirection_token_kind(words[index])
        if redirection is not None:
            index += 1
            if redirection == "separate-target" and index < command_start:
                index += 1
            continue
        return False
    return True


def _is_source_word_boundary(word: str, stop_at_shell_control: bool):
    return stop_at_shell_control and (
        not word
        or word in SHELL_CONTROL_WORDS
        or word in SHELL_CONTROL_OPERATORS
    )


def _invalid_source_option_word(word: str):
    return word.startswith("-") and word != "-"


def _source_option_diagnostic_word(word: str):
    if word.startswith("--"):
        return "--"
    return word[:2]


def invalid_source_option_word(word: str):
    return _invalid_source_option_word(word)


def source_option_diagnostic_word(word: str):
    return _source_option_diagnostic_word(word)


def _redirection_token_kind(word: str) -> str | None:
    if word.startswith("<(") or word.startswith(">("):
        return None
    if _redirection_word_is_heredoc(word):
        return "unsupported"
    if _redirection_word_needs_target(word):
        return "separate-target"
    if _redirection_word_has_target(word):
        return "combined-target"
    return None


def _redirection_word_is_heredoc(word: str):
    return bool(re.match(r"^(?:[0-9]+)?<<", word))


def _redirection_word_needs_target(word: str):
    return bool(re.fullmatch(r"(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|)", word))


def _redirection_word_has_target(word: str):
    return bool(re.match(r"^(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|).+", word))


def shell_word_start(command: str, words: Sequence[str], word_index: int):
    search_start = 0
    for index, word in enumerate(words[:word_index + 1]):
        token_start = command.find(word, search_start)
        if token_start < 0:
            return None
        search_start = token_start + len(word)
        if index == word_index:
            return token_start
    return None


def normalized_trace_wrapper_words(words: Sequence[str]):
    if not words:
        return None
    if words[0] == TRACE_SOURCE_ALIAS:
        if len(words) < 5:
            return None
        if words[1:3] == ("source", "source"):
            command_name = "source"
        elif words[1:3] == ("dot", "."):
            command_name = "."
        else:
            return None
        separator = _trace_wrapper_separator(words, 4)
        if separator is None:
            return None
        source_words = tuple(words[separator + 1:])
        return (command_name, *source_words)

    if words[0] not in {TRACE_BUILTIN_WRAPPER, TRACE_COMMAND_WRAPPER}:
        return None
    separator = _trace_wrapper_separator(words, 1)
    if separator is None:
        return None
    wrapped_words = tuple(words[separator + 1:])
    if not wrapped_words:
        return None
    command_name = "builtin" if words[0] == TRACE_BUILTIN_WRAPPER else "command"
    if wrapped_words[0] == command_name:
        return wrapped_words
    return (command_name, *wrapped_words)


def is_trace_wrapper_source_command(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except Exception:
        return False
    clean_words = tuple(clean_shell_word(word) for word in words)
    normalized = normalized_trace_wrapper_words(clean_words)
    return normalized is not None and source_command_word_index(normalized) is not None


def is_source_like_command_text(command: str):
    stripped = command.strip()
    if not stripped:
        return False
    if source_command_invocation(stripped, normalize_trace_wrappers=True) is not None:
        return True
    return any(stripped == prefix or stripped.startswith(f"{prefix} ") for prefix in SOURCE_LIKE_PREFIXES)


def contains_source_command(command: str):
    return source_command_index(command) is not None


def contains_nested_source_command(command: str):
    """Detect live source commands inside shell constructs we do not lower."""
    return _contains_nested_source_command(command, depth=0)


def shell_single_quote(value: str):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def shell_quote(value: str):
    if value and all(character.isalnum() or character in "@%_+=:,./-" for character in value):
        return value
    return shell_single_quote(value)


def shell_quote_words(words: Sequence[str], *, always_quote: bool = False):
    quote = shell_single_quote if always_quote else shell_quote
    return " ".join(quote(word) for word in words)


def _builtin_source_command_index(words: Sequence[str], command_start: int = 0):
    return _wrapped_source_command_index(words, command_start)


def _command_source_command_index(words: Sequence[str], command_start: int = 0):
    return _wrapped_source_command_index(words, command_start)


def _wrapped_source_command_index(words: Sequence[str], command_start: int = 0):
    index = command_start
    while index < len(words):
        next_index = _skip_leading_redirections(words, index)
        if next_index != index:
            index = next_index
            continue
        word = words[index]
        if word in SOURCE_COMMAND_NAMES:
            return index
        if word == "builtin":
            index += 1
            if index < len(words) and words[index] == "--":
                index += 1
            index = _skip_leading_redirections(words, index)
            continue
        if word == "command":
            index = _command_wrapped_command_index(words, index)
            if index is None:
                return None
            index = _skip_leading_redirections(words, index)
            continue
        return None
    return None


def _command_wrapped_command_index(words: Sequence[str], command_start: int):
    index = command_start + 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option == "--":
            index += 1
            break
        option_letters = option[1:]
        if "v" in option_letters or "V" in option_letters:
            return None
        if set(option_letters) != {"p"}:
            return None
        index += 1
    return index


def _trace_wrapper_separator(words: Sequence[str], start: int):
    for index in range(start, len(words)):
        if words[index] == "--":
            return index
    return None


def _contains_nested_source_command(text: str, depth: int):
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
                return _shell_body_contains_source(text[index + 1 :], depth + 1)
            if _shell_body_contains_source(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if text.startswith("$((", index):
            body, end_index = read_balanced_body(text, index + 3)
            if end_index is None:
                return _shell_body_contains_source(text[index + 3 :], depth + 1)
            if _contains_nested_source_command(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if text.startswith("$(", index):
            body, end_index = read_balanced_body(text, index + 2)
            if end_index is None:
                return _shell_body_contains_source(text[index + 2 :], depth + 1)
            if _shell_body_contains_source(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if not in_double_quote and (text.startswith("<(", index) or text.startswith(">(", index)):
            body, end_index = read_balanced_body(text, index + 2)
            if end_index is None:
                return _shell_body_contains_source(text[index + 2 :], depth + 1)
            if _shell_body_contains_source(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if not in_double_quote and text.startswith("((", index):
            body, end_index = read_balanced_body(text, index + 2)
            if end_index is None:
                return _shell_body_contains_source(text[index + 2 :], depth + 1)
            if _contains_nested_source_command(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if not in_double_quote and char == "(" and is_array_assignment_paren(text, index):
            body, end_index = read_balanced_body(text, index + 1)
            if end_index is None:
                return _shell_body_contains_source(text[index + 1 :], depth + 1)
            if _contains_nested_source_command(body, depth + 1):
                return True
            index = end_index + 1
            continue

        if not in_double_quote and char == "(" and index > 0 and text[index - 1] in "?*+@!":
            _body, end_index = read_balanced_body(text, index + 1)
            index = end_index + 1 if end_index is not None else index + 1
            continue

        if not in_double_quote and char == "(":
            body, end_index = read_balanced_body(text, index + 1)
            if end_index is None:
                return _shell_body_contains_source(text[index + 1 :], depth + 1)
            if _shell_body_contains_source(body, depth + 1):
                return True
            index = end_index + 1
            continue

        index += 1

    return False


def _shell_body_contains_source(body: str, depth: int):
    for line in body.splitlines() or [body]:
        if any(contains_source_command(command) for command in get_commands(line)):
            return True
    return _contains_nested_source_command(body, depth)
