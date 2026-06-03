from __future__ import annotations

import hashlib
import importlib.metadata
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
class _XtraceSourceInvocation:
    source_path: str
    arguments: tuple[str, ...]


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
        (
            xtrace_indexes_by_source_index,
            raw_events_by_xtrace_index,
            source_identities_by_source_index,
            source_identities_by_xtrace_index,
        ) = _reconcile_xtrace_source_coverage(
            raw_trace.sources,
            xtrace_commands,
        )
        processes = _observation_processes(raw_trace.processes)

        source_events = _observation_events(
            raw_trace.sources,
            processes,
            xtrace_indexes_by_source_index,
            source_identities_by_source_index,
            _observed_function_calls(
                raw_trace.sources,
                xtrace_commands,
                all_xtrace_commands,
                xtrace_indexes_by_source_index,
            ),
        )
        xtrace = _observation_xtrace_commands(
            xtrace_commands,
            processes,
            raw_events_by_xtrace_index,
            source_identities_by_xtrace_index,
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


def _positionals_scanner_script():
    repo_root = str(Path(__file__).resolve().parents[1])
    return f'''\
import sys

REPO_ROOT = {repo_root!r}
if REPO_ROOT and REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from methods.compile import file_top_level_source_traits
except Exception as exc:
    print(f"modash positional scanner import failed: {{exc}}", file=sys.stderr)
    sys.exit(2)


def main(argv):
    if len(argv) != 2:
        print("modash positional scanner expected exactly one path", file=sys.stderr)
        return 2
    path = argv[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return 1
    try:
        _, has_top_level_positional_mutation = file_top_level_source_traits(path, content)
    except Exception as exc:
        print(f"modash positional scanner failed for {{path}}: {{exc}}", file=sys.stderr)
        return 2
    return 0 if has_top_level_positional_mutation else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


def _function_definition_scanner_script():
    repo_root = str(Path(__file__).resolve().parents[1])
    return f'''\
import re
import sys

REPO_ROOT = {repo_root!r}
if REPO_ROOT and REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from methods.regex.utilities import remove_comments
    from methods.source_effects import (
        CaseBlock,
        CStyleForLoop,
        ForLoop,
        FunctionDef,
        IfBlock,
        WhileLoop,
    )
    from methods.source_frontend import LineParserFrontend
    from methods.source_resolver import extract_heredoc_delimiters, is_heredoc_end
except Exception as exc:
    print(f"modash function scanner import failed: {{exc}}", file=sys.stderr)
    sys.exit(2)

FUNCTION_DECLARATION_PATTERN = re.compile(
    r"(?=(?:^|[;&|(){{}}]|\\bthen\\b|\\bdo\\b)\\s*"
    r"(?:(?:function\\s+([a-zA-Z_]\\w*)(?:\\s*\\(\\s*\\))?)|([a-zA-Z_]\\w*)\\s*\\(\\s*\\))\\s*(?:\\{{|$))"
)
EVAL_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\\bthen\\b|\\bdo\\b)\\s*eval(?:\\s|$)")
TRAP_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\\bthen\\b|\\bdo\\b)\\s*trap(?:\\s|$)")
ALIAS_COMMAND_PATTERN = re.compile(r"(?:^|[;&|()]|\\bthen\\b|\\bdo\\b)\\s*alias(?:\\s|$)")
EMBEDDED_FUNCTION_DECLARATION_PATTERN = re.compile(
    r"(?:(?:function\\s+([a-zA-Z_]\\w*)(?:\\s*\\(\\s*\\))?)|([a-zA-Z_]\\w*)\\s*\\(\\s*\\))\\s*\\{{"
)


def condition_truth(condition):
    normalized = " ".join((condition or "").strip().split())
    if normalized in ("true", ":"):
        return True
    if normalized == "false":
        return False
    return None


def collect_definitions(nodes, status, records):
    for node in nodes:
        if isinstance(node, FunctionDef):
            records.add((status, node.name, node.location.line))
            for child in node.body:
                collect_unknown_definitions(child, records)
        elif isinstance(node, IfBlock):
            collect_if_block(node, status, records)
        else:
            collect_unknown_definitions(node, records)


def collect_unknown_definitions(node, records):
    if isinstance(node, FunctionDef):
        records.add(("unknown", node.name, node.location.line))
        for child in node.body:
            collect_unknown_definitions(child, records)
    elif isinstance(node, IfBlock):
        for branch in node.branches:
            collect_definitions(branch.body, "unknown", records)
    elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
        collect_definitions(node.body, "unknown", records)
    elif isinstance(node, CaseBlock):
        for arm in node.arms:
            collect_definitions(arm.body, "unknown", records)


def collect_if_block(node, status, records):
    branch_unknown = False
    for branch in node.branches:
        if branch.condition is None:
            collect_definitions(branch.body, "unknown" if branch_unknown else status, records)
            return

        truth = condition_truth(branch.condition)
        if truth is True:
            collect_definitions(branch.body, "unknown" if branch_unknown else status, records)
            return
        if truth is False:
            continue

        branch_unknown = True
        collect_definitions(branch.body, "unknown", records)


def collect_dead_function_lines(nodes, dead_lines):
    for node in nodes:
        if isinstance(node, FunctionDef):
            dead_lines.add(node.location.line)
            collect_dead_function_lines(node.body, dead_lines)
        elif isinstance(node, IfBlock):
            for branch in node.branches:
                collect_dead_function_lines(branch.body, dead_lines)
        elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            collect_dead_function_lines(node.body, dead_lines)
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                collect_dead_function_lines(arm.body, dead_lines)


def collect_dead_if_block_lines(node, dead_lines):
    for branch in node.branches:
        if branch.condition is None:
            return

        truth = condition_truth(branch.condition)
        if truth is True:
            return
        if truth is False:
            collect_dead_function_lines(branch.body, dead_lines)
            continue

        return


def collect_exact_dead_lines(nodes, dead_lines):
    for node in nodes:
        if isinstance(node, IfBlock):
            collect_dead_if_block_lines(node, dead_lines)
            for branch in node.branches:
                collect_exact_dead_lines(branch.body, dead_lines)
        elif isinstance(node, FunctionDef):
            collect_exact_dead_lines(node.body, dead_lines)
        elif isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            collect_exact_dead_lines(node.body, dead_lines)
        elif isinstance(node, CaseBlock):
            for arm in node.arms:
                collect_exact_dead_lines(arm.body, dead_lines)


def possible_function_names_by_line(content):
    active_heredocs = []
    names_by_line = {{}}
    for index, line in enumerate(content.splitlines(), start=1):
        if active_heredocs:
            if is_heredoc_end(line, active_heredocs[0]):
                active_heredocs.pop(0)
            continue

        code_line = remove_comments(
            line,
            ["#"],
            exclusion_patterns=[r"\\#\\!.*"],
            escape_exclusions=False,
        )
        for match in FUNCTION_DECLARATION_PATTERN.finditer(code_line):
            name = match.group(1) or match.group(2)
            if name:
                names_by_line.setdefault(index, set()).add(name)
        if (
            EVAL_COMMAND_PATTERN.search(code_line)
            or TRAP_COMMAND_PATTERN.search(code_line)
            or ALIAS_COMMAND_PATTERN.search(code_line)
        ):
            found_embedded_function = False
            for match in EMBEDDED_FUNCTION_DECLARATION_PATTERN.finditer(code_line):
                name = match.group(1) or match.group(2)
                if name:
                    found_embedded_function = True
                    names_by_line.setdefault(index, set()).add(name)
            if not found_embedded_function:
                names_by_line.setdefault(index, set()).add("*")
        active_heredocs.extend(extract_heredoc_delimiters(line))
    return names_by_line


def main(argv):
    if len(argv) != 2:
        print("modash function scanner expected exactly one path", file=sys.stderr)
        return 2
    path = argv[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return 1
    try:
        ir = LineParserFrontend().parse(path, content)
    except Exception as exc:
        print(f"modash function scanner failed for {{path}}: {{exc}}", file=sys.stderr)
        return 2
    records = set()
    collect_definitions(ir.nodes, "live", records)
    dead_lines = set()
    collect_exact_dead_lines(ir.nodes, dead_lines)
    for line_number, names in possible_function_names_by_line(content).items():
        if line_number in dead_lines:
            continue
        for name in names:
            if not any(record_name == name for _, record_name, _ in records):
                records.add(("unknown", name, line_number))
    for status, name, line_number in sorted(records):
        print(f"{{status}}\\t{{name}}\\t{{line_number}}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


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

    return (
        xtrace_indexes_by_source_index,
        raw_events_by_xtrace_index,
        source_identities_by_source_index,
        source_identities_by_xtrace_index,
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


def _xtrace_source_key(command: _XtraceSourceCommand, invocation: _XtraceSourceInvocation):
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


def _is_dynamic_xtrace_invocation(invocation: _XtraceSourceInvocation):
    return _xtrace_word_is_dynamic(invocation.source_path) or any(
        _xtrace_word_is_dynamic(argument)
        for argument in invocation.arguments
    )


def _xtrace_word_is_dynamic(word: str):
    return "$" in word or "`" in word


def _parse_xtrace_source_invocation(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None
    stripped_words = [_strip_shell_word_quotes(word) for word in words]
    if not stripped_words:
        return None

    normalized_words = _normalized_modash_alias_xtrace_words(stripped_words)
    if normalized_words is not None:
        stripped_words = list(normalized_words)

    return _xtrace_source_invocation_from_words(stripped_words)


def _xtrace_source_invocation_from_words(words):
    index = _xtrace_source_command_index(words)
    if index is None:
        return None
    if index + 1 >= len(words):
        return None
    source_path = words[index + 1]
    if not source_path:
        return None
    return _XtraceSourceInvocation(
        source_path=source_path,
        arguments=tuple(words[index + 2:]),
    )


def _observation_xtrace_command_text(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return command
    stripped_words = [_strip_shell_word_quotes(word) for word in words]
    if not stripped_words:
        return command

    normalized_words = _normalized_modash_alias_xtrace_words(stripped_words)
    if normalized_words is None:
        return command
    return " ".join(_xtrace_shell_quote(word) for word in normalized_words)


def _normalized_modash_alias_xtrace_words(words):
    if words[0] == "__modash_trace_source_alias":
        if len(words) < 5:
            return None
        command_name = "." if words[1] == "dot" or words[2] == "." else "source"
        try:
            separator = words.index("--", 4)
        except ValueError:
            return None
        source_words = words[separator + 1:]
        if not source_words:
            return None
        return (command_name, *source_words)

    if words[0] not in {"__modash_trace_builtin", "__modash_trace_command"}:
        return None
    try:
        separator = words.index("--", 1)
    except ValueError:
        return None
    wrapped_words = words[separator + 1:]
    if not wrapped_words:
        return None
    command_name = "builtin" if words[0] == "__modash_trace_builtin" else "command"
    return (command_name, *wrapped_words)


def _xtrace_shell_quote(value: str):
    if value and all(character.isalnum() or character in "@%_+=:,./-" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _xtrace_source_command_index(words):
    if words[0] in {"source", "."}:
        return 0
    if words[0] == "builtin":
        index = 1
        if index < len(words) and words[index] == "--":
            index += 1
        if index < len(words) and words[index] in {"source", "."}:
            return index
        return None
    if words[0] == "command":
        index = 1
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option == "--":
                index += 1
                break
            if "v" in option[1:] or "V" in option[1:]:
                return None
            if set(option[1:]) != {"p"}:
                return None
            index += 1
        if index < len(words) and words[index] in {"source", "."}:
            return index
        return None
    if words[0] == "__modash_trace_dot_source":
        return 0
    return None


def _strip_shell_word_quotes(word: str):
    return strip_shell_word_quotes(_strip_shell_punctuation(word))


def _strip_shell_punctuation(word: str):
    while word.endswith(";"):
        word = word[:-1]
    return word


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
    command = command.strip()
    if not command:
        return False
    if _parse_xtrace_source_invocation(command) is not None:
        return True
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
        or command == "__modash_trace_source_alias"
        or command.startswith("__modash_trace_source_alias ")
        or command == "__modash_trace_dot_source"
        or command.startswith("__modash_trace_dot_source ")
        or command == "__modash_trace_builtin source"
        or command.startswith("__modash_trace_builtin source ")
        or command == "__modash_trace_builtin ."
        or command.startswith("__modash_trace_builtin . ")
        or command == "__modash_trace_command source"
        or command.startswith("__modash_trace_command source ")
        or command == "__modash_trace_command ."
        or command.startswith("__modash_trace_command . ")
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
PS4=$'+MODASH_XTRACE\x1f${BASHPID}\x1f${PWD}\x1f${BASH_SOURCE[0]}\x1f${LINENO}\x1f${FUNCNAME[0]-}\x1f '

__modash_source_stack=()
declare -A __modash_source_file_map=()
declare -A __modash_function_file_map=()
declare -A __modash_function_metadata_map=()
declare -A __modash_source_positional_mutation_cache=()
__modash_caller_positionals=()
__modash_source_call_args=()
__modash_caller_positionals_captured=0

__modash_trace_abort() {
  local code=$1 message=$2
  {
    printf '%s\n' "$code"
    printf '%s\n' "$message"
  } > "$MODASH_TRACE_FAILURE_FILE"
  printf '%s\n' "$message" >&2
  exit 125
}

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

__modash_source_function_stack() {
  local index name
  __modash_function_stack=()
  for ((index = 2; index < ${#FUNCNAME[@]}; index++)); do
    name=${FUNCNAME[$index]}
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    __modash_function_stack+=("$name")
  done
}

__modash_emit_source_event_with_stack() {
  local index=$1 pid=$2 kind=$3 caller_file=$4 caller_line=$5 cwd=$6 source_path=$7 resolved_path=$8 status=$9
  shift 9
  __modash_source_function_stack
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
    "${#__modash_function_stack[@]}" \
    "${__modash_function_stack[@]}" \
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

__modash_source_file_from_bash_source() {
  local bash_source=${1-}
  local depth=${#__modash_source_stack[@]}

  if [[ -n $bash_source && -n ${__modash_source_file_map[$bash_source]+set} ]]; then
    printf '%s' "${__modash_source_file_map[$bash_source]}"
  elif [[ -n $bash_source && $bash_source == /* ]]; then
    printf '%s' "$bash_source"
  elif [[ -n $bash_source && $bash_source != "$0" ]]; then
    printf '%s/%s' "$MODASH_TRACE_INITIAL_CWD" "$bash_source"
  elif (( depth > 0 )); then
    printf '%s' "${__modash_source_stack[$((depth - 1))]}"
  else
    printf '%s' "$__modash_trace_process_entrypoint"
  fi
}

__modash_current_source_file() {
  local bash_source=${1-} function_name=${2-}
  local mapped_file source_file current_metadata

  if [[ -n $function_name && -n ${__modash_function_file_map[$function_name]+set} ]]; then
    mapped_file=${__modash_function_file_map[$function_name]}
    current_metadata=$(__modash_function_definition_metadata "$function_name" || true)
    if [[ -z $current_metadata || ${__modash_function_metadata_map[$function_name]-} != "$current_metadata" ]]; then
      source_file=$(__modash_source_file_from_bash_source "$bash_source")
      unset "__modash_function_file_map[$function_name]"
      unset "__modash_function_metadata_map[$function_name]"
      printf '%s' "$source_file"
      return
    fi
    printf '%s' "$mapped_file"
    return
  fi

  __modash_source_file_from_bash_source "$bash_source"
}

__modash_function_definition_metadata() {
  local name=$1 definition line file
  definition=$(shopt -s extdebug; declare -F "$name") || return 1
  read -r _ line file <<< "$definition"
  [[ -n $line && -n $file ]] || return 1
  printf '%s\t%s' "$line" "$file"
}

__modash_record_sourced_functions() {
  local resolved_path=$1 name declared_name declared_line current_metadata definition_file definition_line definition_path
  local scanner_status function_records function_record_status function_records_loaded=0
  local -A live_function_map=()
  local -A unknown_function_map=()
  local -A unknown_function_line_map=()

  while IFS= read -r name; do
    [[ -n $name ]] || continue
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    current_metadata=$(__modash_function_definition_metadata "$name") || continue
    definition_line=${current_metadata%%$'\t'*}
    definition_file=${current_metadata#*$'\t'}
    definition_path=$(__modash_current_source_file "$definition_file" "")
    [[ $definition_path == "$resolved_path" ]] || continue
    if ((function_records_loaded == 0)); then
      function_records_loaded=1
      function_records=$("$MODASH_TRACE_PYTHON" "$MODASH_TRACE_FUNCTION_SCANNER" "$resolved_path")
      scanner_status=$?
      case "$scanner_status" in
        0)
          while IFS=$'\t' read -r function_record_status declared_name declared_line; do
            [[ -n $declared_name ]] || continue
            case "$function_record_status" in
              live)
                live_function_map["$declared_name"]=1
                ;;
              unknown)
                if [[ $declared_name == "*" && -n $declared_line ]]; then
                  unknown_function_line_map["$declared_line"]=1
                else
                  unknown_function_map["$declared_name"]=1
                fi
                ;;
            esac
          done <<< "$function_records"
          ;;
        1)
          ;;
        *)
          __modash_trace_abort \
            "runtime.trace.function-scanner" \
            "modash: runtime trace could not scan sourced function definitions: ${resolved_path}"
          ;;
      esac
    fi
    if [[ -n ${unknown_function_map[$name]+set} || -n ${unknown_function_line_map[$definition_line]+set} ]]; then
      __modash_trace_abort \
        "runtime.trace.ambiguous-function-provenance" \
        "modash: runtime trace cannot disambiguate branch-dependent function provenance: ${name} in ${resolved_path}"
    fi
    if [[ -z ${live_function_map[$name]+set} ]]; then
      continue
    fi
    __modash_function_file_map["$name"]=$resolved_path
    __modash_function_metadata_map["$name"]=$current_metadata
  done < <(compgen -A function)
}

__modash_forget_deleted_sourced_functions() {
  local name
  local -n definitions_before_ref=$1

  for name in "${!definitions_before_ref[@]}"; do
    case "$name" in
      __modash_*|source|.)
        continue
        ;;
    esac
    if [[ -n ${__modash_function_file_map[$name]+set} ]] && ! declare -F "$name" >/dev/null; then
      unset "__modash_function_file_map[$name]"
      unset "__modash_function_metadata_map[$name]"
    fi
  done
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

__modash_capture_source_call() {
  local caller_count=${1:-0}
  shift

  __modash_caller_positionals=()
  __modash_source_call_args=()
  __modash_caller_positionals_captured=1

  local index
  for ((index = 0; index < caller_count; index++)); do
    __modash_caller_positionals+=("${1-}")
    shift
  done

  if [[ ${1-} == -- ]]; then
    shift
  fi
  __modash_source_call_args=("$@")
}

__modash_source_may_mutate_positionals() {
  local path=${1-} scanner_status
  if [[ -z $path || ! -r $path ]]; then
    return 1
  fi

  if [[ -n ${__modash_source_positional_mutation_cache[$path]+set} ]]; then
    return "${__modash_source_positional_mutation_cache[$path]}"
  fi

  "$MODASH_TRACE_PYTHON" "$MODASH_TRACE_POSITIONAL_SCANNER" "$path"
  scanner_status=$?
  if ((scanner_status == 0)); then
    __modash_source_positional_mutation_cache["$path"]=0
    return 0
  fi
  if ((scanner_status == 1)); then
    __modash_source_positional_mutation_cache["$path"]=1
    return 1
  fi
  __modash_source_positional_mutation_cache["$path"]=0
  return 0
}

__modash_source_builtin_enabled() {
  case "$1" in
    source)
      enable -p | grep -q '^enable source$'
      ;;
    .)
      enable -p | grep -q '^enable \.$'
      ;;
    *)
      return 1
      ;;
  esac
}

__modash_builtin_source_command_index() {
  __modash_source_command_index=-1
  local index=0
  if [[ ${__modash_source_call_args[0]-} == -- ]]; then
    index=1
  fi
  case "${__modash_source_call_args[$index]-}" in
    source|.)
      __modash_source_command_index=$index
      return 0
      ;;
  esac
  return 1
}

__modash_command_source_command_index() {
  __modash_source_command_index=-1
  local index=0 option letters
  while ((index < ${#__modash_source_call_args[@]})); do
    option=${__modash_source_call_args[$index]}
    if [[ $option == -- ]]; then
      ((index++))
      break
    fi
    if [[ $option != -* ]]; then
      break
    fi
    letters=${option#-}
    if [[ -z $letters || $letters == *v* || $letters == *V* || ${letters//p/} != "" ]]; then
      return 1
    fi
    ((index++))
  done
  case "${__modash_source_call_args[$index]-}" in
    source|.)
      __modash_source_command_index=$index
      return 0
      ;;
  esac
  return 1
}

__modash_trace_source_common() {
  local kind=$1 builtin_name=$2
  shift 2

  local event_index
  event_index=$(__modash_next_source_index)

  local caller_file caller_line cwd source_path resolved_path status source_arg_count track_functions
  local function_name function_definition
  local -a source_args explicit_source_args
  local -A function_definitions_before
  source_args=("$@")
  source_arg_count=${#source_args[@]}
  caller_file=$(__modash_current_source_file "${BASH_SOURCE[2]:-}" "${FUNCNAME[2]:-}")
  caller_line=${BASH_LINENO[1]:-1}
  cwd=$PWD
  source_path=${source_args[0]-}
  resolved_path=$(__modash_resolve_source_path "$source_path")
  track_functions=0

  if ! __modash_source_builtin_enabled "$builtin_name"; then
    __modash_trace_abort \
      "runtime.trace.disabled-source-builtin" \
      "modash: runtime trace cannot observe disabled ${builtin_name} builtin"
  fi

  if ! [[ $caller_line =~ ^[0-9]+$ ]] || ((caller_line < 1)); then
    __modash_trace_abort \
      "runtime.trace.untrusted-call-site" \
      "modash: runtime trace cannot identify a stable source call site for ${source_path}"
  fi

  if ((source_arg_count == 1)); then
    if ((__modash_caller_positionals_captured == 0)); then
      __modash_trace_abort \
        "runtime.trace.nontransparent-source" \
        "modash: runtime trace cannot transparently observe source after its tracing alias was removed: ${source_path}"
    fi
    if __modash_source_may_mutate_positionals "$resolved_path"; then
      __modash_trace_abort \
        "runtime.trace.nontransparent-source-positionals" \
        "modash: runtime trace cannot transparently observe no-argument source of a file that may mutate caller positionals: ${resolved_path}"
    fi
  fi

  if [[ -n $source_path && -r $resolved_path ]]; then
    track_functions=1
    while IFS= read -r function_name; do
      [[ -n $function_name ]] || continue
      function_definition=$(declare -f "$function_name")
      function_definitions_before["$function_name"]=$function_definition
    done < <(compgen -A function)
  fi

  if [[ -n $source_path ]]; then
    __modash_source_file_map["$source_path"]=$resolved_path
    __modash_source_file_map["$resolved_path"]=$resolved_path
    __modash_source_stack+=("$resolved_path")
  fi

  if ((source_arg_count == 1)); then
    if ((${#__modash_caller_positionals[@]} > 0)); then
      builtin "$builtin_name" "$source_path" "${__modash_caller_positionals[@]}"
    else
      set --
      builtin "$builtin_name" "$source_path"
    fi
  else
    builtin "$builtin_name" "${source_args[@]}"
  fi
  status=$?

  if ((track_functions)); then
    __modash_forget_deleted_sourced_functions function_definitions_before
    __modash_record_sourced_functions "$resolved_path"
  fi

  if [[ -n $source_path ]]; then
    unset '__modash_source_stack[-1]'
  fi

  if [[ -n $source_path ]]; then
    if ((source_arg_count > 1)); then
      explicit_source_args=("${source_args[@]:1}")
      __modash_emit_source_event_with_stack \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status" \
        "${explicit_source_args[@]}"
    else
      __modash_emit_source_event_with_stack \
        "$event_index" "$BASHPID" "$kind" "$caller_file" "$caller_line" "$cwd" "$source_path" "$resolved_path" "$status"
    fi
  fi

  return "$status"
}

__modash_trace_source_alias() {
  local kind=$1 builtin_name=$2 caller_count=$3
  shift 3
  __modash_capture_source_call "$caller_count" "$@"
  __modash_trace_source_common "$kind" "$builtin_name" "${__modash_source_call_args[@]}"
}

source() {
  __modash_caller_positionals=()
  __modash_caller_positionals_captured=0
  __modash_trace_source_common source source "$@"
}

__modash_trace_builtin() {
  local caller_count=$1
  shift
  __modash_capture_source_call "$caller_count" "$@"
  local builtin_name
  if __modash_builtin_source_command_index; then
    builtin_name=${__modash_source_call_args[$__modash_source_command_index]}
    case "$builtin_name" in
      source)
        __modash_trace_source_common source source "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
      .)
        __modash_trace_source_common dot . "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
    esac
    return
  fi
  builtin_name=${__modash_source_call_args[0]-}
  case "$builtin_name" in
    source)
      __modash_trace_source_common source source "${__modash_source_call_args[@]:1}"
      ;;
    .)
      __modash_trace_source_common dot . "${__modash_source_call_args[@]:1}"
      ;;
    *)
      builtin "${__modash_source_call_args[@]}"
      ;;
  esac
}

__modash_trace_command() {
  local caller_count=$1
  shift
  __modash_capture_source_call "$caller_count" "$@"
  local command_name
  if __modash_command_source_command_index; then
    command_name=${__modash_source_call_args[$__modash_source_command_index]}
    case "$command_name" in
      source)
        __modash_trace_source_common source source "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
      .)
        __modash_trace_source_common dot . "${__modash_source_call_args[@]:$((__modash_source_command_index + 1))}"
        ;;
    esac
    return
  fi
  command_name=${__modash_source_call_args[0]-}
  case "$command_name" in
    source)
      __modash_trace_source_common source source "${__modash_source_call_args[@]:1}"
      ;;
    .)
      __modash_trace_source_common dot . "${__modash_source_call_args[@]:1}"
      ;;
    *)
      command "${__modash_source_call_args[@]}"
      ;;
  esac
}

alias source='__modash_trace_source_alias source source "$#" "$@" --'
alias .='__modash_trace_source_alias dot . "$#" "$@" --'
alias builtin='__modash_trace_builtin "$#" "$@" --'
alias command='__modash_trace_command "$#" "$@" --'
shopt -s expand_aliases
set -x
'''
