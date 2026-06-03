from __future__ import annotations

# Extracted LineParserFrontend methods. Shared parser names come from .shared.
from .shared import *  # noqa: F401,F403


class SourceFrontendCaseMixin:
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
        for index in SourceFrontendCaseMixin._unquoted_case_in_indices(rest):
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
        return get_commands(SourceFrontendCaseMixin._mark_case_terminators(line))

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
        pattern_end = SourceFrontendCaseMixin._case_pattern_end(command)
        if pattern_end <= 0:
            return None

        pattern_text = command[:pattern_end].strip()
        if pattern_text.startswith("("):
            pattern_text = pattern_text[1:].strip()
        if not pattern_text:
            return None

        patterns = tuple(part.strip() for part in SourceFrontendCaseMixin._split_case_patterns(pattern_text) if part.strip())
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
