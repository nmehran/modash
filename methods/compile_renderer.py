import os
import re
from collections import defaultdict

from methods.compile_context import (
    construct_file_separator,
    read_file,
)
from methods.compile_positionals import (
    render_source_call_wrapper,
    source_positional_capture_names,
    source_positional_sync_replacements,
    wrap_rendered_source_for_positional_frame,
)
from methods.shell.line import get_commands
from methods.source_commands import (
    contains_nested_source_command,
    contains_source_command,
    shell_single_quote as shell_quote,
    source_command_invocation,
)
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    MISSING_SOURCE_INVALID_OPTION,
    MISSING_SOURCE_NO_FILENAME,
    SOURCE_EXPANSION_FAILURE_RETURN,
    UnsupportedSourceError,
    extract_heredoc_delimiters,
    is_heredoc_end,
    is_missing_source_replacement_kind,
    is_source_expansion_failure_replacement_kind,
    missing_source_status,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)
from methods.source_traits import file_top_level_source_traits

SET_SHEBANG = "#!/bin/bash"

def replace_runtime_source_references(line: str, filepath: str, entry_point: str, *, include_zero: bool = True):
    return ''.join(
        _replace_runtime_source_references_segment(segment, filepath, entry_point, include_zero=include_zero)
        if expandable
        else segment
        for segment, expandable in _single_quote_aware_segments(line)
    )


def _replace_runtime_source_references_segment(segment: str, filepath: str, entry_point: str, *, include_zero: bool):
    bash_source = shell_quote(os.path.abspath(filepath))
    entry_source = shell_quote(os.path.abspath(entry_point))

    replacements = {
        '"${BASH_SOURCE[0]}"': bash_source,
        '"${BASH_SOURCE}"': bash_source,
        '"$BASH_SOURCE"': bash_source,
        '${BASH_SOURCE[0]}': bash_source,
        '${BASH_SOURCE}': bash_source,
        '$BASH_SOURCE': bash_source,
    }
    if include_zero:
        replacements.update({
            '"${0}"': entry_source,
            '"$0"': entry_source,
            '${0}': entry_source,
        })

    for old, new in replacements.items():
        segment = segment.replace(old, new)

    if include_zero:
        segment = re.sub(r'\$0(?![0-9])', entry_source, segment)
    return segment


def _single_quote_aware_segments(line: str):
    segments = []
    current = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    segment_expandable = True

    def flush():
        nonlocal current
        if current:
            segments.append((''.join(current), segment_expandable))
            current = []

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            current.append(char)
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            flush()
            current.append(char)
            in_single_quote = not in_single_quote
            segment_expandable = not in_single_quote
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        current.append(char)

    flush()
    return tuple(segments)


def indent_shell_block(content: str, prefix: str):
    output = []
    active_heredocs = []
    for line in content.splitlines():
        if active_heredocs:
            output.append(line)
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        output.append(f"{prefix}{line}" if line else line)
        active_heredocs.extend(extract_heredoc_delimiters(line))
    return '\n'.join(output)


def source_command_name(source_site: str):
    invocation = source_command_invocation(source_site.strip())
    if invocation is not None:
        return invocation.command_name
    try:
        words = parse_shell_words_preserving_quotes(source_site.strip())
    except UnsupportedSourceError:
        words = []
    return strip_shell_word_quotes(words[0]) if words else "source"


def render_missing_source_failure(source_declaration, indent: str):
    command_name = source_command_name(source_declaration.source_site)
    status = missing_source_status(source_declaration.replacement_kind)
    if source_declaration.replacement_kind == MISSING_SOURCE_NO_FILENAME:
        messages = [
            f"{command_name}: filename argument required",
            f"{command_name}: usage: {command_name} filename [arguments]",
        ]
    elif source_declaration.replacement_kind == MISSING_SOURCE_INVALID_OPTION:
        messages = [
            f"{command_name}: {source_declaration.source_value}: invalid option",
            f"{command_name}: usage: {command_name} filename [arguments]",
        ]
    else:
        messages = [f"{source_declaration.source_value}: No such file or directory"]

    first_message, *remaining_messages = messages
    diagnostic_path = (
        shell_quote(source_declaration.source_location_path)
        if source_declaration.source_location_path is not None
        else '"${BASH_SOURCE[0]}"'
    )
    diagnostic_line = (
        shell_quote(str(source_declaration.source_location_line))
        if source_declaration.source_location_line is not None
        else '"${LINENO}"'
    )
    lines = [
        f"{indent}printf '%s: line %s: %s\\n' "
        f"{diagnostic_path} {diagnostic_line} {shell_quote(first_message)} >&2"
    ]
    for message in remaining_messages:
        lines.append(f"{indent}printf '%s\\n' {shell_quote(message)} >&2")
    lines.append(f"{indent}( exit {status} )")
    return "\n".join(lines)


def render_source_expansion_failure(source_declaration, indent: str, *, return_from_function: bool = False):
    lines = [
        f"{indent}printf '%s: line %s: no match: %s\\n' "
        f'"${{BASH_SOURCE[0]}}" "${{LINENO}}" {shell_quote(source_declaration.source_value or "")} >&2',
        f"{indent}( exit 1 )",
    ]
    if return_from_function:
        lines.append(f"{indent}return 1")
    return "\n".join(lines)


def source_values_are_path_ambiguous(source_declarations):
    paths_by_source_value = defaultdict(set)
    for source_declaration in source_declarations:
        source_value = source_declaration.source_value or source_declaration.path
        paths_by_source_value[source_value].add(source_declaration.path)

    return any(len(paths) > 1 for paths in paths_by_source_value.values())


def render_source_dispatch(
    source_expression: str,
    source_declarations,
    render_source,
    indent: str,
    positional_frame_names: dict[str, str] | None = None,
):
    use_resolved_path = source_values_are_path_ambiguous(source_declarations)
    source_path_expression = source_dispatch_path_expression(source_expression)
    dispatch_expression = (
        f'"$(realpath -- {source_path_expression})"'
        if use_resolved_path
        else source_path_expression
    )
    output = [f"case {dispatch_expression} in"]
    seen_patterns = set()

    for source_declaration in source_declarations:
        if use_resolved_path:
            pattern = source_declaration.path
        else:
            pattern = source_declaration.source_value or source_declaration.path
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        if source_declaration.replacement_kind == "noop-source":
            rendered_source = f"{indent}    :"
        elif is_missing_source_replacement_kind(source_declaration.replacement_kind):
            rendered_source = render_missing_source_failure(source_declaration, f"{indent}    ")
        elif is_source_expansion_failure_replacement_kind(source_declaration.replacement_kind):
            rendered_source = render_source_expansion_failure(
                source_declaration,
                f"{indent}    ",
                return_from_function=(
                    source_declaration.replacement_kind == SOURCE_EXPANSION_FAILURE_RETURN
                ),
            )
        else:
            rendered_source = indent_shell_block(
                render_source(
                    source_declaration.path,
                    source_arguments=source_declaration.source_arguments,
                    source_state_generation=source_declaration.positional_assignment_generation,
                    sync_positionals=source_declaration.sync_positionals,
                ),
                f"{indent}    ",
            )
            rendered_source = wrap_rendered_source_for_positional_frame(
                rendered_source,
                source_declaration,
                positional_frame_names,
                f"{indent}    ",
            )
        output.extend([
            f"{indent}  {shell_quote(pattern)})",
            f"{indent}    {{",
            rendered_source,
            f"{indent}    }}",
            f"{indent}    ;;",
        ])

    output.extend([
        f"{indent}  *)",
        f"{indent}    echo {shell_quote(f'modash: unresolved source {source_expression.strip()}')} >&2",
        f"{indent}    exit 1",
        f"{indent}    ;;",
        f"{indent}esac",
    ])
    return '\n'.join(output)


def source_dispatch_path_expression(source_expression: str):
    try:
        words = parse_shell_words_preserving_quotes(source_expression)
    except UnsupportedSourceError:
        words = []
    if not words:
        return source_expression.strip()
    return words[0].strip()


def render_retained_source_dispatch(
    source_declarations,
    render_source,
    indent: str,
    positional_frame_names: dict[str, str] | None = None,
):
    output = ["{"]
    seen_arguments = set()
    branch_keyword = "if"

    for source_declaration in source_declarations:
        source_path_argument = source_declaration.source_value or source_declaration.path
        source_arguments = source_declaration.source_arguments or ()
        arguments = (source_path_argument, *source_arguments)
        if arguments in seen_arguments:
            continue
        seen_arguments.add(arguments)

        rendered_source = indent_shell_block(
            render_source(
                source_declaration.path,
                source_arguments=source_declaration.source_arguments,
                source_state_generation=source_declaration.positional_assignment_generation,
                sync_positionals=source_declaration.sync_positionals,
            ),
            f"{indent}      ",
        )
        rendered_source = wrap_rendered_source_for_positional_frame(
            rendered_source,
            source_declaration,
            positional_frame_names,
            f"{indent}      ",
        )
        if not rendered_source:
            rendered_source = f"{indent}      :"
        quoted_path_argument = shell_quote(source_path_argument)
        conditions = [
            f"$# -eq {len(arguments)}",
            (
                f"( ${{1-}} == {quoted_path_argument} || "
                f"$(realpath -- \"${{1-}}\" 2>/dev/null) == {quoted_path_argument} )"
            ),
        ]
        conditions.extend(
            f"${{{index}-}} == {shell_quote(argument)}"
            for index, argument in enumerate(source_arguments, start=2)
        )
        output.extend([
            f"{indent}  {branch_keyword} [[ {' && '.join(conditions)} ]]; then",
            f"{indent}    {{",
            rendered_source,
            f"{indent}    }}",
        ])
        branch_keyword = "elif"

    output.extend([
        f"{indent}  else",
        f"{indent}    false",
        f"{indent}  fi",
        f"{indent}}}",
    ])
    return '\n'.join(output)


def find_unquoted_substring(text: str, needle: str, start: int = 0):
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for index, char in enumerate(text):
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

        if index >= start and not in_single_quote and not in_double_quote and text.startswith(needle, index):
            return index

    return -1


def replace_command_source_sites(
    line: str,
    source_declarations,
    render_source,
    positional_frame_names: dict[str, str] | None = None,
):
    search_start = 0

    for source_declaration in source_declarations:
        source_site = source_declaration.source_site.strip()
        source_index = find_unquoted_substring(line, source_site, search_start)
        if source_index < 0:
            raise ValueError(f"Could not replace resolved source command: {source_site}")

        indent = re.match(r'\s*', line[:source_index]).group(0)
        if source_declaration.replacement_kind == "noop-command":
            replacement = ":"
        elif source_declaration.replacement_kind == "bash-c-source":
            replacement = render_bash_c_source_command(
                source_declaration,
                render_source,
                indent,
                positional_frame_names,
            )
        else:
            rendered_source = indent_shell_block(
                render_source(
                    source_declaration.path,
                    source_arguments=source_declaration.source_arguments,
                    source_state_generation=source_declaration.positional_assignment_generation,
                    sync_positionals=source_declaration.sync_positionals,
                ),
                indent,
            )
            rendered_source = wrap_rendered_source_for_positional_frame(
                rendered_source,
                source_declaration,
                positional_frame_names,
                indent,
            )
            replacement = f"{{\n{rendered_source}\n{indent}}}"
        line = line[:source_index] + replacement + line[source_index + len(source_site):]
        search_start = source_index + len(replacement)

    return line


def render_bash_c_source_command(
    source_declaration,
    render_source,
    indent: str,
    positional_frame_names: dict[str, str] | None = None,
):
    command = source_declaration.source_site.strip()
    words = parse_shell_words_preserving_quotes(command)
    command_index = 0
    while command_index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[command_index]):
        command_index += 1

    command_prefix = " ".join(words[:command_index])
    command_name = strip_shell_word_quotes(words[command_index])
    payload = strip_shell_word_quotes(words[command_index + 2])
    child_argv_words = tuple(words[command_index + 3:])
    inner_source_site = source_declaration.source_value or f"source {source_declaration.source_expression.strip()}"

    rendered_source = indent_shell_block(
        render_source(
            source_declaration.path,
            source_arguments=source_declaration.source_arguments,
            source_state_generation=source_declaration.positional_assignment_generation,
            sync_positionals=source_declaration.sync_positionals,
        ),
        "",
    )
    rendered_source = wrap_rendered_source_for_positional_frame(
        rendered_source,
        source_declaration,
        positional_frame_names,
        "",
    )
    replacement = f"{{\n{rendered_source}\n}}"
    if inner_source_site not in payload:
        raise ValueError(f"Could not replace bash -c source payload: {inner_source_site}")
    payload = payload.replace(inner_source_site, replacement, 1)
    rewritten_words = [command_name, "-c", shell_quote(payload), *child_argv_words]
    rewritten = " ".join(rewritten_words)
    if command_prefix:
        return f"{command_prefix} {rewritten}"
    return rewritten


def group_source_declarations_by_column(source_declarations):
    declarations_by_column = defaultdict(list)
    fallback_declarations = []

    for source_declaration in source_declarations:
        if source_declaration.source_column is None:
            fallback_declarations.append(source_declaration)
        else:
            declarations_by_column[source_declaration.source_column].append(source_declaration)

    return declarations_by_column, fallback_declarations


def render_source_site_replacement(
    separator: str,
    declarations,
    render_source,
    indent: str,
    positional_frame_names: dict[str, str] | None = None,
):
    declaration = declarations[0]
    redirection_suffix = source_site_redirection_suffix(declaration.source_site)
    retained_declarations = [
        declaration for declaration in declarations
        if declaration.replacement_kind == "retained-source"
    ]
    if retained_declarations:
        replacement = render_retained_source_dispatch(retained_declarations, render_source, indent, positional_frame_names)
        return append_source_site_redirections(f"{separator}{replacement}", redirection_suffix)

    unique_paths = {source_declaration.path for source_declaration in declarations}
    if len(declarations) > 1 and len(unique_paths) > 1:
        replacement = render_source_dispatch(declaration.source_expression, declarations, render_source, indent, positional_frame_names)
        return append_source_site_redirections(f"{separator}{replacement}", redirection_suffix)

    if declaration.replacement_kind == "noop-source":
        return append_source_site_redirections(f"{separator}:", redirection_suffix)
    if is_missing_source_replacement_kind(declaration.replacement_kind):
        replacement = f"{separator}{{\n{render_missing_source_failure(declaration, indent)}\n{indent}}}"
        return append_source_site_redirections(replacement, redirection_suffix)
    if is_source_expansion_failure_replacement_kind(declaration.replacement_kind):
        if declaration.replacement_kind == SOURCE_EXPANSION_FAILURE_RETURN:
            replacement = (
                f"{separator}{{\n"
                f"{render_source_expansion_failure(declaration, indent, return_from_function=True)}\n"
                f"{indent}}}"
            )
            return append_source_site_redirections(replacement, redirection_suffix)
        replacement = f"{separator}{{\n{render_source_expansion_failure(declaration, indent)}\n{indent}}}"
        return append_source_site_redirections(replacement, redirection_suffix) + " #"

    rendered_source = indent_shell_block(
        render_source(
            declaration.path,
            source_arguments=declaration.source_arguments,
            source_state_generation=declaration.positional_assignment_generation,
            sync_positionals=declaration.sync_positionals,
        ),
        indent,
    )
    rendered_source = wrap_rendered_source_for_positional_frame(
        rendered_source,
        declaration,
        positional_frame_names,
        indent,
    )
    replacement = f"{separator}{{\n{rendered_source}\n{indent}}}"
    return append_source_site_redirections(replacement, redirection_suffix)


def append_source_site_redirections(replacement: str, redirection_suffix: str):
    if not redirection_suffix:
        return replacement
    return f"{replacement} {redirection_suffix}"


def source_site_redirection_suffix(source_site: str):
    invocation = source_command_invocation(source_site.strip(), stop_at_shell_control=True)
    if invocation is None:
        return ""
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(source_site.strip()))
    except UnsupportedSourceError:
        return ""
    prefix_words = raw_words[:invocation.source_index]
    source_words = raw_words[invocation.source_index + 1:invocation.source_end_index]
    redirections = source_site_redirection_words(prefix_words) + source_site_redirection_words(source_words)
    return " ".join(redirections)


def source_site_redirection_words(words: tuple[str, ...]):
    redirections: list[str] = []
    index = 0
    while index < len(words):
        redirection = source_site_redirection_token_kind(words[index])
        if redirection is None:
            index += 1
            continue
        if redirection == "unsupported":
            return ()
        redirections.append(words[index])
        index += 1
        if redirection == "separate-target" and index < len(words):
            redirections.append(words[index])
            index += 1
    return tuple(redirections)


def source_site_redirection_token_kind(word: str):
    if re.match(r"^(?:[0-9]+)?<<", word):
        return "unsupported"
    if re.fullmatch(r"(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|)", word):
        return "separate-target"
    if re.match(r"^(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|).+", word):
        return "combined-target"
    return None


def source_declaration_groups(source_declarations):
    groups = []
    index = 0
    while index < len(source_declarations):
        declaration = source_declarations[index]
        source_site = declaration.source_site.strip()
        group = [declaration]
        index += 1
        while index < len(source_declarations) and source_declarations[index].source_site.strip() == source_site:
            group.append(source_declarations[index])
            index += 1
        groups.append(group)
    return groups


def remaining_source_declaration_groups(declarations_by_column, fallback_declarations):
    groups = []
    for source_column in sorted(declarations_by_column):
        groups.extend(source_declaration_groups(declarations_by_column[source_column]))
    groups.extend(source_declaration_groups(fallback_declarations))
    return groups


def spans_overlap(left, right):
    return left[0] < right[1] and right[0] < left[1]


def find_unquoted_source_site_span(line: str, source_site: str, occupied_spans):
    search_start = 0
    while search_start < len(line):
        source_index = find_unquoted_substring(line, source_site, search_start)
        if source_index < 0:
            return None
        span = (source_index, source_index + len(source_site))
        if not any(spans_overlap(span, occupied_span) for occupied_span in occupied_spans):
            return span
        search_start = span[1]
    return None


def source_declaration_site_candidates(source_declaration, runtime_reference_path: str | None, entry_point: str | None):
    source_site = source_declaration.source_site.strip()
    candidates = [source_site]
    if runtime_reference_path is not None and entry_point is not None:
        rewritten = replace_runtime_source_references(source_site, runtime_reference_path, entry_point)
        if rewritten not in candidates:
            candidates.append(rewritten)
    return tuple(candidates)


def find_source_declaration_span(
    line: str,
    source_declaration,
    occupied_spans,
    *,
    runtime_reference_path: str | None = None,
    entry_point: str | None = None,
):
    source_column = source_declaration.source_column
    if source_column is not None:
        source_index = source_column - 1
        if source_index >= 0:
            for source_site in source_declaration_site_candidates(source_declaration, runtime_reference_path, entry_point):
                span = (source_index, source_index + len(source_site))
                if (
                    line.startswith(source_site, source_index)
                    and not any(spans_overlap(span, occupied_span) for occupied_span in occupied_spans)
                ):
                    return span

    for source_site in source_declaration_site_candidates(source_declaration, runtime_reference_path, entry_point):
        span = find_unquoted_source_site_span(line, source_site, occupied_spans)
        if span is not None:
            return span
    return None


def apply_line_replacements(line: str, replacements):
    output = []
    last_end = 0
    for start, end, replacement in sorted(replacements, key=lambda item: item[0]):
        if start < last_end:
            raise ValueError(f"Overlapping source replacements in line: {line.strip()}")
        output.append(line[last_end:start])
        output.append(replacement)
        last_end = end
    output.append(line[last_end:])
    return ''.join(output)


def replace_exact_line_fragments(line: str, line_replacements):
    if not line_replacements:
        return line

    replacements = []
    occupied_spans = []
    for line_replacement in line_replacements:
        old = line_replacement.old
        start = line.find(old)
        if start < 0:
            raise ValueError(f"Could not replace resolved line fragment: {old}")
        span = (start, start + len(old))
        if any(spans_overlap(span, occupied_span) for occupied_span in occupied_spans):
            raise ValueError(f"Overlapping line replacements in line: {line.strip()}")
        replacements.append((*span, line_replacement.new))
        occupied_spans.append(span)

    return apply_line_replacements(line, replacements)


def replace_source_site_declarations(
    line: str,
    source_declarations,
    render_source,
    positional_frame_names: dict[str, str] | None = None,
    *,
    runtime_reference_path: str | None = None,
    entry_point: str | None = None,
):
    if not source_declarations:
        return line

    declarations_by_column, fallback_declarations = group_source_declarations_by_column(source_declarations)
    replacements = []
    occupied_spans = []

    for grouped_declarations in remaining_source_declaration_groups(declarations_by_column, fallback_declarations):
        span = find_source_declaration_span(
            line,
            grouped_declarations[0],
            occupied_spans,
            runtime_reference_path=runtime_reference_path,
            entry_point=entry_point,
        )
        if span is None:
            source_site = grouped_declarations[0].source_site.strip()
            raise ValueError(f"Could not replace resolved source declaration: {source_site}")

        indent = re.match(r'\s*', line[:span[0]]).group(0)
        replacement = render_source_site_replacement(
            "",
            grouped_declarations,
            render_source,
            indent,
            positional_frame_names,
        )
        replacements.append((*span, replacement))
        occupied_spans.append(span)

    return apply_line_replacements(line, replacements)


def assert_no_unresolved_source_sites(content: str):
    active_heredocs = []
    for line in content.splitlines():
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue

        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if line_contains_unresolved_source(line):
            raise UnsupportedSourceError(
                f"unresolved source remained in executable output: {stripped_line}",
                code="unsupported.source.unresolved-output",
                hint="Executable output cannot contain live source statements.",
            )

        active_heredocs.extend(extract_heredoc_delimiters(line))


def line_contains_unresolved_source(line: str):
    if any(contains_source_command(command) for command in get_commands(line)):
        return True
    if "source" not in line and not re.search(r'(^|[\s;&|({])\.\s+', line):
        return False
    return contains_nested_source_command(line)


def render_executable_script(entry_point: str, context: dict):
    file_contents = {}
    top_level_trait_cache = {}
    render_stack = []

    def get_content(filepath):
        if filepath not in file_contents:
            content = read_file(filepath)
            file_contents[filepath] = content
        return file_contents[filepath]

    def top_level_traits(filepath, content):
        if filepath not in top_level_trait_cache:
            top_level_trait_cache[filepath] = file_top_level_source_traits(filepath, content)
        return top_level_trait_cache[filepath]

    def render_file(
        filepath,
        *,
        as_source=False,
        source_arguments=None,
        source_state_generation=None,
        sync_positionals=False,
    ):
        filepath = os.path.abspath(filepath)
        if filepath in render_stack:
            chain = " -> ".join([*render_stack, filepath])
            raise RecursionError(f"Circular source dependency while rendering: {chain}")

        render_stack.append(filepath)
        try:
            source_context = context.get('source_declarations', {}).get(filepath, {})
            output = []

            def render_source_file(
                source_filepath,
                source_arguments=None,
                source_state_generation=None,
                sync_positionals=False,
            ):
                return render_file(
                    source_filepath,
                    as_source=True,
                    source_arguments=source_arguments,
                    source_state_generation=source_state_generation,
                    sync_positionals=sync_positionals,
                )

            content = get_content(filepath)
            has_top_level_return, has_top_level_positional_mutation = top_level_traits(filepath, content)
            wraps_top_level_return = as_source and has_top_level_return
            positional_frame_names = (
                source_positional_capture_names(filepath)
                if as_source and source_arguments is not None and sync_positionals
                else None
            )
            positional_sync_replacements = (
                source_positional_sync_replacements(
                    filepath,
                    content,
                    capture_shift=True,
                    capture_shift_when_set=source_arguments is not None,
                )
                if (
                    as_source
                    and (
                        (source_arguments is not None and sync_positionals)
                        or (source_arguments is None and wraps_top_level_return)
                    )
                    and has_top_level_positional_mutation
                )
                else {}
            )
            should_sync_positionals = bool(positional_sync_replacements) or positional_frame_names is not None
            for num, line in enumerate(content.splitlines()):
                stripped_line = line.strip()
                if not stripped_line or stripped_line.startswith("#"):
                    continue

                line_replacements = [
                    *context.get('line_replacements', {}).get(filepath, {}).get(num, []),
                    *positional_sync_replacements.get(num, []),
                ]
                line = replace_exact_line_fragments(
                    line,
                    line_replacements,
                )
                source_declarations = source_context.get(num, [])
                unsupported_sources = [
                    source_declaration for source_declaration in source_declarations
                    if source_declaration.execution_model not in {"parent-source", "child-shell"}
                ]
                if unsupported_sources:
                    source_site = unsupported_sources[0].source_site
                    raise NotImplementedError(f"unsupported non-parent source in executable mode: {source_site}")

                line = replace_runtime_source_references(line, filepath, entry_point)
                command_sources = [
                    source_declaration for source_declaration in source_declarations
                    if source_declaration.replacement_kind in {"command", "noop-command", "bash-c-source"}
                ]
                line = replace_command_source_sites(
                    line,
                    command_sources,
                    render_source_file,
                    positional_frame_names,
                )
                source_site_declarations = [
                    source_declaration for source_declaration in source_declarations
                    if (
                        source_declaration.replacement_kind in {"source", "noop-source", "retained-source"}
                        or is_missing_source_replacement_kind(source_declaration.replacement_kind)
                        or is_source_expansion_failure_replacement_kind(source_declaration.replacement_kind)
                    )
                ]
                if source_site_declarations:
                    line = replace_source_site_declarations(
                        line,
                        source_site_declarations,
                        render_source_file,
                        positional_frame_names,
                        runtime_reference_path=filepath,
                        entry_point=entry_point,
                    )
                output.append(line)

            rendered = '\n'.join(output)
            if as_source and (source_arguments is not None or wraps_top_level_return):
                return render_source_call_wrapper(
                    filepath,
                    rendered,
                    source_arguments,
                    sync_positionals=should_sync_positionals,
                )
            return rendered
        finally:
            render_stack.pop()

    # Build from the entry point so sourced files execute at their source sites.
    output = [SET_SHEBANG, '']
    output.append(construct_file_separator(entry_point, entry_point))
    rendered_entry = render_file(os.path.abspath(entry_point))
    assert_no_unresolved_source_sites(rendered_entry)
    output.append(rendered_entry)
    output.append('')

    return output
