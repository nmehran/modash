from __future__ import annotations

import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from methods.runtime_source_observations import (
    BashInfo,
    EnvironmentInfo,
    RuntimeProcess,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
    write_observation,
)

TRACE_VERSION = "runtime-wrapper-v3"
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
    file: str
    line: int
    function: str
    command: str


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
        prelude_path = tmpdir_path / "prelude.sh"
        prelude_path.write_text(_trace_prelude(), encoding="utf-8")
        trace_path.write_bytes(b"")
        counter_path.write_text("0\n", encoding="utf-8")
        xtrace_path.write_bytes(b"")

        run_env.update({
            "BASH_ENV": str(prelude_path),
            "MODASH_TRACE_ENTRYPOINT": str(entrypoint_path),
            "MODASH_TRACE_INITIAL_CWD": str(cwd_path),
            "MODASH_TRACE_FILE": str(trace_path),
            "MODASH_TRACE_COUNTER_FILE": str(counter_path),
            "MODASH_TRACE_XTRACE_FILE": str(xtrace_path),
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

        raw_trace = _parse_raw_trace(trace_path.read_bytes())
        _validate_xtrace_source_coverage(
            raw_trace.sources,
            xtrace_path.read_bytes(),
            prelude_path=str(prelude_path),
        )
        processes = _observation_processes(raw_trace.processes)

        source_events = _observation_events(raw_trace.sources, processes)
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
            processes=processes,
            sources=source_events,
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


def _observation_events(raw_events, processes):
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
            process_index=process_index_by_pid[event.pid],
            call_site=SourceCallSite(
                file=event.caller_file,
                line=event.caller_line,
                command=command,
            ),
            resolved_path=event.resolved_path,
            arguments=event.arguments,
            status=event.status,
        ))
    return tuple(events)


def _observation_file_fingerprints(entrypoint_path, events):
    roles_by_path: dict[Path, set[str]] = {}

    def add_role(path, role):
        resolved = Path(path).resolve(strict=False)
        roles_by_path.setdefault(resolved, set()).add(role)

    add_role(entrypoint_path, "entrypoint")
    for event in events:
        if _is_file_backed_call_site(event.call_site):
            add_role(event.call_site.file, "call-site")
        resolved_path = Path(event.resolved_path).resolve(strict=False)
        if event.status == 0 and resolved_path.is_file():
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
        argument_count = _parse_int(fields[offset + 9], "source event argument count")
        offset += 10

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
            arguments=arguments,
        ))
    return _RawTrace(processes=tuple(processes), sources=tuple(sources))


def _validate_xtrace_source_coverage(raw_events, raw_xtrace: bytes, *, prelude_path: str):
    source_commands = _xtrace_source_commands(raw_xtrace, prelude_path=prelude_path)
    if len(source_commands) <= len(raw_events):
        return

    missed = source_commands[len(raw_events)]
    raise RuntimeSourceTraceError(
        "runtime source trace missed source-like command: "
        f"{missed.file}:{missed.line}: {missed.command}",
        code="runtime.trace.incomplete",
    )


def _xtrace_source_commands(raw_xtrace: bytes, *, prelude_path: str):
    if not raw_xtrace:
        return ()

    commands = []
    for line in raw_xtrace.decode(TRACE_FIELD_ENCODING, errors="replace").splitlines():
        record = _parse_xtrace_line(line)
        if record is None:
            continue
        if record.file == prelude_path:
            continue
        source_line = _source_line(record.file, record.line)
        if _is_xtrace_source_like(record.command) or _is_xtrace_source_like(source_line):
            commands.append(record)
    return tuple(commands)


def _parse_xtrace_line(line: str):
    stripped = line.lstrip("+")
    prefix = f"{XTRACE_MARKER}{XTRACE_FIELD_SEPARATOR}"
    if not stripped.startswith(prefix):
        return None

    fields = stripped[len(prefix):].split(XTRACE_FIELD_SEPARATOR, 3)
    if len(fields) != 4:
        raise RuntimeSourceTraceError("malformed runtime xtrace sidecar record")

    file, line_number, function, command = fields
    return _XtraceSourceCommand(
        file=file,
        line=_parse_int(line_number, "xtrace source line"),
        function=function,
        command=command.strip(),
    )


def _is_xtrace_source_like(command: str):
    command = command.strip()
    if not command:
        return False
    return (
        command == "source"
        or command.startswith("source ")
        or command == "."
        or command.startswith(". ")
        or command == "builtin source"
        or command.startswith("builtin source ")
        or command == "builtin ."
        or command.startswith("builtin . ")
        or command == "command source"
        or command.startswith("command source ")
        or command == "command ."
        or command.startswith("command . ")
    )


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
    return r'''
exec 19>>"$MODASH_TRACE_FILE" || exit 125
exec 18>>"$MODASH_TRACE_XTRACE_FILE" || exit 125
BASH_XTRACEFD=18
PS4=$'+MODASH_XTRACE\x1f${BASH_SOURCE[0]}\x1f${LINENO}\x1f${FUNCNAME[0]-}\x1f '

__modash_source_stack=()
declare -A __modash_source_file_map=()

__modash_emit_process_event() {
  local pid=$1 parent_pid=$2 cwd=$3 entrypoint=$4 command=$5
  shift 5
  printf '%s\0' \
    'MODASH_PROCESS_EVENT' \
    "$pid" \
    "$parent_pid" \
    "$cwd" \
    "$entrypoint" \
    "$command" \
    "$#" \
    "$@" >&19
}

__modash_emit_source_event() {
  local index=$1 pid=$2 kind=$3 caller_file=$4 caller_line=$5 cwd=$6 source_path=$7 resolved_path=$8 status=$9
  shift 9
  printf '%s\0' \
    'MODASH_SOURCE_EVENT' \
    "$index" \
    "$pid" \
    "$kind" \
    "$caller_file" \
    "$caller_line" \
    "$cwd" \
    "$source_path" \
    "$resolved_path" \
    "$status" \
    "$#" \
    "$@" >&19
}

__modash_next_source_index() {
  local lock_path="${MODASH_TRACE_COUNTER_FILE}.lock"
  local index
  while ! mkdir "$lock_path" 2>/dev/null; do
    sleep 0.001
  done
  if [[ -r $MODASH_TRACE_COUNTER_FILE ]]; then
    IFS= read -r index < "$MODASH_TRACE_COUNTER_FILE"
  else
    index=0
  fi
  printf '%s\n' "$((index + 1))" > "$MODASH_TRACE_COUNTER_FILE"
  rmdir "$lock_path"
  printf '%s' "$index"
}

__modash_resolve_source_path() {
  local source_path=${1-}
  local directory

  if [[ -z $source_path ]]; then
    printf ''
    return
  fi

  if [[ $source_path == */* ]]; then
    if [[ $source_path == /* ]]; then
      printf '%s' "$source_path"
    else
      printf '%s/%s' "$PWD" "$source_path"
    fi
    return
  fi

  local old_ifs=$IFS
  IFS=:
  for directory in $PATH; do
    if [[ -z $directory ]]; then
      directory=.
    fi
    if [[ -f $directory/$source_path ]]; then
      if [[ $directory == /* ]]; then
        printf '%s/%s' "$directory" "$source_path"
      else
        printf '%s/%s/%s' "$PWD" "$directory" "$source_path"
      fi
      IFS=$old_ifs
      return
    fi
  done
  IFS=$old_ifs
  printf '%s/%s' "$PWD" "$source_path"
}

__modash_current_source_file() {
  local bash_source=${1-}
  local depth=${#__modash_source_stack[@]}
  if (( depth > 0 )); then
    printf '%s' "${__modash_source_stack[$((depth - 1))]}"
  elif [[ -n $bash_source && -n ${__modash_source_file_map[$bash_source]+set} ]]; then
    printf '%s' "${__modash_source_file_map[$bash_source]}"
  elif [[ -n $bash_source && $bash_source == /* ]]; then
    printf '%s' "$bash_source"
  elif [[ -n $bash_source && $bash_source != "$0" ]]; then
    printf '%s/%s' "$MODASH_TRACE_INITIAL_CWD" "$bash_source"
  else
    printf '%s' "$__modash_trace_process_entrypoint"
  fi
}

__modash_process_entrypoint() {
  local shell_entrypoint=${1-}
  if [[ -z $shell_entrypoint ]]; then
    printf '%s' "$MODASH_TRACE_ENTRYPOINT"
  elif [[ $shell_entrypoint == */* ]]; then
    if [[ $shell_entrypoint == /* ]]; then
      printf '%s' "$shell_entrypoint"
    else
      printf '%s/%s' "$PWD" "$shell_entrypoint"
    fi
  else
    __modash_resolve_source_path "$shell_entrypoint"
  fi
}

__modash_trace_process_entrypoint=$(__modash_process_entrypoint "$0")
__modash_trace_process_command=${BASH_EXECUTION_STRING:-$__modash_trace_process_entrypoint}
__modash_emit_process_event \
  "$BASHPID" "$PPID" "$PWD" "$__modash_trace_process_entrypoint" "$__modash_trace_process_command" "$@"

__modash_trace_source_common() {
  local kind=$1 builtin_name=$2
  shift 2

  local event_index
  event_index=$(__modash_next_source_index)

  local caller_file caller_line cwd source_path resolved_path status
  caller_file=$(__modash_current_source_file "${BASH_SOURCE[2]:-}")
  caller_line=${BASH_LINENO[1]:-1}
  cwd=$PWD
  source_path=${1-}
  resolved_path=$(__modash_resolve_source_path "$source_path")

  if [[ -n $source_path ]]; then
    __modash_source_file_map["$source_path"]=$resolved_path
    __modash_source_file_map["$resolved_path"]=$resolved_path
    __modash_source_stack+=("$resolved_path")
  fi

  builtin "$builtin_name" "$@"
  status=$?

  if [[ -n $source_path ]]; then
    unset '__modash_source_stack[-1]'
  fi

  if [[ -n $source_path ]]; then
    if (($# > 1)); then
      __modash_emit_source_event \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status" \
        "${@:2}"
    else
      __modash_emit_source_event \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status"
    fi
  fi

  return "$status"
}

source() {
  __modash_trace_source_common source source "$@"
}

__modash_trace_dot_source() {
  __modash_trace_source_common dot . "$@"
}

__modash_trace_builtin() {
  local builtin_name=${1-}
  case "$builtin_name" in
    source)
      shift
      __modash_trace_source_common source source "$@"
      ;;
    .)
      shift
      __modash_trace_source_common dot . "$@"
      ;;
    *)
      builtin "$@"
      ;;
  esac
}

__modash_trace_command() {
  local command_name=${1-}
  case "$command_name" in
    source)
      shift
      __modash_trace_source_common source source "$@"
      ;;
    .)
      shift
      __modash_trace_source_common dot . "$@"
      ;;
    *)
      command "$@"
      ;;
  esac
}

alias .='__modash_trace_dot_source'
alias builtin='__modash_trace_builtin'
alias command='__modash_trace_command'
shopt -s expand_aliases
set -x
'''
