from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from methods.runtime_evaluator.errors import RuntimeSourceTraceError
from methods.source_commands import (
    SourceCommandInvocation,
    clean_shell_word,
    is_source_like_command_text,
    normalized_trace_wrapper_words,
    shell_quote as quote_runtime_shell_word,
    source_command_invocation,
    source_invocation_from_command as runtime_source_invocation_from_command,
)
from methods.shell.line import get_commands
from methods.source_resolver import parse_shell_words_preserving_quotes

XTRACE_MARKER = "MODASH_XTRACE"
XTRACE_FIELD_SEPARATOR = "\x1f"
TRACE_FIELD_ENCODING = "utf-8"


@dataclass(frozen=True)
class _XtraceSourceCommand:
    index: int
    sequence: int
    pid: int
    file: str
    line: int
    function: str
    cwd: str
    command: str


@dataclass(frozen=True)
class _SourceInvocationKey:
    pid: int
    file: str
    line: int
    source_path: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _ReconciledXtrace:
    xtrace_indexes_by_source_index: dict[int, int]
    raw_events_by_xtrace_index: dict[int, _RawSourceEvent]
    source_identities_by_source_index: dict[int, str]
    source_identities_by_xtrace_index: dict[int, str]


def _reconcile_xtrace_source_coverage(raw_events, xtrace_commands):
    ordered_events = tuple(sorted(raw_events, key=lambda item: item.index))
    event_groups = _group_raw_events_by_source_key(ordered_events)
    command_groups, dynamic_command_groups = _group_xtrace_commands_by_source_key(xtrace_commands)
    matched_groups = []

    unmatched_event_groups = {}
    for key in sorted(event_groups, key=_loose_source_key_sort_tuple):
        commands = command_groups.pop(key, None)
        if commands is None:
            unmatched_event_groups.setdefault(_dynamic_xtrace_match_key(key), []).extend(event_groups[key])
            continue
        matched_groups.append((key, tuple(event_groups[key]), tuple(commands)))

    for dynamic_key in sorted(unmatched_event_groups):
        events = tuple(sorted(unmatched_event_groups[dynamic_key], key=lambda item: item.index))
        commands = dynamic_command_groups.pop(dynamic_key, ())
        if not commands:
            event = events[0]
            raise RuntimeSourceTraceError(
                "runtime source trace recorded source event without xtrace provenance: "
                f"{event.caller_file}:{event.caller_line}: {event.source_path}",
                code="runtime.trace.incomplete",
            )
        matched_groups.append((dynamic_key, events, tuple(commands)))

    if command_groups:
        missed = command_groups[sorted(command_groups, key=_loose_source_key_sort_tuple)[0]][0]
        _raise_missed_source_trace_error(missed)

    if dynamic_command_groups:
        missed = dynamic_command_groups[sorted(dynamic_command_groups)[0]][0]
        _raise_missed_source_trace_error(missed)

    for key, events, commands in matched_groups:
        if len(events) > len(commands):
            event = events[len(commands)]
            raise RuntimeSourceTraceError(
                "runtime source trace recorded source event without xtrace provenance: "
                f"{event.caller_file}:{event.caller_line}: {event.source_path}",
                code="runtime.trace.incomplete",
            )
        if len(commands) > len(events):
            missed = commands[len(events)]
            _raise_missed_source_trace_error(missed)

    if len(xtrace_commands) != len(ordered_events):
        # Defensive fallback; key-level diagnostics above should normally catch this.
        raise RuntimeSourceTraceError(
            "runtime source trace/xtrace source command count mismatch",
            code="runtime.trace.incomplete",
        )

    xtrace_indexes_by_source_index = {}
    raw_events_by_xtrace_index = {}
    source_identities_by_source_index = {}
    source_identities_by_xtrace_index = {}
    for key, events, commands in sorted(matched_groups, key=lambda item: _matched_group_sort_tuple(item[0])):
        events = sorted(events, key=lambda item: item.index)
        commands = sorted(commands, key=lambda item: item.index)
        for occurrence, (event, command) in enumerate(zip(events, commands)):
            event_file = str(Path(event.caller_file).resolve(strict=False))
            command_file = _normalized_xtrace_file(command, None)
            if (
                command.file
                and event_file != command_file
                and not _trusted_xtrace_call_site_file_alias(event, command, command_file)
            ):
                raise RuntimeSourceTraceError(
                    "runtime source trace/xtrace call-site mismatch: "
                    f"source event {event.caller_file}:{event.caller_line} "
                    f"vs xtrace {command.file}:{command.line}",
                    code="runtime.trace.xtrace_mismatch",
                )
            identity = _source_identity(_raw_event_source_key(event), occurrence)
            xtrace_indexes_by_source_index[event.index] = command.index
            raw_events_by_xtrace_index[command.index] = event
            source_identities_by_source_index[event.index] = identity
            source_identities_by_xtrace_index[command.index] = identity

    return _ReconciledXtrace(
        xtrace_indexes_by_source_index=xtrace_indexes_by_source_index,
        raw_events_by_xtrace_index=raw_events_by_xtrace_index,
        source_identities_by_source_index=source_identities_by_source_index,
        source_identities_by_xtrace_index=source_identities_by_xtrace_index,
    )


def _raise_missed_source_trace_error(missed: _XtraceSourceCommand) -> None:
    if "__modash_trace_source_alias" in missed.command:
        raise RuntimeSourceTraceError(
            "runtime source trace could not finalize source command before shell exit; "
            "this can happen when the sourced command exits the shell before trace finalization, "
            "for example through set -e or exit: "
            f"{missed.file}:{missed.line}: {missed.command}",
            code="runtime.trace.errexit_source_exit",
        )
    raise RuntimeSourceTraceError(
        "runtime source trace missed source-like command: "
        f"{missed.file}:{missed.line}: {missed.command}",
        code="runtime.trace.incomplete",
    )


def _matched_group_sort_tuple(key):
    if isinstance(key, _SourceInvocationKey):
        return _loose_source_key_sort_tuple(_loose_source_key(key))
    return key


def _group_raw_events_by_source_key(raw_events):
    groups = {}
    for event in raw_events:
        key = _loose_source_key(_raw_event_source_key(event))
        groups.setdefault(key, []).append(event)
    return groups


def _group_xtrace_commands_by_source_key(xtrace_commands):
    groups = {}
    dynamic_groups = {}
    for command in xtrace_commands:
        invocation = _parse_xtrace_source_invocation(command.command)
        if invocation is None:
            key = _loose_source_key(_xtrace_unknown_source_key(command))
        elif _is_dynamic_xtrace_invocation(invocation):
            key = _dynamic_xtrace_match_key(_xtrace_source_key(command, invocation))
            dynamic_groups.setdefault(key, []).append(command)
            continue
        else:
            key = _loose_source_key(_xtrace_source_key(command, invocation))
        groups.setdefault(key, []).append(command)
    return groups, dynamic_groups


def _raw_event_source_key(event: _RawSourceEvent):
    return _SourceInvocationKey(
        pid=event.pid,
        file=str(Path(event.caller_file).resolve(strict=False)),
        line=event.caller_line,
        source_path=event.source_path,
        arguments=event.arguments,
    )


def _xtrace_source_key(command: _XtraceSourceCommand, invocation: SourceCommandInvocation):
    return _SourceInvocationKey(
        pid=command.pid,
        file=_normalized_xtrace_file(command, None),
        line=command.line,
        source_path=invocation.source_path,
        arguments=invocation.arguments,
    )


def _xtrace_unknown_source_key(command: _XtraceSourceCommand):
    return _SourceInvocationKey(
        pid=command.pid,
        file=_normalized_xtrace_file(command, None),
        line=command.line,
        source_path=f"<unparsed:{command.command}>",
        arguments=(),
    )


def _dynamic_xtrace_match_key(key: _SourceInvocationKey | tuple):
    if isinstance(key, _SourceInvocationKey):
        return key.pid, key.line
    return key[0], key[1]


def _is_dynamic_xtrace_invocation(invocation: SourceCommandInvocation):
    return _xtrace_word_is_dynamic(invocation.source_path) or any(
        _xtrace_word_is_dynamic(argument)
        for argument in invocation.arguments
    )


def _xtrace_word_is_dynamic(word: str):
    return "$" in word or "`" in word


def _parse_xtrace_source_invocation(command: str):
    return runtime_source_invocation_from_command(command)


def _observation_xtrace_command_text(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return command
    stripped_words = tuple(clean_shell_word(word) for word in words)
    if not stripped_words:
        return command

    normalized_words = normalized_trace_wrapper_words(stripped_words)
    if normalized_words is None:
        return command
    return " ".join(quote_runtime_shell_word(word) for word in normalized_words)


def _source_identity(key: _SourceInvocationKey, occurrence: int):
    payload = "\0".join((
        str(key.pid),
        key.file,
        str(key.line),
        key.source_path,
        "\0".join(key.arguments),
        str(occurrence),
    ))
    return "src:" + hashlib.sha256(payload.encode("utf-8", errors="surrogateescape")).hexdigest()[:24]


def _loose_source_key(key: _SourceInvocationKey):
    return (
        key.pid,
        key.line,
        key.source_path,
        key.arguments,
    )


def _loose_source_key_sort_tuple(key):
    return key


def _normalized_xtrace_file(command: _XtraceSourceCommand, event: _RawSourceEvent | None):
    if event is not None:
        event_file = str(Path(event.caller_file).resolve(strict=False))
        if not command.file:
            return event_file
        command_file = _normalized_xtrace_command_file(command)
        if _trusted_xtrace_call_site_file_alias(event, command, command_file):
            return event_file
        return command_file

    if not command.file:
        return str(Path(command.cwd).resolve(strict=False))
    return _normalized_xtrace_command_file(command)


def _normalized_xtrace_command_file(command: _XtraceSourceCommand):
    candidate = Path(command.file)
    if not candidate.is_absolute():
        candidate = Path(command.cwd) / candidate
    return str(candidate.resolve(strict=False))


def _trusted_xtrace_call_site_file_alias(
    event: _RawSourceEvent,
    command: _XtraceSourceCommand,
    command_file: str,
):
    event_file = str(Path(event.caller_file).resolve(strict=False))
    if event_file == command_file:
        return False
    event_line = _source_command_line(event_file, event.caller_line)
    if (
        not _is_xtrace_source_like(event_line)
        and (not command.function or command.function not in event.function_stack)
    ):
        return False
    invocation = _parse_xtrace_source_invocation(command.command)
    if invocation is None:
        return False
    return (
        invocation.source_path == event.source_path
        and invocation.arguments == event.arguments
    )


def _xtrace_commands(raw_xtrace: bytes, *, prelude_path: str):
    if not raw_xtrace:
        return ()

    commands = []
    for line in raw_xtrace.decode(TRACE_FIELD_ENCODING, errors="replace").splitlines():
        record = _parse_xtrace_line(line)
        if record is None:
            continue
        record_file = _normalized_xtrace_file(record, None)
        if record_file == prelude_path:
            continue
        commands.append(record)
    return tuple(
        _XtraceSourceCommand(
            index=index,
            sequence=index,
            pid=command.pid,
            file=command.file,
            line=command.line,
            function=command.function,
            cwd=command.cwd,
            command=command.command,
        )
        for index, command in enumerate(commands)
    )


def _source_like_xtrace_commands(commands):
    source_commands = []
    for command in commands:
        record_file = _normalized_xtrace_file(command, None)
        source_line = _source_command_line(record_file, command.line)
        if _is_xtrace_source_like(command.command) or (
            _xtrace_source_line_fallback_allowed(command.command)
            and _is_xtrace_source_like(source_line)
        ):
            source_commands.append(command)
    return tuple(
        _XtraceSourceCommand(
            index=index,
            sequence=command.sequence,
            pid=command.pid,
            file=command.file,
            line=command.line,
            function=command.function,
            cwd=command.cwd,
            command=command.command,
        )
        for index, command in enumerate(source_commands)
    )


def _xtrace_source_line_fallback_allowed(command: str):
    stripped = command.strip()
    return stripped.startswith("__modash_trace_")


def _parse_xtrace_line(line: str):
    stripped = line.lstrip("+")
    prefix = f"{XTRACE_MARKER}{XTRACE_FIELD_SEPARATOR}"
    if not stripped.startswith(prefix):
        return None

    fields = stripped[len(prefix):].split(XTRACE_FIELD_SEPARATOR, 5)
    if len(fields) != 6:
        raise RuntimeSourceTraceError("malformed runtime xtrace sidecar record")

    pid, cwd, file, line_number, function, command = fields
    return _XtraceSourceCommand(
        index=0,
        sequence=0,
        pid=_parse_int(pid, "xtrace source pid"),
        file=file,
        line=_parse_int(line_number, "xtrace source line"),
        function=function,
        cwd=cwd,
        command=command.strip(),
    )


def _is_xtrace_source_like(command: str):
    return is_source_like_command_text(command)


def _parse_int(value: str, label: str):
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeSourceTraceError(f"{label} must be an integer: {value!r}") from exc


def _source_line(path: str, line: int):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "<unknown>"
    if line < 1 or line > len(lines):
        return "<unknown>"
    return _logical_source_line(lines, line - 1).strip()


def _source_command_line(path: str, line: int):
    logical = _source_line(path, line)
    if logical == "<unknown>":
        return logical
    return _single_source_command(logical) or logical


def _logical_source_line(lines: list[str], start_index: int) -> str:
    line = lines[start_index]
    if not _line_has_continuation(line):
        return line
    logical = line.rstrip()[:-1]
    index = start_index + 1
    while index < len(lines):
        current = lines[index]
        if _line_has_continuation(current):
            logical += current.rstrip()[:-1]
            index += 1
            continue
        logical += current
        break
    return logical


def _single_source_command(line: str) -> str | None:
    commands = get_commands(line)
    source_commands = [
        command.strip()
        for command in commands
        if source_command_invocation(command.strip()) is not None
    ]
    if len(source_commands) == 1:
        return source_commands[0]
    return None


def _line_has_continuation(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped.endswith("\\"):
        return False
    backslashes = 0
    index = len(stripped) - 1
    while index >= 0 and stripped[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1
