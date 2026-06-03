import os
import re
from fnmatch import fnmatch

from methods.shell_commands import create_command_pattern, extract_bash_commands
from methods.shell_text import strip_matching_quotes
from methods.source_commands import SOURCE_PATTERN
from methods.source_errors import FailglobExpansionError, UnsupportedSourceError
from methods.source_globs import (
    MISSING_SOURCE,
    MISSING_SOURCE_NO_FILENAME,
    MISSING_SOURCE_REPLACEMENT_KINDS,
    SOURCE_EXPANSION_FAILURE,
    SOURCE_EXPANSION_FAILURE_REPLACEMENT_KINDS,
    SOURCE_EXPANSION_FAILURE_RETURN,
    UNSUPPORTED_GLOB_OPTIONS,
    _missing_source_result,
    expand_glob_word,
    is_missing_source_replacement_kind,
    is_source_expansion_failure_replacement_kind,
    missing_source_status,
    source_expansion_failure_result,
)
from methods.source_types import GlobMatch, HeredocDelimiter, ResolvedSource
from methods.source_words import (
    ASSIGNMENT_WORD_PATTERN,
    contains_unquoted_token,
    extract_heredoc_delimiters,
    has_unsupported_shell_operator,
    has_unquoted_brace_expansion,
    has_unquoted_extglob,
    has_unquoted_glob,
    is_heredoc_end,
    parse_shell_words,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

BASH_COMMAND_PATTERN = create_command_pattern(r'bash|/bin/bash|/usr/bin/bash', regex=True)
COMMAND_LEVEL_SOURCE_PATTERNS = (
    ('eval', None),
    (r'bash|/bin/bash|/usr/bin/bash', BASH_COMMAND_PATTERN),
)


def source_command_index(command: str):
    from methods.source_commands import source_command_index as shared_source_command_index

    return shared_source_command_index(command)


def source_command_invocation(command: str):
    from methods.source_commands import source_command_invocation as shared_source_command_invocation

    return shared_source_command_invocation(command)


def shell_word_start(command: str, words: list[str], word_index: int):
    from methods.source_commands import shell_word_start as shared_shell_word_start

    return shared_shell_word_start(command, words, word_index)


def contains_source_command(command: str):
    from methods.source_commands import contains_source_command as shared_contains_source_command

    return shared_contains_source_command(command)


def contains_nested_source_command(command: str):
    from methods.source_commands import contains_nested_source_command as shared_contains_nested_source_command

    return shared_contains_nested_source_command(command)


def starts_unsupported_control_block(command: str):
    return bool(re.match(r'^\s*(?:if|for|while|until|case|select)\b', command))


def ends_unsupported_control_block(command: str):
    return bool(re.match(r'^\s*(?:fi|done|esac)\b', command))


def is_control_branch_command(command: str):
    stripped_command = command.strip()
    if re.match(r'^(?:then|elif|else|do)\b', stripped_command):
        return True
    return bool(re.match(r'^[^#\s;]+\)\s+', stripped_command))


def is_unsupported_control_flow_source(command: str, control_depth: int):
    return control_depth > 0 or starts_unsupported_control_block(command) or is_control_branch_command(command)


def is_unsupported_dynamic_source(command: str, source_path: str | None = None):
    stripped_command = command.strip()
    source_path = source_path or ""

    if "`" in source_path:
        return True

    if re.search(r'\$\(\s*(?!dirname\b|basename\b|realpath\b)', source_path):
        return True

    if re.match(r'^(eval|bash\s+-c)\b', stripped_command) and re.search(r'\bsource\b|\.\s+', stripped_command):
        return True

    return False


def extract_exact_command_substitution(source_expression: str):
    expression = strip_matching_quotes(source_expression.strip())
    if not expression.startswith('$(') or not expression.endswith(')'):
        return None

    inner_command = expression[2:-1].strip()
    if not inner_command:
        raise UnsupportedSourceError(f"unsupported empty source command substitution: {source_expression.strip()}")

    if '$(' in inner_command or '`' in inner_command:
        raise UnsupportedSourceError(f"unsupported nested source command substitution: {source_expression.strip()}")

    return inner_command


class SourceResolver:
    def __init__(self, resolve_path, resolve_variable_references, get_commands):
        self.resolve_path = resolve_path
        self.resolve_variable_references = resolve_variable_references
        self.get_commands = get_commands
        self.source_command_substitution_resolvers = {
            'cat': self.resolve_safe_cat_source,
            'find': self.resolve_safe_find_source,
        }
        self.command_level_source_resolvers = (
            self.resolve_eval_source_command,
            self.resolve_bash_c_source_command,
        )

    def resolve_safe_cat_source(self, inner_command: str, source_expression: str, source_site: str, context: dict,
                                execution_model: str, replacement_kind: str):
        if has_unsupported_shell_operator(inner_command):
            raise UnsupportedSourceError(f"unsupported cat source command syntax: {source_site.strip()}")

        words = parse_shell_words(inner_command)
        if len(words) != 2 or words[0] != 'cat' or words[1].startswith('-'):
            raise UnsupportedSourceError(f"unsupported cat source command: {source_site.strip()}")

        path_file = self.resolve_path(words[1], context)
        if not path_file or not os.path.isfile(path_file):
            raise UnsupportedSourceError(f"unsupported cat source path file: {source_site.strip()}")

        with open(path_file, 'r') as file:
            lines = file.read().splitlines()

        if len(lines) != 1 or not lines[0].strip():
            raise UnsupportedSourceError(f"ambiguous cat source output: {source_site.strip()}")

        resolved_path = self.resolve_path(lines[0].strip(), context)
        if not resolved_path:
            raise UnsupportedSourceError(f"unsupported cat-resolved source path: {source_site.strip()}")

        return ResolvedSource(
            path=resolved_path,
            source_expression=source_expression.strip(),
            source_site=source_site.strip(),
            execution_model=execution_model,
            replacement_kind=replacement_kind,
        )

    def parse_find_command(self, words: list[str], context: dict):
        if not words or words[0] != 'find':
            return None

        roots = []
        index = 1
        while index < len(words) and not words[index].startswith('-'):
            roots.append(words[index])
            index += 1

        if not roots:
            roots = ['.']

        resolved_roots = []
        for root in roots:
            resolved_root = self.resolve_path(root, context)
            if not resolved_root or not os.path.isdir(resolved_root):
                raise UnsupportedSourceError(f"unsupported find source root: {root}")
            resolved_roots.append(resolved_root)

        filters = {
            'name': [],
            'path': [],
            'maxdepth': None,
            'mindepth': 0,
            'has_print': False,
            'quit': False,
        }

        while index < len(words):
            token = words[index]
            if token == '-name':
                index += 1
                if index >= len(words):
                    raise UnsupportedSourceError("unsupported find source command: missing -name pattern")
                filters['name'].append(words[index])
            elif token == '-path':
                index += 1
                if index >= len(words):
                    raise UnsupportedSourceError("unsupported find source command: missing -path pattern")
                filters['path'].append(words[index])
            elif token == '-type':
                index += 1
                if index >= len(words) or words[index] != 'f':
                    raise UnsupportedSourceError("unsupported find source command: only -type f is supported")
            elif token == '-maxdepth':
                index += 1
                if index >= len(words) or not words[index].isdigit():
                    raise UnsupportedSourceError("unsupported find source command: invalid -maxdepth")
                filters['maxdepth'] = int(words[index])
            elif token == '-mindepth':
                index += 1
                if index >= len(words) or not words[index].isdigit():
                    raise UnsupportedSourceError("unsupported find source command: invalid -mindepth")
                filters['mindepth'] = int(words[index])
            elif token == '-print':
                filters['has_print'] = True
            elif token == '-quit':
                if not filters['has_print']:
                    raise UnsupportedSourceError("unsupported find source command: -quit requires earlier -print")
                filters['quit'] = True
            else:
                raise UnsupportedSourceError(f"unsupported find source predicate: {token}")
            index += 1

        return resolved_roots, filters

    @staticmethod
    def find_candidate_matches(roots: list[str], filters: dict, context: dict):
        matches = []
        current_directory = context['current_directory']

        for root in roots:
            for directory, dirnames, filenames in os.walk(root):
                relative_directory = os.path.relpath(directory, root)
                directory_depth = 0 if relative_directory == os.curdir else len(relative_directory.split(os.sep))
                maxdepth = filters['maxdepth']
                if maxdepth is not None and directory_depth >= maxdepth:
                    dirnames[:] = []

                for filename in filenames:
                    candidate = os.path.join(directory, filename)
                    candidate_depth = directory_depth + 1
                    if candidate_depth < filters['mindepth']:
                        continue
                    if maxdepth is not None and candidate_depth > maxdepth:
                        continue
                    if not os.path.isfile(candidate):
                        continue
                    if filters['name'] and not any(fnmatch(filename, pattern) for pattern in filters['name']):
                        continue

                    relative_to_current = os.path.relpath(candidate, current_directory)
                    path_variants = {
                        candidate,
                        relative_to_current,
                        f"./{relative_to_current}" if not relative_to_current.startswith(os.pardir) else relative_to_current,
                    }
                    if filters['path'] and not any(
                        fnmatch(path_variant, pattern)
                        for pattern in filters['path']
                        for path_variant in path_variants
                    ):
                        continue

                    matches.append(os.path.abspath(candidate))
                    if filters.get('quit'):
                        return matches
                    if len(matches) > 1:
                        return matches

        return matches

    def resolve_safe_find_source(self, inner_command: str, source_expression: str, source_site: str, context: dict,
                                 execution_model: str, replacement_kind: str):
        if has_unsupported_shell_operator(inner_command):
            raise UnsupportedSourceError(f"unsupported find source command syntax: {source_site.strip()}")

        words = parse_shell_words(inner_command)
        try:
            parsed_find = self.parse_find_command(words, context)
        except UnsupportedSourceError as exc:
            raise UnsupportedSourceError(f"{exc}: {source_site.strip()}") from exc
        if not parsed_find:
            return None

        roots, filters = parsed_find
        matches = self.find_candidate_matches(roots, filters, context)
        if len(matches) != 1:
            raise UnsupportedSourceError(f"ambiguous find source output: {source_site.strip()}")

        return ResolvedSource(
            path=matches[0],
            source_expression=source_expression.strip(),
            source_site=source_site.strip(),
            execution_model=execution_model,
            replacement_kind=replacement_kind,
        )

    def resolve_source_expression(self, source_expression: str, source_site: str, context: dict,
                                  execution_model: str = "parent-source", replacement_kind: str = "source"):
        if '`' in source_expression:
            raise UnsupportedSourceError(f"unsupported backtick source command: {source_site.strip()}")

        if (
            has_unquoted_glob(source_expression)
            or has_unquoted_brace_expansion(source_expression)
            or has_unquoted_extglob(source_expression)
        ):
            words = parse_shell_words(source_expression)
            if len(words) != 1:
                raise UnsupportedSourceError(f"unsupported source glob arguments: {source_site.strip()}")

            matches = expand_glob_word(
                words[0],
                context,
                source_site,
                raw_pattern=source_expression,
                allow_missing_literal=True,
            )
            if not matches:
                return _missing_source_result(
                    "",
                    source_expression,
                    source_site,
                    context,
                    MISSING_SOURCE_NO_FILENAME,
                )
            if not matches[0].exists:
                return _missing_source_result(
                    matches[0].word,
                    source_expression,
                    source_site,
                    context,
                    MISSING_SOURCE,
                )
            source_arguments = tuple(match.word for match in matches[1:]) or None

            return ResolvedSource(
                path=matches[0].path,
                source_expression=source_expression.strip(),
                source_site=source_site.strip(),
                execution_model=execution_model,
                replacement_kind=replacement_kind,
                source_value=matches[0].word,
                source_arguments=source_arguments,
            )

        missing_source_words = context.get('missing_source_words', set())
        if missing_source_words:
            resolved_word = self.resolve_variable_references(source_expression, context)
            resolved_word = os.path.expandvars(strip_matching_quotes(resolved_word))
            if resolved_word in missing_source_words:
                return _missing_source_result(
                    resolved_word,
                    source_expression,
                    source_site,
                    context,
                    MISSING_SOURCE,
                )

        if resolved_path := self.resolve_path(source_expression, context):
            return ResolvedSource(
                path=resolved_path,
                source_expression=source_expression.strip(),
                source_site=source_site.strip(),
                execution_model=execution_model,
                replacement_kind=replacement_kind,
            )

        inner_command = extract_exact_command_substitution(source_expression)
        if inner_command:
            words = parse_shell_words(inner_command)
            if not words:
                raise UnsupportedSourceError(f"unsupported empty source command substitution: {source_site.strip()}")

            resolver = self.source_command_substitution_resolvers.get(words[0])
            if resolver:
                return resolver(
                    inner_command, source_expression, source_site, context, execution_model, replacement_kind
                )
            raise UnsupportedSourceError(f"unsupported source command substitution: {source_site.strip()}")

        if is_unsupported_dynamic_source(source_site, source_expression):
            raise UnsupportedSourceError(f"unsupported dynamic source command: {source_site.strip()}")

        return None

    def resolve_single_source_payload(self, payload: str, source_site: str, context: dict,
                                      execution_model: str, replacement_kind: str):
        if '$(' in payload or '`' in payload:
            raise UnsupportedSourceError(f"unsupported nested dynamic source command: {source_site.strip()}")
        if has_unsupported_shell_operator(payload):
            raise UnsupportedSourceError(f"unsupported source command syntax: {source_site.strip()}")

        payload_commands = self.get_commands(payload)
        if len(payload_commands) != 1:
            raise UnsupportedSourceError(f"unsupported multi-command source payload: {source_site.strip()}")

        source_matches = SOURCE_PATTERN.findall(payload_commands[0])
        if len(source_matches) != 1:
            raise UnsupportedSourceError(f"unsupported source payload: {source_site.strip()}")

        _, _, nested_source_expression = source_matches[0]
        resolved_source = self.resolve_source_expression(
            nested_source_expression,
            source_site,
            context,
            execution_model=execution_model,
            replacement_kind=replacement_kind,
        )
        if not resolved_source:
            raise UnsupportedSourceError(f"unsupported unresolved source payload: {source_site.strip()}")

        return resolved_source

    @staticmethod
    def has_source_command(payload: str):
        return bool(SOURCE_PATTERN.findall(payload))

    def resolve_eval_source_command(self, command: str, context: dict, _mode: str):
        stripped_command = command.strip()
        if not re.match(r'^eval\b', stripped_command):
            return None

        try:
            words = parse_shell_words(stripped_command)
        except UnsupportedSourceError:
            if self.has_source_command(stripped_command) or contains_nested_source_command(stripped_command):
                raise
            return None
        if len(words) != 2 or words[0] != 'eval':
            if self.has_source_command(stripped_command) or contains_nested_source_command(stripped_command):
                raise UnsupportedSourceError(f"unsupported eval source command: {stripped_command}")
            return None

        payload = os.path.expandvars(self.resolve_variable_references(words[1], context))
        if not self.has_source_command(payload):
            return None

        return self.resolve_single_source_payload(
            payload,
            stripped_command,
            context,
            execution_model="parent-source",
            replacement_kind="command",
        )

    def resolve_bash_c_source_command(self, command: str, context: dict, mode: str):
        stripped_command = command.strip()
        if not re.match(r'^(?:bash|/bin/bash|/usr/bin/bash)\b', stripped_command):
            return None

        words = parse_shell_words(stripped_command)
        if len(words) != 3 or words[1] != '-c':
            return None

        payload = os.path.expandvars(self.resolve_variable_references(words[2], context))
        if not self.has_source_command(payload):
            return None

        if mode != "context":
            raise UnsupportedSourceError(f"unsupported child-shell source command in executable mode: {stripped_command}")

        return self.resolve_single_source_payload(
            payload,
            stripped_command,
            context,
            execution_model="child-shell",
            replacement_kind="context",
        )

    def resolve_command_level_source(self, command: str, context: dict, mode: str):
        for resolver in self.command_level_source_resolvers:
            resolved_source = resolver(command, context, mode)
            if resolved_source:
                return resolved_source

        return None

    def resolve_command_level_sources(self, command: str, context: dict, mode: str):
        resolved_sources = []
        seen_commands = set()

        for command_name, pattern in COMMAND_LEVEL_SOURCE_PATTERNS:
            matches = extract_bash_commands(
                command_name,
                command,
                pattern=pattern,
                include_separator=True,
                strip=True,
            )
            for _, matched_command, arguments in matches:
                source_command = f"{matched_command} {arguments}".strip()
                if source_command in seen_commands:
                    continue
                seen_commands.add(source_command)

                resolved_source = self.resolve_command_level_source(source_command, context, mode)
                if resolved_source:
                    resolved_sources.append(resolved_source)

        return resolved_sources
