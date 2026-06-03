from __future__ import annotations

import re
from dataclasses import dataclass

from methods.source_resolver import (
    UnsupportedSourceError,
    contains_nested_source_command,
    contains_source_command,
    has_unsupported_shell_operator,
)

CONTROL_SOURCE_CONDITION_PATTERN = re.compile(
    r'^(?:if|elif|while|until)\s+(.+?)(?:\s*;\s*(?:then|do)\s*)?$',
    re.S,
)


@dataclass(frozen=True)
class ConditionAtom:
    text: str
    offset: int
    separator: str = ""
    negated: bool = False
    source_command: str | None = None
    source_expression: str | None = None
    source_offset: int | None = None


def source_logical_condition_atoms_from_text(condition: str) -> tuple[ConditionAtom, ...]:
    if '$(' in condition or '`' in condition:
        raise UnsupportedSourceError(f"unsupported dynamic if condition: {condition}")

    return tuple(
        parse_logical_condition_atom(text, offset, separator, condition)
        for separator, text, offset in split_logical_condition_segments(condition)
    )


def split_logical_condition_segments(condition: str) -> tuple[tuple[str, str, int], ...]:
    segments = []
    start = 0
    separator = ""
    in_single_quote = False
    in_double_quote = False
    in_double_bracket = False
    escaped = False
    paren_depth = 0
    index = 0

    while index < len(condition):
        char = condition[index]
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
        if not in_single_quote and not in_double_quote and condition.startswith("[[", index):
            in_double_bracket = True
            index += 2
            continue
        if in_double_bracket:
            if not in_single_quote and not in_double_quote and condition.startswith("]]", index):
                in_double_bracket = False
                index += 2
                continue
            index += 1
            continue
        if not in_single_quote and not in_double_quote:
            if char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth:
                paren_depth -= 1
            elif char == ";":
                raise UnsupportedSourceError(f"unsupported if condition list: {condition}")
            elif paren_depth == 0 and condition.startswith(("&&", "||"), index):
                atom_text = condition[start:index]
                stripped_offset = start + len(atom_text) - len(atom_text.lstrip())
                stripped_text = atom_text.strip()
                if not stripped_text:
                    raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
                segments.append((separator, stripped_text, stripped_offset))
                separator = condition[index:index + 2]
                index += 2
                start = index
                continue
            elif char == "|":
                raise UnsupportedSourceError(f"unsupported if condition pipeline: {condition}")
        index += 1

    atom_text = condition[start:]
    stripped_offset = start + len(atom_text) - len(atom_text.lstrip())
    stripped_text = atom_text.strip()
    if not stripped_text:
        raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
    segments.append((separator, stripped_text, stripped_offset))
    return tuple(segments)


def parse_logical_condition_atom(
    text: str,
    offset: int,
    separator: str,
    condition: str,
) -> ConditionAtom:
    negated = False
    command_text = text
    command_offset = offset
    while command_text == "!" or command_text.startswith("! "):
        negated = not negated
        if command_text == "!":
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        stripped = command_text[1:]
        command_offset += 1 + len(stripped) - len(stripped.lstrip())
        command_text = stripped.lstrip()

    source_match = re.fullmatch(r'((?:source)|\.)\s+(.+)', command_text, re.S)
    if source_match:
        command_name, source_expression = source_match.groups()
        source_expression = source_expression.strip()
        if not source_expression:
            raise UnsupportedSourceError(f"unsupported empty source condition: {condition}")
        if has_unsupported_shell_operator(source_expression):
            raise UnsupportedSourceError(f"unsupported source if condition: {condition}")
        return ConditionAtom(
            text=command_text,
            offset=command_offset,
            separator=separator,
            negated=negated,
            source_command=command_name,
            source_expression=source_expression,
            source_offset=command_offset + source_match.start(1),
        )

    if contains_source_command(command_text) or contains_nested_source_command(command_text):
        raise UnsupportedSourceError(f"unsupported source if condition: {condition}")

    return ConditionAtom(
        text=command_text,
        offset=command_offset,
        separator=separator,
        negated=negated,
    )
