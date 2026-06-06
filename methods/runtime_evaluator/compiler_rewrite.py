from __future__ import annotations

import codecs
import re
import secrets
import shutil
import shlex
from dataclasses import dataclass
from pathlib import Path

from methods.compile import find_unquoted_substring, replace_runtime_source_references
from methods.runtime_evaluator.compiler_model import (
    ENTRYPOINT_LOGICAL_PATH,
    RuntimeObservedCompileError,
    _CompilePlan,
    _EmbeddedFile,
    _ProcessPayload,
    _ReplayEdge,
    _RewriteUnit,
    _SourceCandidate,
)
from methods.runtime_evaluator.compiler_plan import _process_logical_path
from methods.runtime_evaluator.compiler_prelude import _render_replay_prelude, _target_logical_paths
from methods.runtime_evaluator.compiler_safety import _shell_command_index_after_wrappers, _words_have_source_bearing_shell_payload
from methods.shell.line import get_commands
from methods.shell.scan import read_backtick_body, read_balanced_body
from methods.source_commands import contains_nested_source_command, contains_source_command, source_command_invocation
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    UnsupportedSourceError,
    extract_heredoc_delimiters,
    is_heredoc_end,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)


_PROCESS_SUBSTITUTION_ARG_PLACEHOLDER = "__modash_process_substitution_arg_placeholder__"


@dataclass(frozen=True)
class _BashCInvocation:
    payload: str
    raw_payload_word: str
    dynamic_payload: bool = False


def _rewrite_process_payloads(plan: _CompilePlan) -> None:
    process_nodes = {node["process_index"]: node for node in plan.graph["nodes"] if node["kind"] == "process"}
    for process_index, process_plan in plan.process_plans.items():
        if process_index == 0:
            continue
        process_node = process_nodes.get(process_index, {})
        unit = _process_payload_unit(process_plan, process_node)
        unit.transformed = _rewrite_content(
            unit,
            process_plan.assignments,
            plan.entrypoint,
            {},
            rewrite_runtime_references=unit.physical_path is not None,
            rewrite_runtime_zero=False,
        )
        embedded_files = []
        for logical_path, file_unit in sorted(process_plan.units.items()):
            if logical_path in {ENTRYPOINT_LOGICAL_PATH, _process_logical_path(process_index), unit.logical_path}:
                continue
            transformed = _rewrite_content(
                file_unit,
                process_plan.assignments,
                plan.entrypoint,
                {},
                rewrite_runtime_references=True,
                rewrite_runtime_zero=False,
            )
            embedded_files.append(_EmbeddedFile(file_unit.logical_path, transformed))
        payload = (
            _render_replay_prelude(
                tuple(embedded_files),
                process_plan.assignments,
                _target_logical_paths(plan.file_units),
                environment_values=plan.graph["environment"].get("values", {}),
            ).rstrip("\n")
            + "\n"
            + unit.transformed.rstrip("\n")
            + "\n"
        )
        plan.process_payloads[process_index] = _ProcessPayload(
            identity=_process_payload_identity(unit, process_node),
            content=payload,
            entrypoint=process_node.get("entrypoint") if isinstance(process_node.get("entrypoint"), str) else None,
            cwd=process_node.get("cwd") if isinstance(process_node.get("cwd"), str) else None,
        )


def _process_payload_unit(process_plan, process_node: dict) -> _RewriteUnit:
    entrypoint = process_node.get("entrypoint")
    command = process_node.get("command")
    if isinstance(entrypoint, str) and isinstance(command, str):
        entrypoint_path = str(Path(entrypoint).resolve(strict=False))
        command_path = str(Path(command).resolve(strict=False))
        if entrypoint_path == command_path:
            for unit in process_plan.units.values():
                if unit.physical_path == entrypoint_path:
                    return unit
    return process_plan.units[_process_logical_path(process_plan.process_index)]


def _process_payload_identity(unit: _RewriteUnit, process_node: dict) -> str:
    if unit.physical_path is not None:
        return str(Path(unit.physical_path).resolve(strict=False))
    return unit.content

def _rewrite_file_units(plan: _CompilePlan) -> None:
    _ensure_unique_process_payloads(plan.process_payloads)
    main_plan = plan.process_plans[0]
    replacement_counts = {process_index: 0 for process_index in plan.process_payloads}
    for unit in plan.file_units.values():
        unit.transformed = _rewrite_content(
            unit,
            main_plan.assignments,
            plan.entrypoint,
            plan.process_payloads,
            process_replacement_counts=replacement_counts,
        )
    for process_index, count in replacement_counts.items():
        if count != 1:
            raise RuntimeObservedCompileError(
                f"observed child bash -c process {process_index} matched {count} parent command sites; expected exactly 1",
                code="runtime.compile.child_process_mapping_failed",
            )

def _rewrite_content(
    unit: _RewriteUnit,
    assignments: dict[str, list[_ReplayEdge]],
    entrypoint: Path,
    process_payloads: dict[int, _ProcessPayload],
    process_replacement_counts: dict[int, int] | None = None,
    rewrite_runtime_references: bool = True,
    rewrite_runtime_zero: bool = True,
    rewrite_lineno: bool = True,
) -> str:
    replacements_by_line = _candidate_replacements_by_line(unit)
    lines = unit.content.splitlines()
    output: list[str] = []
    active_heredocs = []
    quote_state: str | None = None
    for line_index, original_line in enumerate(lines, start=1):
        if active_heredocs:
            active_heredoc = active_heredocs[0]
            if not active_heredoc.quoted and contains_nested_source_command(original_line):
                raise RuntimeObservedCompileError(
                    f"unsupported unrewritten source command in heredoc body at {unit.physical_path or unit.logical_path}:{line_index}: {original_line.strip()}",
                    code="runtime.compile.unrewritten_source",
                )
            if (
                not active_heredoc.quoted
                and rewrite_runtime_references
                and _line_has_runtime_reference(original_line, include_zero=rewrite_runtime_zero)
            ):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler does not support runtime source references inside heredocs in {unit.physical_path or unit.logical_path}:{line_index}",
                    code="runtime.compile.runtime_reference_heredoc",
                )
            output.append(original_line)
            if is_heredoc_end(original_line, active_heredoc):
                active_heredocs.pop(0)
            continue
        if quote_state is not None:
            if quote_state == '"' and contains_nested_source_command(original_line):
                raise RuntimeObservedCompileError(
                    f"unsupported unrewritten source command inside multiline string at {unit.physical_path or unit.logical_path}:{line_index}: {original_line.strip()}",
                    code="runtime.compile.unrewritten_source",
                )
            if rewrite_runtime_references and _line_has_runtime_reference(original_line, include_zero=rewrite_runtime_zero):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler does not support runtime source references inside multiline strings in {unit.physical_path or unit.logical_path}:{line_index}",
                    code="runtime.compile.runtime_reference_multiline",
                )
            output.append(original_line)
            quote_state = _update_multiline_quote_state(original_line, quote_state)
            continue
        line = original_line
        replacements = replacements_by_line.get(line_index, [])
        if replacements:
            line = _apply_replacements(line, replacements)
        if rewrite_lineno:
            line = _replace_lineno_references(line, line_index)
            if _line_has_lineno_reference(line):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler does not support this LINENO reference form in {unit.physical_path or unit.logical_path}:{line_index}",
                    code="runtime.compile.runtime_reference",
                )
        if rewrite_runtime_references and unit.physical_path is not None:
            if _line_has_bash_c_payload(line):
                if _line_has_runtime_reference_outside_bash_c_payload(line):
                    raise RuntimeObservedCompileError(
                        f"runtime graph compiler does not support runtime source references on child bash -c lines in {unit.physical_path or unit.logical_path}:{line_index}",
                        code="runtime.compile.runtime_reference_child_bash",
                    )
            else:
                line = replace_runtime_source_references(
                    line,
                    unit.physical_path,
                    str(entrypoint),
                    include_zero=rewrite_runtime_zero,
                )
                if _line_has_runtime_reference(line, include_zero=rewrite_runtime_zero):
                    raise RuntimeObservedCompileError(
                        f"runtime graph compiler does not support this runtime source reference form in {unit.physical_path or unit.logical_path}:{line_index}",
                        code="runtime.compile.runtime_reference",
                    )
        line = _rewrite_exec_wrapper_commands(line)
        line = _rewrite_bash_c_payloads(line, process_payloads, process_replacement_counts)
        line = _guard_positional_replay_state_targets(line)
        line = _guard_dynamic_command_sites(line)
        line = _rewrite_safe_shopt_restore_eval(line)
        _ensure_no_unrewritten_source(line, unit, line_index)
        output.append(line)
        next_quote_state = _update_multiline_quote_state(original_line, quote_state)
        if next_quote_state is not None and _line_starts_multiline_bash_c(original_line):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler does not support multiline child bash -c payloads in {unit.physical_path or unit.logical_path}:{line_index}",
                code="runtime.compile.unsupported_child_bash",
            )
        if next_quote_state is None:
            active_heredocs.extend(extract_heredoc_delimiters(original_line))
        quote_state = next_quote_state
    rendered = "\n".join(output)
    if unit.content.endswith("\n"):
        rendered += "\n"
    return rendered

def _candidate_replacements_by_line(unit: _RewriteUnit) -> dict[int, list[tuple[int, int, str]]]:
    replacements_by_line: dict[int, list[tuple[int, int, str]]] = {}
    lines = unit.content.splitlines()
    search_start_by_line: dict[int, int] = {}
    for candidate in unit.candidates:
        if candidate.line < 1 or candidate.line > len(lines):
            raise RuntimeObservedCompileError(
                f"source candidate line out of range in {unit.physical_path or unit.logical_path}: {candidate.line}",
                code="runtime.compile.mapping_failed",
            )
        line = lines[candidate.line - 1]
        search_start = search_start_by_line.get(candidate.line, 0)
        if candidate.physical_lines:
            needle = candidate.physical_lines[0].strip()
            start = find_unquoted_substring(line, needle, search_start)
            if start < 0:
                start = line.find(needle, search_start)
        else:
            needle = candidate.text
            start = find_unquoted_substring(line, needle, search_start)
            if start < 0:
                needle = candidate.text.lstrip(";&| ")
                start = find_unquoted_substring(line, needle, search_start)
        if start < 0:
            raise RuntimeObservedCompileError(
                f"could not locate source site {candidate.text!r} in {unit.physical_path or unit.logical_path}:{candidate.line}",
                code="runtime.compile.mapping_failed",
            )
        source_word_offset = _source_word_offset_in_candidate(needle)
        if _source_site_is_inside_process_substitution(line, start + source_word_offset):
            raise RuntimeObservedCompileError(
                f"unsupported unrewritten source command in process substitution at {unit.physical_path or unit.logical_path}:{candidate.line}: {candidate.text.strip()}",
                code="runtime.compile.unrewritten_source",
            )
        if _source_site_has_unsupported_prefix(needle, candidate.separator):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler does not yet support replaying source command prefixes in {unit.physical_path or unit.logical_path}:{candidate.line}: {needle.strip()}",
                code="runtime.compile.unsupported_source_prefix",
            )
        source_expression, redirection_suffix = _source_expression_and_redirections_for_candidate(candidate)
        end = start + len(needle)
        search_start_by_line[candidate.line] = end
        replacement = _render_replay_group(
            candidate,
            source_expression=source_expression,
            source_expression_sets_status=_has_command_substitution(source_expression),
            assignment_words=_source_assignment_words_for_candidate(candidate),
            redirection_suffix=redirection_suffix,
            negated=False,
        )
        replay_prefix = _source_site_replay_prefix(needle, candidate.separator)
        if replay_prefix:
            replacement = f"{replay_prefix} {replacement}"
        if candidate.separator and candidate.text.lstrip().startswith(candidate.separator):
            replacement = f"{candidate.separator} {replacement}"
        if candidate.physical_lines:
            end_line = candidate.end_line or candidate.line
            last_line = lines[end_line - 1] if 1 <= end_line <= len(lines) else ""
            last_fragment = candidate.physical_lines[-1] if candidate.physical_lines else ""
            suffix = last_line[len(last_fragment):] if last_line.startswith(last_fragment) else ""
            replacements_by_line.setdefault(candidate.line, []).append((start, len(line), replacement + suffix))
            for blank_line in range(candidate.line + 1, end_line + 1):
                if 1 <= blank_line <= len(lines):
                    replacements_by_line.setdefault(blank_line, []).append((0, len(lines[blank_line - 1]), ""))
        else:
            replacements_by_line.setdefault(candidate.line, []).append((start, end, replacement))
    return replacements_by_line

def _render_replay_group(
    candidate: _SourceCandidate,
    *,
    source_expression: str,
    source_expression_sets_status: bool,
    assignment_words: tuple[str, ...] = (),
    redirection_suffix: str = "",
    negated: bool = False,
) -> str:
    path_expression, argument_expression, source_argc = _source_path_and_argument_expression_for_candidate(candidate)
    if _has_process_substitution(argument_expression):
        return _render_process_substitution_argument_replay_group(
            candidate,
            path_expression=path_expression,
            argument_expression=argument_expression,
            source_argc=source_argc,
            assignment_words=assignment_words,
            redirection_suffix=redirection_suffix,
            negated=negated,
        )
    if assignment_words:
        return _render_assignment_prefixed_replay_group(
            candidate,
            source_expression=source_expression,
            assignment_words=assignment_words,
            redirection_suffix=redirection_suffix,
            negated=negated,
        )
    redirection_sets_status = _has_command_substitution(redirection_suffix)
    source_entry_status = (
        "__modash_source_redirection_status"
        if redirection_sets_status
        else "__modash_source_expansion_status"
        if source_expression_sets_status
        else "__modash_source_command_prior_status"
    )
    redirected_validation = ""
    if redirection_sets_status:
        redirected_validation = (
            "__modash_source_redirection_status=$?; "
            f"__modash_source_entry_status=${source_entry_status}; "
            "__modash_validate_source_argv \"$__modash_source_entry_status\" \"${__modash_source_argv[@]}\"; "
        )
    file_source_operation = redirected_validation + (
        "( exit \"$__modash_source_entry_status\" ); "
        "builtin source <(__modash_emit_embedded_file \"$__modash_replay_target\") \"${__modash_replay_args[@]}\"; "
        "__modash_replay_actual_status=$?; "
    )
    missing_source_operation = redirected_validation + (
        "builtin printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
    )
    validation_before_source = (
        ""
        if redirection_sets_status
        else "__modash_validate_source_argv \"$__modash_source_entry_status\" \"${__modash_source_argv[@]}\"; "
    )
    missing_status_capture = "__modash_replay_missing_status=$?; "
    if redirection_suffix:
        file_source_operation = (
            "unset __modash_replay_actual_status; "
            f"{{ {file_source_operation}}} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_actual_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_source_operation = (
            "unset __modash_replay_missing_status; "
            f"{{ {missing_source_operation}__modash_replay_missing_status=$?; }} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_missing_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_status_capture = ""
    group = (
        f"{{ __modash_source_command_prior_status=$?; __modash_source_argv=(); __modash_source_argv=( {source_expression} ); "
        "__modash_source_expansion_status=$?; "
        f"__modash_source_entry_status=${source_entry_status}; "
        f"__modash_replay_select_active=1; __modash_select_source_edge {shlex.quote(candidate.base_id)}; "
        "__modash_replay_select_active=0; "
        "__modash_replay_select_status=$?; "
        f"{validation_before_source}"
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "__modash_require_embedded_file \"$__modash_replay_target\"; "
        f"{file_source_operation}"
        "if (( __modash_replay_actual_status != __modash_replay_status )); then "
        "__modash_abort \"observed source status drift\"; "
        "fi; "
        "( exit \"$__modash_replay_actual_status\" ); "
        "else "
        f"{missing_source_operation}"
        f"{missing_status_capture}"
        "( exit \"$__modash_replay_missing_status\" ); "
        "fi; }"
    )
    return f"! {group}" if negated else group


def _render_process_substitution_argument_replay_group(
    candidate: _SourceCandidate,
    *,
    path_expression: str,
    argument_expression: str,
    source_argc: int,
    assignment_words: tuple[str, ...] = (),
    redirection_suffix: str = "",
    negated: bool = False,
) -> str:
    if _has_command_substitution(argument_expression):
        raise RuntimeObservedCompileError(
            f"runtime graph compiler does not yet support source arguments combining process and command substitution: {candidate.text!r}",
            code="runtime.compile.source_arguments",
        )
    if assignment_words:
        return _render_assignment_prefixed_process_substitution_argument_replay_group(
            candidate,
            path_expression=path_expression,
            argument_expression=argument_expression,
            source_argc=source_argc,
            assignment_words=assignment_words,
            redirection_suffix=redirection_suffix,
            negated=negated,
        )
    validation_expression, validation_skip_mask = _source_validation_expression_for_process_substitution_arguments(
        path_expression,
        argument_expression,
    )
    path_sets_status = _has_command_substitution(path_expression)
    redirection_sets_status = _has_command_substitution(redirection_suffix)
    source_entry_status = (
        "__modash_source_redirection_status"
        if redirection_sets_status
        else "__modash_source_expansion_status"
        if path_sets_status
        else "__modash_source_command_prior_status"
    )
    redirected_validation = ""
    if redirection_sets_status:
        redirected_validation = (
            "__modash_source_redirection_status=$?; "
            f"__modash_source_entry_status=${source_entry_status}; "
            f"__modash_validate_source_argv_masked \"$__modash_source_entry_status\" {shlex.quote(validation_skip_mask)} \"${{__modash_source_argv[@]}}\"; "
        )
    validation_before_source = (
        ""
        if redirection_sets_status
        else f"__modash_validate_source_argv_masked \"$__modash_source_entry_status\" {shlex.quote(validation_skip_mask)} \"${{__modash_source_argv[@]}}\"; "
    )
    live_arguments = f" {argument_expression}" if argument_expression else ""
    file_source_operation = redirected_validation + (
        "__modash_live_source_path=$(__modash_materialize_embedded_file \"$__modash_replay_target\"); "
        "( exit \"$__modash_source_entry_status\" ); "
        f"builtin source \"$__modash_live_source_path\"{live_arguments}; "
        "__modash_replay_actual_status=$?; "
    )
    missing_source_operation = redirected_validation + (
        "builtin printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
    )
    missing_status_capture = "__modash_replay_missing_status=$?; "
    if redirection_suffix:
        file_source_operation = (
            "unset __modash_replay_actual_status; "
            f"{{ {file_source_operation}}} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_actual_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_source_operation = (
            "unset __modash_replay_missing_status; "
            f"{{ {missing_source_operation}__modash_replay_missing_status=$?; }} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_missing_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_status_capture = ""
    group = (
        f"{{ __modash_source_command_prior_status=$?; __modash_source_argv=(); __modash_source_argv=( {validation_expression} ); "
        "__modash_source_expansion_status=$?; "
        f"__modash_source_entry_status=${source_entry_status}; "
        f"__modash_replay_select_active=1; __modash_select_source_edge {shlex.quote(candidate.base_id)}; "
        "__modash_replay_select_active=0; "
        "__modash_replay_select_status=$?; "
        f"{validation_before_source}"
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "__modash_require_embedded_file \"$__modash_replay_target\"; "
        f"{file_source_operation}"
        "if (( __modash_replay_actual_status != __modash_replay_status )); then "
        "__modash_abort \"observed source status drift\"; "
        "fi; "
        "( exit \"$__modash_replay_actual_status\" ); "
        "else "
        f"{missing_source_operation}"
        f"{missing_status_capture}"
        "( exit \"$__modash_replay_missing_status\" ); "
        "fi; }"
    )
    return f"! {group}" if negated else group


def _render_assignment_prefixed_process_substitution_argument_replay_group(
    candidate: _SourceCandidate,
    *,
    path_expression: str,
    argument_expression: str,
    source_argc: int,
    assignment_words: tuple[str, ...],
    redirection_suffix: str = "",
    negated: bool = False,
) -> str:
    _source_assignment_names(assignment_words)
    if any(_has_command_substitution(word) for word in assignment_words):
        raise RuntimeObservedCompileError(
            f"runtime graph compiler does not yet support assignment command substitutions with process-substitution source arguments: {candidate.text!r}",
            code="runtime.compile.source_arguments",
        )
    validation_expression, validation_skip_mask = _source_validation_expression_for_process_substitution_arguments(
        path_expression,
        argument_expression,
    )
    path_sets_status = _has_command_substitution(path_expression)
    redirection_sets_status = _has_command_substitution(redirection_suffix)
    source_entry_status = (
        "__modash_source_redirection_status"
        if redirection_sets_status
        else "__modash_source_expansion_status"
        if path_sets_status
        else "__modash_source_command_prior_status"
    )
    redirected_validation = ""
    if redirection_sets_status:
        redirected_validation = (
            "__modash_source_redirection_status=$?; "
            f"__modash_source_entry_status=${source_entry_status}; "
            f"__modash_validate_source_argv_masked \"$__modash_source_entry_status\" {shlex.quote(validation_skip_mask)} \"${{__modash_source_argv[@]}}\"; "
        )
    validation_before_source = (
        ""
        if redirection_sets_status
        else f"__modash_validate_source_argv_masked \"$__modash_source_entry_status\" {shlex.quote(validation_skip_mask)} \"${{__modash_source_argv[@]}}\"; "
    )
    live_arguments = f" {argument_expression}" if argument_expression else ""
    assignment_prefix = " ".join(assignment_words)
    file_source_operation = redirected_validation + (
        "__modash_live_source_path=$(__modash_materialize_embedded_file \"$__modash_replay_target\"); "
        "( exit \"$__modash_source_entry_status\" ); "
        f"{assignment_prefix} command source \"$__modash_live_source_path\"{live_arguments}; "
        "__modash_replay_actual_status=$?; "
    )
    missing_source_operation = redirected_validation + (
        "builtin printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
    )
    missing_status_capture = "__modash_replay_missing_status=$?; "
    if redirection_suffix:
        file_source_operation = (
            "unset __modash_replay_actual_status; "
            f"{{ {file_source_operation}}} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_actual_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_source_operation = (
            "unset __modash_replay_missing_status; "
            f"{{ {missing_source_operation}__modash_replay_missing_status=$?; }} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_missing_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi; "
        )
        missing_status_capture = ""
    group = (
        f"{{ __modash_source_command_prior_status=$?; __modash_source_argv=(); __modash_source_argv=( {validation_expression} ); "
        "__modash_source_expansion_status=$?; "
        f"__modash_source_entry_status=${source_entry_status}; "
        f"__modash_replay_select_active=1; __modash_select_source_edge {shlex.quote(candidate.base_id)}; "
        "__modash_replay_select_active=0; "
        "__modash_replay_select_status=$?; "
        f"{validation_before_source}"
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "__modash_require_embedded_file \"$__modash_replay_target\"; "
        f"{file_source_operation}"
        "if (( __modash_replay_actual_status != __modash_replay_status )); then "
        "__modash_abort \"observed source status drift\"; "
        "fi; "
        "( exit \"$__modash_replay_actual_status\" ); "
        "else "
        f"{missing_source_operation}"
        f"{missing_status_capture}"
        "( exit \"$__modash_replay_missing_status\" ); "
        "fi; }"
    )
    return f"! {group}" if negated else group


def _render_assignment_prefixed_replay_group(
    candidate: _SourceCandidate,
    *,
    source_expression: str,
    assignment_words: tuple[str, ...],
    redirection_suffix: str = "",
    negated: bool = False,
) -> str:
    _source_assignment_names(assignment_words)
    shim = _render_assignment_prefixed_replay_shim(
        candidate.base_id,
        redirection_suffix,
        source_expression_sets_status=_has_command_substitution(source_expression),
        assignment_sets_status=any(_has_command_substitution(word) for word in assignment_words),
        redirection_sets_status=_has_command_substitution(redirection_suffix),
    )
    group = (
        f"{{ __modash_source_command_prior_status=$?; __modash_source_argv=(); __modash_source_argv=( {source_expression} ); "
        "__modash_source_expansion_status=$?; "
        f"{' '.join(assignment_words)} command source <({shim}); "
        "__modash_replay_actual_status=$?; "
        "( exit \"$__modash_replay_actual_status\" ); "
        "}"
    )
    return f"! {group}" if negated else group


def _render_assignment_prefixed_replay_shim(
    base_id: str,
    redirection_suffix: str,
    *,
    source_expression_sets_status: bool,
    assignment_sets_status: bool,
    redirection_sets_status: bool,
) -> str:
    status_initializer = [
        "__modash_assignment_source_entry_status=$?",
        "__modash_source_entry_status=$__modash_assignment_source_entry_status",
    ]
    if source_expression_sets_status and not assignment_sets_status and not redirection_sets_status:
        status_initializer.append("__modash_source_entry_status=$__modash_source_expansion_status")
    redirected_validation = ""
    validation_before_source = (
        ""
        if redirection_sets_status
        else "__modash_validate_source_argv \"$__modash_source_entry_status\" \"${__modash_source_argv[@]}\""
    )
    if redirection_sets_status:
        redirected_validation = (
            "__modash_source_redirection_status=$?; "
            "__modash_source_entry_status=$__modash_source_redirection_status; "
            "__modash_validate_source_argv \"$__modash_source_entry_status\" \"${__modash_source_argv[@]}\"; "
        )
    file_source = (
        f"{redirected_validation}"
        "( exit \"$__modash_source_entry_status\" ); "
        "command source <(__modash_emit_embedded_file \"$__modash_replay_target\") \"${__modash_replay_args[@]}\"; "
        "__modash_replay_actual_status=$?"
    )
    missing_source = (
        f"{redirected_validation}"
        "builtin printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
        "__modash_replay_missing_status=$?"
    )
    if redirection_suffix:
        file_source = (
            "unset __modash_replay_actual_status; "
            f"{{ {file_source}; }} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_actual_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi"
        )
        missing_source = (
            "unset __modash_replay_missing_status; "
            f"{{ {missing_source}; }} {redirection_suffix}; "
            "if [[ ! ${__modash_replay_missing_status+x} ]]; then "
            "__modash_abort \"observed source redirection drift\"; "
            "fi"
        )
    lines = [
        *status_initializer,
        f"__modash_replay_select_active=1; __modash_select_source_edge {shlex.quote(base_id)}; __modash_replay_select_active=0",
        "__modash_replay_select_status=$?",
        validation_before_source,
        "if (( __modash_replay_select_status != 0 )); then",
        "  return \"$__modash_replay_select_status\"",
        "elif [[ $__modash_replay_kind == file ]]; then",
        "  __modash_require_embedded_file \"$__modash_replay_target\"",
        f"  {file_source}",
        "  if (( __modash_replay_actual_status != __modash_replay_status )); then",
        "    __modash_abort \"observed source status drift\"",
        "  fi",
        "  return \"$__modash_replay_actual_status\"",
        "else",
        f"  {missing_source}",
        "  return \"$__modash_replay_missing_status\"",
        "fi",
    ]
    lines = [line for line in lines if line]
    return "builtin printf '%s\\n' " + " ".join(shlex.quote(line) for line in lines)


def _source_expression_for_candidate(candidate: _SourceCandidate) -> str:
    source_expression, _redirections = _source_expression_and_redirections_for_candidate(candidate)
    return source_expression


def _source_expression_and_redirections_for_candidate(candidate: _SourceCandidate) -> tuple[str, str]:
    argv_words, redirection_words = _source_argv_words_and_redirections_for_candidate(candidate)
    return " ".join(argv_words), " ".join(redirection_words)


def _source_path_and_argument_expression_for_candidate(candidate: _SourceCandidate) -> tuple[str, str, int]:
    argv_words, _redirection_words = _source_argv_words_and_redirections_for_candidate(candidate)
    path_expression = argv_words[0]
    argument_words = argv_words[1:]
    argument_expression = " ".join(argument_words)
    return path_expression, argument_expression, _source_argument_count(argument_expression)


def _source_validation_expression_for_process_substitution_arguments(
    path_expression: str,
    argument_expression: str,
) -> tuple[str, str]:
    masked_arguments = _mask_process_substitutions(argument_expression).strip()
    validation_expression = " ".join(part for part in (path_expression, masked_arguments) if part)
    return validation_expression, _process_substitution_argument_skip_mask(masked_arguments)


def _process_substitution_argument_skip_mask(masked_arguments: str) -> str:
    if not masked_arguments:
        return ":"
    try:
        words = tuple(parse_shell_words_preserving_quotes(masked_arguments))
    except UnsupportedSourceError:
        words = tuple(masked_arguments.split())
    skipped = [
        str(index)
        for index, word in enumerate(words)
        if _PROCESS_SUBSTITUTION_ARG_PLACEHOLDER in word
    ]
    return ":" + ":".join(skipped) + ":" if skipped else ":"


def _source_argv_words_and_redirections_for_candidate(candidate: _SourceCandidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    probe = candidate.text.strip()
    if candidate.separator and probe.startswith(candidate.separator):
        probe = probe[len(candidate.separator):].strip()
    invocation = source_command_invocation(probe, stop_at_shell_control=True)
    if invocation is None:
        raise RuntimeObservedCompileError(
            f"could not render source argv validation for observed source site: {candidate.text!r}",
            code="runtime.compile.mapping_failed",
        )
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(probe))
    except UnsupportedSourceError as exc:
        raise RuntimeObservedCompileError(
            f"could not parse source argv validation for observed source site: {candidate.text!r}",
            code="runtime.compile.mapping_failed",
        ) from exc
    source_word_count = 1 + len(invocation.arguments)
    source_words = raw_words[invocation.source_index + 1:invocation.source_index + 1 + source_word_count]
    argv_words, redirection_words = _split_source_redirections(source_words)
    if not argv_words:
        raise RuntimeObservedCompileError(
            f"could not render source argv validation for observed source site: {candidate.text!r}",
            code="runtime.compile.mapping_failed",
        )
    return argv_words, redirection_words


def _source_assignment_words_for_candidate(candidate: _SourceCandidate) -> tuple[str, ...]:
    probe = candidate.text.strip()
    if candidate.separator and probe.startswith(candidate.separator):
        probe = probe[len(candidate.separator):].strip()
    invocation = source_command_invocation(probe, stop_at_shell_control=True)
    if invocation is None or invocation.source_index <= 0:
        return ()
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(probe))
    except UnsupportedSourceError:
        return ()
    assignment_words: list[str] = []
    for raw_word, word in zip(raw_words[:invocation.source_index], invocation.words[:invocation.source_index]):
        if ASSIGNMENT_WORD_PATTERN.match(word):
            assignment_words.append(raw_word)
    return tuple(assignment_words)


def _source_assignment_names(assignment_words: tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for word in assignment_words:
        clean = strip_shell_word_quotes(word)
        if "=" not in clean:
            continue
        name = clean.split("=", 1)[0]
        if name.endswith("+"):
            name = name[:-1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler cannot replay this source assignment prefix: {word!r}",
                code="runtime.compile.unsupported_source_prefix",
            )
        if name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


def _source_expression_sets_status(candidate: _SourceCandidate) -> bool:
    expression = _source_expression_for_candidate(candidate)
    return _has_command_substitution(expression)


def _split_source_redirections(words: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    argv_words: list[str] = []
    redirection_words: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if _is_process_substitution_word(word):
            argv_words.append(word)
            index += 1
            continue
        redirection = _source_redirection_token_kind(word)
        if redirection is None:
            argv_words.append(word)
            index += 1
            continue
        if redirection == "unsupported":
            raise RuntimeObservedCompileError(
                "runtime graph compiler does not yet support source heredoc redirections",
                code="runtime.compile.source_redirection",
            )
        redirection_words.append(word)
        index += 1
        if redirection == "separate-target":
            if index >= len(words):
                raise RuntimeObservedCompileError(
                    "runtime graph compiler could not parse source redirection target",
                    code="runtime.compile.source_redirection",
                )
            target, index = _source_redirection_target_words(words, index)
            redirection_words.append(target)
    return tuple(argv_words), tuple(redirection_words)


def _source_redirection_target_words(words: tuple[str, ...], index: int) -> tuple[str, int]:
    word = words[index]
    if not _is_process_substitution_word(word):
        return word, index + 1
    collected = [word]
    probe = word
    index += 1
    while _process_substitution_needs_more_words(probe) and index < len(words):
        collected.append(words[index])
        probe = " ".join(collected)
        index += 1
    return " ".join(collected), index


def _process_substitution_needs_more_words(text: str) -> bool:
    if not _is_process_substitution_word(text):
        return False
    _body, end_index = read_balanced_body(text, 2)
    return end_index is None or end_index != len(text) - 1


def _is_process_substitution_word(word: str) -> bool:
    return word.startswith("<(") or word.startswith(">(")


def _source_redirection_token_kind(word: str) -> str | None:
    if re.match(r"^(?:[0-9]+)?<<", word):
        return "unsupported"
    if re.fullmatch(r"(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|)", word):
        return "separate-target"
    if re.match(r"^(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|).+", word):
        return "combined-target"
    return None


def _has_command_substitution(text: str) -> bool:
    in_single_quote = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'":
            in_single_quote = not in_single_quote
            index += 1
            continue
        if in_single_quote:
            index += 1
            continue
        if char == "`" or text.startswith("$(", index):
            return True
        index += 1
    return False


def _has_process_substitution(text: str) -> bool:
    return "<(" in text or ">(" in text


def _source_argument_count(argument_expression: str) -> int:
    if not argument_expression.strip():
        return 0
    masked = _mask_process_substitutions(argument_expression)
    try:
        return len(parse_shell_words_preserving_quotes(masked))
    except UnsupportedSourceError:
        return len(argument_expression.split())


def _mask_process_substitutions(text: str) -> str:
    rendered: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            rendered.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            rendered.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            rendered.append(char)
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            rendered.append(char)
            index += 1
            continue
        if not in_single_quote and (text.startswith("<(", index) or text.startswith(">(", index)):
            _body, end_index = read_balanced_body(text, index + 2)
            if end_index is not None:
                rendered.append(_PROCESS_SUBSTITUTION_ARG_PLACEHOLDER)
                index = end_index + 1
                continue
        rendered.append(char)
        index += 1
    return "".join(rendered)


def _apply_replacements(line: str, replacements: list[tuple[int, int, str]]) -> str:
    rendered = line
    occupied: list[tuple[int, int]] = []
    for start, end, replacement in sorted(replacements, reverse=True):
        if any(start < right and end > left for left, right in occupied):
            raise RuntimeObservedCompileError(
                f"overlapping source site rewrites in line: {line.strip()}",
                code="runtime.compile.mapping_failed",
            )
        rendered = rendered[:start] + replacement + rendered[end:]
        occupied.append((start, end))
    return rendered


def _rewrite_exec_wrapper_commands(line: str) -> str:
    rewritten = line
    search_start = 0
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            continue
        replacement = _exec_wrapper_command_replacement(command)
        if replacement is None:
            search_start = start + len(command)
            continue
        end = start + len(command)
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        search_start = start + len(replacement)
    return rewritten


@dataclass(frozen=True)
class _BashScriptInvocation:
    script_word: str
    script_index: int


def _bash_script_invocation(command: str) -> _BashScriptInvocation | None:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    index = _command_word_index_after_wrappers(raw_words)
    if index is None:
        return None
    command_name = strip_shell_word_quotes(raw_words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"}:
        return None
    if _bash_c_payload_index(raw_words, index + 1) is not None:
        return None
    script_index = _bash_script_word_index(raw_words, index + 1)
    if script_index is None:
        return None
    script_word = strip_shell_word_quotes(raw_words[script_index])
    if _has_dynamic_shell_expansion(script_word):
        return None
    return _BashScriptInvocation(script_word, script_index)


def _bash_script_word_index(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = strip_shell_word_quotes(words[index])
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("+"):
            index += 1
            continue
        return index
    return None


def _rewrite_bash_script_command(
    command: str,
    invocation: _BashScriptInvocation,
    payload: str,
    observed_entrypoint: str,
) -> str:
    words = parse_shell_words_preserving_quotes(command.strip())
    index = _command_word_index_after_wrappers(words)
    if index is None:
        raise RuntimeObservedCompileError(f"unsupported bash script command: {command}")
    bash_word = _trusted_bash_command_word(words[index])
    prefix_words, guard_names = _trusted_child_bash_prefix(words[:index])
    bash_options = list(words[index + 1:invocation.script_index])
    if not any(strip_shell_word_quotes(option) == "-p" or "p" in strip_shell_word_quotes(option)[1:] for option in bash_options if strip_shell_word_quotes(option).startswith("-") and not strip_shell_word_quotes(option).startswith("--")):
        bash_options.insert(0, "-p")
    rewritten_words = [
        *prefix_words,
        bash_word,
        *bash_options,
        "-c",
        shlex.quote(payload),
        words[invocation.script_index],
        *words[invocation.script_index + 1:],
    ]
    guard = " ".join(f"__modash_reject_function_override {shlex.quote(name)};" for name in guard_names)
    validation = _child_script_validation_command(words[invocation.script_index], observed_entrypoint)
    return f"{guard} {validation} {' '.join(rewritten_words)}".strip()


def _child_script_validation_command(script_word: str, observed_entrypoint: str) -> str:
    return (
        "__modash_validate_child_script_path "
        f"{shlex.quote(str(Path(observed_entrypoint).resolve(strict=False)))} "
        f"{script_word};"
    )


def _child_script_matches_observed(script_word: str, process_payload: _ProcessPayload) -> bool:
    if process_payload.entrypoint is None or process_payload.cwd is None:
        return False
    if _has_dynamic_shell_expansion(script_word):
        return False
    script_path = Path(script_word)
    if not script_path.is_absolute():
        script_path = Path(process_payload.cwd) / script_path
    return str(script_path.resolve(strict=False)) == str(Path(process_payload.entrypoint).resolve(strict=False))


def _exec_wrapper_command_replacement(command: str) -> str | None:
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(command.strip()))
    except UnsupportedSourceError:
        return None
    if not raw_words:
        return None
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    command_index = _command_or_builtin_exec_index(words)
    if command_index is None:
        return None
    target_index = _command_or_builtin_target_index(words, command_index)
    if target_index is None or target_index >= len(words) or words[target_index] != "exec":
        return None
    return " ".join((*raw_words[:command_index], "exec", *raw_words[target_index + 1:]))


def _command_or_builtin_exec_index(words: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "time", "!"}:
        word = words[index]
        index += 1
        if word == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
    return index if index < len(words) and words[index] in {"command", "builtin"} else None


def _command_or_builtin_target_index(words: tuple[str, ...], index: int) -> int | None:
    if words[index] == "builtin":
        index += 1
        if index < len(words) and words[index] == "--":
            index += 1
        return index if index < len(words) else None
    index += 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word == "-p":
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _rewrite_bash_c_payloads(
    line: str,
    process_payloads: dict[int, _ProcessPayload],
    process_replacement_counts: dict[int, int] | None = None,
    rewrite_runtime_references: bool = True,
) -> str:
    rewritten = line
    search_start = 0
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            continue
        end = start + len(command)
        invocation = _bash_c_invocation(command)
        if invocation is None:
            script_replacement = None
            script_replacement_process = None
            script_invocation = _bash_script_invocation(command)
            if script_invocation is not None:
                for process_index, process_payload in process_payloads.items():
                    if not _child_script_matches_observed(script_invocation.script_word, process_payload):
                        continue
                    if script_replacement is not None:
                        raise RuntimeObservedCompileError(
                            f"ambiguous observed child bash script mapping for: {script_invocation.script_word}",
                            code="runtime.compile.child_process_mapping_failed",
                        )
                    script_replacement = _rewrite_bash_script_command(
                        command,
                        script_invocation,
                        process_payload.content,
                        process_payload.entrypoint or process_payload.identity,
                    )
                    script_replacement_process = process_index
            if script_replacement is not None:
                script_replacement = _wrap_child_process_command(script_replacement_process, script_replacement)
                rewritten = rewritten[:start] + script_replacement + rewritten[end:]
                if process_replacement_counts is not None and script_replacement_process is not None:
                    process_replacement_counts[script_replacement_process] = process_replacement_counts.get(script_replacement_process, 0) + 1
                search_start = start + len(script_replacement)
                continue
            unsupported_invocation = _bash_c_invocation_anywhere(command)
            if unsupported_invocation is not None:
                if unsupported_invocation.dynamic_payload:
                    raise RuntimeObservedCompileError(
                        f"runtime graph compiler does not support dynamic child bash -c payloads in {command.strip()}",
                        code="runtime.compile.unsupported_child_bash",
                    )
                if _payload_is_runtime_unsafe(unsupported_invocation.payload):
                    raise RuntimeObservedCompileError(
                        f"unobserved child bash -c source payload in {command.strip()}",
                        code="runtime.compile.unobserved_child_source",
                    )
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler does not support this child bash wrapper: {command.strip()}",
                    code="runtime.compile.unsupported_child_bash",
                )
            if _bash_invocation_without_c(command):
                raise RuntimeObservedCompileError(
                    f"runtime graph compiler does not support child bash script invocations: {command.strip()}",
                    code="runtime.compile.unsupported_child_bash",
                )
            search_start = end
            continue
        if invocation.dynamic_payload:
            raise RuntimeObservedCompileError(
                f"runtime graph compiler does not support dynamic child bash -c payloads in {command.strip()}",
                code="runtime.compile.unsupported_child_bash",
            )
        payload = invocation.payload
        if _payload_is_instrumentation_sensitive(payload):
            raise RuntimeObservedCompileError(
                f"runtime graph compiler input observes trace-sensitive child bash state in {command.strip()}",
                code="runtime.compile.instrumentation_sensitive",
            )
        replacement = None
        replacement_process = None
        for process_index, process_payload in process_payloads.items():
            if payload == process_payload.identity:
                if replacement is not None:
                    raise RuntimeObservedCompileError(
                        f"ambiguous observed child bash -c payload mapping for: {payload}",
                        code="runtime.compile.child_process_mapping_failed",
                    )
                replacement = _rewrite_bash_c_command(command, process_payload.content)
                replacement_process = process_index
        if replacement is None:
            if _payload_is_runtime_unsafe(payload):
                raise RuntimeObservedCompileError(
                    f"unobserved child bash -c source payload in {command.strip()}",
                    code="runtime.compile.unobserved_child_source",
                )
            replacement = _rewrite_bash_c_command(command, _source_free_child_payload(payload), disable_privileged=True)
            rewritten = rewritten[:start] + replacement + rewritten[end:]
            search_start = start + len(replacement)
            continue
        if replacement_process is not None:
            replacement = _wrap_child_process_command(replacement_process, replacement)
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        if process_replacement_counts is not None and replacement_process is not None:
            process_replacement_counts[replacement_process] = process_replacement_counts.get(replacement_process, 0) + 1
        search_start = start + len(replacement)
    return rewritten


def _wrap_child_process_command(process_index: int, command: str) -> str:
    token = secrets.token_hex(16)
    command = _inject_child_replay_token(command)
    return (
        "{ "
        f"__modash_replay_select_active=1; __modash_select_child_process {process_index}; __modash_replay_select_active=0; "
        f"__modash_child_token={shlex.quote(token)}; "
        f"__modash_child_marker=\"$__modash_tmp/child-process-{process_index}.ok\"; "
        f"__modash_child_token_file=\"$__modash_tmp/child-process-{process_index}.token\"; "
        '"$__modash_rm" -f -- "$__modash_child_marker"; '
        'builtin printf "%s\\n" "$__modash_child_token" > "$__modash_child_token_file" || '
        "__modash_abort \"could not prepare child replay token\"; "
        'export __modash_child_replay_marker="$__modash_child_marker"; '
        'export __modash_child_replay_token_file="$__modash_child_token_file"; '
        f"{command}; "
        "__modash_child_status=$?; "
        "unset __modash_child_replay_marker __modash_child_replay_token_file; "
        '"$__modash_rm" -f -- "$__modash_child_token_file"; '
        'if [[ ! -f "$__modash_child_marker" ]]; then '
        "__modash_abort \"observed child process replay failed\"; "
        "fi; "
        "IFS= builtin read -r __modash_child_seen_token < \"$__modash_child_marker\" || "
        "__modash_abort \"observed child process replay failed\"; "
        'if [[ $__modash_child_seen_token != "$__modash_child_token" ]]; then '
        "__modash_abort \"observed child process replay failed\"; "
        "fi; "
        "unset __modash_child_seen_token __modash_child_token; "
        "( exit \"$__modash_child_status\" ); "
        "}"
    )


def _inject_child_replay_token(command: str) -> str:
    invocation = _bash_c_invocation_anywhere(command)
    if invocation is None:
        raise RuntimeObservedCompileError(
            "runtime graph compiler could not inject child replay token",
            code="runtime.compile.child_process_mapping_failed",
        )
    payload = (
        "IFS= read -r __modash_child_replay_token < \"$__modash_child_replay_token_file\" || exit 125\n"
        '"$__modash_rm" -f -- "$__modash_child_replay_token_file" 2>/dev/null || :\n'
        "unset __modash_child_replay_token_file\n"
        f"{invocation.payload}"
    )
    replacement = shlex.quote(payload)
    start = command.find(invocation.raw_payload_word)
    if start < 0:
        raise RuntimeObservedCompileError(
            "runtime graph compiler could not locate child payload for replay token injection",
            code="runtime.compile.child_process_mapping_failed",
        )
    end = start + len(invocation.raw_payload_word)
    return command[:start] + replacement + command[end:]

def _rewrite_bash_c_command(command: str, payload: str, *, disable_privileged: bool = False) -> str:
    words = parse_shell_words_preserving_quotes(command.strip())
    index = _command_word_index_after_wrappers(words)
    if index is None or index + 2 >= len(words):
        raise RuntimeObservedCompileError(f"unsupported bash -c command: {command}")
    payload_index = _bash_c_payload_index(words, index + 1)
    if payload_index is None:
        raise RuntimeObservedCompileError(f"unsupported bash -c command: {command}")
    bash_word = _trusted_bash_command_word(words[index])
    prefix_words, guard_names = _trusted_child_bash_prefix(words[:index])
    bash_options = list(words[index + 1:payload_index])
    if not any(strip_shell_word_quotes(option) == "-p" or "p" in strip_shell_word_quotes(option)[1:] for option in bash_options if strip_shell_word_quotes(option).startswith("-") and not strip_shell_word_quotes(option).startswith("--")):
        bash_options.insert(0, "-p")
    if disable_privileged and not any(strip_shell_word_quotes(option) == "+p" for option in bash_options):
        bash_options.append("+p")
    rewritten_words = [
        *prefix_words,
        bash_word,
        *bash_options,
        shlex.quote(payload),
        *words[payload_index + 1:],
    ]
    if payload_index + 1 == len(words):
        rewritten_words.append(shlex.quote(strip_shell_word_quotes(words[index])))
    guard = " ".join(f"__modash_reject_function_override {shlex.quote(name)};" for name in guard_names)
    command_text = " ".join(rewritten_words)
    return f"{guard} {command_text}".strip()


def _source_free_child_payload(payload: str) -> str:
    return payload


def _trusted_child_bash_prefix(raw_words: list[str]) -> tuple[list[str], tuple[str, ...]]:
    trusted: list[str] = []
    guard_names: list[str] = []
    index = 0
    while index < len(raw_words):
        word = strip_shell_word_quotes(raw_words[index])
        if ASSIGNMENT_WORD_PATTERN.match(word):
            trusted.append(raw_words[index])
            index += 1
            continue
        if word == "command":
            guard_names.append("command")
            index += 1
            while index < len(raw_words):
                option = strip_shell_word_quotes(raw_words[index])
                if option == "--":
                    index += 1
                    break
                if option == "-p":
                    index += 1
                    continue
                if option.startswith("-"):
                    raise RuntimeObservedCompileError(
                        "runtime graph compiler does not support this command wrapper for child bash replay",
                        code="runtime.compile.unsupported_child_bash",
                    )
                break
            continue
        if word == "env":
            guard_names.append("env")
            trusted.append(shlex.quote(_required_absolute_tool("env", code="runtime.compile.env_unavailable")))
            index += 1
            continue
        trusted.append(raw_words[index])
        index += 1
    return trusted, tuple(guard_names)


def _trusted_bash_command_word(raw_word: str) -> str:
    word = strip_shell_word_quotes(raw_word)
    if word in {"/bin/bash", "/usr/bin/bash"}:
        try:
            return shlex.quote(str(Path(word).resolve(strict=True)))
        except OSError as exc:
            raise RuntimeObservedCompileError(
                "runtime graph compiler cannot locate an absolute bash executable for child process replay",
                code="runtime.compile.bash_unavailable",
            ) from exc
    return shlex.quote(_required_absolute_tool("bash", code="runtime.compile.bash_unavailable"))


def _required_absolute_tool(name: str, *, code: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeObservedCompileError(
            f"runtime graph compiler cannot locate an absolute {name} executable for child process replay",
            code=code,
        )
    try:
        path = Path(resolved).resolve(strict=True)
    except OSError as exc:
        raise RuntimeObservedCompileError(
            f"runtime graph compiler cannot resolve an absolute {name} executable for child process replay: {resolved}",
            code=code,
        ) from exc
    if not path.is_absolute():
        raise RuntimeObservedCompileError(
            f"runtime graph compiler resolved a non-absolute {name} executable for child process replay: {path}",
            code=code,
        )
    return str(path)

def _line_has_bash_c_payload(line: str) -> bool:
    return any(_bash_c_invocation(command) is not None for command in get_commands(line))


def _source_site_replay_prefix(source_site: str, separator: str) -> str:
    probe = source_site.strip()
    if separator and probe.startswith(separator):
        probe = probe[len(separator):].strip()
    invocation = source_command_invocation(probe, stop_at_shell_control=True)
    if invocation is None or invocation.source_index <= 0:
        return ""
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(probe))
    except UnsupportedSourceError:
        return ""
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    prefix_words: list[str] = []
    index = 0
    while index < invocation.source_index:
        word = words[index]
        if word == "{":
            index += 1
            continue
        if word == "!":
            prefix_words.append(raw_words[index])
            index += 1
            continue
        if ASSIGNMENT_WORD_PATTERN.match(word):
            index += 1
            continue
        if word == "time":
            prefix_words.append(raw_words[index])
            index += 1
            while index < invocation.source_index and words[index].startswith("-"):
                prefix_words.append(raw_words[index])
                index += 1
            continue
        if word == "builtin":
            index += 1
            if index < invocation.source_index and words[index] == "--":
                index += 1
            continue
        if word == "command":
            index += 1
            while index < invocation.source_index:
                option = words[index]
                if option == "--":
                    index += 1
                    break
                if option == "-p":
                    index += 1
                    continue
                if option.startswith("-"):
                    break
                break
            continue
        index += 1
    return " ".join(prefix_words)


def _source_site_has_unsupported_prefix(source_site: str, separator: str = "") -> bool:
    probe = source_site.strip()
    if separator and probe.startswith(separator):
        probe = probe[len(separator):].strip()
    invocation = source_command_invocation(probe)
    if invocation is None:
        return False
    return any(word == "coproc" for word in invocation.words[:invocation.source_index])


def _line_has_runtime_reference(line: str, *, include_zero: bool = True) -> bool:
    if _expandable_runtime_reference(line, r"\bBASH_SOURCE\b"):
        return True
    if _line_has_lineno_reference(line):
        return True
    return include_zero and _expandable_runtime_reference(line, r"\$\{?0(?:\}|(?![0-9]))")


def _line_has_runtime_reference_outside_bash_c_payload(line: str) -> bool:
    masked = line
    search_start = 0
    for command in get_commands(line):
        command_start = find_unquoted_substring(masked, command, search_start)
        if command_start < 0:
            continue
        invocation = _bash_c_invocation(command)
        if invocation is None:
            search_start = command_start + len(command)
            continue
        payload_start = masked.find(invocation.raw_payload_word, command_start)
        if payload_start >= 0:
            payload_end = payload_start + len(invocation.raw_payload_word)
            masked = masked[:payload_start] + (" " * len(invocation.raw_payload_word)) + masked[payload_end:]
        search_start = command_start + len(command)
    return _line_has_runtime_reference(masked)


def _replace_lineno_references(line: str, line_index: int) -> str:
    replacement = str(line_index)
    line = _replace_lineno_in_double_bracket_arithmetic(line, replacement)
    line = _replace_lineno_in_let_commands(line, replacement)
    line = _replace_lineno_in_legacy_arithmetic_expansions(line, replacement)
    line = _replace_lineno_in_array_subscripts(line, replacement)
    rendered: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            rendered.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            rendered.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            rendered.append(char)
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            rendered.append(char)
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            rendered.append(line[index:])
            break
        if not in_single_quote and line.startswith("$((", index):
            body, end_index = read_balanced_body(line, index + 3)
            if body is not None:
                end_after = _arithmetic_context_end_after(line, end_index)
                if re.search(r"\bLINENO\b", body):
                    rendered.append("$((")
                    rendered.append(_replace_bare_lineno_identifier(body, replacement))
                    rendered.append("))")
                else:
                    rendered.append(line[index:end_after])
                index = end_after
                continue
        if not in_single_quote and line.startswith("((", index):
            body, end_index = read_balanced_body(line, index + 2)
            if body is not None:
                end_after = _arithmetic_context_end_after(line, end_index)
                if re.search(r"\bLINENO\b", body):
                    rendered.append("((")
                    rendered.append(_replace_bare_lineno_identifier(body, replacement))
                    rendered.append("))")
                else:
                    rendered.append(line[index:end_after])
                index = end_after
                continue
        if not in_single_quote and line.startswith("${LINENO}", index):
            rendered.append(replacement)
            index += len("${LINENO}")
            continue
        if not in_single_quote and line.startswith("$LINENO", index):
            rendered.append(replacement)
            index += len("$LINENO")
            continue
        rendered.append(char)
        index += 1
    return "".join(rendered)


def _replace_lineno_in_double_bracket_arithmetic(line: str, replacement: str) -> str:
    rendered: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            rendered.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            rendered.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            rendered.append(char)
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            rendered.append(char)
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            rendered.append(line[index:])
            break
        if not in_single_quote and line.startswith("[[", index):
            body, end_after = _double_bracket_body(line, index + 2)
            if body is not None:
                rendered.append("[[")
                if _double_bracket_body_has_arithmetic_comparison(body):
                    rendered.append(_replace_bare_lineno_identifier(body, replacement))
                else:
                    rendered.append(body)
                rendered.append("]]")
                index = end_after
                continue
        rendered.append(char)
        index += 1
    return "".join(rendered)


def _double_bracket_body(line: str, index: int) -> tuple[str | None, int]:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    start = index
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if not in_single_quote and not in_double_quote and line.startswith("]]", index):
            return line[start:index], index + 2
        index += 1
    return None, start


def _double_bracket_body_has_arithmetic_comparison(body: str) -> bool:
    return re.search(r"(^|[^\w-])-(?:eq|ne|lt|le|gt|ge)(?:$|[^\w-])", body) is not None


def _replace_lineno_in_let_commands(line: str, replacement: str) -> str:
    rewritten = line
    search_start = 0
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            start = rewritten.find(command, search_start)
        if start < 0:
            continue
        replacement_command = _replace_lineno_in_let_command(command, replacement)
        rewritten = rewritten[:start] + replacement_command + rewritten[start + len(command):]
        search_start = start + len(replacement_command)
    return rewritten


def _replace_lineno_in_let_command(command: str, replacement: str) -> str:
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(command.strip()))
    except UnsupportedSourceError:
        return command
    if not raw_words:
        return command
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    index = _let_command_index(words)
    if index is None:
        return command
    spans = _raw_word_spans(command, raw_words)
    if spans is None:
        return command
    rendered = command
    for word_index in range(len(raw_words) - 1, index, -1):
        start, end = spans[word_index]
        rendered = rendered[:start] + _replace_bare_lineno_identifier(rendered[start:end], replacement) + rendered[end:]
    return rendered


def _let_command_index(words: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "time", "!"}:
        index += 1
        if index < len(words) and words[index - 1] == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
    if index < len(words) and words[index] in {"command", "builtin"}:
        index = _command_or_builtin_payload_target_index(words, index)
        if index is None:
            return None
    return index if index < len(words) and words[index] == "let" else None


def _replace_lineno_in_legacy_arithmetic_expansions(line: str, replacement: str) -> str:
    rendered: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            rendered.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            rendered.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            rendered.append(char)
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            rendered.append(char)
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            rendered.append(line[index:])
            break
        if not in_single_quote and line.startswith("$[", index):
            body, end_index = _bracket_body(line, index + 2)
            if body is not None:
                rendered.append("$[")
                rendered.append(_replace_bare_lineno_identifier(body, replacement))
                rendered.append("]")
                index = end_index + 1
                continue
        rendered.append(char)
        index += 1
    return "".join(rendered)


def _replace_lineno_in_array_subscripts(line: str, replacement: str) -> str:
    rendered: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            rendered.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            rendered.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            rendered.append(char)
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            rendered.append(char)
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            rendered.append(line[index:])
            break
        if not in_single_quote and char == "[" and _is_lineno_array_subscript_start(line, index):
            body, end_index = _bracket_body(line, index + 1)
            if body is not None:
                rendered.append("[")
                rendered.append(_replace_bare_lineno_identifier(body, replacement))
                rendered.append("]")
                index = end_index + 1
                continue
        rendered.append(char)
        index += 1
    return "".join(rendered)


def _is_lineno_array_subscript_start(line: str, index: int) -> bool:
    if line.startswith("[[", index) or (index > 0 and line[index - 1] == "["):
        return False
    body, end_index = _bracket_body(line, index + 1)
    if body is None or not re.search(r"\bLINENO\b", body):
        return False
    previous = line[index - 1] if index > 0 else ""
    if previous.isalnum() or previous == "_":
        return True
    if previous == "(":
        next_index = end_index + 1
        while next_index < len(line) and line[next_index].isspace():
            next_index += 1
        return next_index < len(line) and line[next_index] == "="
    return False


def _bracket_body(line: str, index: int) -> tuple[str | None, int]:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    depth = 1
    start = index
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if not in_single_quote and not in_double_quote:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return line[start:index], index
        index += 1
    return None, start


def _replace_bare_lineno_identifier(text: str, replacement: str) -> str:
    return re.sub(r"\bLINENO\b", replacement, text)


def _arithmetic_context_end_after(line: str, end_index: int) -> int:
    if end_index + 1 < len(line) and line[end_index + 1] == ")":
        return end_index + 2
    return end_index + 1


def _line_has_lineno_reference(line: str) -> bool:
    return (
        _expandable_runtime_reference(line, r"\$(?:LINENO|\{LINENO[^}]*\})")
        or _arithmetic_context_has_bare_lineno(line)
        or _double_bracket_arithmetic_has_bare_lineno(line)
        or _let_command_has_bare_lineno(line)
        or _legacy_arithmetic_has_bare_lineno(line)
        or _array_subscript_has_bare_lineno(line)
    )


def _arithmetic_context_has_bare_lineno(line: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if in_single_quote:
            index += 1
            continue
        if line.startswith("$((", index):
            body, end_index = read_balanced_body(line, index + 3)
            if body is None:
                index += 3
                continue
            if re.search(r"\bLINENO\b", body):
                return True
            index = end_index + 1
            continue
        if line.startswith("((", index):
            body, end_index = read_balanced_body(line, index + 2)
            if body is None:
                index += 2
                continue
            if re.search(r"\bLINENO\b", body):
                return True
            index = end_index + 1
            continue
        index += 1
    return False


def _legacy_arithmetic_has_bare_lineno(line: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            return False
        if not in_single_quote and line.startswith("$[", index):
            body, end_index = _bracket_body(line, index + 2)
            if body is None:
                index += 2
                continue
            if re.search(r"\bLINENO\b", body):
                return True
            index = end_index + 1
            continue
        index += 1
    return False


def _array_subscript_has_bare_lineno(line: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            return False
        if not in_single_quote and char == "[" and _is_lineno_array_subscript_start(line, index):
            return True
        index += 1
    return False


def _double_bracket_arithmetic_has_bare_lineno(line: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            return False
        if not in_single_quote and line.startswith("[[", index):
            body, end_after = _double_bracket_body(line, index + 2)
            if body is None:
                return False
            if _double_bracket_body_has_arithmetic_comparison(body) and re.search(r"\bLINENO\b", body):
                return True
            index = end_after
            continue
        index += 1
    return False


def _let_command_has_bare_lineno(line: str) -> bool:
    for command in get_commands(line):
        try:
            raw_words = tuple(parse_shell_words_preserving_quotes(command.strip()))
        except UnsupportedSourceError:
            continue
        words = tuple(strip_shell_word_quotes(word) for word in raw_words)
        index = _let_command_index(words)
        if index is None:
            continue
        if any(re.search(r"\bLINENO\b", word) for word in raw_words[index + 1:]):
            return True
    return False


def _ensure_unique_process_payloads(process_payloads: dict[int, _ProcessPayload]) -> None:
    payload_owners: dict[str, int] = {}
    for process_index, process_payload in process_payloads.items():
        owner = payload_owners.get(process_payload.identity)
        if owner is not None:
            raise RuntimeObservedCompileError(
                f"repeated identical bash -c payload is not yet replayable without a parent process occurrence key: processes {owner} and {process_index}",
                code="runtime.compile.ambiguous_child_process",
            )
        payload_owners[process_payload.identity] = process_index

def _ensure_no_unrewritten_source(line: str, unit: _RewriteUnit, line_index: int) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    for command in get_commands(line):
        if (
            "__modash_select_source_edge" in command
            or "__modash_emit_embedded_file" in command
            or "__modash_live_source_path" in command
        ):
            continue
        if _is_generated_dynamic_command_guard(command):
            continue
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            words = []
        name = strip_shell_word_quotes(words[0]) if words else ""
        if (
            contains_source_command(command)
            or contains_nested_source_command(command)
            or _process_substitution_contains_source(command)
            or _source_bearing_shell_c_payload(command)
            or (name == "eval" and ("source" in command or re.search(r"(^|[\s;&|({])\.\s+", command)))
        ):
            raise RuntimeObservedCompileError(
                f"unsupported unrewritten source command in {unit.physical_path or unit.logical_path}:{line_index}: {command.strip()}",
                code="runtime.compile.unrewritten_source",
            )


def _is_generated_dynamic_command_guard(command: str) -> bool:
    stripped = command.strip()
    return (
        "__modash_guard_dynamic_command_preserve_status" in stripped
        or stripped.startswith("__modash_dynamic_argv=")
        or stripped.startswith("__modash_dynamic_command_name=")
    )


def _process_substitution_contains_source(command: str) -> bool:
    return bool(re.search(r"[<>]\([^)]*(?:^|[\s;&|({])(?:source|\.|builtin\s+(?:source|\.)|command\s+(?:source|\.))\b", command))


def _source_site_is_inside_process_substitution(line: str, start: int) -> bool:
    opener = max(line.rfind("<(", 0, start), line.rfind(">(", 0, start))
    if opener < 0:
        return False
    closer = line.rfind(")", 0, start)
    return closer < opener


def _source_word_offset_in_candidate(source_site: str) -> int:
    invocation = source_command_invocation(source_site)
    if invocation is None or invocation.source_column_offset is None:
        return 0
    return max(invocation.source_column_offset, 0)


def _bash_c_payload(command: str) -> str | None:
    invocation = _bash_c_invocation(command)
    return invocation.payload if invocation is not None and not invocation.dynamic_payload else None


def _source_bearing_shell_c_payload(command: str) -> bool:
    if _words_have_source_bearing_shell_payload(command):
        return True
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return False
    words = [strip_shell_word_quotes(word) for word in raw_words]
    index = _command_word_index_after_wrappers(raw_words)
    if index is None:
        return False
    command_name = words[index]
    payload_index = _shell_c_payload_index(words, index + 1)
    if payload_index is None:
        return False
    raw_payload = raw_words[payload_index]
    payload = strip_shell_word_quotes(raw_payload)
    payload_is_dynamic = _has_dynamic_shell_expansion(raw_payload)
    if _has_dynamic_shell_expansion(command_name):
        return payload_is_dynamic or _payload_contains_source(payload)
    if _is_non_bash_shell_command(command_name) and payload_is_dynamic:
        return True
    return _is_non_bash_shell_command(command_name) and _payload_contains_source(payload)


def _shell_c_payload_index(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = words[index]
        if word == "-c":
            return index + 1 if index + 1 < len(words) else None
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return index + 1 if index + 1 < len(words) else None
        if word in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("+"):
            index += 1
            continue
        return None
    return None


def _is_non_bash_shell_command(command_name: str) -> bool:
    name = Path(command_name).name
    return name in {"sh", "dash", "ksh", "ksh93", "mksh", "zsh", "ash", "busybox"}


def _bash_c_invocation(command: str) -> _BashCInvocation | None:
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    index = _command_word_index_after_wrappers(words)
    if index is None:
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"}:
        return None
    payload_index = _bash_c_payload_index(words, index + 1)
    if payload_index is None:
        return None
    raw_payload = words[payload_index]
    payload = _bash_c_payload_text(raw_payload)
    return _BashCInvocation(payload, raw_payload, _dynamic_bash_c_payload_word(raw_payload))


def _bash_c_invocation_anywhere(command: str) -> _BashCInvocation | None:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    words = [strip_shell_word_quotes(word) for word in raw_words]
    for index, command_name in enumerate(words):
        if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"}:
            continue
        payload_index = _bash_c_payload_index(words, index + 1)
        if payload_index is None:
            continue
        raw_payload = raw_words[payload_index]
        payload = _bash_c_payload_text(raw_payload)
        return _BashCInvocation(payload, raw_payload, _dynamic_bash_c_payload_word(raw_payload))
    return None


def _bash_invocation_without_c(command: str) -> bool:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return False
    index = _command_word_index_after_wrappers(raw_words)
    if index is None:
        return False
    command_name = strip_shell_word_quotes(raw_words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"}:
        return False
    return _bash_c_payload_index(raw_words, index + 1) is None


def _command_word_index_after_wrappers(words: list[str]) -> int | None:
    return _shell_command_index_after_wrappers([strip_shell_word_quotes(word) for word in words])


def _bash_c_payload_index(words: list[str], index: int) -> int | None:
    while index < len(words):
        word = strip_shell_word_quotes(words[index])
        if word == "-c":
            return index + 1 if index + 1 < len(words) else None
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return index + 1 if index + 1 < len(words) else None
        if word in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if word.startswith("-") or word.startswith("+"):
            index += 1
            continue
        return None
    return None


def _dynamic_bash_c_payload_word(word: str) -> bool:
    stripped = word.strip()
    if stripped.startswith("$'"):
        return False
    if _is_single_quoted_word(stripped):
        return False
    return _has_dynamic_shell_expansion(stripped)


def _bash_c_payload_text(word: str) -> str:
    stripped = word.strip()
    if stripped.startswith("$'") and stripped.endswith("'") and len(stripped) >= 3:
        return _decode_ansi_c_quoted_payload(stripped[2:-1])
    return strip_shell_word_quotes(word)


def _decode_ansi_c_quoted_payload(body: str) -> str:
    try:
        return codecs.decode(body, "unicode_escape")
    except UnicodeDecodeError:
        return body.replace(r"\'", "'").replace(r"\\", "\\")


def _is_single_quoted_word(word: str) -> bool:
    return len(word) >= 2 and word[0] == "'" and word[-1] == "'"


def _has_dynamic_shell_expansion(word: str) -> bool:
    in_single_quote = False
    escaped = False
    for index, char in enumerate(word):
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            continue
        if char == "'":
            in_single_quote = not in_single_quote
            continue
        if in_single_quote:
            continue
        if char == "`":
            return True
        if char != "$":
            continue
        next_char = word[index + 1] if index + 1 < len(word) else ""
        if next_char in {"(", "{", "'"} or next_char.isalpha() or next_char == "_":
            return True
    return False


def _line_starts_multiline_bash_c(line: str) -> bool:
    try:
        if _bash_c_invocation(line) is not None:
            return True
    except RuntimeObservedCompileError:
        return True
    except Exception:
        pass
    return bool(re.search(r"(?:^|\s)(?:env\s+.*)?(?:command\s+)?(?:bash|/bin/bash|/usr/bin/bash)\b.*(?:^|\s)-c(?:\s|$)", line))


def _payload_contains_source(payload: str) -> bool:
    active_heredocs = []
    for line in payload.splitlines() or [payload]:
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        for command in get_commands(line):
            if contains_source_command(command):
                return True
            if contains_nested_source_command(command):
                return True
            try:
                words = parse_shell_words_preserving_quotes(command.strip())
            except UnsupportedSourceError:
                words = []
            name = strip_shell_word_quotes(words[0]) if words else ""
            if name == "eval" and ("source" in command or re.search(r"(^|[\s;&|({])\.\s+", command)):
                return True
        active_heredocs.extend(extract_heredoc_delimiters(line))
    return False


def _payload_is_runtime_unsafe(payload: str) -> bool:
    if _payload_contains_source(payload):
        return True
    if _payload_has_nested_eval(payload):
        return True
    exact_variables: dict[str, str] = {}
    for line in payload.splitlines() or [payload]:
        if _line_contains_coproc_command(line):
            return True
        for command in get_commands(line):
            if _source_bearing_shell_c_payload(command):
                return True
            try:
                raw_words = tuple(parse_shell_words_preserving_quotes(command.strip()))
            except UnsupportedSourceError:
                return True
            clean_words = tuple(strip_shell_word_quotes(word) for word in raw_words)
            _update_payload_exact_variables(raw_words, clean_words, exact_variables)
            if _payload_command_has_dynamic_dispatch(clean_words, exact_variables):
                return True
    return False


def _payload_has_nested_eval(payload: str) -> bool:
    return _scan_payload_shell_fragments(payload, _payload_fragment_has_eval)


def _scan_payload_shell_fragments(text: str, predicate, *, depth: int = 0) -> bool:
    if depth > 8:
        return True
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue
        if in_single_quote:
            index += 1
            continue
        if char == "`":
            body, end_index = read_backtick_body(text, index + 1)
            if body is None:
                index += 1
                continue
            if predicate(body) or _scan_payload_shell_fragments(body, predicate, depth=depth + 1):
                return True
            index = end_index + 1
            continue
        if text.startswith("$((", index):
            _body, end_index = read_balanced_body(text, index + 3)
            index = end_index + 1 if end_index is not None else index + 3
            continue
        if text.startswith("$(", index):
            body, end_index = read_balanced_body(text, index + 2)
            if body is None:
                index += 2
                continue
            if predicate(body) or _scan_payload_shell_fragments(body, predicate, depth=depth + 1):
                return True
            index = end_index + 1
            continue
        index += 1
    return False


def _payload_fragment_has_eval(fragment: str) -> bool:
    for line in fragment.splitlines() or [fragment]:
        for command in get_commands(line):
            try:
                words = tuple(strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(command.strip()))
            except UnsupportedSourceError:
                return True
            index = _payload_effective_command_index(words)
            if index is not None and words[index] == "eval" and not _payload_eval_is_literal_safe(words, index):
                return True
            if index is not None and words[index] == "coproc":
                return True
    return False


def _payload_is_instrumentation_sensitive(payload: str) -> bool:
    for line in payload.splitlines() or [payload]:
        for command in get_commands(line):
            try:
                words = tuple(strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(command.strip()))
            except UnsupportedSourceError:
                return True
            if not words:
                continue
            index = 0
            while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
                name = words[index].split("=", 1)[0]
                if name in {"BASH_ENV", "BASH_XTRACEFD", "PS4", "SHELLOPTS", "BASHOPTS", "BASH_ALIASES"}:
                    return True
                index += 1
            index = _payload_effective_command_index(words, index)
            if index is None:
                continue
            name = words[index]
            if name == "alias":
                return True
            if name in {"env", "/usr/bin/env", "/bin/env"} and (len(words) == index + 1 or "|" in words[index + 1:]):
                return True
            if name in {"printenv", "/usr/bin/printenv", "/bin/printenv"} and (
                len(words) == index + 1 or "|" in words[index + 1:]
            ):
                return True
            if _payload_introspects_replay_critical_command_resolution(words, index):
                return True
            if name == "shopt" and any(word in {"-q", "-p"} for word in words[index + 1:]):
                if any(word in {"expand_aliases", "xtrace"} for word in words[index + 1:]):
                    return True
            if name == "set" and (len(words) == index + 1 or any(word in {"-o", "+o"} for word in words[index + 1:])):
                return True
            if name == "set" and "|" in words[index + 1:]:
                return True
            if name in {"python", "python3", "perl", "ruby", "node", "php", "lua"}:
                if any(word.startswith("<<") for word in words[index + 1:]):
                    return True
                if "-c" in words[index + 1:] and any(
                    "environ" in word or "BASH_" in word or "MODASH_TRACE_" in word
                    for word in words[index + 1:]
                ):
                    return True
            if any(_word_mentions_instrumentation_state(word) for word in words[index:]):
                return True
    return False


def _payload_effective_command_index(words: tuple[str, ...], index: int = 0) -> int | None:
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "for", "time", "!"}:
        index += 1
        if index < len(words) and words[index - 1] == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
    if index < len(words) and words[index] in {"command", "builtin"}:
        index = _command_or_builtin_payload_target_index(words, index)
    return index if index < len(words) else None


def _word_mentions_instrumentation_state(word: str) -> bool:
    return bool(
        re.search(r"\$\-|\$\{\-\}", word)
        or any(token in word for token in ("BASH_ENV", "BASH_XTRACEFD", "PS4", "SHELLOPTS", "BASHOPTS", "BASH_ALIASES", "BASH_EXECUTION_STRING"))
        or re.search(r"(?:^|[<>=:])/?proc/(?:self|\$\$|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/(?:environ|cmdline)(?:$|[\s;&|])", word)
        or re.search(r"(?:^|[<>]&?|/dev/fd/|/proc/(?:self|\$\$|\$BASHPID|\$\{BASHPID\}|[0-9]+|\*)/fd/)(?:18|19)(?:$|[^\d])", word)
        or re.search(r"[<>]&\$|\{[A-Za-z_][A-Za-z0-9_]*\}[<>]", word)
    )


def _payload_introspects_replay_critical_command_resolution(words: tuple[str, ...], index: int) -> bool:
    name = words[index]
    if name == "type":
        return _payload_resolution_probe_targets_replay_critical(words[index + 1:])
    if name == "command":
        probe = index + 1
        saw_resolution = False
        while probe < len(words):
            word = words[probe]
            if word == "--":
                probe += 1
                break
            if word in {"-v", "-V"} or (word.startswith("-") and ("v" in word or "V" in word)):
                saw_resolution = True
                probe += 1
                continue
            if word.startswith("-"):
                probe += 1
                continue
            break
        return saw_resolution and _payload_resolution_probe_targets_replay_critical(words[probe:])
    if name in {"declare", "typeset", "local", "export", "readonly"} and any(word in {"-F", "-f"} for word in words[index + 1:]):
        return _payload_resolution_probe_targets_replay_critical(words[index + 1:])
    if name == "compgen" and "-A" in words[index + 1:]:
        try:
            option_index = words.index("-A", index + 1)
        except ValueError:
            return False
        if option_index + 1 < len(words) and words[option_index + 1] in {"function", "alias"}:
            return _payload_resolution_probe_targets_replay_critical(words[option_index + 2:])
    return False


def _payload_resolution_probe_targets_replay_critical(words: tuple[str, ...]) -> bool:
    targets = [word for word in words if word not in {"-a", "-t", "-p", "-P", "--"} and not word.startswith("-")]
    if not targets:
        return True
    return any(target in {"source", ".", "exec", "trap", "command", "builtin", "shopt", "env", "exit", "enable"} for target in targets)


def _line_contains_coproc_command(line: str) -> bool:
    return bool(re.search(r"(^|[;&|({]|\bthen\b|\bdo\b)\s*coproc\b", line))


def _update_payload_exact_variables(raw_words: tuple[str, ...], words: tuple[str, ...], exact_variables: dict[str, str]) -> None:
    if not words:
        return
    assignment_count = 0
    for word in words:
        if not ASSIGNMENT_WORD_PATTERN.match(word):
            break
        assignment_count += 1
    if assignment_count != len(words):
        for word in words[:assignment_count]:
            exact_variables.pop(word.split("=", 1)[0], None)
        return
    for raw_word, word in zip(raw_words, words):
        name, value = word.split("=", 1)
        raw_value = raw_word.split("=", 1)[1] if "=" in raw_word else ""
        if _payload_exact_assignment_value_is_safe(value) and (
            raw_word == word or _payload_quoted_literal_assignment_value_is_safe(raw_value)
        ):
            exact_variables[name] = value
        else:
            exact_variables.pop(name, None)


def _payload_exact_assignment_value_is_safe(value: str) -> bool:
    return bool(value) and not _has_dynamic_shell_expansion(value) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _payload_quoted_literal_assignment_value_is_safe(raw_value: str) -> bool:
    if _has_dynamic_shell_expansion(raw_value):
        return False
    return re.fullmatch(r"'[^']*'|\"[^\"]*\"", raw_value) is not None


def _payload_command_has_dynamic_dispatch(words: tuple[str, ...], exact_variables: dict[str, str] | None = None) -> bool:
    exact_variables = exact_variables or {}
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "for"}:
        index += 1
    if index >= len(words):
        return False
    while index < len(words) and words[index] in {"time", "!", "coproc", "(", "{"}:
        prefix = words[index]
        index += 1
        if prefix == "time":
            while index < len(words) and words[index].startswith("-"):
                index += 1
        if prefix == "coproc" and index < len(words) and words[index] not in {"{", "source", ".", "builtin", "command", "time", "!"}:
            index += 1
    if index >= len(words):
        return False
    name = words[index]
    if _has_dynamic_shell_expansion(name):
        exact_name = _payload_exact_dynamic_command_name(name, exact_variables)
        if exact_name is None:
            return True
        return exact_name in {"source", ".", "command", "builtin", "exec", "trap", "eval"}
    if name in {"command", "builtin"}:
        index = _command_or_builtin_payload_target_index(words, index)
        return index is None or _has_dynamic_shell_expansion(words[index])
    if name == "eval":
        return not _payload_eval_is_literal_safe(words, index)
    return False


def _payload_exact_dynamic_command_name(word: str, exact_variables: dict[str, str]) -> str | None:
    variable = _simple_variable_reference_name(word)
    if variable is None:
        return None
    return exact_variables.get(variable)


def _payload_eval_is_literal_safe(words: tuple[str, ...], index: int) -> bool:
    if len(words) != index + 2:
        return False
    return words[index + 1] in {"echo", ":"} and not _has_dynamic_shell_expansion(words[index + 1])


def _payload_dynamic_tail_can_be_source(words: tuple[str, ...]) -> bool:
    saw_dynamic = False
    for word in words:
        if word in {"|", "||", "&&", ";", ")", "}", "then", "do", "fi", "done"}:
            break
        if word in {"source", ".", "command", "builtin"}:
            return True
        if _has_dynamic_shell_expansion(word):
            saw_dynamic = True
        elif saw_dynamic and (
            word.startswith(("./", "../", "/"))
            or word.endswith((".sh", ".bash"))
            or "/" in word
        ):
            return True
    return False


def _guard_positional_replay_state_targets(line: str) -> str:
    for command in get_commands(line):
        guard = _positional_nameref_guard(command) or _positional_read_guard(command)
        if guard is None:
            continue
        start = find_unquoted_substring(line, command)
        if start < 0:
            continue
        line = line[:start] + guard + command + line[start + len(command):]
    return line


def _positional_read_guard(command: str) -> str | None:
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    clean_words = tuple(strip_shell_word_quotes(word) for word in words)
    index = 0
    while index < len(clean_words) and ASSIGNMENT_WORD_PATTERN.match(clean_words[index]):
        index += 1
    if index < len(clean_words) and clean_words[index] in {"command", "builtin"}:
        index = _command_or_builtin_payload_target_index(clean_words, index)
        if index is None:
            return None
    if index >= len(clean_words) or clean_words[index] != "read":
        return None
    guarded_positionals = []
    for target in _read_targets(clean_words[index:]):
        positional = _nameref_positional_target(target)
        if positional is not None:
            guarded_positionals.append(positional)
    if not guarded_positionals:
        return None
    tests = " || ".join(f"[[ ${{{position}-}} == __modash_* ]]" for position in sorted(set(guarded_positionals)))
    return f"{{ {tests}; }} && __modash_abort \"runtime replay state read target rejected\"; "


def _guard_dynamic_command_sites(line: str) -> str:
    if "__modash_select_source_edge" in line:
        return line
    search_start = 0
    rewritten = line
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            start = rewritten.find(command, search_start)
        if start < 0:
            continue
        replacement = _dynamic_command_guarded_replacement(command)
        if replacement is None:
            search_start = start + len(command)
            continue
        rewritten = rewritten[:start] + replacement + rewritten[start + len(command):]
        search_start = start + len(replacement)
    return rewritten


def _dynamic_command_guard(command: str) -> str | None:
    guarded = _dynamic_command_guarded_replacement(command)
    if guarded is None:
        return None
    return guarded


def _dynamic_command_guarded_replacement(command: str) -> str | None:
    try:
        raw_words = tuple(parse_shell_words_preserving_quotes(command.strip()))
    except UnsupportedSourceError:
        return None
    if not raw_words:
        return None
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    index = _dynamic_command_index(words)
    if index is None:
        return None
    raw_name = raw_words[index]
    name = words[index]
    if not (_has_dynamic_shell_expansion(raw_name) or _has_dynamic_shell_expansion(name)):
        return None
    if _dynamic_command_word_is_nested_expansion(raw_name):
        return None
    simple_start = _dynamic_simple_command_start(raw_words, words, index)
    simple_end = _dynamic_simple_command_end(words, index)
    spans = _raw_word_spans(command, raw_words)
    if spans is None:
        return None
    start = spans[simple_start][0]
    end = spans[simple_end - 1][1]
    prefix_words = raw_words[simple_start:index]
    command_prefix = " ".join(prefix_words)
    argv_words, redirection_words = _split_dynamic_command_redirections(raw_words[index:simple_end])
    if not argv_words:
        return None
    simple_command_text = command[start:end]
    if _has_process_substitution(simple_command_text):
        command_tail = command[spans[index][1]:end].strip()
        return command[:start] + _dynamic_command_guarded_process_substitution_replacement(
            command_prefix=command_prefix,
            command_name=raw_name,
            command_tail=command_tail,
        ) + command[end:]
    argv_expression = " ".join(argv_words)
    status_variable = "__modash_dynamic_argv_status" if _has_command_substitution(argv_expression) else "__modash_dynamic_prior_status"
    command_redirections = " ".join(redirection_words)
    actual_command = " ".join(part for part in (command_prefix, '"${__modash_dynamic_argv[@]}"', command_redirections) if part)
    guarded = (
        "{ __modash_dynamic_prior_status=$?; __modash_dynamic_argv=(); "
        f"__modash_dynamic_argv=( {argv_expression} ); "
        "__modash_dynamic_argv_status=$?; "
        f"( exit \"${status_variable}\" ); "
        '__modash_guard_dynamic_command_preserve_status "${__modash_dynamic_argv[@]}"; '
        f"{actual_command}; }}"
    )
    return command[:start] + guarded + command[end:]


def _dynamic_command_guarded_process_substitution_replacement(
    *,
    command_prefix: str,
    command_name: str,
    command_tail: str,
) -> str:
    command_name_status = "__modash_dynamic_name_status" if _has_command_substitution(command_name) else "__modash_dynamic_prior_status"
    actual_command = " ".join(
        part
        for part in (
            command_prefix,
            '"${__modash_dynamic_command_name[@]}"',
            command_tail,
        )
        if part
    )
    return (
        "{ __modash_dynamic_prior_status=$?; __modash_dynamic_command_name=(); "
        f"__modash_dynamic_command_name=( {command_name} ); "
        "__modash_dynamic_name_status=$?; "
        f"( exit \"${command_name_status}\" ); "
        '__modash_guard_dynamic_command_preserve_status "${__modash_dynamic_command_name[@]}"; '
        f"{actual_command}; }}"
    )

def _dynamic_simple_command_start(raw_words: tuple[str, ...], words: tuple[str, ...], command_index: int) -> int:
    index = command_index
    while index > 0 and ASSIGNMENT_WORD_PATTERN.match(words[index - 1]):
        index -= 1
    return index


def _dynamic_simple_command_end(words: tuple[str, ...], command_index: int) -> int:
    index = command_index + 1
    while index < len(words):
        if words[index] in {"|", "||", "&&", ";", "then", "do", "fi", "done", ")", "}"}:
            break
        index += 1
    return index


def _split_dynamic_command_redirections(words: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    argv_words: list[str] = []
    redirection_words: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if _is_process_substitution_word(word):
            argv_words.append(word)
            index += 1
            continue
        redirection = _source_redirection_token_kind(word)
        if redirection is None:
            argv_words.append(word)
            index += 1
            continue
        if redirection == "unsupported":
            raise RuntimeObservedCompileError(
                "runtime graph compiler does not yet support dynamic command heredoc redirections",
                code="runtime.compile.dynamic_command",
            )
        redirection_words.append(word)
        index += 1
        if redirection == "separate-target":
            if index >= len(words):
                raise RuntimeObservedCompileError(
                    "runtime graph compiler could not parse dynamic command redirection target",
                    code="runtime.compile.dynamic_command",
                )
            redirection_words.append(words[index])
            index += 1
    return tuple(argv_words), tuple(redirection_words)


def _dynamic_command_word_is_nested_expansion(raw_word: str) -> bool:
    stripped = raw_word.lstrip()
    return stripped.startswith("$(") or stripped.startswith("`")


def _raw_word_spans(command: str, raw_words: tuple[str, ...]) -> tuple[tuple[int, int], ...] | None:
    spans: list[tuple[int, int]] = []
    search_start = 0
    for word in raw_words:
        start = command.find(word, search_start)
        if start < 0:
            start = find_unquoted_substring(command, word, search_start)
        if start < 0:
            return None
        end = start + len(word)
        spans.append((start, end))
        search_start = end
    return tuple(spans)


def _dynamic_command_index(words: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "for", "time", "!"}:
        if words[index] == "time":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            continue
        index += 1
    if index >= len(words):
        return None
    if _not_dynamic_command_dispatch_word(words[index]):
        return None
    if words[index] in {"command", "builtin"}:
        return _command_or_builtin_payload_target_index(words, index)
    return index


def _not_dynamic_command_dispatch_word(word: str) -> bool:
    return (
        word in {"[[", "[", "(("}
        or word.startswith("-")
        or word.startswith("((")
        or bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]+\])?(?:\+)?=", word))
    )


def _rewrite_safe_shopt_restore_eval(line: str) -> str:
    search_start = 0
    rewritten = line
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            continue
        replacement = _safe_shopt_restore_eval_replacement(command)
        if replacement is None:
            search_start = start + len(command)
            continue
        end = start + len(command)
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        search_start = start + len(replacement)
    return rewritten


def _safe_shopt_restore_eval_replacement(command: str) -> str | None:
    try:
        raw_words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    words = tuple(strip_shell_word_quotes(word) for word in raw_words)
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    while index < len(words) and words[index] in {"if", "then", "elif", "else", "do", "while", "until", "for", "time", "!"}:
        index += 1
    if index >= len(words) or words[index] != "eval":
        return None
    if len(words) != index + 2:
        return None
    variable = _simple_variable_reference_name(words[index + 1])
    if variable is None:
        return None
    prefix = " ".join(raw_words[:index])
    replacement = f"__modash_eval_shopt_restore {raw_words[index + 1]}"
    return f"{prefix} {replacement}".strip()


def _simple_variable_reference_name(word: str) -> str | None:
    match = re.fullmatch(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})", word)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _read_targets(words: tuple[str, ...]) -> tuple[str, ...]:
    targets = []
    index = 1
    while index < len(words):
        word = words[index]
        if word == "-a":
            if index + 1 < len(words):
                targets.append(words[index + 1])
            index += 2
            continue
        if word in {"-d", "-n", "-N", "-p", "-t", "-u"}:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        targets.extend(word for word in words[index:] if not word.startswith(("<", ">")))
        break
    return tuple(targets)


def _positional_nameref_guard(command: str) -> str | None:
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    clean_words = tuple(strip_shell_word_quotes(word) for word in words)
    if len(clean_words) < 3 or clean_words[0] != "local" or "-n" not in clean_words[1:]:
        return None
    guarded_positionals = []
    for word in clean_words[1:]:
        if word.startswith("-") or "=" not in word:
            continue
        _name, value = word.split("=", 1)
        positional = _nameref_positional_target(value)
        if positional is not None:
            guarded_positionals.append(positional)
    if not guarded_positionals:
        return None
    tests = " || ".join(f"[[ ${{{position}-}} == __modash_* ]]" for position in sorted(set(guarded_positionals)))
    return f"{{ {tests}; }} && __modash_abort \"runtime replay state nameref target rejected\"; "


def _nameref_positional_target(value: str) -> int | None:
    match = re.fullmatch(r"\$(?:([0-9])|\{([0-9]+)(?::-[A-Za-z_][A-Za-z0-9_]*)?\})", value)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _command_or_builtin_payload_target_index(words: tuple[str, ...], index: int) -> int | None:
    if words[index] == "builtin":
        return index + 1 if index + 1 < len(words) else None
    index += 1
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word == "-p":
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _expandable_runtime_reference(line: str, pattern: str) -> bool:
    in_single_quote = False
    in_double_quote = False
    escaped = False
    current = []
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and not in_single_quote:
            escaped = True
            current.append(" ")
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current.append(" ")
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current.append(" ")
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            break
        current.append(" " if in_single_quote else char)
    return bool(re.search(pattern, "".join(current)))


def _update_multiline_quote_state(line: str, state: str | None) -> str | None:
    escaped = False
    for char in line:
        if escaped:
            escaped = False
            continue
        if state == "'":
            if char == "'":
                state = None
            continue
        if char == "\\":
            escaped = True
            continue
        if state == '"':
            if char == '"':
                state = None
            continue
        if char in {"'", '"'}:
            state = char
    return state


__all__ = [
    "_rewrite_bash_c_payloads",
    "_rewrite_file_units",
    "_rewrite_process_payloads",
]
