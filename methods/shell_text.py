"""Small shell-text helpers that do not parse full Bash syntax."""

from __future__ import annotations

import re

QUOTE_STRIP_PATTERN = re.compile(r'^([\'"]+)(.*?)(\1)$')


def remove_comments(text, comment_patterns, exclusion_patterns=None, escape_exclusions=True) -> str:
    """Remove comments while preserving quoted spans and explicit exclusions."""
    exclusion_regex = ''
    if exclusion_patterns:
        exclusion_regex = '(?:' + '|'.join(
            f"{re.escape(pattern) if escape_exclusions else pattern}" for pattern in exclusion_patterns
        ) + ')'

    comment_regex = '|'.join(
        rf'(?<!\S){re.escape(pattern)}' if pattern == '#' else re.escape(pattern)
        for pattern in comment_patterns
    )

    pattern = re.compile(rf"""
        {exclusion_regex}
        |(\\?['"]+)(?:(?=(\\?))\2.)*?\1
        |(?P<comments>{comment_regex}).*
    """, re.VERBOSE)

    def remove_or_keep(match):
        if match.group('comments'):
            return ''
        return match.group(0)

    return re.sub(pattern, remove_or_keep, text)


def strip_matching_quotes(s: str) -> str:
    """Strip matching outer quotes and unescape escaped quote characters."""
    if len(s) < 2 or s[0] != s[-1] or s[0] not in "\"'":
        return s

    match = QUOTE_STRIP_PATTERN.match(s)
    if match:
        return re.sub(r'\\([\'"])', r'\1', match.group(2))

    return s


def replace_substring(original, old, new, start, end):
    if new is None:
        return original

    before = original[:start]
    substring = original[start:end]
    after = original[end:]
    return ''.join([before, substring.replace(old, new), after])
