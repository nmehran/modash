"""Regex patterns for shell assignment and variable-reference recognition."""

from __future__ import annotations

import re

VARIABLE_ASSIGNMENT_PATTERN = re.compile(r'''
    (?:
        "(?:\\.|[^"\\])*?"
        |'[^']*?'
        |[^"'#\n]
    )*?
    \s*
        (?:^|(?<=[;&|\n({])
        |(?<=\b(?:then|else|elif)\b)
        |".*\$\()
    \s*
    (
        (?:export|declare(?:\s+-\w+)*|local)?
        \s*
    )
    (?<![^\s(])
    ([a-zA-Z_]\w*)
    (\s*[-+]?=\s*)
    (
    (?:
            "(?:\\.|[^"\\]|\$\{?[\w}]+)*"
            |'[^']*'
            |\$?\((?:\(?[^()]*\)?)*
            |[^"';#\n\s]+
        )+
    )
    (?=\s*[;#\n]|\s*$|\s*[)|&])
''', re.VERBOSE | re.MULTILINE)


VARIABLE_REFERENCE_PATTERN = re.compile(r'''
        (?<![\\])
        (?:\\\\)*
        (
        \$
        (?:
            \w+
            |
            \{
            (?:
                [^{}$]+
                |
                (\$(?:\w+|\{[^}]+\}))
                |
                \{(?:[^{}]+|\{[^}]+\})+\}
            )+
            \}
        )
        (?=
            [^']*
            (?:'[^']*'[^']*)*
            $
        ))
        ''', re.VERBOSE)


VARIABLE_NAME_PATTERN = re.compile(r'\${?(\w+)}?')
