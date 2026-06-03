from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from methods.regex.patterns import SOURCE_PATTERN, VARIABLE_ASSIGNMENT_PATTERN
from methods.regex.utilities import remove_comments
from methods.source_effects import (
    ArrayAssignment,
    Assignment,
    CaseArm,
    CaseBlock,
    CdCommand,
    CStyleForLoop,
    FunctionDef,
    ForLoop,
    IfBlock,
    IfBranch,
    RawCommand,
    ScriptIR,
    SetCommand,
    SourceLocation,
    SourceSite,
    WhileLoop,
)
from methods.source_patterns import extglob_operator_at
from methods.source_resolver import (
    contains_source_command,
    contains_nested_source_command,
    ends_unsupported_control_block,
    extract_heredoc_delimiters,
    is_unsupported_control_flow_source,
    is_heredoc_end,
    parse_shell_words,
    parse_shell_words_preserving_quotes,
    source_command_invocation,
    strip_shell_word_quotes,
    starts_unsupported_control_block,
    source_command_index,
    UnsupportedSourceError,
)
from methods.shell.line import first_top_level_pipeline_index, get_commands

ARRAY_ASSIGNMENT_PATTERN = re.compile(r'^(?:(declare)\s+(-[aA])\s+)?([a-zA-Z_]\w*)(\+?)=\((.*)\)$')
ARRAY_INDEX_ASSIGNMENT_PATTERN = re.compile(r'^([a-zA-Z_]\w*)\[([^\]]+)\](\+?)=(.*)$')
FUNCTION_HEADER_PATTERN = re.compile(
    r'^\s*(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*\{\s*(.*)$'
)
FUNCTION_SIGNATURE_PATTERN = re.compile(
    r'^\s*(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*$'
)
FUNCTION_OPEN_PATTERN = re.compile(r'^\s*\{\s*(.*)$')
FOR_LOOP_PATTERN = re.compile(r'^\s*for\s+([a-zA-Z_]\w*)\s+in\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
FOR_HEADER_PATTERN = re.compile(r'^\s*for\s+([a-zA-Z_]\w*)\s+in\s+(.+?)\s*$')
C_FOR_LOOP_PATTERN = re.compile(r'^\s*for\s*\(\(\s*(.*?)\s*;\s*(.*?)\s*;\s*(.*?)\s*\)\)\s*;\s*do(?:\s*(.*))?$')
C_FOR_HEADER_PATTERN = re.compile(r'^\s*for\s*\(\(\s*(.*?)\s*;\s*(.*?)\s*;\s*(.*?)\s*\)\)\s*$')
WHILE_LOOP_PATTERN = re.compile(r'^\s*(while|until)\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
WHILE_HEADER_PATTERN = re.compile(r'^\s*(while|until)\s+(.+?)\s*$')
PIPELINE_WHILE_LOOP_PATTERN = re.compile(r'^\s*(.+?)\s*\|\s*while\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
PIPELINE_WHILE_HEADER_PATTERN = re.compile(r'^\s*(.+?)\s*\|\s*while\s+(.+?)\s*$')
DO_LINE_PATTERN = re.compile(r'^\s*do\s*$')
INLINE_DONE_PATTERN = re.compile(r'^(.*?)(?:;\s*)?done(?:\s+(.*))?$')
IF_COMMAND_PATTERN = re.compile(r'^\s*if\s+(.+?)\s*$')
ELIF_COMMAND_PATTERN = re.compile(r'^\s*elif\s+(.+?)\s*$')
IF_INLINE_THEN_PATTERN = re.compile(r'^\s*((?:if|elif)\s+.+?)\s*;\s*then(?:\s*(.*))?\s*$')
THEN_COMMAND_PATTERN = re.compile(r'^\s*then(?:\s+(.+?))?\s*$')
ELSE_COMMAND_PATTERN = re.compile(r'^\s*else(?:\s+(.+?))?\s*$')
FI_COMMAND_PATTERN = re.compile(r'^\s*fi\s*$')
ESAC_COMMAND_PATTERN = re.compile(r'^\s*esac\s*$')
CASE_TERMINATOR_COMMANDS = {
    "__MODASH_CASE_TERM_END__": ";;",
    "__MODASH_CASE_TERM_FALLTHROUGH__": ";&",
    "__MODASH_CASE_TERM_FALLTHROUGH_TEST__": ";;&",
}


class ParserFrontend(Protocol):
    def parse(self, path: Path | str, content: str) -> ScriptIR:
        ...


class LineParserFrontend:
    """Current parser frontend backed by the existing line-level splitter.

    This is a compatibility frontend, not the final Bash parser. Its output is
    the stable contract that a future real parser adapter must preserve.
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
                column = match.start(2) + 1
                is_control_flow = self._column_in_ranges(column, control_flow_source_ranges)
                nodes.append(SourceSite(
                    location=SourceLocation(script_path, line_number, column),
                    text=text,
                    command_name=command_name.strip(),
                    source_expression=(arguments or '').strip(),
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

    def _command_node(self, script_path: Path, line_number: int, line: str, command: str):
        location = SourceLocation(script_path, line_number, self._command_column(line, command))
        separator = self._command_separator(line, command)

        if array_assignment := self._array_assignment_node(location, command):
            return array_assignment

        if assignment := self._assignment_node(location, command):
            return assignment

        if cd_command := self._cd_node(location, command):
            return cd_command

        if set_command := self._set_node(location, command):
            return set_command

        return RawCommand(location=location, text=command, separator=separator)

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

    @staticmethod
    def _line_starts_function_definition(line: str):
        return bool(FUNCTION_HEADER_PATTERN.match(line) or FUNCTION_SIGNATURE_PATTERN.match(line))

    def _parse_function_def(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                            line_index: int):
        match = FUNCTION_HEADER_PATTERN.match(code_line)
        if not match:
            signature_match = FUNCTION_SIGNATURE_PATTERN.match(code_line)
            if not signature_match:
                return None, line_index + 1
            opening_line_index, opening_code_line = self._next_function_opening_line(lines, line_index + 1)
            if opening_line_index is None:
                return None, line_index + 1
            open_match = FUNCTION_OPEN_PATTERN.match(opening_code_line)
            if not open_match:
                return None, line_index + 1
            function_name = signature_match.group(1) or signature_match.group(2)
            first_tail = open_match.group(1) or ""
            first_tail_line_number = opening_line_index + 1
            body_start_index = opening_line_index + 1
        else:
            function_name = match.group(1) or match.group(2)
            first_tail = match.group(3) or ""
            first_tail_line_number = line_number
            body_start_index = line_index + 1

        body_lines = []
        before_close, has_close, trailing = self._split_function_closing_brace(first_tail)
        if before_close.strip():
            body_lines.append((first_tail_line_number, before_close.strip()))
        if has_close:
            return self._function_definition_nodes(
                script_path,
                line_number,
                code_line,
                function_name,
                body_lines,
                trailing,
            ), body_start_index

        body_index = body_start_index
        active_heredocs = []
        nested_function_depth = 0
        while body_index < len(lines):
            body_line_number = body_index + 1
            body_line = lines[body_index]
            body_code_line = remove_comments(
                body_line,
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )

            if active_heredocs:
                body_lines.append((body_line_number, body_code_line))
                if is_heredoc_end(body_code_line, active_heredocs[0]):
                    active_heredocs.pop(0)
                body_index += 1
                continue

            if nested_function_depth:
                body_lines.append((body_line_number, body_code_line))
                if self._line_starts_function_definition(body_code_line):
                    _, nested_has_close, _ = self._split_function_closing_brace(body_code_line)
                    if not nested_has_close:
                        nested_function_depth += 1
                else:
                    _, nested_has_close, _ = self._split_function_closing_brace(body_code_line)
                    if nested_has_close:
                        nested_function_depth -= 1
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                body_index += 1
                continue

            if self._line_starts_function_definition(body_code_line):
                body_lines.append((body_line_number, body_code_line))
                _, nested_has_close, _ = self._split_function_closing_brace(body_code_line)
                if not nested_has_close:
                    nested_function_depth = 1
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                body_index += 1
                continue

            before_close, has_close, trailing = self._split_function_closing_brace(body_code_line)
            if has_close:
                if self._function_tail_command_text(trailing) is None:
                    raw_text = "\n".join(lines[line_index:body_index + 1])
                    return self._unsupported_function_node(script_path, line_number, raw_text), body_index + 1
                if before_close.strip():
                    body_lines.append((body_line_number, before_close.strip()))
                return self._function_definition_nodes(
                    script_path,
                    line_number,
                    code_line,
                    function_name,
                    body_lines,
                    trailing,
                ), body_index + 1

            body_lines.append((body_line_number, body_code_line))
            active_heredocs.extend(extract_heredoc_delimiters(body_line))
            body_index += 1

        return None, line_index + 1

    def _function_definition_nodes(
        self,
        script_path: Path,
        line_number: int,
        code_line: str,
        function_name: str,
        body_lines,
        trailing: str,
    ):
        tail_command = self._function_tail_command_text(trailing)
        if tail_command is None:
            return (self._unsupported_function_node(script_path, line_number, code_line),)

        function_def = FunctionDef(
            location=SourceLocation(script_path, line_number, self._command_column(code_line, function_name)),
            text=code_line.strip(),
            name=function_name,
            body=self._parse_loop_body(script_path, body_lines),
        )
        if not tail_command:
            return (function_def,)

        tail_nodes = self._parse_line(
            script_path,
            line_number,
            tail_command,
            self._control_flow_source_ranges(tail_command, 0),
        )
        return (function_def, *tail_nodes)

    @staticmethod
    def _unsupported_function_node(script_path: Path, line_number: int, text: str):
        return RawCommand(
            location=SourceLocation(script_path, line_number, 1),
            text=text.strip(),
        )

    @staticmethod
    def _function_tail_command_text(trailing: str):
        tail = re.sub(r'^(?:;\s*)+', '', trailing.strip())
        if not tail:
            return ""

        commands = get_commands(tail)
        if not commands:
            return ""

        if re.match(r'^(?:\d*(?:<>|>>|>|<|>\||<<-?|<<<)|&>>?)', commands[0].strip()):
            return "" if len(commands) == 1 else None

        return tail

    @staticmethod
    def _next_function_opening_line(lines: list[str], line_index: int):
        while line_index < len(lines):
            code_line = remove_comments(
                lines[line_index],
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )
            if code_line.strip():
                return line_index, code_line
            line_index += 1
        return None, ""

    @staticmethod
    def _split_function_closing_brace(text: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False

        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue

            if char == '\\' and not in_single_quote:
                escaped = True
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                continue

            if (
                char == "}"
                and not in_single_quote
                and not in_double_quote
                and LineParserFrontend._is_function_closing_brace(text, index)
            ):
                return text[:index], True, text[index + 1:]

        return text, False, ""

    @staticmethod
    def _is_function_closing_brace(text: str, index: int):
        previous_char = text[index - 1] if index > 0 else ""
        next_index = index + 1
        next_char = text[next_index] if next_index < len(text) else ""
        previous_boundary = index == 0 or previous_char.isspace() or previous_char == ";"
        next_boundary = next_index == len(text) or next_char.isspace() or next_char == ";"
        return previous_boundary and next_boundary

    def _parse_case_block(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                          line_index: int):
        header = self._split_case_header(code_line)
        if not header:
            return None, line_index + 1

        subject, first_tail = header
        arms = []
        current_patterns = None
        current_body = []
        current_terminator = ";;"
        index = line_index

        while index < len(lines):
            if index == line_index:
                commands = self._case_commands(first_tail or "")
            else:
                code = remove_comments(
                    lines[index],
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                commands = self._case_commands(code)

            for command in commands:
                stripped_command = command.strip()

                if terminator := CASE_TERMINATOR_COMMANDS.get(stripped_command):
                    if current_patterns is None:
                        return None, line_index + 1
                    current_terminator = terminator
                    arms.append(self._case_arm(script_path, current_patterns, current_body, current_terminator))
                    current_patterns = None
                    current_body = []
                    current_terminator = ";;"
                    continue

                if ESAC_COMMAND_PATTERN.match(stripped_command):
                    if current_patterns is not None:
                        arms.append(self._case_arm(script_path, current_patterns, current_body, current_terminator))
                    if not arms:
                        return None, line_index + 1
                    column = self._command_column(code_line, "case")
                    return CaseBlock(
                        location=SourceLocation(script_path, line_number, column),
                        text=code_line.strip(),
                        subject=subject.strip(),
                        arms=tuple(arms),
                    ), index + 1

                arm = self._split_case_arm_header(stripped_command)
                if arm:
                    if current_patterns is not None:
                        return None, line_index + 1
                    patterns, body_command = arm
                    current_patterns = patterns
                    current_body = []
                    current_terminator = ";;"
                    if body_command:
                        current_body.append((index + 1, body_command))
                    continue

                if current_patterns is None:
                    return None, line_index + 1
                current_body.append((index + 1, command))

            index += 1

        return None, line_index + 1

    @staticmethod
    def _split_case_header(line: str):
        match = re.match(r'^\s*case(?:\s+|$)', line)
        if not match:
            return None

        rest = line[match.end():]
        for index in LineParserFrontend._unquoted_case_in_indices(rest):
            subject = rest[:index].strip()
            if not subject:
                continue

            try:
                subject_words = parse_shell_words_preserving_quotes(subject)
            except UnsupportedSourceError:
                continue
            if len(subject_words) != 1:
                continue

            return subject, rest[index + 2:].strip()

        return None

    @staticmethod
    def _unquoted_case_in_indices(text: str):
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

            if char == '\\' and not in_single_quote:
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

            if not in_single_quote and not in_double_quote and text.startswith("in", index):
                previous_char = text[index - 1] if index > 0 else ""
                next_index = index + 2
                next_char = text[next_index] if next_index < len(text) else ""
                previous_boundary = index > 0 and previous_char.isspace()
                next_boundary = next_index == len(text) or next_char.isspace()
                if previous_boundary and next_boundary:
                    yield index

            index += 1

    def _case_arm(self, script_path: Path, patterns: tuple[str, ...], body_lines, terminator: str):
        return CaseArm(
            patterns=patterns,
            body=self._parse_loop_body(script_path, body_lines),
            terminator=terminator,
        )

    @staticmethod
    def _case_commands(line: str):
        return get_commands(LineParserFrontend._mark_case_terminators(line))

    @staticmethod
    def _mark_case_terminators(line: str):
        output = []
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0

        while index < len(line):
            char = line[index]
            if escaped:
                output.append(char)
                escaped = False
                index += 1
                continue

            if char == '\\' and not in_single_quote:
                output.append(char)
                escaped = True
                index += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                output.append(char)
                index += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                output.append(char)
                index += 1
                continue

            if not in_single_quote and not in_double_quote:
                if line.startswith(";;&", index):
                    output.append("; __MODASH_CASE_TERM_FALLTHROUGH_TEST__ ;")
                    index += 3
                    continue
                if line.startswith(";&", index):
                    output.append("; __MODASH_CASE_TERM_FALLTHROUGH__ ;")
                    index += 2
                    continue
                if line.startswith(";;", index):
                    output.append("; __MODASH_CASE_TERM_END__ ;")
                    index += 2
                    continue

            output.append(char)
            index += 1

        return ''.join(output)

    @staticmethod
    def _split_case_arm_header(command: str):
        pattern_end = LineParserFrontend._case_pattern_end(command)
        if pattern_end <= 0:
            return None

        pattern_text = command[:pattern_end].strip()
        if pattern_text.startswith("("):
            pattern_text = pattern_text[1:].strip()
        if not pattern_text:
            return None

        patterns = tuple(part.strip() for part in LineParserFrontend._split_case_patterns(pattern_text) if part.strip())
        if not patterns:
            return None
        return patterns, command[pattern_end + 1:].strip()

    @staticmethod
    def _case_pattern_end(command: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False
        bracket_depth = 0
        extglob_depth = 0
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

            if in_single_quote or in_double_quote:
                index += 1
                continue

            if char == "[":
                bracket_depth += 1
                index += 1
                continue
            if char == "]" and bracket_depth:
                bracket_depth -= 1
                index += 1
                continue

            if bracket_depth == 0 and extglob_operator_at(command, index) is not None:
                extglob_depth += 1
                index += 2
                continue

            if char == ")" and bracket_depth == 0:
                if extglob_depth:
                    extglob_depth -= 1
                    index += 1
                    continue
                return index

            index += 1

        return -1

    @staticmethod
    def _split_case_patterns(text: str):
        parts = []
        current = []
        in_single_quote = False
        in_double_quote = False
        escaped = False
        bracket_depth = 0
        extglob_depth = 0
        index = 0

        while index < len(text):
            char = text[index]
            if escaped:
                current.append(char)
                escaped = False
                index += 1
                continue

            if char == "\\" and not in_single_quote:
                current.append(char)
                escaped = True
                index += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current.append(char)
                index += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current.append(char)
                index += 1
                continue

            if not in_single_quote and not in_double_quote:
                if char == "[":
                    bracket_depth += 1
                elif char == "]" and bracket_depth:
                    bracket_depth -= 1
                elif bracket_depth == 0 and extglob_operator_at(text, index) is not None:
                    extglob_depth += 1
                    current.append(char)
                    current.append("(")
                    index += 2
                    continue
                elif char == ")" and bracket_depth == 0 and extglob_depth:
                    extglob_depth -= 1
                elif char == "|" and bracket_depth == 0 and extglob_depth == 0:
                    parts.append("".join(current))
                    current = []
                    index += 1
                    continue

            current.append(char)
            index += 1

        parts.append("".join(current))
        return parts

    @staticmethod
    def _split_unquoted(text: str, separator: str):
        parts = []
        current = []
        in_single_quote = False
        in_double_quote = False
        escaped = False

        for char in text:
            if escaped:
                current.append(char)
                escaped = False
                continue

            if char == '\\' and not in_single_quote:
                current.append(char)
                escaped = True
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current.append(char)
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current.append(char)
                continue

            if char == separator and not in_single_quote and not in_double_quote:
                parts.append(''.join(current))
                current = []
                continue

            current.append(char)

        parts.append(''.join(current))
        return parts

    @staticmethod
    def _unquoted_index(text: str, needle: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False

        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue

            if char == '\\' and not in_single_quote:
                escaped = True
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                continue

            if char == needle and not in_single_quote and not in_double_quote:
                return index

        return -1

    def _parse_c_style_for_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                                line_index: int):
        match = C_FOR_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            init, condition, update, inline_body = match.groups()
        else:
            match = C_FOR_HEADER_PATTERN.match(code_line)
            if not match or line_index + 1 >= len(lines):
                return None, line_index + 1

            do_line_index = line_index + 1
            do_code_line = remove_comments(
                lines[do_line_index],
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )
            do_match = DO_LINE_PATTERN.match(do_code_line)
            if not do_match:
                return None, line_index + 1

            init, condition, update = match.groups()
            inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        next_line_index = body_start_index

        if inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                if stripped_body_line == "done" and control_depth == 0:
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        column = self._command_column(code_line, "for")
        return CStyleForLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            init=init.strip(),
            condition=condition.strip(),
            update=update.strip(),
            body=self._parse_loop_body(script_path, body_lines),
        ), next_line_index

    def _parse_for_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str], line_index: int):
        match = FOR_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            variable, words_text, inline_body = match.groups()
        else:
            match = FOR_HEADER_PATTERN.match(code_line)
            if not match or line_index + 1 >= len(lines):
                return None, line_index + 1

            do_line_index = line_index + 1
            do_code_line = remove_comments(
                lines[do_line_index],
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )
            do_match = DO_LINE_PATTERN.match(do_code_line)
            if not do_match:
                return None, line_index + 1

            variable, words_text = match.groups()
            inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        next_line_index = body_start_index
        end_line_number = do_line_index + 1
        trailing = ""

        if inline_body is not None and inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            trailing = (done_match.group(2) or "").strip()
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                if stripped_body_line == "done" and control_depth == 0:
                    end_line_number = body_line_number
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        loop_words, is_exact = self._parse_loop_words(words_text)
        body = self._parse_loop_body(script_path, body_lines)
        column = self._command_column(code_line, "for")

        return ForLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            variable=variable,
            words=loop_words,
            body=body,
            words_text=words_text.strip(),
            is_exact=is_exact,
            end_location=SourceLocation(script_path, end_line_number, 1),
            trailing=trailing,
        ), next_line_index

    def _parse_while_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                          line_index: int):
        producer = ""
        match = PIPELINE_WHILE_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            producer, condition, inline_body = match.groups()
            keyword = "while"
        else:
            match = WHILE_LOOP_PATTERN.match(code_line)
            if match:
                keyword, condition, inline_body = match.groups()
            else:
                match = PIPELINE_WHILE_HEADER_PATTERN.match(code_line)
                if match:
                    producer, condition = match.groups()
                    keyword = "while"
                else:
                    match = WHILE_HEADER_PATTERN.match(code_line)
                    if not match or line_index + 1 >= len(lines):
                        return None, line_index + 1
                    keyword, condition = match.groups()

                if line_index + 1 >= len(lines):
                    return None, line_index + 1

                do_line_index = line_index + 1
                do_code_line = remove_comments(
                    lines[do_line_index],
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                do_match = DO_LINE_PATTERN.match(do_code_line)
                if not do_match:
                    return None, line_index + 1
                inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        trailing = ""
        end_line_number = line_number
        next_line_index = body_start_index

        if inline_body is not None and inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            trailing = (done_match.group(2) or "").strip()
            end_line_number = do_line_index + 1
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                done_match = re.match(r'^done(?:\s+(.*))?$', stripped_body_line)
                if done_match and control_depth == 0:
                    trailing = (done_match.group(1) or "").strip()
                    end_line_number = body_line_number
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        column = self._command_column(code_line, keyword)
        return WhileLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            keyword=keyword,
            condition=condition.strip(),
            body=self._parse_loop_body(script_path, body_lines),
            trailing=trailing,
            end_location=SourceLocation(script_path, end_line_number, 1),
            producer=producer.strip(),
        ), next_line_index

    @staticmethod
    def _parse_loop_words(words_text: str):
        try:
            raw_words = parse_shell_words_preserving_quotes(words_text)
            return tuple(strip_shell_word_quotes(word) for word in raw_words), True
        except UnsupportedSourceError:
            return (), False

    def _parse_loop_body(self, script_path: Path, body_lines):
        if not body_lines:
            return ()

        start_index = min(line_number for line_number, _ in body_lines) - 1
        end_index = max(line_number for line_number, _ in body_lines)
        lines = [""] * end_index
        for line_number, code_line in body_lines:
            lines[line_number - 1] = code_line

        return tuple(self._parse_lines(script_path, lines, start_index, end_index))

    @staticmethod
    def _command_column(line: str, command: str):
        column = line.find(command)
        return 1 if column < 0 else column + 1

    @staticmethod
    def _command_separator(line: str, command: str):
        column = line.find(command)
        if column <= 0:
            return ""

        prefix = line[:column].rstrip()
        for separator in ("&&", "||", ";"):
            if prefix.endswith(separator):
                return separator
        return ""

    @staticmethod
    def _array_assignment_node(location: SourceLocation, command: str):
        match = ARRAY_ASSIGNMENT_PATTERN.match(command)
        if match:
            _, flag, name, append_operator, values_text = match.groups()
            is_exact = True
            associative_values = ()
            try:
                raw_values = tuple(parse_shell_words_preserving_quotes(values_text))
                values = tuple(strip_shell_word_quotes(value) for value in raw_values)
            except UnsupportedSourceError:
                raw_values = ()
                values = ()
                is_exact = False

            if flag == "-A" and is_exact:
                associative_values = LineParserFrontend._parse_associative_array_values(raw_values)
                is_exact = bool(associative_values) or not raw_values

            return ArrayAssignment(
                location=location,
                text=command,
                name=name,
                values=values,
                is_exact=is_exact,
                operation="append" if append_operator else "assign",
                associative_values=associative_values,
                raw_values=raw_values,
            )

        match = ARRAY_INDEX_ASSIGNMENT_PATTERN.match(command)
        if not match:
            return None

        name, index, append_operator, value = match.groups()

        return ArrayAssignment(
            location=location,
            text=command,
            name=name,
            values=(strip_shell_word_quotes(value.strip()),),
            is_exact=True,
            operation="append" if append_operator else "set",
            index=index.strip(),
            raw_values=(value.strip(),),
        )

    @staticmethod
    def _parse_associative_array_values(raw_values):
        pairs = []
        for raw_value in raw_values:
            match = re.match(r'^\[([^\]]+)\]=(.*)$', raw_value, re.S)
            if not match:
                return ()
            key, value = match.groups()
            pairs.append((strip_shell_word_quotes(key.strip()), strip_shell_word_quotes(value.strip())))
        return tuple(pairs)

    @staticmethod
    def _assignment_node(location: SourceLocation, command: str):
        if contains_nested_source_command(command):
            return None

        match = VARIABLE_ASSIGNMENT_PATTERN.match(command)
        if not match:
            return None

        prefix, name, operator, value = match.groups()
        if '(' in operator or command.strip().startswith(f"{name}=("):
            return None

        return Assignment(
            location=location,
            text=command,
            name=name,
            value=value.strip(),
            prefix=prefix.strip(),
        )

    @staticmethod
    def _cd_node(location: SourceLocation, command: str):
        if not re.match(r'^cd(?:\s|$)', command):
            return None

        return CdCommand(
            location=location,
            text=command,
            path_expression=command[2:].strip(),
        )

    @staticmethod
    def _set_node(location: SourceLocation, command: str):
        if not re.match(r'^set(?:\s|$)', command):
            return None

        try:
            words = parse_shell_words(command)
        except UnsupportedSourceError:
            words = command.split()

        return SetCommand(
            location=location,
            text=command,
            arguments=tuple(words[1:]),
        )

    @staticmethod
    def _fallback_source_site(script_path: Path, line_number: int, line: str, command: str,
                              control_flow_source_ranges):
        invocation = source_command_invocation(command)
        if invocation is None:
            words = command.split()
            source_index = source_command_index(command)
            command_name = words[source_index] if source_index is not None and source_index < len(words) else "source"
            source_offset = command.find(command_name)
            expression = command[source_offset + len(command_name):].strip() if source_offset >= 0 else ""
            source_site = f"{command_name} {expression}".strip()
        else:
            command_name = invocation.command_name
            source_offset = invocation.source_site_column_offset
            expression = invocation.source_expression
            source_site = invocation.source_site
        command_offset = line.find(command)
        column = command_offset + source_offset + 1 if command_offset >= 0 and source_offset >= 0 else 1
        is_control_flow = LineParserFrontend._column_in_ranges(max(column, 1), control_flow_source_ranges)

        return SourceSite(
            location=SourceLocation(script_path, line_number, max(column, 1)),
            text=command,
            command_name=command_name,
            source_expression=expression,
            source_site=source_site,
            is_control_flow=is_control_flow,
        )

    @staticmethod
    def _control_flow_source_ranges(line: str, control_depth: int):
        ranges = []
        simulated_depth = control_depth
        search_start = 0

        for command in get_commands(line):
            command_start = line.find(command, search_start)
            if command_start < 0:
                command_start = search_start
            command_end = command_start + len(command)

            if contains_source_command(command) and is_unsupported_control_flow_source(command, simulated_depth):
                ranges.append((command_start + 1, command_end + 1))

            if starts_unsupported_control_block(command):
                simulated_depth += 1
            elif ends_unsupported_control_block(command):
                simulated_depth = max(0, simulated_depth - 1)

            search_start = command_end

        return tuple(ranges)

    @staticmethod
    def _next_control_depth(line: str, control_depth: int):
        for command in get_commands(line):
            if starts_unsupported_control_block(command):
                control_depth += 1
            elif ends_unsupported_control_block(command):
                control_depth = max(0, control_depth - 1)
        return control_depth

    @staticmethod
    def _column_in_ranges(column: int, ranges):
        return any(start <= column < end for start, end in ranges)
