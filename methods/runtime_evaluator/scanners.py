from __future__ import annotations

import re
import sys

from methods.shell_text import remove_comments
from methods.source_effects import (
    Assignment,
    CaseBlock,
    CStyleForLoop,
    ForLoop,
    FunctionDef,
    IfBlock,
    RawCommand,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend
from methods.source_resolver import extract_heredoc_delimiters, is_heredoc_end
from methods.source_traits import file_top_level_source_traits

FUNCTION_DECLARATION_PATTERN = re.compile(
    r"(?=(?:^|[;&|(){}]|\bthen\b|\bdo\b)\s*"
    r"(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*(?:\{|$))"
)
EVAL_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*eval(?:\s|$)")
TRAP_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*trap(?:\s|$)")
ALIAS_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*alias(?:\s|$)")
FUNCTION_CONTEXT_SENSITIVE_PATTERN = re.compile(
    r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*(?:local|caller|declare|typeset)(?:\s|$)"
    r"|\b(?:FUNCNAME|BASH_LINENO|PIPESTATUS)\b"
    r"|\$(?:_|{_})"
)
EMBEDDED_FUNCTION_DECLARATION_PATTERN = re.compile(
    r"(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*\{"
)


def main(argv):
    if not argv:
        return _usage("modash runtime source scanner expected a scanner name")
    command, *rest = argv
    if command == "positionals":
        return positionals_main(rest)
    if command == "functions":
        return functions_main(rest)
    return _usage(f"unknown modash runtime source scanner: {command}")


def positionals_main(argv):
    if len(argv) != 1:
        return _usage("modash positional scanner expected exactly one path")
    path = argv[0]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return 1
    try:
        _, has_top_level_positional_mutation = file_top_level_source_traits(path, content)
    except Exception as exc:  # pragma: no cover - defensive subprocess boundary
        print(f"modash positional scanner failed for {path}: {exc}", file=sys.stderr)
        return 2
    return 0 if has_top_level_positional_mutation else 1


def functions_main(argv):
    if len(argv) != 1:
        return _usage("modash function scanner expected exactly one path")
    path = argv[0]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return 1
    try:
        ir = LineParserFrontend().parse(path, content)
    except Exception as exc:  # pragma: no cover - defensive subprocess boundary
        print(f"modash function scanner failed for {path}: {exc}", file=sys.stderr)
        return 2
    records = set()
    collect_definitions(ir.nodes, "live", records)
    dead_lines = set()
    collect_exact_dead_lines(ir.nodes, dead_lines)
    for line_number, names in possible_function_names_by_line(content).items():
        if line_number in dead_lines:
            continue
        for name in names:
            if not any(record_name == name for _, record_name, _ in records):
                records.add(("unknown", name, line_number))
    for status, name, line_number in sorted(records):
        print(f"{status}\t{name}\t{line_number}")
    return 0


def condition_truth(condition):
    normalized = " ".join((condition or "").strip().split())
    if normalized in ("true", ":"):
        return True
    if normalized == "false":
        return False
    return None


def collect_definitions(nodes, status, records):
    for node in nodes:
        if isinstance(node, FunctionDef):
            records.add((status, node.name, node.location.line))
            for child in node.body:
                collect_unknown_definitions(child, records)
        elif isinstance(node, IfBlock):
            collect_if_block(node, status, records)
        else:
            collect_unknown_definitions(node, records)


def collect_unknown_definitions(node, records):
    if isinstance(node, FunctionDef):
        records.add(("unknown", node.name, node.location.line))
        for child in node.body:
            collect_unknown_definitions(child, records)
    elif isinstance(node, IfBlock):
        for branch in node.branches:
            collect_definitions(branch.body, "unknown", records)
    elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
        collect_definitions(node.body, "unknown", records)
    elif isinstance(node, CaseBlock):
        for arm in node.arms:
            collect_definitions(arm.body, "unknown", records)


def collect_if_block(node, status, records):
    branch_unknown = False
    for branch in node.branches:
        if branch.condition is None:
            collect_definitions(branch.body, "unknown" if branch_unknown else status, records)
            return

        truth = condition_truth(branch.condition)
        if truth is True:
            collect_definitions(branch.body, "unknown" if branch_unknown else status, records)
            return
        if truth is False:
            continue

        branch_unknown = True
        collect_definitions(branch.body, "unknown", records)


def collect_dead_function_lines(nodes, dead_lines):
    for node in nodes:
        if isinstance(node, FunctionDef):
            dead_lines.add(node.location.line)
            collect_dead_function_lines(node.body, dead_lines)
        elif isinstance(node, IfBlock):
            for branch in node.branches:
                collect_dead_function_lines(branch.body, dead_lines)
        elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            collect_dead_function_lines(node.body, dead_lines)
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                collect_dead_function_lines(arm.body, dead_lines)


def collect_dead_if_block_lines(node, dead_lines):
    for branch in node.branches:
        if branch.condition is None:
            return

        truth = condition_truth(branch.condition)
        if truth is True:
            return
        if truth is False:
            collect_dead_function_lines(branch.body, dead_lines)
            continue

        return


def collect_exact_dead_lines(nodes, dead_lines):
    for node in nodes:
        if isinstance(node, IfBlock):
            collect_dead_if_block_lines(node, dead_lines)
            for branch in node.branches:
                collect_exact_dead_lines(branch.body, dead_lines)
        elif isinstance(node, FunctionDef):
            collect_exact_dead_lines(node.body, dead_lines)
        elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            collect_exact_dead_lines(node.body, dead_lines)
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                collect_exact_dead_lines(arm.body, dead_lines)


def possible_function_names_by_line(content):
    active_heredocs = []
    names_by_line = {}
    for index, line in enumerate(content.splitlines(), start=1):
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue

        code_line = remove_comments(
            line,
            ["#"],
            exclusion_patterns=[r"\#\!.*"],
            escape_exclusions=False,
        )
        for match in FUNCTION_DECLARATION_PATTERN.finditer(code_line):
            name = match.group(1) or match.group(2)
            if name:
                names_by_line.setdefault(index, set()).add(name)
        if (
            EVAL_COMMAND_PATTERN.search(code_line)
            or TRAP_COMMAND_PATTERN.search(code_line)
            or ALIAS_COMMAND_PATTERN.search(code_line)
        ):
            found_embedded_function = False
            for match in EMBEDDED_FUNCTION_DECLARATION_PATTERN.finditer(code_line):
                name = match.group(1) or match.group(2)
                if name:
                    found_embedded_function = True
                    names_by_line.setdefault(index, set()).add(name)
            if not found_embedded_function:
                names_by_line.setdefault(index, set()).add("*")
        active_heredocs.extend(extract_heredoc_delimiters(line))
    return names_by_line


def function_context_sensitive_top_level_lines(path, content: str | None = None) -> tuple[int, ...]:
    if content is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            return ()
    try:
        ir = LineParserFrontend().parse(str(path), content)
    except Exception:
        return tuple(_text_function_context_sensitive_lines(content))
    lines: set[int] = set()
    _collect_function_context_sensitive_lines(ir.nodes, lines)
    return tuple(sorted(lines))


def inert_function_body_lines(path, content: str | None = None, active_lines=()) -> frozenset[int]:
    if content is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            return frozenset()
    try:
        ir = LineParserFrontend().parse(str(path), content)
    except Exception:
        return frozenset()
    lines: set[int] = set()
    _collect_inert_function_body_lines(ir.nodes, lines, set(active_lines))
    return frozenset(lines)


def _collect_inert_function_body_lines(nodes, lines: set[int], active_lines: set[int]) -> None:
    for node in nodes:
        if isinstance(node, FunctionDef):
            body_lines: set[int] = set()
            _collect_node_lines(node.body, body_lines)
            if not body_lines.intersection(active_lines):
                lines.update(body_lines)
            continue
        if isinstance(node, IfBlock):
            for branch in node.branches:
                _collect_inert_function_body_lines(branch.body, lines, active_lines)
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            _collect_inert_function_body_lines(node.body, lines, active_lines)
            continue
        if isinstance(node, CaseBlock):
            for arm in node.arms:
                _collect_inert_function_body_lines(arm.body, lines, active_lines)


def _collect_node_lines(nodes, lines: set[int]) -> None:
    for node in nodes:
        lines.add(node.location.line)
        if isinstance(node, FunctionDef):
            _collect_node_lines(node.body, lines)
        elif isinstance(node, IfBlock):
            for branch in node.branches:
                if branch.condition_location is not None:
                    lines.add(branch.condition_location.line)
                _collect_node_lines(branch.body, lines)
        elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            _collect_node_lines(node.body, lines)
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                _collect_node_lines(arm.body, lines)


def _collect_function_context_sensitive_lines(nodes, lines: set[int]) -> None:
    for node in nodes:
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (Assignment, RawCommand)):
            if _function_context_sensitive_text(node.text):
                lines.add(node.location.line)
            continue
        if isinstance(node, IfBlock):
            for branch in node.branches:
                if branch.condition and _function_context_sensitive_text(branch.condition):
                    location = branch.condition_location or node.location
                    lines.add(location.line)
                _collect_function_context_sensitive_lines(branch.body, lines)
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            _collect_function_context_sensitive_lines(node.body, lines)
            continue
        if isinstance(node, CaseBlock):
            for arm in node.arms:
                _collect_function_context_sensitive_lines(arm.body, lines)


def _text_function_context_sensitive_lines(content: str):
    active_heredocs = []
    for index, line in enumerate(content.splitlines(), start=1):
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        code_line = remove_comments(
            line,
            ["#"],
            exclusion_patterns=[r"\#\!.*"],
            escape_exclusions=False,
        )
        if _function_context_sensitive_text(code_line):
            yield index
        active_heredocs.extend(extract_heredoc_delimiters(line))


def _function_context_sensitive_text(text: str) -> bool:
    return FUNCTION_CONTEXT_SENSITIVE_PATTERN.search(text) is not None


def _usage(message):
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
