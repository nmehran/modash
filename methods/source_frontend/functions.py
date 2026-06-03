from __future__ import annotations

# Extracted LineParserFrontend methods. Shared parser names come from .shared.
from .shared import *  # noqa: F401,F403


class SourceFrontendFunctionMixin:
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
                and SourceFrontendFunctionMixin._is_function_closing_brace(text, index)
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
