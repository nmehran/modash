from __future__ import annotations

import re
from collections.abc import Iterator


def read_backtick_body(text: str, start_index: int):
    body = []
    escaped = False
    index = start_index

    while index < len(text):
        char = text[index]
        if escaped:
            body.append(char)
            escaped = False
            index += 1
            continue

        if char == "\\":
            body.append(char)
            escaped = True
            index += 1
            continue

        if char == "`":
            return "".join(body), index

        body.append(char)
        index += 1

    return None, None


def read_balanced_body(text: str, start_index: int):
    body = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    depth = 1
    index = start_index

    while index < len(text):
        char = text[index]
        if escaped:
            body.append(char)
            escaped = False
            index += 1
            continue

        if char == "\\" and not in_single_quote:
            body.append(char)
            escaped = True
            index += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            body.append(char)
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            body.append(char)
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return "".join(body), index

        body.append(char)
        index += 1

    return None, None


def is_array_assignment_paren(text: str, paren_index: int):
    if paren_index == 0 or text[paren_index - 1] != "=":
        return False

    word_start = paren_index - 2
    while word_start >= 0 and not text[word_start].isspace() and text[word_start] not in ";&|":
        word_start -= 1

    assignment_name = text[word_start + 1:paren_index - 1]
    return bool(re.fullmatch(r"[a-zA-Z_]\w*(?:\[[^\]]+\])?\+?", assignment_name))


def subshell_bodies(command: str) -> Iterator[tuple[str, int, int]]:
    context_index = 0
    for index in _scan_shell_text(command, skip_double_quoted=True):
        if (
            command[index] == "("
            and not command.startswith("((", index)
            and not is_array_assignment_paren(command, index)
            and (index == 0 or command[index - 1] not in "$<>")
        ):
            body, end_index = read_balanced_body(command, index + 1)
            if end_index is None:
                continue
            context_index += 1
            yield body, index + 1, context_index


def command_substitution_bodies(command: str) -> Iterator[tuple[str, int, int]]:
    context_index = 0
    index = 0
    while index < len(command):
        next_index = _next_shell_index(command, index, skip_double_quoted=False)
        if next_index is None:
            break
        index = next_index
        if command.startswith("$((", index):
            _, end_index = read_balanced_body(command, index + 3)
            index = end_index + 1 if end_index is not None else index + 3
            continue
        if command.startswith("$(", index):
            body, end_index = read_balanced_body(command, index + 2)
            if end_index is None:
                index += 2
                continue
            context_index += 1
            yield body, index + 2, context_index
            index = end_index + 1
            continue
        index += 1


def process_substitution_bodies(command: str) -> Iterator[tuple[str, int, int]]:
    context_index = 0
    for index in _scan_shell_text(command, skip_double_quoted=True):
        if command.startswith("<(", index) or command.startswith(">(", index):
            body, end_index = read_balanced_body(command, index + 2)
            if end_index is None:
                continue
            context_index += 1
            yield body, index + 2, context_index


def top_level_pipeline_segments(command: str):
    segments = []
    current_start = 0
    in_single_quote = False
    in_double_quote = False
    escaped = False
    paren_depth = 0
    index = 0

    while index < len(command):
        char = command[index]
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

        if not in_single_quote and not in_double_quote:
            if command.startswith("||", index):
                index += 2
                continue

            if char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth:
                paren_depth -= 1

            if char == "|" and paren_depth == 0 and not command.startswith("||", index):
                segment = command[current_start:index].strip()
                if segment:
                    leading = len(command[current_start:index]) - len(command[current_start:index].lstrip())
                    segments.append((segment, current_start + leading))
                current_start = index + 1
                index += 1
                continue

        index += 1

    tail = command[current_start:].strip()
    if tail:
        leading = len(command[current_start:]) - len(command[current_start:].lstrip())
        segments.append((tail, current_start + leading))
    return tuple(segments)


def _scan_shell_text(command: str, *, skip_double_quoted: bool) -> Iterator[int]:
    index = 0
    while index < len(command):
        next_index = _next_shell_index(command, index, skip_double_quoted=skip_double_quoted)
        if next_index is None:
            return
        yield next_index
        index = next_index + 1


def _next_shell_index(command: str, index: int, *, skip_double_quoted: bool):
    in_single_quote = False
    in_double_quote = False
    escaped = False

    while index < len(command):
        char = command[index]
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

        if in_single_quote or (skip_double_quoted and in_double_quote):
            index += 1
            continue

        return index

    return None
