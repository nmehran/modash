from __future__ import annotations

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
    _ReplayEdge,
    _RewriteUnit,
    _SourceCandidate,
)
from methods.runtime_evaluator.compiler_plan import _process_logical_path
from methods.runtime_evaluator.compiler_prelude import _render_replay_prelude, _target_logical_paths
from methods.runtime_evaluator.compiler_safety import _words_have_source_bearing_shell_payload
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


@dataclass(frozen=True)
class _BashCInvocation:
    payload: str
    raw_payload_word: str
    dynamic_payload: bool = False


def _rewrite_process_payloads(plan: _CompilePlan) -> None:
    for process_index, process_plan in plan.process_plans.items():
        if process_index == 0:
            continue
        unit = process_plan.units[_process_logical_path(process_index)]
        unit.transformed = _rewrite_content(
            unit,
            process_plan.assignments,
            plan.entrypoint,
            {},
            rewrite_runtime_references=False,
        )
        embedded_files = []
        for logical_path, file_unit in sorted(process_plan.units.items()):
            if logical_path in {ENTRYPOINT_LOGICAL_PATH, _process_logical_path(process_index)}:
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
        plan.process_payloads[process_index] = (unit.content, payload)

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
    process_payloads: dict[int, tuple[str, str]],
    process_replacement_counts: dict[int, int] | None = None,
    rewrite_runtime_references: bool = True,
    rewrite_runtime_zero: bool = True,
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
        if _source_site_has_unsupported_prefix(needle):
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
            negated=_source_site_is_negated(needle, candidate.separator),
        )
        if candidate.separator and candidate.text.lstrip().startswith(candidate.separator):
            replacement = f"{candidate.separator} {replacement}"
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
    assignment_names = _source_assignment_names(assignment_words)
    assignment_sets_status = any(_has_command_substitution(word) for word in assignment_words)
    source_entry_status = (
        "__modash_source_assignment_status"
        if assignment_sets_status
        else
        "__modash_source_expansion_status"
        if source_expression_sets_status
        else "__modash_source_command_prior_status"
    )
    assignment_setup = ""
    assignment_restore = ""
    if assignment_words:
        assignment_setup = (
            f"__modash_save_source_assignments {' '.join(shlex.quote(name) for name in assignment_names)}; "
            f"{' '.join(assignment_words)}; "
            "__modash_source_assignment_status=$?; "
        )
        assignment_restore = "__modash_restore_source_assignments; "
    group = (
        f"{{ __modash_source_command_prior_status=$?; __modash_source_argv=(); __modash_source_argv=( {source_expression} ); "
        "__modash_source_expansion_status=$?; "
        f"{assignment_setup}"
        f"__modash_source_entry_status=${source_entry_status}; "
        f"__modash_replay_select_active=1; __modash_select_source_edge {shlex.quote(candidate.base_id)}; "
        "__modash_replay_select_active=0; "
        "__modash_replay_select_status=$?; "
        "__modash_validate_source_argv \"$__modash_source_entry_status\" \"${__modash_source_argv[@]}\"; "
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "__modash_require_embedded_file \"$__modash_replay_target\"; "
        "( exit \"$__modash_source_entry_status\" ); "
        "builtin source <(__modash_emit_embedded_file \"$__modash_replay_target\") \"${__modash_replay_args[@]}\"; "
        "__modash_replay_actual_status=$?; "
        f"{assignment_restore}"
        "if (( __modash_replay_actual_status != __modash_replay_status )); then "
        "__modash_abort \"observed source status drift\"; "
        "fi; "
        "( exit \"$__modash_replay_actual_status\" ); "
        "else "
        f"{assignment_restore}"
        "builtin printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
        "fi; }"
    )
    if redirection_suffix:
        group = f"{group} {redirection_suffix}"
    return f"! {group}" if negated else group


def _source_expression_for_candidate(candidate: _SourceCandidate) -> str:
    source_expression, _redirections = _source_expression_and_redirections_for_candidate(candidate)
    return source_expression


def _source_expression_and_redirections_for_candidate(candidate: _SourceCandidate) -> tuple[str, str]:
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
    source_words = raw_words[invocation.source_index + 1:]
    argv_words, redirection_words = _split_source_redirections(source_words)
    if not argv_words:
        raise RuntimeObservedCompileError(
            f"could not render source argv validation for observed source site: {candidate.text!r}",
            code="runtime.compile.mapping_failed",
        )
    return " ".join(argv_words), " ".join(redirection_words)


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
            redirection_words.append(words[index])
            index += 1
    return tuple(argv_words), tuple(redirection_words)


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

def _rewrite_bash_c_payloads(
    line: str,
    process_payloads: dict[int, tuple[str, str]],
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
        for process_index, (original_payload, transformed_payload) in process_payloads.items():
            if payload == original_payload:
                if replacement is not None:
                    raise RuntimeObservedCompileError(
                        f"ambiguous observed child bash -c payload mapping for: {payload}",
                        code="runtime.compile.child_process_mapping_failed",
                    )
                replacement = _rewrite_bash_c_command(command, transformed_payload)
                replacement_process = process_index
        if replacement is None:
            if _payload_is_runtime_unsafe(payload):
                raise RuntimeObservedCompileError(
                    f"unobserved child bash -c source payload in {command.strip()}",
                    code="runtime.compile.unobserved_child_source",
                )
            replacement = _rewrite_bash_c_command(command, payload)
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

def _rewrite_bash_c_command(command: str, payload: str) -> str:
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


def _source_site_is_negated(source_site: str, separator: str) -> bool:
    probe = source_site.lstrip()
    if separator and probe.startswith(separator):
        probe = probe[len(separator):].lstrip()
    return probe.startswith("!")


def _source_site_has_unsupported_prefix(source_site: str) -> bool:
    invocation = source_command_invocation(source_site)
    if invocation is None:
        return False
    return any(word in {"time", "coproc"} for word in invocation.words[:invocation.source_index])


def _line_has_runtime_reference(line: str, *, include_zero: bool = True) -> bool:
    if _expandable_runtime_reference(line, r"\bBASH_SOURCE\b"):
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


def _ensure_unique_process_payloads(process_payloads: dict[int, tuple[str, str]]) -> None:
    payload_owners: dict[str, int] = {}
    for process_index, (payload, _transformed) in process_payloads.items():
        owner = payload_owners.get(payload)
        if owner is not None:
            raise RuntimeObservedCompileError(
                f"repeated identical bash -c payload is not yet replayable without a parent process occurrence key: processes {owner} and {process_index}",
                code="runtime.compile.ambiguous_child_process",
            )
        payload_owners[payload] = process_index

def _ensure_no_unrewritten_source(line: str, unit: _RewriteUnit, line_index: int) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    for command in get_commands(line):
        if "__modash_select_source_edge" in command or "__modash_emit_embedded_file" in command:
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
    payload = strip_shell_word_quotes(raw_payload)
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
        payload = strip_shell_word_quotes(raw_payload)
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
    index = _command_word_index_after_env(words)
    if index is None:
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name != "command":
        return index
    index += 1
    while index < len(words):
        word = strip_shell_word_quotes(words[index])
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word == "-p":
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


def _command_word_index_after_env(words: list[str]) -> int | None:
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    if index >= len(words):
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name != "env":
        return index
    index += 1
    while index < len(words):
        word = strip_shell_word_quotes(words[index])
        if word == "--":
            return index + 1 if index + 1 < len(words) else None
        if word in {"-i", "--ignore-environment"}:
            index += 1
            continue
        if word == "-u":
            index += 2
            continue
        if word.startswith("--unset="):
            index += 1
            continue
        if ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
            continue
        if word.startswith("-"):
            return None
        return index
    return None


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
        return True
    if _is_single_quoted_word(stripped):
        return False
    return _has_dynamic_shell_expansion(stripped)


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
        guard = _dynamic_command_guard(command)
        if guard is None:
            search_start = start + len(command)
            continue
        rewritten = rewritten[:start] + guard + rewritten[start:]
        search_start = start + len(guard) + len(command)
    return rewritten


def _dynamic_command_guard(command: str) -> str | None:
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
    guard_args = " ".join(raw_words[index:])
    return f"__modash_guard_dynamic_command_preserve_status {guard_args}; "


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
