from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .shared import *  # noqa: F401,F403
from .case import SourceFrontendCaseMixin
from .commands import SourceFrontendCommandMixin
from .functions import SourceFrontendFunctionMixin
from .loops import SourceFrontendLoopMixin


class ParserFrontend(Protocol):
    def parse(self, path: Path | str, content: str) -> ScriptIR:
        ...


class LineParserFrontend(
    SourceFrontendCaseMixin,
    SourceFrontendCommandMixin,
    SourceFrontendFunctionMixin,
    SourceFrontendLoopMixin,
):
    """Current parser frontend backed by the existing line-level splitter.

    Its output is the stable IR contract that parser improvements must preserve.
    """

    def parse(self, path: Path | str, content: str) -> ScriptIR:
        script_path = Path(path)
        lines = content.splitlines()
        return ScriptIR(path=script_path, nodes=tuple(self._parse_lines(script_path, lines, 0, len(lines))))

    def _parse_lines(self, script_path: Path, lines: list[str], start_index: int, end_index: int):
        nodes = []
        active_heredocs = []
        control_depth = 0
        line_index = start_index

        while line_index < end_index:
            line_number = line_index + 1
            line = lines[line_index]
            if active_heredocs:
                if is_heredoc_end(line, active_heredocs[0]):
                    active_heredocs.pop(0)
                line_index += 1
                continue

            code_line = remove_comments(
                line,
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )

            if_block, next_line_index = self._parse_if_block(script_path, line_number, code_line, lines, line_index)
            if if_block:
                nodes.append(if_block)
                line_index = next_line_index
                continue

            function_def, next_line_index = self._parse_function_def(
                script_path,
                line_number,
                code_line,
                lines,
                line_index,
            )
            if function_def:
                if isinstance(function_def, tuple):
                    nodes.extend(function_def)
                else:
                    nodes.append(function_def)
                line_index = next_line_index
                continue

            case_block, next_line_index = self._parse_case_block(script_path, line_number, code_line, lines, line_index)
            if case_block:
                nodes.append(case_block)
                line_index = next_line_index
                continue

            c_for_loop, next_line_index = self._parse_c_style_for_loop(
                script_path,
                line_number,
                code_line,
                lines,
                line_index,
            )
            if c_for_loop:
                nodes.append(c_for_loop)
                line_index = next_line_index
                continue

            for_loop, next_line_index = self._parse_for_loop(script_path, line_number, code_line, lines, line_index)
            if for_loop:
                nodes.append(for_loop)
                line_index = next_line_index
                continue

            while_loop, next_line_index = self._parse_while_loop(script_path, line_number, code_line, lines, line_index)
            if while_loop:
                nodes.append(while_loop)
                line_index = next_line_index
                continue

            control_flow_source_ranges = self._control_flow_source_ranges(code_line, control_depth)
            nodes.extend(self._parse_line(script_path, line_number, code_line, control_flow_source_ranges))
            control_depth = self._next_control_depth(code_line, control_depth)
            active_heredocs.extend(extract_heredoc_delimiters(line))
            line_index += 1

        return nodes

    def _parse_line(self, script_path: Path, line_number: int, line: str, control_flow_source_ranges):
        nodes = []
        source_spans = []

        if first_top_level_pipeline_index(line) is None:
            for match in SOURCE_PATTERN.finditer(line):
                if self._source_match_is_nested_shell(match):
                    continue
                separator, command_name, arguments = match.groups()
                if not command_name:
                    continue

                text = ''.join(part or '' for part in (separator, command_name, arguments)).strip()
                invocation = source_command_invocation(text, stop_at_shell_control=True)
                if invocation is None:
                    parsed_command_name = command_name.strip()
                    source_expression = (arguments or '').strip()
                    source_site = ""
                else:
                    parsed_command_name = invocation.command_name
                    source_expression = invocation.source_expression
                    source_site = invocation.source_site
                column = match.start(2) + 1
                is_control_flow = self._column_in_ranges(column, control_flow_source_ranges)
                nodes.append(SourceSite(
                    location=SourceLocation(script_path, line_number, column),
                    text=text,
                    command_name=parsed_command_name,
                    source_expression=source_expression,
                    source_site=source_site,
                    separator=(separator or '').strip(),
                    is_control_flow=is_control_flow,
                ))
                source_spans.append(match.span())

        for command, command_start, command_end in self._commands_with_spans(line):
            if not command:
                continue
            if any(self._spans_overlap((command_start, command_end), source_span) for source_span in source_spans):
                continue
            if contains_source_command(command):
                if first_top_level_pipeline_index(command) is not None:
                    nodes.append(self._command_node(script_path, line_number, line, command))
                    continue
                nodes.append(self._fallback_source_site(
                    script_path,
                    line_number,
                    line,
                    command,
                    control_flow_source_ranges,
                ))
                continue
            nodes.append(self._command_node(script_path, line_number, line, command))

        return sorted(nodes, key=lambda node: node.location.column)

    @staticmethod
    def _commands_with_spans(line: str):
        commands = []
        search_start = 0
        for command in get_commands(line):
            command_start = line.find(command, search_start)
            if command_start < 0:
                command_start = line.find(command)
            if command_start < 0:
                command_start = 0
            command_end = command_start + len(command)
            commands.append((command, command_start, command_end))
            search_start = command_end
        return tuple(commands)

    @staticmethod
    def _spans_overlap(left, right):
        return left[0] < right[1] and right[0] < left[1]

    @staticmethod
    def _source_match_is_nested_shell(match):
        return match.group(0).lstrip().startswith('$(')

    def _parse_if_block(self, script_path: Path, line_number: int, code_line: str, lines: list[str], line_index: int):
        commands = self._if_block_commands(code_line)
        if not commands or not IF_COMMAND_PATTERN.match(commands[0]):
            return None, line_index + 1

        branches = []
        current_condition = None
        current_condition_location = None
        current_condition_text = ""
        current_keyword = None
        current_body = []
        saw_then = False
        nested_depth = 0
        index = line_index

        while index < len(lines):
            line = lines[index]
            code = remove_comments(
                line,
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )

            if (
                current_keyword is not None
                and saw_then
                and not nested_depth
                and self._line_starts_function_definition(code)
            ):
                current_body.append((index + 1, code))
                index += 1
                continue

            for command in self._if_block_commands(code):
                stripped_command = command.strip()

                if nested_depth:
                    current_body.append((index + 1, command))
                    if IF_COMMAND_PATTERN.match(stripped_command):
                        nested_depth += 1
                    elif FI_COMMAND_PATTERN.match(stripped_command):
                        nested_depth -= 1
                    continue

                if match := IF_COMMAND_PATTERN.match(stripped_command):
                    if current_keyword is not None:
                        current_body.append((index + 1, command))
                        nested_depth = 1
                        continue
                    current_keyword = "if"
                    current_condition = match.group(1).strip()
                    current_condition_location = SourceLocation(
                        script_path,
                        index + 1,
                        self._command_column(code, command) + match.start(1),
                    )
                    current_condition_text = current_condition
                    saw_then = False
                    continue

                if match := ELIF_COMMAND_PATTERN.match(stripped_command):
                    if current_keyword is None:
                        return None, line_index + 1
                    branches.append(self._if_branch(
                        script_path,
                        current_keyword,
                        current_condition,
                        current_body,
                        current_condition_location,
                        current_condition_text,
                    ))
                    current_keyword = "elif"
                    current_condition = match.group(1).strip()
                    current_condition_location = SourceLocation(
                        script_path,
                        index + 1,
                        self._command_column(code, command) + match.start(1),
                    )
                    current_condition_text = current_condition
                    current_body = []
                    saw_then = False
                    continue

                if match := THEN_COMMAND_PATTERN.match(stripped_command):
                    if current_keyword not in {"if", "elif"}:
                        return None, line_index + 1
                    saw_then = True
                    if match.group(1):
                        current_body.append((index + 1, match.group(1).strip()))
                    continue

                if match := ELSE_COMMAND_PATTERN.match(stripped_command):
                    if current_keyword is None:
                        return None, line_index + 1
                    branches.append(self._if_branch(
                        script_path,
                        current_keyword,
                        current_condition,
                        current_body,
                        current_condition_location,
                        current_condition_text,
                    ))
                    current_keyword = "else"
                    current_condition = None
                    current_condition_location = None
                    current_condition_text = ""
                    current_body = []
                    saw_then = True
                    if match.group(1):
                        current_body.append((index + 1, match.group(1).strip()))
                    continue

                if FI_COMMAND_PATTERN.match(stripped_command):
                    if current_keyword is None or (current_keyword in {"if", "elif"} and not saw_then):
                        return None, line_index + 1
                    branches.append(self._if_branch(
                        script_path,
                        current_keyword,
                        current_condition,
                        current_body,
                        current_condition_location,
                        current_condition_text,
                    ))
                    column = self._command_column(code_line, "if")
                    return IfBlock(
                        location=SourceLocation(script_path, line_number, column),
                        text=code_line.strip(),
                        branches=tuple(branches),
                        end_location=SourceLocation(script_path, index + 1, 1),
                    ), index + 1

                if current_keyword is None or (current_keyword in {"if", "elif"} and not saw_then):
                    return None, line_index + 1
                current_body.append((index + 1, command))

            index += 1

        return None, line_index + 1

    @staticmethod
    def _if_block_commands(code: str):
        stripped = code.strip()
        if not re.match(r'^(?:if|elif)\s+', stripped):
            return get_commands(code)

        if match := IF_INLINE_THEN_PATTERN.match(stripped):
            header, tail = match.groups()
            commands = [header.strip()]
            tail_commands = get_commands(tail or "")
            if tail_commands:
                commands.append(f"then {tail_commands[0]}")
                commands.extend(tail_commands[1:])
            else:
                commands.append("then")
            return commands

        return [stripped]

    def _if_branch(
        self,
        script_path: Path,
        keyword: str,
        condition: str | None,
        body_lines,
        condition_location: SourceLocation | None,
        condition_text: str,
    ):
        return IfBranch(
            condition=condition,
            body=self._parse_loop_body(script_path, body_lines),
            keyword=keyword,
            condition_location=condition_location,
            condition_text=condition_text,
        )
