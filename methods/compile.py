import hashlib
import os
import re
from collections import defaultdict

from methods.regex.patterns import SOURCE_PATTERN
from methods.shell.line import first_top_level_pipeline_index, get_commands
from methods.source_evaluator import SourceEvaluator
from methods.source_effects import (
    CaseBlock,
    CStyleForLoop,
    ForLoop,
    FunctionDef,
    IfBlock,
    LineReplacement,
    RawCommand,
    SetCommand,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend
from methods.source_commands import (
    contains_nested_source_command,
    contains_source_command,
    source_command_invocation,
)
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    MISSING_SOURCE_NO_FILENAME,
    SOURCE_EXPANSION_FAILURE_RETURN,
    ResolvedSource,
    UnsupportedSourceError,
    extract_heredoc_delimiters,
    is_heredoc_end,
    is_missing_source_replacement_kind,
    is_source_expansion_failure_replacement_kind,
    missing_source_status,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)
from methods.source_supplements import load_source_supplement
from methods.sources import validate_path

SET_SHEBANG = "#!/bin/bash"


def shell_quote(value: str):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def replace_runtime_source_references(line: str, filepath: str, entry_point: str):
    return ''.join(
        _replace_runtime_source_references_segment(segment, filepath, entry_point)
        if expandable
        else segment
        for segment, expandable in _single_quote_aware_segments(line)
    )


def _replace_runtime_source_references_segment(segment: str, filepath: str, entry_point: str):
    bash_source = shell_quote(os.path.abspath(filepath))
    entry_source = shell_quote(os.path.abspath(entry_point))

    replacements = {
        '"${BASH_SOURCE[0]}"': bash_source,
        '"${BASH_SOURCE}"': bash_source,
        '"$BASH_SOURCE"': bash_source,
        '${BASH_SOURCE[0]}': bash_source,
        '${BASH_SOURCE}': bash_source,
        '$BASH_SOURCE': bash_source,
        '"${0}"': entry_source,
        '"$0"': entry_source,
        '${0}': entry_source,
    }

    for old, new in replacements.items():
        segment = segment.replace(old, new)

    return re.sub(r'\$0(?![0-9])', entry_source, segment)


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


def indent_block(content: str, prefix: str):
    lines = content.splitlines()
    return '\n'.join(f"{prefix}{line}" if line else line for line in lines)


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


def shell_quote_words(words):
    return " ".join(shell_quote(word) for word in words)


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
        suffix = f"{suffix} (args: {shell_quote_words(source_declaration.source_arguments)})"

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


def generated_source_function_name(filepath: str):
    digest = hashlib.sha1(os.path.abspath(filepath).encode("utf-8")).hexdigest()[:12]
    return f"__modash_source_{digest}"


def raw_command_is_return(node: RawCommand):
    return bool(re.match(r'^return(?:\s|$)', node.text.strip()))


def raw_command_is_shift(node: RawCommand):
    return bool(re.match(r'^shift(?:\s|$)', node.text.strip()))


def raw_command_is_eval(node: RawCommand):
    return bool(re.match(r'^eval(?:\s|$)', node.text.strip()))


def raw_command_is_simple_shift(node: RawCommand):
    return bool(re.fullmatch(r'shift(?:\s+\d+)?', node.text.strip()))


def set_command_assigns_positionals(node: SetCommand):
    arguments = node.arguments
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return True
        if not argument.startswith(("-", "+")):
            return True
        if argument in {'-o', '+o'}:
            if index + 1 >= len(arguments):
                return False
            index += 2
            continue
        if argument in {'-', '+'}:
            return False
        index += 1
    return False


def set_command_is_simple_positional_assignment(node: SetCommand):
    return bool(node.arguments) and node.arguments[0] == "--"


def nodes_have_top_level_return(nodes):
    for node in nodes:
        if isinstance(node, RawCommand) and raw_command_is_return(node):
            return True
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if nodes_have_top_level_return(node.body):
                return True
        elif isinstance(node, IfBlock):
            if any(nodes_have_top_level_return(branch.body) for branch in node.branches):
                return True
        elif isinstance(node, CaseBlock):
            if any(nodes_have_top_level_return(arm.body) for arm in node.arms):
                return True
    return False


def nodes_have_top_level_positional_mutation(nodes):
    for node in nodes:
        if isinstance(node, RawCommand) and raw_command_is_shift(node):
            return True
        if isinstance(node, RawCommand) and raw_command_is_eval(node):
            return True
        if isinstance(node, SetCommand) and set_command_assigns_positionals(node):
            return True
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if nodes_have_top_level_positional_mutation(node.body):
                return True
        elif isinstance(node, IfBlock):
            if any(nodes_have_top_level_positional_mutation(branch.body) for branch in node.branches):
                return True
        elif isinstance(node, CaseBlock):
            if any(nodes_have_top_level_positional_mutation(arm.body) for arm in node.arms):
                return True
    return False


def file_top_level_source_traits(filepath: str, content: str):
    has_return_text = "return" in content
    has_positional_mutation_text = bool(re.search(r'\b(?:eval|set|shift)\b', content))
    if not has_return_text and not has_positional_mutation_text:
        return False, False
    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    return (
        nodes_have_top_level_return(ir.nodes) if has_return_text else False,
        nodes_have_top_level_positional_mutation(ir.nodes) if has_positional_mutation_text else False,
    )


def source_positional_capture_names(filepath: str):
    prefix = generated_source_function_name(filepath)
    return {
        "positionals": f"{prefix}_positionals",
        "positionals_set": f"{prefix}_positionals_set",
        "source_status": f"{prefix}_source_status",
        "finish": f"{prefix}_finish",
        "shift_status": f"{prefix}_shift_status",
    }


def render_set_positional_capture(command: str, names: dict[str, str]):
    return (
        f"{{ {command}; "
        f"{names['positionals']}=(\"$@\"); "
        f"{names['positionals_set']}=1; "
        f"}}"
    )


def render_shift_positional_capture(
    command: str,
    names: dict[str, str],
    *,
    require_existing_capture: bool = False,
):
    capture_condition = f"{names['shift_status']} == 0"
    if require_existing_capture:
        capture_condition = f"{capture_condition} && {names['positionals_set']}"
    return (
        f"{{ {command}; "
        f"local {names['shift_status']}=$?; "
        f"if (( {capture_condition} )); then "
        f"{names['positionals']}=(\"$@\"); "
        f"{names['positionals_set']}=1; "
        f"fi; "
        f"( exit \"${names['shift_status']}\" ); "
        f"}}"
    )


def render_positional_frame_reset(names: dict[str, str], indent: str):
    return f"{indent}{names['positionals_set']}=0"


def render_positional_frame_capture(names: dict[str, str], indent: str):
    return (
        f"{indent}{names['source_status']}=$?\n"
        f"{indent}{names['positionals']}=(\"$@\"); "
        f"{names['positionals_set']}=1\n"
        f"{indent}( exit \"${names['source_status']}\" )"
    )


def wrap_rendered_source_for_positional_frame(
    rendered_source: str,
    source_declaration,
    names: dict[str, str] | None,
    indent: str,
):
    if names is None or source_declaration.replacement_kind.startswith("noop-"):
        return rendered_source

    lines = [render_positional_frame_reset(names, indent), rendered_source]
    if source_declaration.source_arguments is None and source_declaration.sync_positionals:
        lines.append(render_positional_frame_capture(names, indent))
    return '\n'.join(lines)


def _collect_positional_sync_replacements(
    nodes,
    names: dict[str, str],
    replacements: dict[int, list],
    *,
    capture_shift: bool,
    capture_shift_when_set: bool,
):
    for node in nodes:
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, SetCommand) and set_command_assigns_positionals(node):
            if not set_command_is_simple_positional_assignment(node):
                raise UnsupportedSourceError(
                    f"unsupported positional set syntax in wrapped sourced file: {node.text.strip()}",
                    code="unsupported.source.positionals",
                    hint="Only top-level set -- positional mutation is supported in wrapped sourced files.",
                )
            replacements.setdefault(node.location.line - 1, []).append(LineReplacement(
                node.location,
                node.text,
                render_set_positional_capture(node.text, names),
            ))
            continue
        if isinstance(node, RawCommand) and raw_command_is_shift(node):
            if not capture_shift:
                continue
            if not raw_command_is_simple_shift(node):
                raise UnsupportedSourceError(
                    f"unsupported shift syntax in wrapped sourced file: {node.text.strip()}",
                    code="unsupported.source.positionals",
                    hint="Only top-level shift with an optional exact non-negative integer count is supported.",
                )
            replacements.setdefault(node.location.line - 1, []).append(LineReplacement(
                node.location,
                node.text,
                render_shift_positional_capture(
                    node.text,
                    names,
                    require_existing_capture=capture_shift_when_set,
                ),
            ))
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            _collect_positional_sync_replacements(
                node.body,
                names,
                replacements,
                capture_shift=capture_shift,
                capture_shift_when_set=capture_shift_when_set,
            )
        elif isinstance(node, IfBlock):
            for branch in node.branches:
                _collect_positional_sync_replacements(
                    branch.body,
                    names,
                    replacements,
                    capture_shift=capture_shift,
                    capture_shift_when_set=capture_shift_when_set,
                )
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                _collect_positional_sync_replacements(
                    arm.body,
                    names,
                    replacements,
                    capture_shift=capture_shift,
                    capture_shift_when_set=capture_shift_when_set,
                )


def source_positional_sync_replacements(
    filepath: str,
    content: str,
    *,
    capture_shift: bool,
    capture_shift_when_set: bool = False,
):
    names = source_positional_capture_names(filepath)
    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    replacements = {}
    _collect_positional_sync_replacements(
        ir.nodes,
        names,
        replacements,
        capture_shift=capture_shift,
        capture_shift_when_set=capture_shift_when_set,
    )
    return replacements


def render_source_call_wrapper(filepath: str, content: str, source_arguments=None, sync_positionals=False):
    body_function = generated_source_function_name(filepath)
    wrapper_function = f"{body_function}_run"
    status_variable = f"{body_function}_status"
    if source_arguments is None:
        call_arguments = ' "$@"'
    elif source_arguments:
        call_arguments = " " + shell_quote_words(source_arguments)
    else:
        call_arguments = ""
    definitions = (
        f"{body_function}() {{\n{content}\n}}\n"
        f"{wrapper_function}() {{\n"
        f"{body_function} \"$@\"\n"
        f"local {status_variable}=$?\n"
        f"unset -f {wrapper_function} {body_function}\n"
        f"return ${status_variable}\n"
        f"}}\n"
    )
    call = f"{wrapper_function}{call_arguments}"
    if not sync_positionals:
        return f"{definitions}{call}"

    names = source_positional_capture_names(filepath)
    return (
        f"{definitions}"
        "{\n"
        f"{names['positionals_set']}=0\n"
        f"{names['positionals']}=()\n"
        f"{call}\n"
        f"{names['source_status']}=$?\n"
        f"if (( {names['positionals_set']} )); then\n"
        f"  set -- \"${{{names['positionals']}[@]}}\"\n"
        "fi\n"
        f"{names['finish']}() {{\n"
        f"  local {names['finish']}_status=$1\n"
        f"  unset {names['positionals_set']} {names['positionals']} {names['source_status']}\n"
        f"  unset -f {names['finish']}\n"
        f"  return \"${names['finish']}_status\"\n"
        "}\n"
        f"{names['finish']} \"${names['source_status']}\"\n"
        "}"
    )


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


def source_site_for_match(match):
    _, command_name, arguments = match.groups()
    return f"{command_name.strip()} {(arguments or '').strip()}".strip()


def source_column_for_match(match):
    return match.start(2) + 1


def pop_source_declarations_for_match(match, declarations_by_column, group_fallback=True):
    grouped_declarations = declarations_by_column.pop(source_column_for_match(match), [])
    if grouped_declarations:
        return grouped_declarations

    match_source_site = source_site_for_match(match)
    for source_column, declarations in list(declarations_by_column.items()):
        if declarations and declarations[0].source_site == match_source_site:
            if not group_fallback:
                grouped_declarations = [declarations.pop(0)]
                if not declarations:
                    declarations_by_column.pop(source_column)
                return grouped_declarations
            return declarations_by_column.pop(source_column)

    return []


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
    retained_declarations = [
        declaration for declaration in declarations
        if declaration.replacement_kind == "retained-source"
    ]
    if retained_declarations:
        return f"{separator}{render_retained_source_dispatch(retained_declarations, render_source, indent, positional_frame_names)}"

    declaration = declarations[0]
    unique_paths = {source_declaration.path for source_declaration in declarations}
    if len(declarations) > 1 and len(unique_paths) > 1:
        return f"{separator}{render_source_dispatch(declaration.source_expression, declarations, render_source, indent, positional_frame_names)}"

    if declaration.replacement_kind == "noop-source":
        return f"{separator}:"
    if is_missing_source_replacement_kind(declaration.replacement_kind):
        return f"{separator}{{\n{render_missing_source_failure(declaration, indent)}\n{indent}}}"
    if is_source_expansion_failure_replacement_kind(declaration.replacement_kind):
        if declaration.replacement_kind == SOURCE_EXPANSION_FAILURE_RETURN:
            return (
                f"{separator}{{\n"
                f"{render_source_expansion_failure(declaration, indent, return_from_function=True)}\n"
                f"{indent}}}"
            )
        return f"{separator}{{\n{render_source_expansion_failure(declaration, indent)}\n{indent}}} #"

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
    return f"{separator}{{\n{rendered_source}\n{indent}}}"


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


def find_source_declaration_span(line: str, source_declaration, occupied_spans):
    source_site = source_declaration.source_site.strip()
    source_column = source_declaration.source_column
    if source_column is not None:
        source_index = source_column - 1
        span = (source_index, source_index + len(source_site))
        if (
            source_index >= 0
            and line.startswith(source_site, source_index)
            and not any(spans_overlap(span, occupied_span) for occupied_span in occupied_spans)
        ):
            return span

    return find_unquoted_source_site_span(line, source_site, occupied_spans)


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
):
    if not source_declarations:
        return line

    if first_top_level_pipeline_index(line) is None:
        matches = [
            match for match in SOURCE_PATTERN.finditer(line)
            if not match.group(0).lstrip().startswith('$(')
        ]
    else:
        matches = []
    declarations_by_column, fallback_declarations = group_source_declarations_by_column(source_declarations)
    replacements = []
    occupied_spans = []

    for match in matches:
        grouped_declarations = pop_source_declarations_for_match(
            match,
            declarations_by_column,
            group_fallback=len(matches) == 1,
        )
        if not grouped_declarations and fallback_declarations:
            grouped_declarations.append(fallback_declarations.pop(0))
        if not grouped_declarations:
            continue

        separator = match.group(1) or ''
        indent = re.match(r'\s*', separator).group(0) if separator else ''
        replacement = render_source_site_replacement(
            separator,
            grouped_declarations,
            render_source,
            indent,
            positional_frame_names,
        )
        span = (match.start(), match.end())
        replacements.append((*span, replacement))
        occupied_spans.append(span)

    for grouped_declarations in remaining_source_declaration_groups(declarations_by_column, fallback_declarations):
        span = find_source_declaration_span(line, grouped_declarations[0], occupied_spans)
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
    if SOURCE_PATTERN.findall(line):
        return True
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


def compile_sources(
    entry_point: str,
    output_file: str,
    mode: str = "context",
    source_supplement=None,
):
    if mode not in {"context", "executable"}:
        raise ValueError(f"Unsupported compile mode: {mode}")

    if not validate_path(entry_point):
        raise FileNotFoundError(f"Error: Could not resolve the path to the entry point - {entry_point}")

    if not os.path.isfile(entry_point):
        raise OSError(f"Error: entry point must be a file - {entry_point}")

    entry_point = os.path.abspath(entry_point)
    supplement = load_source_supplement(source_supplement, os.path.dirname(entry_point))
    evaluation = SourceEvaluator(
        mode=mode,
        source_supplement=supplement,
    ).evaluate(entry_point)
    context = context_from_source_events(evaluation.events, evaluation.disabled_sources, evaluation.line_replacements)
    if mode == "executable":
        output = render_executable_script(entry_point, context)
    else:
        sources = context_paths_from_source_events(entry_point, evaluation.events)
        output = render_context_files(sources, entry_point, context)
    content = '\n'.join(output)
    write_output(output_file, content)
