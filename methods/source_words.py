import re

from methods.shell.scan import read_backtick_body, read_balanced_body
from methods.source_errors import UnsupportedSourceError
from methods.source_types import HeredocDelimiter

ASSIGNMENT_WORD_PATTERN = re.compile(r'^[a-zA-Z_]\w*(?:\+)?=.*$')


def extract_heredoc_delimiters(line: str):
    delimiters = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    arithmetic_depth = 0
    index = 0

    while index < len(line):
        char = line[index]

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

        if in_single_quote or in_double_quote:
            index += 1
            continue

        if arithmetic_depth:
            if line.startswith('))', index):
                arithmetic_depth -= 1
                index += 2
            else:
                index += 1
            continue

        if line.startswith('$((', index):
            arithmetic_depth += 1
            index += 3
            continue

        if line.startswith('((', index) and (index == 0 or line[index - 1].isspace() or line[index - 1] in ';|&'):
            arithmetic_depth += 1
            index += 2
            continue

        if line.startswith('<<<', index):
            index += 3
            continue

        if line.startswith('<<', index):
            delimiter_start = index + 2
            strip_tabs = False
            if delimiter_start < len(line) and line[delimiter_start] == '-':
                strip_tabs = True
                delimiter_start += 1

            while delimiter_start < len(line) and line[delimiter_start].isspace():
                delimiter_start += 1

            if delimiter_start >= len(line):
                break

            quote = line[delimiter_start] if line[delimiter_start] in {'"', "'"} else ''
            quoted = bool(quote)
            if quote:
                delimiter_end = line.find(quote, delimiter_start + 1)
                if delimiter_end < 0:
                    break
                delimiter = line[delimiter_start + 1:delimiter_end]
                index = delimiter_end + 1
            else:
                delimiter_end = delimiter_start
                while delimiter_end < len(line) and not line[delimiter_end].isspace() and line[delimiter_end] not in ';|&<>':
                    delimiter_end += 1
                delimiter = _clean_heredoc_delimiter(line[delimiter_start:delimiter_end])
                index = delimiter_end

            if delimiter:
                delimiters.append(HeredocDelimiter(delimiter, strip_tabs, quoted))
            continue

        index += 1

    return delimiters


def _clean_heredoc_delimiter(value: str):
    cleaned = []
    escaped = False
    for char in value:
        if escaped:
            cleaned.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        cleaned.append(char)
    if escaped:
        cleaned.append("\\")
    return "".join(cleaned)


def is_heredoc_end(line: str, heredoc: HeredocDelimiter):
    candidate = line.rstrip('\n')
    if heredoc.strip_tabs:
        candidate = candidate.lstrip('\t')
    return candidate == heredoc.value


def parse_shell_words(command: str):
    return [strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(command)]


def strip_shell_word_quotes(word: str):
    output = []
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for char in word:
        if escaped:
            output.append(char)
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

        output.append(char)

    return ''.join(output)


def parse_shell_words_preserving_quotes(command: str):
    words = []
    current = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    current_started = False

    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            current_started = True
            index += 1
            continue

        if char == '\\' and not in_single_quote:
            current.append(char)
            escaped = True
            current_started = True
            index += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(char)
            current_started = True
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(char)
            current_started = True
            index += 1
            continue

        if not in_single_quote and (command.startswith('<(', index) or command.startswith('>(', index)):
            opener = command[index:index + 2]
            body, end_index = read_balanced_body(command, index + 2)
            if end_index is None:
                raise UnsupportedSourceError(
                    f"unsupported source command syntax: {command.strip()} (unterminated process substitution)"
                )
            current.append(f"{opener}{body})")
            current_started = True
            index = end_index + 1
            continue

        if not in_single_quote and command.startswith('$(', index):
            body, end_index = read_balanced_body(command, index + 2)
            if end_index is None:
                raise UnsupportedSourceError(
                    f"unsupported source command syntax: {command.strip()} (unterminated command substitution)"
                )
            current.append(f"$({body})")
            current_started = True
            index = end_index + 1
            continue

        if not in_single_quote and char == '`':
            body, end_index = read_backtick_body(command, index + 1)
            if body is None:
                raise UnsupportedSourceError(
                    f"unsupported source command syntax: {command.strip()} (unterminated backtick substitution)"
                )
            current.append(f"`{body}`")
            current_started = True
            index = end_index + 1
            continue

        if char.isspace() and not in_single_quote and not in_double_quote:
            word = ''.join(current).strip()
            if current_started:
                words.append(word)
            current = []
            current_started = False
            index += 1
            continue

        current.append(char)
        current_started = True
        index += 1

    if escaped or in_single_quote or in_double_quote:
        raise UnsupportedSourceError(f"unsupported source command syntax: {command.strip()} (unterminated quote)")

    word = ''.join(current).strip()
    if current_started:
        words.append(word)

    return words


def contains_unquoted_token(text: str, token: str):
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

        if not in_single_quote and not in_double_quote and text.startswith(token, index):
            return True

        index += 1

    return False


def has_unquoted_glob(text: str):
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for char in text:
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

        if not in_single_quote and not in_double_quote and char in {'*', '?', '['}:
            return True

    return False


def has_unquoted_extglob(text: str):
    return any(contains_unquoted_token(text, token) for token in {"@(", "?(", "*(", "+(", "!("})


def has_unquoted_brace_expansion(text: str):
    return contains_unquoted_token(text, "{") and contains_unquoted_token(text, "}")





def has_unsupported_shell_operator(command: str):
    return bool(re.search(r'(?<!\\)(?:[|;&<>]|\n)', command))
