def get_commands(line: str):
    """Split one physical shell line into top-level command fragments.

    This is a quote-aware splitter for compiler frontends and resolvers, not a
    full Bash parser.
    """
    commands = []
    current = []
    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    in_double_bracket_test = False
    escaped = False
    paren_depth = 0
    index = 0

    def append_current_command():
        command = ''.join(current).strip()
        if command:
            commands.append(command)
        current.clear()

    while index < len(line):
        char = line[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue

        if char == '\\' and not in_single_quote:
            current.append(char)
            escaped = True
            index += 1
            continue

        if char == '`' and not in_single_quote:
            in_backtick = not in_backtick
            current.append(char)
            index += 1
            continue

        if in_backtick:
            current.append(char)
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

        if (
            not in_single_quote
            and not in_double_quote
            and not in_double_bracket_test
            and line.startswith('[[', index)
        ):
            in_double_bracket_test = True
            current.append('[[')
            index += 2
            continue

        if in_double_bracket_test:
            if not in_single_quote and not in_double_quote and line.startswith(']]', index):
                in_double_bracket_test = False
                current.append(']]')
                index += 2
                continue
            current.append(char)
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            if char == '(':
                paren_depth += 1
            elif char == ')' and paren_depth:
                paren_depth -= 1

        if char == '#' and not in_single_quote and not in_double_quote and paren_depth == 0:
            if not current or current[-1].isspace():
                break

        if char == ';' and not in_single_quote and not in_double_quote and paren_depth == 0:
            append_current_command()
            index += 1
            continue

        if (
            not in_single_quote
            and not in_double_quote
            and paren_depth == 0
            and (line.startswith('&&', index) or line.startswith('||', index))
        ):
            append_current_command()
            index += 2
            continue

        current.append(char)
        index += 1

    append_current_command()

    return commands


def first_top_level_pipeline_index(line: str):
    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    in_double_bracket_test = False
    escaped = False
    paren_depth = 0
    index = 0

    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue

        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue

        if char == "`" and not in_single_quote:
            in_backtick = not in_backtick
            index += 1
            continue

        if in_backtick:
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

        if (
            not in_single_quote
            and not in_double_quote
            and not in_double_bracket_test
            and line.startswith('[[', index)
        ):
            in_double_bracket_test = True
            index += 2
            continue

        if in_double_bracket_test:
            if not in_single_quote and not in_double_quote and line.startswith(']]', index):
                in_double_bracket_test = False
                index += 2
                continue
            index += 1
            continue

        if not in_single_quote and not in_double_quote:
            if char == "(":
                paren_depth += 1
            elif char == ")" and paren_depth:
                paren_depth -= 1
            elif (
                char == "|"
                and paren_depth == 0
                and not line.startswith("||", index)
                and (index == 0 or line[index - 1] != "|")
            ):
                return index

        index += 1

    return None
