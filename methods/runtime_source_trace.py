from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from methods.runtime_source_observations import (
    BashInfo,
    EnvironmentInfo,
    RuntimeProcess,
    RuntimeRunInfo,
    RuntimeFunctionCall,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    RuntimeXtraceSourceCommand,
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
    write_observation,
)
from methods.runtime_source_commands import (
    SourceCommandWords,
    clean_shell_word,
    is_source_like_command_text,
    normalized_trace_wrapper_words,
    shell_quote as quote_runtime_shell_word,
    source_invocation_from_command as runtime_source_invocation_from_command,
)
from methods.source_resolver import (
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

TRACE_VERSION = "runtime-wrapper-v9"
PROCESS_MARKER = "MODASH_PROCESS_EVENT"
TRACE_MARKER = "MODASH_SOURCE_EVENT"
XTRACE_MARKER = "MODASH_XTRACE"
XTRACE_FIELD_SEPARATOR = "\x1f"
TRACE_FIELD_ENCODING = "utf-8"
DEFAULT_TRACE_TIMEOUT_SECONDS = 30
BASH_VERSION_TIMEOUT_SECONDS = 5


class RuntimeSourceTraceError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.trace.invalid"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class RuntimeTraceResult:
    observation: RuntimeSourceObservation
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class _RawSourceEvent:
    index: int
    pid: int
    kind: str
    caller_file: str
    caller_line: int
    cwd: str
    source_path: str
    resolved_path: str
    status: int
    function_stack: tuple[str, ...]
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _RawProcessEvent:
    pid: int
    parent_pid: int
    cwd: str
    entrypoint: str
    command: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _RawTrace:
    processes: tuple[_RawProcessEvent, ...]
    sources: tuple[_RawSourceEvent, ...]


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


def trace_sources(
    entrypoint: str | os.PathLike,
    *,
    argv=None,
    cwd=None,
    env=None,
    bash="bash",
    timeout=DEFAULT_TRACE_TIMEOUT_SECONDS,
):
    cwd_is_explicit = cwd is not None
    entrypoint_path, cwd_path = _resolve_trace_paths(entrypoint, cwd)
    timeout_seconds = _normalize_timeout(timeout)
    if cwd_is_explicit and not cwd_path.is_dir():
        raise RuntimeSourceTraceError(
            f"runtime trace cwd does not exist or is not a directory: {cwd_path}",
            code="runtime.trace.cwd_missing",
        )
    if not entrypoint_path.is_file():
        raise RuntimeSourceTraceError(
            f"runtime trace entrypoint does not exist: {entrypoint_path}",
            code="runtime.trace.entrypoint_missing",
        )

    argv = tuple(str(argument) for argument in (argv or ()))
    run_env = _trace_environment(env)

    with tempfile.TemporaryDirectory(prefix="modash-trace-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        trace_path = tmpdir_path / "source-events.bin"
        counter_path = tmpdir_path / "source-events.counter"
        xtrace_path = tmpdir_path / "xtrace.log"
        failure_path = tmpdir_path / "trace-failure.txt"
        scanner_path = tmpdir_path / "positionals-scanner.py"
        function_scanner_path = tmpdir_path / "function-scanner.py"
        prelude_path = tmpdir_path / "prelude.sh"
        prelude_path.write_text(_trace_prelude(), encoding="utf-8")
        scanner_path.write_text(_positionals_scanner_script(), encoding="utf-8")
        function_scanner_path.write_text(_function_definition_scanner_script(), encoding="utf-8")
        trace_path.write_bytes(b"")
        counter_path.write_text("0\n", encoding="utf-8")
        xtrace_path.write_bytes(b"")
        failure_path.write_text("", encoding="utf-8")

        run_env.update({
            "BASH_ENV": str(prelude_path),
            "MODASH_TRACE_ENTRYPOINT": str(entrypoint_path),
            "MODASH_TRACE_INITIAL_CWD": str(cwd_path),
            "MODASH_TRACE_FILE": str(trace_path),
            "MODASH_TRACE_COUNTER_FILE": str(counter_path),
            "MODASH_TRACE_XTRACE_FILE": str(xtrace_path),
            "MODASH_TRACE_FAILURE_FILE": str(failure_path),
            "MODASH_TRACE_POSITIONAL_SCANNER": str(scanner_path),
            "MODASH_TRACE_FUNCTION_SCANNER": str(function_scanner_path),
            "MODASH_TRACE_PYTHON": sys.executable,
        })

        try:
            completed = subprocess.run(
                [str(bash), str(entrypoint_path), *argv],
                cwd=str(cwd_path),
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeSourceTraceError(
                f"runtime source trace timed out after {timeout_seconds:g} seconds: {entrypoint_path}",
                code="runtime.trace.timeout",
            ) from exc
        except OSError as exc:
            raise RuntimeSourceTraceError(
                f"unable to run Bash for runtime trace: {bash}: {exc}",
                code="runtime.trace.bash_unavailable",
            ) from exc

        _raise_trace_prelude_failure(failure_path)

        raw_trace = _parse_raw_trace(trace_path.read_bytes())
        all_xtrace_commands = _xtrace_commands(
            xtrace_path.read_bytes(),
            prelude_path=str(prelude_path),
        )
        xtrace_commands = _source_like_xtrace_commands(all_xtrace_commands)
        reconciled_xtrace = _reconcile_xtrace_source_coverage(
            raw_trace.sources,
            xtrace_commands,
        )
        processes = _observation_processes(raw_trace.processes)

        source_events = _observation_events(
            raw_trace.sources,
            processes,
            reconciled_xtrace.xtrace_indexes_by_source_index,
            reconciled_xtrace.source_identities_by_source_index,
            _observed_function_calls(
                raw_trace.sources,
                xtrace_commands,
                all_xtrace_commands,
                reconciled_xtrace.xtrace_indexes_by_source_index,
            ),
        )
        xtrace = _observation_xtrace_commands(
            xtrace_commands,
            processes,
            reconciled_xtrace.raw_events_by_xtrace_index,
            reconciled_xtrace.source_identities_by_xtrace_index,
        )
        observation = RuntimeSourceObservation(
            entrypoint=str(entrypoint_path),
            cwd=str(cwd_path),
            argv=argv,
            bash=BashInfo(version=_bash_version(bash)),
            trace=TraceInfo(version=TRACE_VERSION),
            environment=EnvironmentInfo(
                policy="inherit" if env is None else "overlay",
                recorded_keys=tuple(sorted(str(key) for key in (env or {}).keys())),
            ),
            run=RuntimeRunInfo(
                observed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                modash_version=_modash_version(),
                platform=platform.platform(),
                python_version=sys.version.split()[0],
                shell=_resolved_shell(bash),
                target_status=completed.returncode,
                timeout_seconds=timeout_seconds,
            ),
            processes=processes,
            sources=source_events,
            xtrace=xtrace,
            files=_observation_file_fingerprints(entrypoint_path, source_events),
        )
        return RuntimeTraceResult(
            observation=observation,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def default_observation_path(entrypoint: str | os.PathLike, *, output_dir=None, run_id=None):
    directory = Path(output_dir) if output_dir is not None else Path(".modash") / "observations"
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{_artifact_stem(entrypoint)}-{run_id}.json"


def write_trace_observation(result: RuntimeTraceResult, path: str | os.PathLike):
    return write_observation(path, result.observation)


def _trace_environment(env):
    run_env = os.environ.copy()
    if env:
        run_env.update({str(key): str(value) for key, value in env.items()})
    return run_env


def _raise_trace_prelude_failure(path: Path):
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not content:
        return
    lines = content.splitlines()
    code = lines[0] if lines else "runtime.trace.failed"
    message = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    raise RuntimeSourceTraceError(
        message or "runtime source trace failed before trustworthy observation completed",
        code=code or "runtime.trace.failed",
    )


def _normalize_timeout(timeout):
    if timeout is None:
        return None
    if isinstance(timeout, bool):
        raise RuntimeSourceTraceError(
            "runtime trace timeout must be a positive number",
            code="runtime.trace.invalid_timeout",
        )
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as exc:
        raise RuntimeSourceTraceError(
            "runtime trace timeout must be a positive number",
            code="runtime.trace.invalid_timeout",
        ) from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RuntimeSourceTraceError(
            "runtime trace timeout must be a positive number",
            code="runtime.trace.invalid_timeout",
        )
    return timeout_seconds


def _scanner_entrypoint_script(scanner_name: str):
    repo_root = str(Path(__file__).resolve().parents[1])
    return (
        "import sys\n\n"
        f"REPO_ROOT = {repo_root!r}\n"
        "if REPO_ROOT and REPO_ROOT not in sys.path:\n"
        "    sys.path.insert(0, REPO_ROOT)\n\n"
        "from methods.runtime_source_scanners import main\n\n"
        "if __name__ == \"__main__\":\n"
        f"    sys.exit(main([{scanner_name!r}, *sys.argv[1:]]))\n"
    )


def _positionals_scanner_script():
    return _scanner_entrypoint_script("positionals")


def _function_definition_scanner_script():
    return _scanner_entrypoint_script("functions")

def _resolve_trace_paths(entrypoint, cwd):
    entrypoint_path = Path(entrypoint)
    if cwd is None:
        base = Path.cwd()
        resolved_entrypoint = (
            entrypoint_path if entrypoint_path.is_absolute() else base / entrypoint_path
        ).resolve(strict=False)
        return resolved_entrypoint, resolved_entrypoint.parent

    cwd_path = Path(cwd).resolve(strict=False)
    resolved_entrypoint = (
        entrypoint_path if entrypoint_path.is_absolute() else cwd_path / entrypoint_path
    ).resolve(strict=False)
    return resolved_entrypoint, cwd_path


def _artifact_stem(entrypoint):
    stem = Path(entrypoint).name or "trace"
    safe = []
    for character in stem:
        if character.isalnum() or character in {".", "_", "-"}:
            safe.append(character)
        else:
            safe.append("_")
    return "".join(safe).strip("._-") or "trace"


def _bash_version(bash):
    try:
        completed = subprocess.run(
            [str(bash), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            check=False,
            timeout=BASH_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeSourceTraceError(
            f"unable to read Bash version before runtime trace: {bash}",
            code="runtime.trace.bash_timeout",
        ) from exc
    except OSError as exc:
        raise RuntimeSourceTraceError(
            f"unable to run Bash for runtime trace: {bash}: {exc}",
            code="runtime.trace.bash_unavailable",
        ) from exc

    first_line = completed.stdout.splitlines()[0] if completed.stdout else str(bash)
    return first_line


def _resolved_shell(bash):
    resolved = shutil.which(str(bash))
    return resolved or str(bash)


def _modash_version():
    try:
        return importlib.metadata.version("modash")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _observation_processes(raw_processes):
    if not raw_processes:
        raise RuntimeSourceTraceError("runtime source trace did not record a Bash process")

    processes = []
    process_index_by_pid = {}
    for raw_process in raw_processes:
        if raw_process.pid in process_index_by_pid:
            continue
        process_index_by_pid[raw_process.pid] = len(processes)
        processes.append(raw_process)

    return tuple(
        RuntimeProcess(
            index=index,
            pid=process.pid,
            parent_index=process_index_by_pid.get(process.parent_pid),
            parent_pid=process.parent_pid,
            entrypoint=process.entrypoint,
            cwd=process.cwd,
            argv=process.argv,
            command=process.command,
        )
        for index, process in enumerate(processes)
    )


def _observation_events(
    raw_events,
    processes,
    xtrace_indexes_by_source_index=None,
    source_identities_by_source_index=None,
    function_calls_by_source_index=None,
):
    xtrace_indexes_by_source_index = xtrace_indexes_by_source_index or {}
    source_identities_by_source_index = source_identities_by_source_index or {}
    function_calls_by_source_index = function_calls_by_source_index or {}
    process_index_by_pid = {process.pid: process.index for process in processes}
    process_by_pid = {process.pid: process for process in processes}
    events = []
    for index, event in enumerate(sorted(raw_events, key=lambda item: item.index)):
        if event.pid not in process_index_by_pid:
            raise RuntimeSourceTraceError(
                f"runtime source trace event references unknown process: {event.pid}",
                code="runtime.trace.unknown_process",
            )
        command = _source_line(event.caller_file, event.caller_line)
        if command == "<unknown>":
            command = _process_command_line(process_by_pid[event.pid].command, event.caller_line)
        if command == "<unknown>":
            command = _source_command_from_event(event)
        events.append(RuntimeSourceEvent(
            index=index,
            source_identity=source_identities_by_source_index.get(event.index, ""),
            process_index=process_index_by_pid[event.pid],
            xtrace_index=xtrace_indexes_by_source_index.get(event.index),
            call_site=SourceCallSite(
                file=event.caller_file,
                line=event.caller_line,
                command=command,
            ),
            function_stack=event.function_stack,
            function_call=function_calls_by_source_index.get(event.index),
            resolved_path=event.resolved_path,
            arguments=event.arguments,
            status=event.status,
        ))
    return tuple(events)


def _observed_function_calls(raw_events, xtrace_commands, all_xtrace_commands, xtrace_indexes_by_source_index):
    source_commands_by_index = {command.index: command for command in xtrace_commands}
    calls = {}
    for event in raw_events:
        if not event.function_stack:
            continue
        xtrace_index = xtrace_indexes_by_source_index.get(event.index)
        if xtrace_index is None:
            continue
        source_command = source_commands_by_index.get(xtrace_index)
        if source_command is None:
            continue
        function_call = _observed_function_call_for_stack(
            event.function_stack,
            event.pid,
            source_command.sequence,
            all_xtrace_commands,
        )
        if function_call is not None:
            calls[event.index] = function_call
    return calls


def _observed_function_call_for_stack(function_stack, pid: int, before_sequence: int, all_xtrace_commands):
    if not function_stack:
        return None
    for function_name in reversed(function_stack):
        for command in reversed(all_xtrace_commands[:before_sequence]):
            if command.pid != pid:
                continue
            parsed = _xtrace_function_call(command.command)
            if parsed is None:
                continue
            candidate_name, arguments = parsed
            if candidate_name != function_name:
                continue
            call_file = _normalized_xtrace_file(command, None)
            if not Path(call_file).is_file():
                continue
            if not _xtrace_function_call_needs_observed_replay(command, function_name):
                break
            return RuntimeFunctionCall(
                file=call_file,
                line=command.line,
                function=function_name,
                command=command.command,
                arguments=arguments,
            )
    return None


def _xtrace_function_call_needs_observed_replay(command: _XtraceSourceCommand, function_name: str):
    source_line = _source_line(_normalized_xtrace_file(command, None), command.line)
    if source_line == "<unknown>":
        return True
    try:
        words = parse_shell_words_preserving_quotes(source_line)
    except Exception:
        return True
    if not words:
        return True
    name = strip_shell_word_quotes(words[0])
    if name != function_name:
        return True
    return any(_xtrace_word_is_dynamic(word) for word in words[1:])


def _xtrace_function_call(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None
    if not words:
        return None
    name = strip_shell_word_quotes(words[0])
    if not re.fullmatch(r'[a-zA-Z_]\w*', name):
        return None
    return name, tuple(strip_shell_word_quotes(word) for word in words[1:])


def _observation_xtrace_commands(
    xtrace_commands,
    processes,
    raw_events_by_xtrace_index,
    source_identities_by_xtrace_index,
):
    process_index_by_pid = {process.pid: process.index for process in processes}
    commands = []
    for index, command in enumerate(xtrace_commands):
        if command.pid not in process_index_by_pid:
            raise RuntimeSourceTraceError(
                f"runtime xtrace source command references unknown process: {command.pid}",
                code="runtime.trace.unknown_process",
            )
        event = raw_events_by_xtrace_index.get(index)
        commands.append(RuntimeXtraceSourceCommand(
            index=index,
            source_identity=source_identities_by_xtrace_index[index],
            process_index=process_index_by_pid[command.pid],
            file=_normalized_xtrace_file(command, event),
            line=command.line,
            function=command.function,
            cwd=command.cwd,
            command=_observation_xtrace_command_text(command.command),
        ))
    return tuple(commands)


def _observation_file_fingerprints(entrypoint_path, events):
    roles_by_path: dict[Path, set[str]] = {}

    def add_role(path, role):
        resolved = Path(path).resolve(strict=False)
        roles_by_path.setdefault(resolved, set()).add(role)

    add_role(entrypoint_path, "entrypoint")
    for event in events:
        if _is_file_backed_call_site(event.call_site):
            add_role(event.call_site.file, "call-site")
        if event.function_call is not None and Path(event.function_call.file).is_file():
            add_role(event.function_call.file, "call-site")
        resolved_path = Path(event.resolved_path).resolve(strict=False)
        if resolved_path.is_file():
            add_role(resolved_path, "source")

    fingerprints = []
    for path in sorted(roles_by_path, key=lambda item: item.as_posix()):
        fingerprints.append(fingerprint_file(path, roles_by_path[path]))
    return tuple(fingerprints)


def _is_file_backed_call_site(call_site: SourceCallSite):
    return Path(call_site.file).is_file() and _source_line(call_site.file, call_site.line) != "<unknown>"


def _source_command_from_event(event: _RawSourceEvent):
    command_name = "." if event.kind == "dot" else "source"
    words = [command_name, event.source_path, *event.arguments]
    return " ".join(words)


def _process_command_line(command: str, line: int):
    lines = command.splitlines()
    if line < 1 or line > len(lines):
        return "<unknown>"
    return lines[line - 1].strip() or "<unknown>"


def _parse_raw_trace(raw_trace: bytes):
    if not raw_trace:
        return _RawTrace(processes=(), sources=())

    fields = [field.decode(TRACE_FIELD_ENCODING, errors="surrogateescape") for field in raw_trace.split(b"\0")]
    if fields and fields[-1] == "":
        fields.pop()

    processes = []
    sources = []
    offset = 0
    while offset < len(fields):
        marker = fields[offset]
        offset += 1
        if marker == PROCESS_MARKER:
            if offset + 6 > len(fields):
                raise RuntimeSourceTraceError("truncated runtime process trace event")

            pid = _parse_int(fields[offset], "process event pid")
            parent_pid = _parse_int(fields[offset + 1], "process event parent pid")
            cwd = fields[offset + 2]
            entrypoint = fields[offset + 3]
            command = fields[offset + 4]
            argument_count = _parse_int(fields[offset + 5], "process event argument count")
            offset += 6

            if argument_count < 0:
                raise RuntimeSourceTraceError("process event argument count must be non-negative")
            if offset + argument_count > len(fields):
                raise RuntimeSourceTraceError("truncated runtime process trace arguments")
            arguments = tuple(fields[offset:offset + argument_count])
            offset += argument_count
            processes.append(_RawProcessEvent(
                pid=pid,
                parent_pid=parent_pid,
                cwd=cwd,
                entrypoint=entrypoint,
                command=command,
                argv=arguments,
            ))
            continue

        if marker != TRACE_MARKER:
            raise RuntimeSourceTraceError(f"invalid runtime trace marker: {marker!r}")
        if offset + 10 > len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace event")

        index = _parse_int(fields[offset], "source event index")
        pid = _parse_int(fields[offset + 1], "source event pid")
        kind = fields[offset + 2]
        caller_file = fields[offset + 3]
        caller_line = _parse_int(fields[offset + 4], "source event caller line")
        cwd = fields[offset + 5]
        source_path = fields[offset + 6]
        resolved_path = fields[offset + 7]
        status = _parse_int(fields[offset + 8], "source event status")
        function_count = _parse_int(fields[offset + 9], "source event function stack count")
        offset += 10

        if function_count < 0:
            raise RuntimeSourceTraceError("source event function stack count must be non-negative")
        if offset + function_count > len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace function stack")
        function_stack = tuple(fields[offset:offset + function_count])
        offset += function_count

        if offset >= len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace event")
        argument_count = _parse_int(fields[offset], "source event argument count")
        offset += 1

        if argument_count < 0:
            raise RuntimeSourceTraceError("source event argument count must be non-negative")
        if offset + argument_count > len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace arguments")
        arguments = tuple(fields[offset:offset + argument_count])
        offset += argument_count

        if kind not in {"source", "dot"}:
            raise RuntimeSourceTraceError(f"unsupported runtime source event kind: {kind!r}")
        if not source_path:
            raise RuntimeSourceTraceError("runtime source event missing source path")
        sources.append(_RawSourceEvent(
            index=index,
            pid=pid,
            kind=kind,
            caller_file=caller_file,
            caller_line=caller_line,
            cwd=cwd,
            source_path=source_path,
            resolved_path=resolved_path,
            status=status,
            function_stack=function_stack,
            arguments=arguments,
        ))
    return _RawTrace(processes=tuple(processes), sources=tuple(sources))


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
        raise RuntimeSourceTraceError(
            "runtime source trace missed source-like command: "
            f"{missed.file}:{missed.line}: {missed.command}",
            code="runtime.trace.incomplete",
        )

    if dynamic_command_groups:
        missed = dynamic_command_groups[sorted(dynamic_command_groups)[0]][0]
        raise RuntimeSourceTraceError(
            "runtime source trace missed source-like command: "
            f"{missed.file}:{missed.line}: {missed.command}",
            code="runtime.trace.incomplete",
        )

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
            raise RuntimeSourceTraceError(
                "runtime source trace missed source-like command: "
                f"{missed.file}:{missed.line}: {missed.command}",
                code="runtime.trace.incomplete",
            )

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


def _xtrace_source_key(command: _XtraceSourceCommand, invocation: SourceCommandWords):
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


def _is_dynamic_xtrace_invocation(invocation: SourceCommandWords):
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


def _source_key_sort_tuple(key: _SourceInvocationKey):
    return (
        key.pid,
        key.file,
        key.line,
        key.source_path,
        key.arguments,
    )


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
    event_line = _source_line(event_file, event.caller_line)
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
        source_line = _source_line(record_file, command.line)
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
    return lines[line - 1].strip()


def _trace_prelude():
    return importlib.resources.files("methods").joinpath("runtime_source_trace_prelude.bash").read_text(encoding="utf-8")
