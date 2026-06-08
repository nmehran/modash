import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass

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


@dataclass(frozen=True)
class SourceArgumentReplay:
    render_words: tuple[str, ...] | None = None
    setup_lines: tuple[str, ...] = ()
    cleanup_lines: tuple[str, ...] = ()
    outer_words: tuple[str, ...] = ()

def replace_runtime_source_references(
    line: str,
    filepath: str,
    entry_point: str,
    *,
    include_zero: bool = True,
    bash_source_stack: tuple[str, ...] | None = None,
    bash_source_stack_array: str | None = None,
    support_extended_bash_source: bool = True,
    reject_unsupported_bash_source: bool = True,
):
    bash_source_stack = bash_source_stack or (os.path.abspath(filepath),)
    code, comment = _split_shell_comment(line)
    return ''.join(
        _replace_runtime_source_references_segment(
            segment,
            filepath,
            entry_point,
            include_zero=include_zero,
            bash_source_stack=bash_source_stack,
            bash_source_stack_array=bash_source_stack_array,
            support_extended_bash_source=support_extended_bash_source,
            reject_unsupported_bash_source=reject_unsupported_bash_source,
        )
        if expandable
        else segment
        for segment, expandable in _single_quote_aware_segments(code)
    ) + comment


def _replace_runtime_source_references_segment(
    segment: str,
    filepath: str,
    entry_point: str,
    *,
    include_zero: bool,
    bash_source_stack: tuple[str, ...],
    bash_source_stack_array: str | None,
    support_extended_bash_source: bool,
    reject_unsupported_bash_source: bool,
):
    bash_source = (
        f'"${{{bash_source_stack_array}[0]}}"'
        if bash_source_stack_array is not None
        else shell_quote(bash_source_stack[0] if bash_source_stack else os.path.abspath(filepath))
    )
    entry_source = shell_quote(os.path.abspath(entry_point))

    if support_extended_bash_source:
        segment = _replace_bash_source_array_word(segment, bash_source_stack)
        segment = _replace_bash_source_dirname_ops(segment, bash_source_stack)
        stack_indexes = range(len(bash_source_stack))
    else:
        stack_indexes = range(min(len(bash_source_stack), 1))
    for index in stack_indexes:
        quoted_source = (
            f'"${{{bash_source_stack_array}[{index}]}}"'
            if bash_source_stack_array is not None
            else shell_quote(bash_source_stack[index])
        )
        for token in (f'"${{BASH_SOURCE[{index}]}}"', f"${{BASH_SOURCE[{index}]}}"):
            segment = _replace_shell_word_token(segment, token, quoted_source)
    for token in ('"${BASH_SOURCE}"', '"$BASH_SOURCE"', '${BASH_SOURCE}', '$BASH_SOURCE'):
        segment = _replace_shell_word_token(segment, token, bash_source)

    if include_zero:
        for token in ('"${0}"', '"$0"', '${0}'):
            segment = _replace_shell_word_token(segment, token, entry_source)

    if include_zero:
        segment = re.sub(r'\$0(?![0-9])', entry_source, segment)
    if reject_unsupported_bash_source and re.search(r'\$(?:\{BASH_SOURCE(?:[^}]*)?\}|BASH_SOURCE\b)', segment):
        raise UnsupportedSourceError(
            f"unsupported BASH_SOURCE runtime reference in executable output: {segment.strip()}",
            code="unsupported.source.runtime-reference",
            hint="Use simple BASH_SOURCE scalar indexes or a standalone BASH_SOURCE[@] expansion.",
        )
    return segment


def _replace_bash_source_array_word(segment: str, bash_source_stack: tuple[str, ...]):
    replacement = " ".join(shell_quote(source) for source in bash_source_stack)
    for token in ('"${BASH_SOURCE[@]}"', '${BASH_SOURCE[@]}'):
        segment = _replace_shell_word_token(segment, token, replacement)
    return segment


def _replace_bash_source_dirname_ops(segment: str, bash_source_stack: tuple[str, ...]):
    for index, source_value in enumerate(bash_source_stack):
        dirname = _shell_percent_slash_star(source_value)
        quoted_dirname = shell_quote(dirname)
        for token in (
            f'"${{BASH_SOURCE[{index}]%/*}}"',
            f"${{BASH_SOURCE[{index}]%/*}}",
        ):
            segment = _replace_shell_word_token(segment, token, quoted_dirname)
    dirname = _shell_percent_slash_star(bash_source_stack[0]) if bash_source_stack else ""
    for token in ('"${BASH_SOURCE%/*}"', '${BASH_SOURCE%/*}'):
        segment = _replace_shell_word_token(segment, token, shell_quote(dirname))
    return segment


def _shell_percent_slash_star(value: str):
    if "/" not in value:
        return value
    return value.rsplit("/", 1)[0]


def _replace_shell_word_token(segment: str, token: str, replacement: str):
    pattern = rf"(?<![A-Za-z0-9_./:}}]){re.escape(token)}(?![A-Za-z0-9_./:{{])"
    if token.startswith('"'):
        return re.sub(pattern, lambda _match: replacement, segment)
    return ''.join(
        re.sub(pattern, lambda _match: replacement, part) if not in_double_quote else part
        for part, in_double_quote in _double_quote_aware_segments(segment)
    )


def _double_quote_aware_segments(segment: str):
    segments = []
    current = []
    in_double_quote = False
    escaped = False

    def flush():
        nonlocal current
        if current:
            segments.append((''.join(current), in_double_quote))
            current = []

    for char in segment:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char == '"':
            flush()
            current.append(char)
            in_double_quote = not in_double_quote
            flush()
            continue
        current.append(char)
    flush()
    return segments


def _split_shell_comment(line: str):
    in_single_quote = False
    in_double_quote = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if (
            char == "#"
            and not in_single_quote
            and not in_double_quote
            and (index == 0 or line[index - 1].isspace() or line[index - 1] in ";&|(")
        ):
            return line[:index], line[index:]
    return line, ""


def replace_lineno_references(line: str, line_index: int):
    from methods.runtime_evaluator.compiler_rewrite import (  # Local import avoids a renderer/runtime import cycle.
        _line_has_lineno_reference,
        _replace_lineno_references,
    )

    line = _replace_lineno_references(line, line_index)
    if _line_has_lineno_reference(line):
        raise UnsupportedSourceError(
            f"unsupported LINENO runtime reference in executable output: {line.strip()}",
            code="unsupported.source.runtime-reference",
            hint="Use simple LINENO scalar, arithmetic, test, let, or array-subscript forms.",
        )
    return line


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
                    source_argument_words=source_declaration.source_argument_words,
                    source_state_generation=source_declaration.positional_assignment_generation,
                    sync_positionals=source_declaration.sync_positionals,
                    bash_source_value=source_declaration.source_value or source_declaration.path,
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
                source_argument_words=source_declaration.source_argument_words,
                source_state_generation=source_declaration.positional_assignment_generation,
                sync_positionals=source_declaration.sync_positionals,
                bash_source_value=source_declaration.source_value or source_declaration.path,
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
                    source_argument_words=source_declaration.source_argument_words,
                    source_state_generation=source_declaration.positional_assignment_generation,
                    sync_positionals=source_declaration.sync_positionals,
                    bash_source_value=source_declaration.source_value or source_declaration.path,
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
            source_argument_words=source_declaration.source_argument_words,
            source_state_generation=source_declaration.positional_assignment_generation,
            sync_positionals=source_declaration.sync_positionals,
            bash_source_value=source_declaration.source_value or source_declaration.path,
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
    force_source_site_payload: bool = False,
):
    declaration = declarations[0]
    redirection_suffix = source_site_redirection_suffix(declaration.source_site)
    negation_prefix = source_site_negation_prefix(declaration.source_site)
    assignment_words = source_site_assignment_words(declaration.source_site)
    uses_embedded_payload = bool(assignment_words) or source_site_uses_builtin_wrapper(declaration.source_site)
    force_embedded_payload = (
        force_source_site_payload
        or uses_embedded_payload
        or source_site_needs_embedded_payload(declaration)
    )
    source_argument_replay = source_site_argument_replay(
        declaration,
        assignment_words,
        force_embedded_payload=force_embedded_payload,
    )
    retained_declarations = [
        declaration for declaration in declarations
        if declaration.replacement_kind == "retained-source"
    ]
    if retained_declarations:
        replacement = render_retained_source_dispatch(retained_declarations, render_source, indent, positional_frame_names)
        return render_source_site_shell_replacement(
            separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
            declaration.source_site, source_argument_replay=source_argument_replay,
        )

    unique_paths = {source_declaration.path for source_declaration in declarations}
    if len(declarations) > 1 and len(unique_paths) > 1:
        replacement = render_source_dispatch(declaration.source_expression, declarations, render_source, indent, positional_frame_names)
        return render_source_site_shell_replacement(
            separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
            declaration.source_site, source_argument_replay=source_argument_replay,
        )

    if declaration.replacement_kind == "noop-source":
        return render_source_site_shell_replacement(
            separator, negation_prefix, assignment_words, ":", redirection_suffix, indent,
            declaration.source_site, source_argument_replay=source_argument_replay,
        )
    if is_missing_source_replacement_kind(declaration.replacement_kind):
        replacement = f"{{\n{render_missing_source_failure(declaration, indent)}\n{indent}}}"
        return render_source_site_shell_replacement(
            separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
            declaration.source_site, source_argument_replay=source_argument_replay,
        )
    if is_source_expansion_failure_replacement_kind(declaration.replacement_kind):
        if declaration.replacement_kind == SOURCE_EXPANSION_FAILURE_RETURN:
            replacement = (
                "{\n"
                f"{render_source_expansion_failure(declaration, indent, return_from_function=True)}\n"
                f"{indent}}}"
            )
            return render_source_site_shell_replacement(
                separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
                declaration.source_site, source_argument_replay=source_argument_replay,
            )
        replacement = f"{{\n{render_source_expansion_failure(declaration, indent)}\n{indent}}}"
        return render_source_site_shell_replacement(
            separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
            declaration.source_site, source_argument_replay=source_argument_replay,
        ) + " #"

    rendered_source = render_source(
        declaration.path,
        source_arguments=declaration.source_arguments,
        source_argument_words=(
            declaration.source_argument_words
            if force_embedded_payload
            else source_argument_replay.render_words
        ),
        source_state_generation=declaration.positional_assignment_generation,
        sync_positionals=declaration.sync_positionals,
        wrap_source_call=not force_embedded_payload,
        force_source_site_payloads=force_embedded_payload,
        bash_source_value=declaration.source_value or declaration.path,
    )
    if force_embedded_payload:
        replacement = rendered_source
    else:
        rendered_source = indent_shell_block(
            rendered_source,
            indent,
        )
        rendered_source = wrap_rendered_source_for_positional_frame(
            rendered_source,
            declaration,
            positional_frame_names,
            indent,
        )
        replacement = f"{{\n{rendered_source}\n{indent}}}"
    return render_source_site_shell_replacement(
        separator, negation_prefix, assignment_words, replacement, redirection_suffix, indent,
        declaration.source_site,
        source_argument_replay=source_argument_replay,
        force_embedded_payload=force_embedded_payload,
    )


def source_site_needs_embedded_payload(declaration):
    return declaration.source_arguments is not None or declaration.source_argument_words is not None


def source_site_argument_replay(
    declaration,
    assignment_words: tuple[str, ...],
    *,
    force_embedded_payload: bool = False,
):
    source_argument_words = declaration.source_argument_words
    if source_argument_words is None:
        if force_embedded_payload and declaration.source_arguments is not None:
            quoted_arguments = tuple(shell_quote(argument) for argument in declaration.source_arguments)
            return SourceArgumentReplay(outer_words=quoted_arguments)
        return SourceArgumentReplay(render_words=None)
    if force_embedded_payload:
        return SourceArgumentReplay(outer_words=source_argument_words)
    if not assignment_words:
        return SourceArgumentReplay(render_words=source_argument_words)

    if any(source_argument_word_has_process_substitution(word) for word in source_argument_words):
        return SourceArgumentReplay(
            render_words=('"$@"',),
            outer_words=source_argument_words,
        )

    digest = hashlib.sha1(
        f"{declaration.source_site}\0{' '.join(source_argument_words)}".encode("utf-8")
    ).hexdigest()[:12]
    args_variable = f"__modash_source_args_{digest}"
    capture_function = f"{args_variable}_capture"
    return SourceArgumentReplay(
        render_words=(f'"${{{args_variable}[@]}}"',),
        setup_lines=(
            f"{args_variable}=()",
            f"{capture_function}() {{ {args_variable}=(\"$@\"); }}",
            f"{capture_function} {' '.join(source_argument_words)}",
        ),
        cleanup_lines=(
            f"unset -f {capture_function}",
            f"unset {args_variable}",
        ),
    )


def source_argument_word_has_process_substitution(word: str):
    return "<(" in word or ">(" in word


def render_source_site_shell_replacement(
    separator: str,
    negation_prefix: str,
    assignment_words: tuple[str, ...],
    replacement: str,
    redirection_suffix: str,
    indent: str,
    source_site: str = "",
    source_argument_replay: SourceArgumentReplay | None = None,
    force_embedded_payload: bool = False,
):
    if force_embedded_payload or assignment_words or source_site_uses_builtin_wrapper(source_site):
        replay = source_argument_replay or SourceArgumentReplay()
        replacement = wrap_source_site_embedded_payload(
            replacement,
            assignment_words,
            indent,
            source_site,
            negation_prefix,
            redirection_suffix,
            replay,
        )
        return f"{separator}{replacement}"
    return append_source_site_redirections(f"{separator}{negation_prefix}{replacement}", redirection_suffix)


def append_source_site_redirections(replacement: str, redirection_suffix: str):
    if not redirection_suffix:
        return replacement
    return f"{replacement} {redirection_suffix}"


def wrap_source_site_embedded_payload(
    replacement: str,
    assignment_words: tuple[str, ...],
    indent: str,
    source_site: str,
    negation_prefix: str,
    redirection_suffix: str,
    source_argument_replay: SourceArgumentReplay,
):
    digest = hashlib.sha1(f"{source_site}\0{replacement}".encode("utf-8")).hexdigest()[:12]
    payload_variable = f"__modash_source_payload_{digest}_file"
    entry_status_variable = f"__modash_source_payload_{digest}_entry_status"
    status_variable = f"__modash_source_payload_{digest}_status"
    delimiter = source_site_payload_delimiter(replacement, digest)
    source_call = source_site_embedded_source_call(
        source_site,
        payload_variable,
        assignment_words,
        negation_prefix,
        redirection_suffix,
        source_argument_replay.outer_words,
    )
    inner_indent = f"{indent}  "
    setup_lines = [f"{inner_indent}{line}" for line in source_argument_replay.setup_lines]
    cleanup_lines = [f"{inner_indent}{line}" for line in source_argument_replay.cleanup_lines]
    return "\n".join([
        "{",
        f"{inner_indent}{entry_status_variable}=$?",
        f"{inner_indent}{payload_variable}=$(mktemp \"${{TMPDIR:-/tmp}}/modash-source.XXXXXXXXXX\") || exit",
        f"{inner_indent}command cat > \"${{{payload_variable}}}\" <<'{delimiter}'",
        replacement,
        delimiter,
        *setup_lines,
        f"{inner_indent}if (( {entry_status_variable} == 0 )); then",
        f"{inner_indent}  {source_call}",
        f"{inner_indent}else",
        f"{inner_indent}  ( exit \"${{{entry_status_variable}}}\" ) || {source_call}",
        f"{inner_indent}fi",
        f"{inner_indent}{status_variable}=$?",
        *cleanup_lines,
        f"{inner_indent}command rm -f -- \"${{{payload_variable}}}\"",
        f"{inner_indent}unset {entry_status_variable}",
        f"{inner_indent}( exit \"${{{status_variable}}}\" )",
        f"{indent}}}",
    ])


def source_site_payload_delimiter(payload: str, seed: str):
    base = f"__MODASH_SOURCE_PAYLOAD_{seed.upper()}__"
    delimiter = base
    counter = 0
    payload_lines = set(payload.splitlines())
    while delimiter in payload_lines:
        counter += 1
        delimiter = f"{base}_{counter}"
    return delimiter


def source_site_embedded_source_call(
    source_site: str,
    payload_variable: str,
    assignment_words: tuple[str, ...],
    negation_prefix: str,
    redirection_suffix: str,
    source_argument_words: tuple[str, ...] = (),
):
    invocation = source_command_invocation(source_site.strip(), stop_at_shell_control=True)
    if invocation is None:
        command_words = ("source",)
    else:
        try:
            raw_words = tuple(parse_shell_words_preserving_quotes(source_site.strip()))
        except UnsupportedSourceError:
            raw_words = invocation.words
        command_words = (
            *source_site_wrapper_words(raw_words, invocation),
            raw_words[invocation.source_index],
        )
    argument_words = [f'"${{{payload_variable}}}"']
    argument_words.extend(source_argument_words)
    command = " ".join((*assignment_words, *command_words, *argument_words))
    if negation_prefix:
        command = f"{negation_prefix}{command}"
    if redirection_suffix:
        command = f"{command} {redirection_suffix}"
    return command


def source_site_uses_builtin_wrapper(source_site: str):
    invocation = source_command_invocation(source_site.strip(), stop_at_shell_control=True)
    if invocation is None or invocation.source_index <= invocation.command_start_index:
        return False
    return "builtin" in invocation.words[invocation.command_start_index:invocation.source_index]


def source_site_wrapper_words(raw_words: tuple[str, ...], invocation):
    wrappers = []
    index = 0
    while index < invocation.source_index:
        clean_word = invocation.words[index]
        if clean_word == "!" or ASSIGNMENT_WORD_PATTERN.match(clean_word):
            index += 1
            continue
        redirection = source_site_redirection_token_kind(clean_word)
        if redirection is not None:
            index += 1
            if redirection == "separate-target" and index < invocation.source_index:
                index += 1
            continue
        wrappers.append(raw_words[index])
        index += 1
    return tuple(wrappers)


def source_site_assignment_words(source_site: str):
    invocation = source_command_invocation(source_site.strip(), stop_at_shell_control=True)
    if invocation is None or invocation.source_index <= 0:
        return ()
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(source_site.strip()))
    except UnsupportedSourceError:
        return ()
    return tuple(
        raw_word
        for raw_word, word in zip(raw_words[:invocation.source_index], invocation.words[:invocation.source_index])
        if ASSIGNMENT_WORD_PATTERN.match(word)
    )


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


def source_site_negation_prefix(source_site: str):
    invocation = source_command_invocation(source_site.strip(), stop_at_shell_control=True)
    if invocation is None or invocation.source_index <= 0:
        return ""
    negations = sum(1 for word in invocation.words[:invocation.source_index] if word == "!")
    return "! " * negations


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
    if word.startswith("<(") or word.startswith(">("):
        return None
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


def source_declaration_site_candidates(
    source_declaration,
    runtime_reference_path: str | None,
    entry_point: str | None,
    bash_source_stack: tuple[str, ...] | None = None,
):
    source_site = source_declaration.source_site.strip()
    candidates = [source_site]
    if runtime_reference_path is not None and entry_point is not None:
        rewritten = replace_runtime_source_references(
            source_site,
            runtime_reference_path,
            entry_point,
            bash_source_stack=bash_source_stack,
        )
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
    bash_source_stack: tuple[str, ...] | None = None,
):
    source_column = source_declaration.source_column
    if source_column is not None:
        source_index = source_column - 1
        if source_index >= 0:
            for source_site in source_declaration_site_candidates(
                source_declaration,
                runtime_reference_path,
                entry_point,
                bash_source_stack,
            ):
                span = (source_index, source_index + len(source_site))
                if (
                    line.startswith(source_site, source_index)
                    and not any(spans_overlap(span, occupied_span) for occupied_span in occupied_spans)
                ):
                    return span

    for source_site in source_declaration_site_candidates(
        source_declaration,
        runtime_reference_path,
        entry_point,
        bash_source_stack,
    ):
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
    force_source_site_payloads: bool = False,
    bash_source_stack: tuple[str, ...] | None = None,
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
            bash_source_stack=bash_source_stack,
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
            force_source_site_payload=force_source_site_payloads,
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
    commands = get_commands(line)
    source_commands = [command for command in commands if contains_source_command(command)]
    if source_commands:
        return any(not command_sources_generated_payload(command) for command in source_commands)
    if "source" not in line and not re.search(r'(^|[\s;&|({])\.\s+', line):
        return False
    return contains_nested_source_command(line)


def command_sources_generated_payload(command: str):
    invocation = source_command_invocation(command.strip(), stop_at_shell_control=True)
    return (
        invocation is not None
        and re.fullmatch(r"\$?\{?__modash_source_payload_[A-Fa-f0-9]{12}_file\}?", invocation.source_path)
    )


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
        source_argument_words=None,
        source_state_generation=None,
        sync_positionals=False,
        wrap_source_call=True,
        force_source_site_payloads=False,
        bash_source_stack=None,
    ):
        filepath = os.path.abspath(filepath)
        bash_source_stack = bash_source_stack or (os.path.abspath(entry_point),)
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
                source_argument_words=None,
                source_state_generation=None,
                sync_positionals=False,
                wrap_source_call=True,
                force_source_site_payloads=False,
                bash_source_value=None,
            ):
                return render_file(
                    source_filepath,
                    as_source=True,
                    source_arguments=source_arguments,
                    source_argument_words=source_argument_words,
                    source_state_generation=source_state_generation,
                    sync_positionals=sync_positionals,
                    wrap_source_call=wrap_source_call,
                    force_source_site_payloads=force_source_site_payloads,
                    bash_source_stack=(
                        bash_source_value or os.path.abspath(source_filepath),
                        *bash_source_stack,
                    ),
                )

            content = get_content(filepath)
            has_top_level_return, has_top_level_positional_mutation = top_level_traits(filepath, content)
            wraps_top_level_return = wrap_source_call and as_source and has_top_level_return
            positional_frame_names = (
                source_positional_capture_names(filepath)
                if (
                    as_source
                    and (source_arguments is not None or source_argument_words is not None)
                    and sync_positionals
                )
                else None
            )
            positional_sync_replacements = (
                source_positional_sync_replacements(
                    filepath,
                    content,
                    capture_shift=True,
                    capture_shift_when_set=(
                        source_arguments is not None or source_argument_words is not None
                    ),
                )
                if (
                    as_source
                    and (
                        (
                            (source_arguments is not None or source_argument_words is not None)
                            and sync_positionals
                        )
                        or (
                            source_arguments is None
                            and source_argument_words is None
                            and wraps_top_level_return
                        )
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

                line = replace_runtime_source_references(
                    line,
                    filepath,
                    entry_point,
                    bash_source_stack=bash_source_stack,
                )
                line = replace_lineno_references(line, num + 1)
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
                        force_source_site_payloads=force_source_site_payloads,
                        bash_source_stack=bash_source_stack,
                    )
                output.append(line)

            rendered = '\n'.join(output)
            if as_source and (
                wrap_source_call
                and (
                    source_arguments is not None
                    or source_argument_words is not None
                    or wraps_top_level_return
                )
            ):
                return render_source_call_wrapper(
                    filepath,
                    rendered,
                    source_arguments,
                    source_argument_words=source_argument_words,
                    sync_positionals=should_sync_positionals,
                )
            return rendered
        finally:
            render_stack.pop()

    # Build from the entry point so sourced files execute at their source sites.
    output = [SET_SHEBANG, '']
    output.append(f"cd -- {shell_quote(os.path.dirname(os.path.abspath(entry_point)))} || exit 1")
    output.append('')
    output.append(construct_file_separator(entry_point, entry_point))
    rendered_entry = render_file(os.path.abspath(entry_point))
    assert_no_unresolved_source_sites(rendered_entry)
    output.append(rendered_entry)
    output.append('')

    return output
