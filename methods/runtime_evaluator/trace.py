from __future__ import annotations

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

from methods.runtime_evaluator.errors import RuntimeSourceTraceError
from methods.runtime_evaluator.observations import (
    BashInfo,
    EnvironmentInfo,
    RuntimeFileFingerprint,
    RuntimeProcess,
    RuntimeRunInfo,
    RuntimeFunctionCall,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeXtraceSourceCommand,
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
    write_observation,
)
from methods.runtime_evaluator.xtrace import (
    TRACE_FIELD_ENCODING,
    _XtraceSourceCommand,
    _normalized_xtrace_file,
    _observation_xtrace_command_text,
    _parse_int,
    _reconcile_xtrace_source_coverage,
    _source_line,
    _source_like_xtrace_commands,
    _xtrace_commands,
    _xtrace_word_is_dynamic,
)
from methods.source_resolver import (
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

TRACE_VERSION = "runtime-wrapper-v11"
PROCESS_MARKER = "MODASH_PROCESS_EVENT"
TRACE_MARKER = "MODASH_SOURCE_EVENT"
DEFAULT_TRACE_TIMEOUT_SECONDS = 30
BASH_VERSION_TIMEOUT_SECONDS = 5
TRACE_OWNED_ENVIRONMENT_KEYS = frozenset({
    "BASH_ENV",
    "BASH_XTRACEFD",
    "PS4",
    "SHELLOPTS",
    "MODASH_TRACE_FILE",
    "MODASH_TRACE_COUNTER_FILE",
    "MODASH_TRACE_XTRACE_FILE",
    "MODASH_TRACE_FAILURE_FILE",
    "MODASH_TRACE_POSITIONAL_SCANNER",
    "MODASH_TRACE_FUNCTION_SCANNER",
    "MODASH_TRACE_FINGERPRINT_SCANNER",
    "MODASH_TRACE_PYTHON",
})


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
    source_size: int | None
    source_mtime_ns: int | None
    source_sha256: str | None
    source_entry_status: int
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
    user_visible_env = dict(run_env)

    with tempfile.TemporaryDirectory(prefix="modash-trace-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        trace_path = tmpdir_path / "source-events.bin"
        counter_path = tmpdir_path / "source-events.counter"
        xtrace_path = tmpdir_path / "xtrace.log"
        failure_path = tmpdir_path / "trace-failure.txt"
        scanner_path = tmpdir_path / "positionals-scanner.py"
        function_scanner_path = tmpdir_path / "function-scanner.py"
        fingerprint_scanner_path = tmpdir_path / "fingerprint-scanner.py"
        prelude_path = tmpdir_path / "prelude.sh"
        prelude_path.write_text(_trace_prelude(), encoding="utf-8")
        scanner_path.write_text(_positionals_scanner_script(), encoding="utf-8")
        function_scanner_path.write_text(_function_definition_scanner_script(), encoding="utf-8")
        fingerprint_scanner_path.write_text(_fingerprint_scanner_script(), encoding="utf-8")
        trace_path.write_bytes(b"")
        counter_path.write_text("0\n", encoding="utf-8")
        xtrace_path.write_bytes(b"")
        failure_path.write_text("", encoding="utf-8")
        entrypoint_fingerprint = fingerprint_file(entrypoint_path, ("entrypoint",))

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
            "MODASH_TRACE_FINGERPRINT_SCANNER": str(fingerprint_scanner_path),
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
        environment_values = _recorded_environment_values(
            user_visible_env,
            env,
            source_events,
        )
        observation = RuntimeSourceObservation(
            entrypoint=str(entrypoint_path),
            cwd=str(cwd_path),
            argv=argv,
            bash=BashInfo(version=_bash_version(bash)),
            trace=TraceInfo(version=TRACE_VERSION),
            environment=EnvironmentInfo(
                policy="inherit" if env is None else "overlay",
                recorded_keys=tuple(sorted(environment_values)),
                values=environment_values,
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
            files=_observation_file_fingerprints(entrypoint_path, entrypoint_fingerprint, raw_trace.sources, source_events),
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
    present_trace_owned = sorted(key for key in TRACE_OWNED_ENVIRONMENT_KEYS if key in run_env)
    if present_trace_owned:
        raise RuntimeSourceTraceError(
            "runtime source trace does not support user-provided trace instrumentation environment: "
            + ", ".join(present_trace_owned),
            code="runtime.trace.instrumentation_environment",
        )
    exported_functions = sorted(
        key
        for key in run_env
        if key.startswith("BASH_FUNC_") and key.endswith("%%")
    )
    if exported_functions:
        raise RuntimeSourceTraceError(
            "runtime source trace does not support exported Bash functions because trusted replay runs under bash -p: "
            + ", ".join(exported_functions),
            code="runtime.trace.exported_function",
        )
    return run_env


def _recorded_environment_values(run_env: dict[str, str], explicit_env, source_events) -> dict[str, str]:
    values = {
        str(key): str(value)
        for key, value in (explicit_env or {}).items()
    }
    for event in source_events:
        payloads = (event.resolved_path, *event.arguments)
        for key in _source_relevant_variable_names(event.call_site.command):
            if key in values or key not in run_env or key in TRACE_OWNED_ENVIRONMENT_KEYS:
                continue
            value = str(run_env[key])
            if _environment_value_contributed_to_source(value, payloads):
                values[key] = value
    return {key: values[key] for key in sorted(values)}


def _source_relevant_variable_names(text: str) -> set[str]:
    names: set[str] = set()
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
        if in_single_quote or char != "$":
            index += 1
            continue
        if index + 1 >= len(text):
            index += 1
            continue
        if text[index + 1] == "{":
            end = text.find("}", index + 2)
            if end < 0:
                index += 2
                continue
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text[index + 2:end])
            if match:
                names.add(match.group(1))
            index = end + 1
            continue
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[index + 1:])
        if match:
            names.add(match.group(0))
            index += 1 + len(match.group(0))
            continue
        index += 1
    return names


def _environment_value_contributed_to_source(value: str, payloads: tuple[str, ...]) -> bool:
    if value == "":
        return False
    if any(value in payload for payload in payloads):
        return True
    try:
        resolved = str(Path(value).resolve(strict=False))
    except OSError:
        return False
    return any(resolved and resolved in payload for payload in payloads)


def _is_shell_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


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
    repo_root = str(Path(__file__).resolve().parents[2])
    return (
        "import sys\n\n"
        f"REPO_ROOT = {repo_root!r}\n"
        "if REPO_ROOT and REPO_ROOT not in sys.path:\n"
        "    sys.path.insert(0, REPO_ROOT)\n\n"
        "from methods.runtime_evaluator.scanners import main\n\n"
        "if __name__ == \"__main__\":\n"
        f"    sys.exit(main([{scanner_name!r}, *sys.argv[1:]]))\n"
    )


def _positionals_scanner_script():
    return _scanner_entrypoint_script("positionals")


def _function_definition_scanner_script():
    return _scanner_entrypoint_script("functions")


def _fingerprint_scanner_script():
    return (
        "import hashlib\n"
        "import os\n"
        "import sys\n\n"
        "path = sys.argv[1]\n"
        "stat_result = os.stat(path)\n"
        "digest = hashlib.sha256()\n"
        "with open(path, 'rb') as handle:\n"
        "    for chunk in iter(lambda: handle.read(1024 * 1024), b''):\n"
        "        digest.update(chunk)\n"
        "print(stat_result.st_size)\n"
        "print(stat_result.st_mtime_ns)\n"
        "print(digest.hexdigest())\n"
    )


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
            source_entry_status=event.source_entry_status,
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


def _observation_file_fingerprints(entrypoint_path, entrypoint_fingerprint, raw_events, events):
    roles_by_path: dict[Path, set[str]] = {}
    trace_source_fingerprints: dict[Path, RuntimeFileFingerprint] = {}
    for raw_event in raw_events:
        if raw_event.source_size is None or raw_event.source_mtime_ns is None or raw_event.source_sha256 is None:
            continue
        resolved_path = Path(raw_event.resolved_path).resolve(strict=False)
        fingerprint = RuntimeFileFingerprint(
            path=str(resolved_path),
            size=raw_event.source_size,
            mtime_ns=raw_event.source_mtime_ns,
            sha256=raw_event.source_sha256,
            roles=("source",),
        )
        previous = trace_source_fingerprints.get(resolved_path)
        if previous is not None and (
            previous.size != fingerprint.size
            or previous.mtime_ns != fingerprint.mtime_ns
            or previous.sha256 != fingerprint.sha256
        ):
            raise RuntimeSourceTraceError(
                f"runtime source trace observed multiple versions of sourced file: {resolved_path}",
                code="runtime.trace.source_version_drift",
            )
        trace_source_fingerprints[resolved_path] = fingerprint

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
        trace_fingerprint = trace_source_fingerprints.get(path)
        if trace_fingerprint is not None:
            roles = tuple(role for role in ("entrypoint", "call-site", "source") if role in roles_by_path[path])
            fingerprints.append(RuntimeFileFingerprint(
                path=trace_fingerprint.path,
                size=trace_fingerprint.size,
                mtime_ns=trace_fingerprint.mtime_ns,
                sha256=trace_fingerprint.sha256,
                roles=roles,
            ))
            continue
        if path == Path(entrypoint_path).resolve(strict=False):
            roles = tuple(role for role in ("entrypoint", "call-site", "source") if role in roles_by_path[path])
            fingerprints.append(RuntimeFileFingerprint(
                path=entrypoint_fingerprint.path,
                size=entrypoint_fingerprint.size,
                mtime_ns=entrypoint_fingerprint.mtime_ns,
                sha256=entrypoint_fingerprint.sha256,
                roles=roles,
            ))
            continue
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
        if offset + 14 > len(fields):
            raise RuntimeSourceTraceError("truncated runtime source trace event")

        index = _parse_int(fields[offset], "source event index")
        pid = _parse_int(fields[offset + 1], "source event pid")
        kind = fields[offset + 2]
        caller_file = fields[offset + 3]
        caller_line = _parse_int(fields[offset + 4], "source event caller line")
        cwd = fields[offset + 5]
        source_path = fields[offset + 6]
        resolved_path = fields[offset + 7]
        source_size = _parse_optional_int(fields[offset + 8], "source event source size")
        source_mtime_ns = _parse_optional_int(fields[offset + 9], "source event source mtime_ns")
        source_sha256 = fields[offset + 10] or None
        status = _parse_int(fields[offset + 11], "source event status")
        source_entry_status = _parse_int(fields[offset + 12], "source event source entry status")
        function_count = _parse_int(fields[offset + 13], "source event function stack count")
        offset += 14

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
        sources.append(_RawSourceEvent(
            index=index,
            pid=pid,
            kind=kind,
            caller_file=caller_file,
            caller_line=caller_line,
            cwd=cwd,
            source_path=source_path,
            resolved_path=resolved_path,
            source_size=source_size,
            source_mtime_ns=source_mtime_ns,
            source_sha256=source_sha256,
            source_entry_status=source_entry_status,
            status=status,
            function_stack=function_stack,
            arguments=arguments,
        ))
    return _RawTrace(processes=tuple(processes), sources=tuple(sources))




def _trace_prelude():
    return importlib.resources.files("methods.runtime_evaluator").joinpath("trace_prelude.bash").read_text(encoding="utf-8")


def _parse_optional_int(value: str, label: str):
    if value == "":
        return None
    return _parse_int(value, label)
