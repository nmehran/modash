import hashlib
import os

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
from methods.source_commands import shell_quote_words
from methods.source_resolver import UnsupportedSourceError
from methods.source_traits import (
    raw_command_is_shift,
    raw_command_is_simple_shift,
    set_command_assigns_positionals,
    set_command_is_simple_positional_assignment,
)


def generated_source_function_name(filepath: str):
    digest = hashlib.sha1(os.path.abspath(filepath).encode("utf-8")).hexdigest()[:12]
    return f"__modash_source_{digest}"


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
    has_explicit_source_arguments = (
        source_declaration.source_arguments is not None
        or getattr(source_declaration, "source_argument_words", None) is not None
    )
    if not has_explicit_source_arguments and source_declaration.sync_positionals:
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


def render_source_call_wrapper(
    filepath: str,
    content: str,
    source_arguments=None,
    *,
    source_argument_words=None,
    sync_positionals=False,
):
    body_function = generated_source_function_name(filepath)
    wrapper_function = f"{body_function}_run"
    status_variable = f"{body_function}_status"
    if source_argument_words is not None:
        call_arguments = f" {' '.join(source_argument_words)}" if source_argument_words else ""
    elif source_arguments is None:
        call_arguments = ' "$@"'
    elif source_arguments:
        call_arguments = " " + shell_quote_words(source_arguments, always_quote=True)
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
