"""Shared parsing helpers for runtime-traced Bash source commands.

These utilities keep trace, graph-validation, and observation-report code on the
same definition of which direct/wrapped source invocations are trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from methods.source_resolver import (
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

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


@dataclass(frozen=True)
class SourceCommandWords:
    source_path: str
    arguments: tuple[str, ...]
    command_index: int
    words: tuple[str, ...]


def clean_shell_word(word: str):
    return strip_shell_word_quotes(strip_trailing_shell_punctuation(word))


def strip_trailing_shell_punctuation(word: str):
    while word.endswith(";"):
        word = word[:-1]
    return word


def source_command_index(words: Sequence[str]):
    if not words:
        return None
    if words[0] in SOURCE_COMMAND_NAMES:
        return 0
    if words[0] == "builtin":
        return _builtin_source_command_index(words)
    if words[0] == "command":
        return _command_source_command_index(words)
    if words[0] == TRACE_DOT_ALIAS:
        return 0
    return None


def source_invocation_from_words(words: Sequence[str]):
    index = source_command_index(words)
    if index is None or index + 1 >= len(words):
        return None
    source_path = words[index + 1]
    if not source_path:
        return None
    return SourceCommandWords(
        source_path=source_path,
        arguments=tuple(words[index + 2:]),
        command_index=index,
        words=tuple(words),
    )


def source_invocation_from_command(command: str, *, normalize_trace_wrappers: bool = True):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None
    clean_words = tuple(clean_shell_word(word) for word in words)
    if not clean_words:
        return None
    if normalize_trace_wrappers:
        clean_words = normalized_trace_wrapper_words(clean_words) or clean_words
    return source_invocation_from_words(clean_words)


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
        if not source_words:
            return None
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
    return normalized is not None and source_command_index(normalized) is not None


def is_source_like_command_text(command: str):
    stripped = command.strip()
    if not stripped:
        return False
    if source_invocation_from_command(stripped) is not None:
        return True
    return any(stripped == prefix or stripped.startswith(f"{prefix} ") for prefix in SOURCE_LIKE_PREFIXES)


def shell_quote(value: str):
    if value and all(character.isalnum() or character in "@%_+=:,./-" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _builtin_source_command_index(words: Sequence[str]):
    index = 1
    if index < len(words) and words[index] == "--":
        index += 1
    if index < len(words) and words[index] in SOURCE_COMMAND_NAMES:
        return index
    return None


def _command_source_command_index(words: Sequence[str]):
    index = 1
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
    if index < len(words) and words[index] in SOURCE_COMMAND_NAMES:
        return index
    return None


def _trace_wrapper_separator(words: Sequence[str], start: int):
    for index in range(start, len(words)):
        if words[index] == "--":
            return index
    return None
