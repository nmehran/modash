from __future__ import annotations

# Extracted LineParserFrontend methods. Shared parser names come from .shared.
from .shared import *  # noqa: F401,F403


class SourceFrontendCommandMixin:
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
                associative_values = SourceFrontendCommandMixin._parse_associative_array_values(raw_values)
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
        invocation = source_command_invocation(command, stop_at_shell_control=True)
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
        is_control_flow = SourceFrontendCommandMixin._column_in_ranges(max(column, 1), control_flow_source_ranges)

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
