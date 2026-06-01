from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from methods.runtime_source_observations import (
    BashInfo,
    EnvironmentInfo,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    SourceCallSite,
    TraceInfo,
    write_observation,
)

TRACE_VERSION = "runtime-wrapper-v1"
TRACE_MARKER = "MODASHC_SOURCE_EVENT"
TRACE_FIELD_ENCODING = "utf-8"


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
    kind: str
    caller_file: str
    caller_line: int
    cwd: str
    source_path: str
    resolved_path: str
    status: int
    arguments: tuple[str, ...]


def trace_sources(entrypoint: str | os.PathLike, *, argv=None, cwd=None, env=None, bash="bash"):
    entrypoint_path = Path(entrypoint)
    if cwd is None:
        cwd_path = entrypoint_path.parent if entrypoint_path.parent != Path("") else Path.cwd()
    else:
        cwd_path = Path(cwd)
    cwd_path = cwd_path.resolve(strict=False)

    if not entrypoint_path.is_absolute():
        entrypoint_path = cwd_path / entrypoint_path
    entrypoint_path = entrypoint_path.resolve(strict=False)
    if not entrypoint_path.is_file():
        raise RuntimeSourceTraceError(
            f"runtime trace entrypoint does not exist: {entrypoint_path}",
            code="runtime.trace.entrypoint_missing",
        )

    argv = tuple(str(argument) for argument in (argv or ()))
    run_env = _trace_environment(env)

    with tempfile.TemporaryDirectory(prefix="modashc-trace-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        trace_path = tmpdir_path / "source-events.bin"
        prelude_path = tmpdir_path / "prelude.sh"
        prelude_path.write_text(_trace_prelude(), encoding="utf-8")
        trace_path.write_bytes(b"")

        run_env.update({
            "BASH_ENV": str(prelude_path),
            "MODASHC_TRACE_ENTRYPOINT": str(entrypoint_path),
            "MODASHC_TRACE_FILE": str(trace_path),
        })

        try:
            completed = subprocess.run(
                [str(bash), str(entrypoint_path), *argv],
                cwd=str(cwd_path),
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise RuntimeSourceTraceError(
                f"unable to run Bash for runtime trace: {bash}: {exc}",
                code="runtime.trace.bash_unavailable",
            ) from exc

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
            sources=_observation_events(trace_path.read_bytes()),
        )
        return RuntimeTraceResult(
            observation=observation,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def default_observation_path(entrypoint: str | os.PathLike, *, output_dir=None, run_id=None):
    directory = Path(output_dir) if output_dir is not None else Path(".modashc") / "observations"
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return directory / f"{_artifact_stem(entrypoint)}-{run_id}.json"


def write_trace_observation(result: RuntimeTraceResult, path: str | os.PathLike):
    return write_observation(path, result.observation)


def _trace_environment(env):
    run_env = os.environ.copy()
    if env:
        run_env.update({str(key): str(value) for key, value in env.items()})
    return run_env


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
            check=False,
        )
    except OSError as exc:
        raise RuntimeSourceTraceError(
            f"unable to run Bash for runtime trace: {bash}: {exc}",
            code="runtime.trace.bash_unavailable",
        ) from exc

    first_line = completed.stdout.splitlines()[0] if completed.stdout else str(bash)
    return first_line


def _observation_events(raw_trace: bytes):
    raw_events = _parse_raw_trace(raw_trace)
    return tuple(
        RuntimeSourceEvent(
            index=index,
            call_site=SourceCallSite(
                file=event.caller_file,
                line=event.caller_line,
                command=_source_line(event.caller_file, event.caller_line),
            ),
            resolved_path=event.resolved_path,
            arguments=event.arguments,
            status=event.status,
        )
        for index, event in enumerate(sorted(raw_events, key=lambda item: item.index))
    )


def _parse_raw_trace(raw_trace: bytes):
    if not raw_trace:
        return ()

    fields = [field.decode(TRACE_FIELD_ENCODING, errors="surrogateescape") for field in raw_trace.split(b"\0")]
    if fields and fields[-1] == "":
        fields.pop()

    events = []
    offset = 0
    while offset < len(fields):
        marker = fields[offset]
        offset += 1
        if marker != TRACE_MARKER:
            raise RuntimeSourceTraceError(f"invalid runtime trace marker: {marker!r}")
        if offset + 9 > len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace event")

        index = _parse_int(fields[offset], "source event index")
        kind = fields[offset + 1]
        caller_file = fields[offset + 2]
        caller_line = _parse_int(fields[offset + 3], "source event caller line")
        cwd = fields[offset + 4]
        source_path = fields[offset + 5]
        resolved_path = fields[offset + 6]
        status = _parse_int(fields[offset + 7], "source event status")
        argument_count = _parse_int(fields[offset + 8], "source event argument count")
        offset += 9

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
        events.append(_RawSourceEvent(
            index=index,
            kind=kind,
            caller_file=caller_file,
            caller_line=caller_line,
            cwd=cwd,
            source_path=source_path,
            resolved_path=resolved_path,
            status=status,
            arguments=arguments,
        ))
    return tuple(events)


def _parse_int(value: str, label: str):
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeSourceTraceError(f"{label} must be an integer: {value!r}") from exc


def _source_line(path: str, line: int):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return "<unknown>"
    if line < 1 or line > len(lines):
        return "<unknown>"
    return lines[line - 1].strip()


def _trace_prelude():
    return r'''
exec 19>>"$MODASHC_TRACE_FILE" || exit 125
unset BASH_ENV

__modashc_source_stack=()
__modashc_source_index=0

__modashc_emit_source_event() {
  local index=$1 kind=$2 caller_file=$3 caller_line=$4 cwd=$5 source_path=$6 resolved_path=$7 status=$8
  shift 8
  printf '%s\0' \
    'MODASHC_SOURCE_EVENT' \
    "$index" \
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

__modashc_resolve_source_path() {
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

__modashc_current_source_file() {
  local depth=${#__modashc_source_stack[@]}
  if (( depth > 0 )); then
    printf '%s' "${__modashc_source_stack[$((depth - 1))]}"
  else
    printf '%s' "$MODASHC_TRACE_ENTRYPOINT"
  fi
}

__modashc_trace_source_common() {
  local kind=$1 builtin_name=$2
  shift 2

  local event_index=$__modashc_source_index
  __modashc_source_index=$((__modashc_source_index + 1))

  local caller_file caller_line cwd source_path resolved_path status
  caller_file=$(__modashc_current_source_file)
  caller_line=${BASH_LINENO[1]:-1}
  cwd=$PWD
  source_path=${1-}
  resolved_path=$(__modashc_resolve_source_path "$source_path")

  if [[ -n $source_path ]]; then
    __modashc_source_stack+=("$resolved_path")
  fi

  builtin "$builtin_name" "$@"
  status=$?

  if [[ -n $source_path ]]; then
    unset '__modashc_source_stack[-1]'
  fi

  if [[ -n $source_path ]]; then
    if (($# > 1)); then
      __modashc_emit_source_event \
        "$event_index" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status" \
        "${@:2}"
    else
      __modashc_emit_source_event \
        "$event_index" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status"
    fi
  fi

  return "$status"
}

source() {
  __modashc_trace_source_common source source "$@"
}

__modashc_trace_dot_source() {
  __modashc_trace_source_common dot . "$@"
}

alias .='__modashc_trace_dot_source'
shopt -s expand_aliases
'''
