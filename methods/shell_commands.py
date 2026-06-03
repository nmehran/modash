"""Generic shell command regex extraction helpers."""

from __future__ import annotations

import re

from methods.shell_text import remove_comments

COMMAND_TEMPLATE_PATTERN = (r'''
        (?<!
            (?<!['"`])
            \#
            [^\n'"`]
        )
        (?:
            (
                (?:
                    (?<![`'"])
                    ^|\n|&&|\|\||;
                )\s*
            )
            |
                (?<!['`])
                \$\(\s*

        )
        \b({command})\b
        (
            \s+
            (?:
                (?<!\\)(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|`(?:\\.|[^`])*`)
                |
                \$\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)
                |
                <\([^)]+\)
                |
                [^"'`\s\n&|;()]+
                |
                \s+
                |
                >[>&]?
                |
                [12]?>&[12]?
            )*
        )?
        \s*
        (?:
            (?<=\s)(?=\#)
            |
            (?=\s*(?:&&|\|\||;|\n|$|\)|\)\s*\)))
        )
''')

PATH_COMMAND_TEMPLATE_PATTERN = (
    r'\$\(\s*\b{command}\b\s+(".*?"|\'.*?\'|[^)]+)\s*\)'
)


def create_command_pattern(command, template=None, regex=False):
    if template is None:
        template = COMMAND_TEMPLATE_PATTERN

    if regex:
        template = template.replace(r'\b({command})\b', '({command})')
        escaped_command = command
    else:
        escaped_command = re.escape(command)

    return re.compile(template.format(command=escaped_command), re.VERBOSE)


def extract_bash_commands(command, input_string, pattern=None, search_comments=False, include_separator=False, strip=False):
    matches = []

    if not re.search(rf'\b{command}\b', input_string):
        return matches

    if pattern is None:
        pattern = create_command_pattern(command)

    if not search_comments:
        input_string = remove_comments(input_string, ['#'])

    for match in pattern.finditer(input_string):
        groups: tuple = match.groups()
        if groups and groups[1]:
            if strip:
                groups = tuple(part.strip() if part else '' for part in groups)

            separator, command_name, argument = groups
            if include_separator:
                matches.append((separator or '', command_name or '', argument or ''))
            else:
                matches.append((command_name or '', argument or ''))

    return matches


DIRNAME_PATTERN = create_command_pattern(command='dirname', template=PATH_COMMAND_TEMPLATE_PATTERN)
BASENAME_PATTERN = create_command_pattern(command='basename', template=PATH_COMMAND_TEMPLATE_PATTERN)
REALPATH_PATTERN = create_command_pattern(command='realpath', template=PATH_COMMAND_TEMPLATE_PATTERN)
SET_PATTERN = create_command_pattern(command='set')
CD_PATTERN = create_command_pattern(command='cd')
