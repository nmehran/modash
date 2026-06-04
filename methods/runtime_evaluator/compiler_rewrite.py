from __future__ import annotations

import re
import shlex
from pathlib import Path

from methods.compile import find_unquoted_substring, replace_runtime_source_references
from methods.runtime_evaluator.compiler_model import (
    ENTRYPOINT_LOGICAL_PATH,
    RuntimeObservedCompileError,
    _CompilePlan,
    _EmbeddedFile,
    _ReplayEdge,
    _RewriteUnit,
)
from methods.runtime_evaluator.compiler_plan import _process_logical_path
from methods.runtime_evaluator.compiler_prelude import _render_replay_prelude, _target_logical_paths
from methods.shell.line import get_commands
from methods.source_commands import contains_source_command
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    UnsupportedSourceError,
    extract_heredoc_delimiters,
    is_heredoc_end,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

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
                rewrite_runtime_references=False,
            )
            embedded_files.append(_EmbeddedFile(file_unit.logical_path, transformed))
        payload = (
            _render_replay_prelude(tuple(embedded_files), process_plan.assignments, _target_logical_paths(plan.file_units)).rstrip("\n")
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
) -> str:
    replacements_by_line = _candidate_replacements_by_line(unit)
    lines = unit.content.splitlines()
    output: list[str] = []
    active_heredocs = []
    quote_state: str | None = None
    for line_index, original_line in enumerate(lines, start=1):
        if active_heredocs:
            output.append(original_line)
            if is_heredoc_end(original_line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue
        if quote_state is not None:
            output.append(original_line)
            quote_state = _update_multiline_quote_state(original_line, quote_state)
            continue
        line = original_line
        replacements = replacements_by_line.get(line_index, [])
        if replacements:
            line = _apply_replacements(line, replacements)
        if rewrite_runtime_references and unit.physical_path is not None and not _line_has_bash_c_payload(line):
            line = replace_runtime_source_references(line, unit.physical_path, str(entrypoint))
        line = _rewrite_bash_c_payloads(line, process_payloads, process_replacement_counts)
        _ensure_no_unrewritten_source(line, unit, line_index)
        output.append(line)
        next_quote_state = _update_multiline_quote_state(original_line, quote_state)
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
        end = start + len(needle)
        search_start_by_line[candidate.line] = end
        replacement = _render_replay_group(candidate.base_id)
        if candidate.separator and candidate.text.lstrip().startswith(candidate.separator):
            replacement = f"{candidate.separator} {replacement}"
        replacements_by_line.setdefault(candidate.line, []).append((start, end, replacement))
    return replacements_by_line

def _render_replay_group(base_id: str) -> str:
    return (
        f"{{ __modash_select_source_edge {shlex.quote(base_id)}; "
        "__modash_replay_select_status=$?; "
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "builtin source \"$__modash_replay_file\" \"${__modash_replay_args[@]}\"; "
        "else "
        "printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
        "fi; }"
    )

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
        payload = _bash_c_payload(command)
        if payload is None:
            search_start = end
            continue
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
            if _payload_contains_source(payload):
                raise RuntimeObservedCompileError(
                    f"unobserved child bash -c source payload in {command.strip()}",
                    code="runtime.compile.unobserved_child_source",
                )
            search_start = end
            continue
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        if process_replacement_counts is not None and replacement_process is not None:
            process_replacement_counts[replacement_process] = process_replacement_counts.get(replacement_process, 0) + 1
        search_start = start + len(replacement)
    return rewritten

def _rewrite_bash_c_command(command: str, payload: str) -> str:
    words = parse_shell_words_preserving_quotes(command.strip())
    index = _command_word_index_after_env(words)
    if index is None or index + 2 >= len(words):
        raise RuntimeObservedCompileError(f"unsupported bash -c command: {command}")
    command_name = strip_shell_word_quotes(words[index])
    rewritten_words = [*words[:index], command_name, "-c", shlex.quote(payload), *words[index + 3:]]
    return " ".join(rewritten_words)

def _line_has_bash_c_payload(line: str) -> bool:
    return any(_bash_c_payload(command) is not None for command in get_commands(line))

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
        if "__modash_select_source_edge" in command:
            continue
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            words = []
        name = strip_shell_word_quotes(words[0]) if words else ""
        if (
            contains_source_command(command)
            or (name == "eval" and ("source" in command or re.search(r"(^|[\s;&|({])\.\s+", command)))
        ):
            raise RuntimeObservedCompileError(
                f"unsupported unrewritten source command in {unit.physical_path or unit.logical_path}:{line_index}: {command.strip()}",
                code="runtime.compile.unrewritten_source",
            )

def _bash_c_payload(command: str) -> str | None:
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    index = _command_word_index_after_env(words)
    if index is None or index + 2 >= len(words):
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"} or words[index + 1] != "-c":
        return None
    return strip_shell_word_quotes(words[index + 2])


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
            try:
                words = parse_shell_words_preserving_quotes(command.strip())
            except UnsupportedSourceError:
                words = []
            name = strip_shell_word_quotes(words[0]) if words else ""
            if name == "eval" and ("source" in command or re.search(r"(^|[\s;&|({])\.\s+", command)):
                return True
        active_heredocs.extend(extract_heredoc_delimiters(line))
    return False


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
