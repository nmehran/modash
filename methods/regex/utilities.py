import re
from methods.regex.patterns import (
    QUOTE_STRIP_PATTERN,
    create_command_pattern
)


def extract_bash_commands(command, input_string, pattern=None, search_comments=False, include_separator=False, strip=False):
    matches = []

    if not re.search(rf'\b{command}\b', input_string):
        return matches

    # Step 1: Regex to find the command followed by its arguments
    if pattern is None:
        pattern = create_command_pattern(command)

    if not search_comments:
        input_string = remove_comments(input_string, ['#'])

    # Step 2: Find all matches for the command
    for match in pattern.finditer(input_string):
        # Extract the full command with its arguments
        groups: tuple = match.groups()
        if groups and groups[1]:
            if strip:
                groups = tuple(map(lambda s: s.strip() if s else '', groups))

            separator, command, argument = groups
            if include_separator:
                matches.append((separator or '', command or '', argument or ''))
            else:
                matches.append((command or '', argument or ''))

    return matches


def remove_comments(text, comment_patterns, exclusion_patterns=None, escape_exclusions=True) -> str:
    """
    Removes comments from text, taking into account quoted strings and optional exclusions.

    Args:
    - text (str): The input text from which to remove comments.
    - comment_patterns (list of str): List of comment markers to remove (e.g., ['#', '//']).
    - exclusion_patterns (list of str, optional): List of patterns that, if preceding a comment marker, prevent the comment's removal.
    - escape_exclusions (bool): Whether to escape the exclusion patterns (default: True).

    Returns:
    - str: Text with comments removed as specified.
    """
    # Handling exclusions and comments in a single regex expression
    exclusion_regex = ''
    if exclusion_patterns:
        exclusion_regex = '(?:' + '|'.join(
            f"{re.escape(pattern) if escape_exclusions else pattern}" for pattern in exclusion_patterns) + ')'

    # Combine exclusions and comment markers into a single regex. In shell text,
    # an unquoted # starts a comment only at a word boundary, not inside paths.
    comment_regex = '|'.join(
        rf'(?<!\S){re.escape(pattern)}' if pattern == '#' else re.escape(pattern)
        for pattern in comment_patterns
    )

    # pattern = re.compile(rf"""
    #     {exclusion_regex}                         # Match exclusions
    #     |\\"|"(?:\\"|[^"$])*"|'(?:\\'|[^'])*'     # Match quoted strings, including escaped quotes
    #     |(?P<comments>{comment_regex}).*          # Match comments
    # """, re.VERBOSE)

    pattern = re.compile(rf"""
        {exclusion_regex}                         # Match exclusions
        |(\\?['"]+)(?:(?=(\\?))\2.)*?\1           # Match quoted strings
        |(?P<comments>{comment_regex}).*          # Match comments
    """, re.VERBOSE)

    # Replace matches: keep exclusions and quotes, remove comments
    def remove_or_keep(match):
        if match.group('comments'):  # If match is a comment, remove
            return ''
        return match.group(0)  # If match is an exclusion or a quote, keep

    # Apply the regex
    return re.sub(pattern, remove_or_keep, text)


def strip_matching_quotes(s: str) -> str:
    """
    Strip matching outer quotes from a string and unescape any escaped quotes inside.

    Args:
    s (str): The input string potentially enclosed in matching single or double quotes.

    Returns:
    str: The unquoted string with inner escaped quotes unescaped, if outer quotes matched;
         otherwise, the original string.
    """
    # Early exit if the string is too short to be quoted or doesn't start and end with the same quote
    if len(s) < 2 or s[0] != s[-1] or s[0] not in "\"'":
        return s

    # Regex to strip only if the outermost characters are matching quotes
    match = QUOTE_STRIP_PATTERN.match(s)
    if match:
        # Extract the content between the quotes and unescape any escaped quotes
        return re.sub(r'\\([\'"])', r'\1', match.group(2))

    return s


def replace_substring(original, old, new, start, end):
    if new is None:
        return original

    before = original[:start]
    substring = original[start:end]
    after = original[end:]

    replaced_substring = substring.replace(old, new)

    return ''.join([before, replaced_substring, after])
