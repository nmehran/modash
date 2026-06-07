import os
import re
from collections import defaultdict

from methods.source_commands import shell_quote_words, source_command_invocation
from methods.source_resolver import ResolvedSource, UnsupportedSourceError


def construct_file_separator(filepath, entry_point, delimiter="-", length=120):
    # Get the basename of the file for the header
    filename = os.path.relpath(filepath, start=os.path.dirname(entry_point))

    # Create the header with the filename centered
    header_line = f"{filename}".center(length - 1, delimiter)

    # Create the full separator block
    line_block = f"#{delimiter * (length - 1)}\n"
    separator = f"{line_block}#{header_line}\n{line_block}\n"

    return separator


def unique_paths(paths: list[str]):
    unique = []
    seen = set()
    for path in paths:
        resolved = os.path.abspath(path)
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def format_context_path(filepath: str, entry_point: str):
    entry_directory = os.path.abspath(os.path.dirname(entry_point))
    filepath = os.path.abspath(filepath)

    try:
        relative_path = os.path.relpath(filepath, start=entry_directory)
    except ValueError:
        return filepath

    if relative_path == os.pardir or relative_path.startswith(os.pardir + os.sep):
        return filepath
    return relative_path


def construct_context_source_comment(source_declaration, entry_point: str):
    source_site_text = source_declaration.source_site.strip()
    direct_source_label = f"source {source_declaration.source_expression.strip()}"
    invocation = source_command_invocation(source_site_text)
    first_source_site_word = source_site_text.split(None, 1)[0] if source_site_text else ""
    is_wrapped_source_invocation = (
        invocation is not None
        and invocation.wrapped
        and invocation.command_name in {"source", "."}
        and first_source_site_word in {"builtin", "command"}
    )
    if source_declaration.execution_model == "parent-source":
        source_label = source_site_text if is_wrapped_source_invocation else direct_source_label
        suffix = ""
    else:
        source_label = source_site_text
        suffix = f" ({source_declaration.execution_model})"

    if source_declaration.source_arguments:
        suffix = f"{suffix} (args: {shell_quote_words(source_declaration.source_arguments, always_quote=True)})"

    if source_declaration.replacement_kind.startswith("noop-"):
        condition = f": {source_declaration.condition}" if source_declaration.condition else ""
        return f"# modash: {source_label} -> <skipped>{suffix} (disabled{condition})"

    if source_declaration.occurrence_model in {"conditional", "mutually-exclusive"}:
        condition = f": {source_declaration.condition}" if source_declaration.condition else ""
        suffix = f"{suffix} ({source_declaration.occurrence_model}{condition})"

    return f"# modash: {source_label} -> {format_context_path(source_declaration.path, entry_point)}{suffix}"


def read_file(filepath):
    with open(filepath, 'r') as file:
        return file.read()


def write_output(filename, content):
    with open(filename, 'w') as file:
        file.write(content)


def render_context_files(ordered_dependencies: list[str], entry_point: str, context: dict):
    output = [
        "# modash context",
        f"# entrypoint: {format_context_path(entry_point, entry_point)}",
        "# mode: context",
        "",
    ]

    source_declarations = context.get('source_declarations', {})

    for filepath in unique_paths(ordered_dependencies):
        source_context = source_declarations.get(filepath, {})
        output.append(construct_file_separator(filepath, entry_point))

        for num, line in enumerate(read_file(filepath).splitlines()):
            line_indent = re.match(r'\s*', line).group(0)
            for source_declaration in source_context.get(num, []):
                output.append(f"{line_indent}{construct_context_source_comment(source_declaration, entry_point)}")
            output.append(line)

        output.append('')

    return output


def context_from_source_events(events, disabled_sources=(), line_replacements=()):
    source_declarations = defaultdict(lambda: defaultdict(list))
    line_replacement_context = defaultdict(lambda: defaultdict(list))

    for event in events:
        source_declarations[str(event.location.path)][event.location.line - 1].append(ResolvedSource(
            path=str(event.path),
            source_expression=event.source_expression,
            source_site=event.source_site,
            execution_model=event.execution_model.value,
            replacement_kind=event.replacement_kind,
            source_value=event.source_value,
            source_arguments=event.source_arguments,
            source_argument_words=event.source_argument_words,
            source_column=event.location.column,
            occurrence_model=event.occurrence_model.value,
            condition=event.condition,
            positional_assignment_generation=(
                event.state_before.positional_assignment_generation
                if event.state_before
                else None
            ),
            sync_positionals=event.sync_positionals,
            source_location_path=str(event.location.path),
            source_location_line=event.location.line,
        ))

    for disabled_source in disabled_sources:
        source_declarations[str(disabled_source.location.path)][disabled_source.location.line - 1].append(ResolvedSource(
            path="",
            source_expression=disabled_source.source_expression,
            source_site=disabled_source.source_site,
            execution_model="parent-source",
            replacement_kind=f"noop-{disabled_source.replacement_kind}",
            source_column=disabled_source.location.column,
            occurrence_model="once",
            condition=disabled_source.condition,
            source_location_path=str(disabled_source.location.path),
            source_location_line=disabled_source.location.line,
        ))

    for line_replacement in line_replacements:
        replacements = line_replacement_context[str(line_replacement.location.path)][line_replacement.location.line - 1]
        for existing in replacements:
            if existing.old == line_replacement.old and existing.new != line_replacement.new:
                raise UnsupportedSourceError(
                    f"conflicting exact line replacement for {line_replacement.old}: "
                    f"{existing.new} != {line_replacement.new}"
                )
            if existing == line_replacement:
                break
        else:
            replacements.append(line_replacement)

    return {
        'source_declarations': source_declarations,
        'line_replacements': line_replacement_context,
    }


def context_paths_from_source_events(entry_point: str, events):
    children_by_parent = defaultdict(list)
    for event in events:
        children_by_parent[os.path.abspath(event.location.path)].append(os.path.abspath(event.path))

    ordered_paths = []
    seen_paths = set()

    def visit(filepath: str):
        filepath = os.path.abspath(filepath)
        for child in children_by_parent.get(filepath, []):
            visit(child)
        if filepath not in seen_paths:
            seen_paths.add(filepath)
            ordered_paths.append(filepath)

    visit(entry_point)
    return ordered_paths
