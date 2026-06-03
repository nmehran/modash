import os
import re

from methods.regex.utilities import strip_matching_quotes, replace_substring
from methods.regex.patterns import (
    BASENAME_PATTERN,
    DIRNAME_PATTERN,
    REALPATH_PATTERN,
    VARIABLE_NAME_PATTERN,
    VARIABLE_REFERENCE_PATTERN,
)
from methods.source_resolver import SourceResolver, UnsupportedSourceError, parse_shell_words_preserving_quotes
from methods.shell.line import get_commands


def validate_path(path):
    """Check for common path errors and warn about potential issues."""
    warnings = []

    # Check for unresolved variables
    if '$' in path:
        warnings.append(f"Warning: Path may contain unresolved variables - {path}")

    # Report all found issues regardless of file existence
    for warning in warnings:
        print(warning)

    # Finally, check if the file exists and return appropriate status
    if not os.path.exists(path):
        print(f"Error: File does not exist - {path}")
        return False

    return True


def shell_utility_dirname(value: str):
    if value == "":
        return "."
    stripped = value.rstrip("/")
    if stripped == "":
        return "/"
    directory = stripped.rsplit("/", 1)[0] if "/" in stripped else "."
    directory = directory.rstrip("/")
    return directory or "/"


def shell_utility_basename(value: str, suffix: str = ""):
    if value == "":
        return ""
    stripped = value.rstrip("/")
    if stripped == "":
        return "/"
    basename = stripped.rsplit("/", 1)[-1]
    if suffix and basename != suffix and basename.endswith(suffix):
        return basename[:-len(suffix)]
    return basename


def resolve_path_command(command_name: str, arguments: str, base_dir=None):
    try:
        words = parse_shell_words_preserving_quotes(arguments)
    except UnsupportedSourceError:
        return None

    words = [strip_quotes(word) for word in words]
    option_like_operands = False
    if words and words[0] == "--":
        option_like_operands = True
        words = words[1:]
    if not words or (not option_like_operands and any(word.startswith("-") for word in words)):
        return None

    if command_name == "dirname":
        if len(words) != 1:
            return None
        return shell_utility_dirname(words[0])
    if command_name == "basename":
        if len(words) not in {1, 2}:
            return None
        return shell_utility_basename(*words)
    if command_name == "realpath":
        if len(words) != 1:
            return None
        path = words[0]
        if base_dir and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        return os.path.abspath(path)
    return None


def resolve_shell_path_commands(path_command: str, base_dir=None):
    """Resolve shell path utilities like $(dirname ...) and $(basename ...)."""
    commands = {
        'dirname': DIRNAME_PATTERN,
        'basename': BASENAME_PATTERN,
        'realpath': REALPATH_PATTERN,
    }

    while True:
        modified = False
        for cmd_name, pattern in commands.items():
            match = pattern.search(path_command)
            if match:
                full_match = match.group(0)
                result = resolve_path_command(cmd_name, match.group(1), base_dir)
                if result is None:
                    return path_command
                path_command = path_command.replace(full_match, result)
                modified = True
                break

        if not modified:
            break

    return path_command


def strip_quotes(path):
    """Strip incorrect usage of quotes within paths."""
    # This regex will target quotes that are at the very beginning or end of the string
    # and quotes around path separators.
    path = re.sub(r'^["\']|["\']$', '', path)  # Remove quotes at the start or end
    path = re.sub(r'(?<=/)"|"(?=/)', '', path)  # Remove quotes around slashes
    return path


def get_valid_path(command, base_dir=None):
    if len(command) >= 1:
        unquoted_command = strip_quotes(strip_matching_quotes(command))
        expanded_command = os.path.expanduser(os.path.expandvars(unquoted_command))

        candidates = []
        if os.path.isabs(expanded_command):
            candidates.append(expanded_command)
        elif base_dir:
            candidates.append(os.path.join(base_dir, expanded_command))
        else:
            candidates.append(expanded_command)

        for candidate in candidates:
            resolved = os.path.abspath(candidate)
            if os.path.exists(resolved):
                return resolved
    return ""


def parameter_expansion_value(reference, context):
    if not reference.startswith("${") or not reference.endswith("}"):
        return None

    body = reference[2:-1]
    match = re.fullmatch(r'([a-zA-Z_]\w*|[0-9]+)(:?)([-+])(.*)', body, re.S)
    if not match:
        return None

    name, colon, operator, word = match.groups()
    value = context['vars'].get(name, os.environ.get(name))
    is_set = value is not None and (not colon or value != "")
    if operator == "-":
        if not is_set:
            return resolve_variable_references(word, context)
        return value
    if is_set:
        return resolve_variable_references(word, context)
    return ""


def resolve_variable_references(command, context):
    command_len = len(command)

    search_start = 0
    while search_start < command_len:
        variable_reference = VARIABLE_REFERENCE_PATTERN.search(command, search_start)
        if not variable_reference:
            break

        outer_reference, inner_reference = variable_reference.groups()
        start, end = variable_reference.span()

        try:
            parameter_value = parameter_expansion_value(outer_reference, context)
            if parameter_value is not None:
                command = replace_substring(command, outer_reference, parameter_value, start, end)
                command_len = len(command)
                search_start = start + len(parameter_value)
                continue

            if inner_reference:
                inner_name = VARIABLE_NAME_PATTERN.match(inner_reference).group(1)
                inner_definition = context['vars'].get(inner_name)
                if inner_definition is None:
                    search_start = end
                    continue
                command = replace_substring(command, inner_reference, inner_definition, start, end)
                command_len = len(command)
                search_start = start
                continue

            outer_name = VARIABLE_NAME_PATTERN.match(outer_reference).group(1)
            outer_definition = context['vars'].get(outer_name)
            command = replace_substring(command, outer_reference, outer_definition, start, end)
            command_len = len(command)

            search_start = end

        except AttributeError:
            # Cases where pattern matching fails
            search_start = end

    return command


def resolve_command(command, context):
    """Resolve a path using dynamic context, supporting shell operations."""
    command = resolve_variable_references(command, context)

    # Expand environment variables
    command = os.path.expandvars(command)

    # Handle shell functions like $(dirname ...) and $(basename ...)
    command = resolve_shell_path_commands(command, context.get('current_directory'))

    # If path, normalize and convert to absolute path
    is_valid_path = False
    if path := get_valid_path(command, context.get('current_directory')):
        is_valid_path = True
        command = path
    elif not command:
        command = ""

    return command, is_valid_path


def change_directory(path: str, context: dict) -> str:
    """Change the current directory based on a cd command."""

    # Remove potential quotes and expand environment variables in the path
    path = os.path.expandvars(strip_quotes(path))

    # Resolve non-environment variables in the path given current context
    resolved_command, is_valid_path = resolve_command(path, context)
    if not is_valid_path:
        raise ValueError(f"No path could be resolved for the value: {path}")

    new_path = os.path.abspath(resolved_command)

    # If the path is a file, use its directory part
    if os.path.isfile(new_path):
        new_path = os.path.dirname(new_path)

    # Check if the new path is a directory
    if not os.path.isdir(new_path):
        raise NotADirectoryError(f"Directory not found: {new_path}")

    context['current_directory'] = new_path
    return new_path


def is_relative_path(path: str):
    # Strip matching single and double quotes from the start and end of the path
    stripped_path = strip_matching_quotes(path)
    return bool(stripped_path) and not os.path.isabs(stripped_path)


def resolve_path(source_path: str, context: dict):
    stripped_path = strip_quotes(source_path.strip())
    resolved_command, is_valid_path = resolve_command(stripped_path, context)
    resolved_path = strip_quotes(resolved_command)
    if is_valid_path:
        if is_relative_path(resolved_path):
            current_dir = context['current_directory']
            resolved_path = os.path.join(current_dir, resolved_path)
        if resolved_path:
            return os.path.abspath(resolved_path)
    return None


SOURCE_RESOLVER = SourceResolver(resolve_path, resolve_variable_references, get_commands)


def get_sources(entrypoint, mode="executable", source_supplement=None):
    from methods.compile import context_from_source_events, context_paths_from_source_events
    from methods.source_evaluator import SourceEvaluator
    from methods.source_supplements import load_source_supplement

    if not validate_path(entrypoint):
        raise FileNotFoundError(f"Error: File does not exist - {entrypoint}")

    entrypoint = os.path.abspath(entrypoint)
    supplement = load_source_supplement(source_supplement, os.path.dirname(entrypoint))
    evaluation = SourceEvaluator(mode=mode, source_supplement=supplement).evaluate(entrypoint)
    sources = context_paths_from_source_events(entrypoint, evaluation.events)
    context = context_from_source_events(evaluation.events, evaluation.disabled_sources)
    context.update({
        'vars': {**supplement.variables, '0': entrypoint},
        'current_directory': os.path.dirname(entrypoint),
    })
    return sources, context
