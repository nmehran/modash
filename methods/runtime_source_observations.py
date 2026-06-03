from __future__ import annotations

import json
import os
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path

OBSERVATION_VERSION = 8
TOP_LEVEL_KEYS = frozenset({
    "version",
    "entrypoint",
    "cwd",
    "argv",
    "bash",
    "trace",
    "environment",
    "run",
    "processes",
    "sources",
    "xtrace",
    "files",
})
BASH_KEYS = frozenset({"version"})
TRACE_KEYS = frozenset({"version"})
ENVIRONMENT_KEYS = frozenset({"policy", "recorded_keys"})
RUN_KEYS = frozenset({
    "observed_at_utc",
    "modash_version",
    "platform",
    "python_version",
    "shell",
    "target_status",
    "timeout_seconds",
})
PROCESS_KEYS = frozenset({
    "index",
    "pid",
    "parent_index",
    "parent_pid",
    "entrypoint",
    "cwd",
    "argv",
    "command",
})
SOURCE_EVENT_KEYS = frozenset({
    "index",
    "source_identity",
    "process_index",
    "call_site",
    "function_stack",
    "function_call",
    "xtrace_index",
    "resolved_path",
    "arguments",
    "status",
})
CALL_SITE_KEYS = frozenset({"file", "line", "command"})
FUNCTION_CALL_KEYS = frozenset({"file", "line", "function", "command", "arguments"})
XTRACE_SOURCE_KEYS = frozenset({
    "index",
    "source_identity",
    "process_index",
    "file",
    "line",
    "function",
    "cwd",
    "command",
})
FILE_FINGERPRINT_KEYS = frozenset({"path", "size", "mtime_ns", "sha256", "roles"})
FILE_FINGERPRINT_ROLE_ORDER = ("entrypoint", "call-site", "source")
FILE_FINGERPRINT_ROLES = frozenset(FILE_FINGERPRINT_ROLE_ORDER)
SHA256_HEX_LENGTH = 64
FINGERPRINT_CHUNK_SIZE = 1024 * 1024


class RuntimeSourceObservationError(ValueError):
    def __init__(self, message: str, code: str = "runtime.observation.invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BashInfo:
    version: str

    def __post_init__(self):
        object.__setattr__(self, "version", _nonempty_string(self.version, "bash.version"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, BASH_KEYS, "bash")
        return cls(version=data["version"])

    def to_dict(self):
        return {
            "version": self.version,
        }


@dataclass(frozen=True)
class TraceInfo:
    version: str

    def __post_init__(self):
        object.__setattr__(self, "version", _nonempty_string(self.version, "trace.version"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, TRACE_KEYS, "trace")
        return cls(version=data["version"])

    def to_dict(self):
        return {
            "version": self.version,
        }


@dataclass(frozen=True)
class EnvironmentInfo:
    policy: str
    recorded_keys: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "policy", _nonempty_string(self.policy, "environment.policy"))
        keys = tuple(sorted({
            _environment_key(key, "environment.recorded_keys")
            for key in _sequence(self.recorded_keys, "environment.recorded_keys")
        }))
        object.__setattr__(self, "recorded_keys", keys)

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, ENVIRONMENT_KEYS, "environment")
        return cls(
            policy=data["policy"],
            recorded_keys=_string_list(data["recorded_keys"], "environment.recorded_keys"),
        )

    def to_dict(self):
        return {
            "policy": self.policy,
            "recorded_keys": list(self.recorded_keys),
        }


@dataclass(frozen=True)
class RuntimeRunInfo:
    observed_at_utc: str = "unknown"
    modash_version: str = "unknown"
    platform: str = "unknown"
    python_version: str = "unknown"
    shell: str = "bash"
    target_status: int = 0
    timeout_seconds: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "observed_at_utc", _nonempty_string(self.observed_at_utc, "run.observed_at_utc"))
        object.__setattr__(self, "modash_version", _nonempty_string(self.modash_version, "run.modash_version"))
        object.__setattr__(self, "platform", _nonempty_string(self.platform, "run.platform"))
        object.__setattr__(self, "python_version", _nonempty_string(self.python_version, "run.python_version"))
        object.__setattr__(self, "shell", _nonempty_string(self.shell, "run.shell"))
        object.__setattr__(self, "target_status", _integer(self.target_status, "run.target_status"))
        object.__setattr__(
            self,
            "timeout_seconds",
            _optional_positive_number(self.timeout_seconds, "run.timeout_seconds"),
        )

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, RUN_KEYS, "run")
        return cls(
            observed_at_utc=data["observed_at_utc"],
            modash_version=data["modash_version"],
            platform=data["platform"],
            python_version=data["python_version"],
            shell=data["shell"],
            target_status=data["target_status"],
            timeout_seconds=data["timeout_seconds"],
        )

    def to_dict(self):
        return {
            "observed_at_utc": self.observed_at_utc,
            "modash_version": self.modash_version,
            "platform": self.platform,
            "python_version": self.python_version,
            "shell": self.shell,
            "target_status": self.target_status,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class RuntimeFileFingerprint:
    path: str
    size: int
    mtime_ns: int
    sha256: str
    roles: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "path", _absolute_path(self.path, "files[].path"))
        object.__setattr__(self, "size", _nonnegative_int(self.size, "files[].size"))
        object.__setattr__(self, "mtime_ns", _nonnegative_int(self.mtime_ns, "files[].mtime_ns"))
        object.__setattr__(self, "sha256", _sha256_hex(self.sha256, "files[].sha256"))
        role_set = {
            _file_role(role, "files[].roles")
            for role in _sequence(self.roles, "files[].roles")
        }
        roles = tuple(role for role in FILE_FINGERPRINT_ROLE_ORDER if role in role_set)
        if not roles:
            raise _schema_error("files[].roles must contain at least one role")
        object.__setattr__(self, "roles", roles)

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, FILE_FINGERPRINT_KEYS, "file fingerprint")
        return cls(
            path=data["path"],
            size=data["size"],
            mtime_ns=data["mtime_ns"],
            sha256=data["sha256"],
            roles=_string_list(data["roles"], "files[].roles"),
        )

    def to_dict(self):
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "roles": list(self.roles),
        }


@dataclass(frozen=True)
class SourceCallSite:
    file: str
    line: int
    command: str

    def __post_init__(self):
        object.__setattr__(self, "file", _absolute_path(self.file, "call_site.file"))
        object.__setattr__(self, "line", _positive_int(self.line, "call_site.line"))
        object.__setattr__(self, "command", _nonempty_string(self.command, "call_site.command"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, CALL_SITE_KEYS, "call_site")
        return cls(
            file=data["file"],
            line=data["line"],
            command=data["command"],
        )

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "command": self.command,
        }


@dataclass(frozen=True)
class RuntimeProcess:
    index: int
    pid: int
    parent_pid: int
    entrypoint: str
    cwd: str
    argv: tuple[str, ...] = field(default_factory=tuple)
    command: str = ""
    parent_index: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "index", _nonnegative_int(self.index, "processes[].index"))
        object.__setattr__(self, "pid", _positive_int(self.pid, "processes[].pid"))
        object.__setattr__(self, "parent_pid", _nonnegative_int(self.parent_pid, "processes[].parent_pid"))
        if self.parent_index is not None:
            object.__setattr__(
                self,
                "parent_index",
                _nonnegative_int(self.parent_index, "processes[].parent_index"),
            )
        object.__setattr__(self, "entrypoint", _absolute_path(self.entrypoint, "processes[].entrypoint"))
        object.__setattr__(self, "cwd", _absolute_path(self.cwd, "processes[].cwd"))
        object.__setattr__(
            self,
            "argv",
            tuple(_exact_string(arg, "processes[].argv") for arg in _sequence(self.argv, "processes[].argv")),
        )
        command = self.command if self.command else self.entrypoint
        object.__setattr__(self, "command", _nonempty_string(command, "processes[].command"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, PROCESS_KEYS, "process")
        return cls(
            index=data["index"],
            pid=data["pid"],
            parent_index=data["parent_index"],
            parent_pid=data["parent_pid"],
            entrypoint=data["entrypoint"],
            cwd=data["cwd"],
            argv=_string_list(data["argv"], "processes[].argv"),
            command=data["command"],
        )

    def to_dict(self):
        return {
            "index": self.index,
            "pid": self.pid,
            "parent_index": self.parent_index,
            "parent_pid": self.parent_pid,
            "entrypoint": self.entrypoint,
            "cwd": self.cwd,
            "argv": list(self.argv),
            "command": self.command,
        }


@dataclass(frozen=True)
class RuntimeSourceEvent:
    index: int
    call_site: SourceCallSite
    resolved_path: str
    source_identity: str = ""
    arguments: tuple[str, ...] = field(default_factory=tuple)
    function_stack: tuple[str, ...] = field(default_factory=tuple)
    function_call: RuntimeFunctionCall | None = None
    status: int = 0
    process_index: int = 0
    xtrace_index: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "index", _nonnegative_int(self.index, "sources[].index"))
        object.__setattr__(
            self,
            "source_identity",
            _exact_string(self.source_identity, "sources[].source_identity"),
        )
        object.__setattr__(
            self,
            "process_index",
            _nonnegative_int(self.process_index, "sources[].process_index"),
        )
        if self.xtrace_index is not None:
            object.__setattr__(
                self,
                "xtrace_index",
                _nonnegative_int(self.xtrace_index, "sources[].xtrace_index"),
            )
        if not isinstance(self.call_site, SourceCallSite):
            raise _schema_error("sources[].call_site must be a SourceCallSite")
        object.__setattr__(self, "resolved_path", _absolute_path(self.resolved_path, "sources[].resolved_path"))
        object.__setattr__(
            self,
            "arguments",
            tuple(
                _exact_string(arg, "sources[].arguments")
                for arg in _sequence(self.arguments, "sources[].arguments")
            ),
        )
        object.__setattr__(
            self,
            "function_stack",
            tuple(
                _exact_string(name, "sources[].function_stack")
                for name in _sequence(self.function_stack, "sources[].function_stack")
            ),
        )
        if self.function_call is not None and not isinstance(self.function_call, RuntimeFunctionCall):
            raise _schema_error("sources[].function_call must be null or a RuntimeFunctionCall")
        if self.function_call is not None and self.function_call.function not in self.function_stack:
            raise _schema_error("sources[].function_call.function must be present in sources[].function_stack")
        object.__setattr__(self, "status", _nonnegative_int(self.status, "sources[].status"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, SOURCE_EVENT_KEYS, "source event")
        return cls(
            index=data["index"],
            source_identity=data["source_identity"],
            process_index=data["process_index"],
            xtrace_index=data["xtrace_index"],
            call_site=SourceCallSite.from_dict(data["call_site"]),
            function_stack=_string_list(data["function_stack"], "sources[].function_stack"),
            function_call=(
                RuntimeFunctionCall.from_dict(data["function_call"])
                if data["function_call"] is not None
                else None
            ),
            resolved_path=data["resolved_path"],
            arguments=_string_list(data["arguments"], "sources[].arguments"),
            status=data["status"],
        )

    def to_dict(self):
        return {
            "index": self.index,
            "source_identity": self.source_identity,
            "process_index": self.process_index,
            "xtrace_index": self.xtrace_index,
            "call_site": self.call_site.to_dict(),
            "function_stack": list(self.function_stack),
            "function_call": self.function_call.to_dict() if self.function_call is not None else None,
            "resolved_path": self.resolved_path,
            "arguments": list(self.arguments),
            "status": self.status,
        }


@dataclass(frozen=True)
class RuntimeFunctionCall:
    file: str
    line: int
    function: str
    command: str
    arguments: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "file", _absolute_path(self.file, "sources[].function_call.file"))
        object.__setattr__(self, "line", _positive_int(self.line, "sources[].function_call.line"))
        object.__setattr__(self, "function", _nonempty_string(self.function, "sources[].function_call.function"))
        object.__setattr__(self, "command", _nonempty_string(self.command, "sources[].function_call.command"))
        object.__setattr__(
            self,
            "arguments",
            tuple(
                _exact_string(arg, "sources[].function_call.arguments")
                for arg in _sequence(self.arguments, "sources[].function_call.arguments")
            ),
        )

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, FUNCTION_CALL_KEYS, "function call")
        return cls(
            file=data["file"],
            line=data["line"],
            function=data["function"],
            command=data["command"],
            arguments=_string_list(data["arguments"], "sources[].function_call.arguments"),
        )

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "command": self.command,
            "arguments": list(self.arguments),
        }


@dataclass(frozen=True)
class RuntimeXtraceSourceCommand:
    index: int
    source_identity: str
    process_index: int
    file: str
    line: int
    function: str
    cwd: str
    command: str

    def __post_init__(self):
        object.__setattr__(self, "index", _nonnegative_int(self.index, "xtrace[].index"))
        object.__setattr__(
            self,
            "source_identity",
            _nonempty_string(self.source_identity, "xtrace[].source_identity"),
        )
        object.__setattr__(
            self,
            "process_index",
            _nonnegative_int(self.process_index, "xtrace[].process_index"),
        )
        object.__setattr__(self, "file", _nonempty_string(self.file, "xtrace[].file"))
        object.__setattr__(self, "line", _positive_int(self.line, "xtrace[].line"))
        object.__setattr__(self, "function", _exact_string(self.function, "xtrace[].function"))
        object.__setattr__(self, "cwd", _absolute_path(self.cwd, "xtrace[].cwd"))
        object.__setattr__(self, "command", _nonempty_string(self.command, "xtrace[].command"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, XTRACE_SOURCE_KEYS, "xtrace source command")
        return cls(
            index=data["index"],
            source_identity=data["source_identity"],
            process_index=data["process_index"],
            file=data["file"],
            line=data["line"],
            function=data["function"],
            cwd=data["cwd"],
            command=data["command"],
        )

    def to_dict(self):
        return {
            "index": self.index,
            "source_identity": self.source_identity,
            "process_index": self.process_index,
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "cwd": self.cwd,
            "command": self.command,
        }


@dataclass(frozen=True)
class RuntimeSourceObservation:
    entrypoint: str
    cwd: str
    argv: tuple[str, ...]
    bash: BashInfo
    trace: TraceInfo
    environment: EnvironmentInfo
    processes: tuple[RuntimeProcess, ...]
    run: RuntimeRunInfo = field(default_factory=RuntimeRunInfo)
    sources: tuple[RuntimeSourceEvent, ...] = field(default_factory=tuple)
    xtrace: tuple[RuntimeXtraceSourceCommand, ...] = field(default_factory=tuple)
    files: tuple[RuntimeFileFingerprint, ...] = field(default_factory=tuple)
    version: int = OBSERVATION_VERSION

    def __post_init__(self):
        if self.version != OBSERVATION_VERSION:
            raise _schema_error(f"runtime source observation version must be {OBSERVATION_VERSION}")
        object.__setattr__(self, "entrypoint", _absolute_path(self.entrypoint, "entrypoint"))
        object.__setattr__(self, "cwd", _absolute_path(self.cwd, "cwd"))
        object.__setattr__(
            self,
            "argv",
            tuple(_exact_string(arg, "argv") for arg in _sequence(self.argv, "argv")),
        )
        if not isinstance(self.bash, BashInfo):
            raise _schema_error("bash must be a BashInfo")
        if not isinstance(self.trace, TraceInfo):
            raise _schema_error("trace must be a TraceInfo")
        if not isinstance(self.environment, EnvironmentInfo):
            raise _schema_error("environment must be an EnvironmentInfo")
        if not isinstance(self.run, RuntimeRunInfo):
            raise _schema_error("run must be a RuntimeRunInfo")
        processes = tuple(_sequence(self.processes, "processes"))
        if not processes:
            raise _schema_error("processes must contain at least one process")
        for expected_index, process in enumerate(processes):
            if not isinstance(process, RuntimeProcess):
                raise _schema_error("processes must contain RuntimeProcess values")
            if process.index != expected_index:
                raise _schema_error("processes must be indexed contiguously from 0")
            if process.parent_index is not None and process.parent_index >= len(processes):
                raise _schema_error("process parent_index must reference an existing process")
        object.__setattr__(self, "processes", processes)
        sources = tuple(_sequence(self.sources, "sources"))
        for expected_index, event in enumerate(sources):
            if not isinstance(event, RuntimeSourceEvent):
                raise _schema_error("sources must contain RuntimeSourceEvent values")
            if event.index != expected_index:
                raise _schema_error("sources must be indexed contiguously from 0")
            if event.process_index >= len(processes):
                raise _schema_error("sources[].process_index must reference an existing process")
        object.__setattr__(self, "sources", sources)
        xtrace = tuple(_sequence(self.xtrace, "xtrace"))
        for expected_index, command in enumerate(xtrace):
            if not isinstance(command, RuntimeXtraceSourceCommand):
                raise _schema_error("xtrace must contain RuntimeXtraceSourceCommand values")
            if command.index != expected_index:
                raise _schema_error("xtrace must be indexed contiguously from 0")
            if command.process_index >= len(processes):
                raise _schema_error("xtrace[].process_index must reference an existing process")
        _validate_xtrace_links(sources, xtrace)
        object.__setattr__(self, "xtrace", xtrace)
        files = tuple(_sequence(self.files, "files"))
        if not files:
            raise _schema_error("files must contain at least one file fingerprint")
        seen_paths = set()
        for fingerprint in files:
            if not isinstance(fingerprint, RuntimeFileFingerprint):
                raise _schema_error("files must contain RuntimeFileFingerprint values")
            if fingerprint.path in seen_paths:
                raise _schema_error("files[].path values must be unique")
            seen_paths.add(fingerprint.path)
        files = tuple(sorted(files, key=lambda item: item.path))
        _validate_fingerprint_coverage(self.entrypoint, processes, sources, files)
        object.__setattr__(self, "files", files)

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, TOP_LEVEL_KEYS, "runtime source observation")
        if data["version"] != OBSERVATION_VERSION:
            raise _schema_error(f"runtime source observation version must be {OBSERVATION_VERSION}")
        return cls(
            version=data["version"],
            entrypoint=data["entrypoint"],
            cwd=data["cwd"],
            argv=_string_list(data["argv"], "argv"),
            bash=BashInfo.from_dict(data["bash"]),
            trace=TraceInfo.from_dict(data["trace"]),
            environment=EnvironmentInfo.from_dict(data["environment"]),
            run=RuntimeRunInfo.from_dict(data["run"]),
            processes=tuple(RuntimeProcess.from_dict(process) for process in _object_list(data["processes"], "processes")),
            sources=tuple(RuntimeSourceEvent.from_dict(event) for event in _object_list(data["sources"], "sources")),
            xtrace=tuple(
                RuntimeXtraceSourceCommand.from_dict(command)
                for command in _object_list(data["xtrace"], "xtrace")
            ),
            files=tuple(RuntimeFileFingerprint.from_dict(fingerprint) for fingerprint in _object_list(data["files"], "files")),
        )

    def to_dict(self):
        return {
            "version": self.version,
            "entrypoint": self.entrypoint,
            "cwd": self.cwd,
            "argv": list(self.argv),
            "bash": self.bash.to_dict(),
            "trace": self.trace.to_dict(),
            "environment": self.environment.to_dict(),
            "run": self.run.to_dict(),
            "processes": [process.to_dict() for process in self.processes],
            "sources": [event.to_dict() for event in self.sources],
            "xtrace": [command.to_dict() for command in self.xtrace],
            "files": [fingerprint.to_dict() for fingerprint in self.files],
        }


def fingerprint_file(path: str | os.PathLike, roles):
    candidate = Path(path).resolve(strict=False)
    try:
        stat_result = candidate.stat()
    except OSError as exc:
        raise RuntimeSourceObservationError(
            f"unable to fingerprint runtime observation file: {candidate}: {exc}",
            code="runtime.observation.fingerprint_failed",
        ) from exc
    if not candidate.is_file():
        raise RuntimeSourceObservationError(
            f"runtime observation fingerprint target is not a file: {candidate}",
            code="runtime.observation.fingerprint_failed",
        )
    return RuntimeFileFingerprint(
        path=str(candidate),
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        sha256=_file_sha256(candidate),
        roles=tuple(roles),
    )


def current_fingerprint_mismatch(fingerprint: RuntimeFileFingerprint):
    details = current_fingerprint_mismatch_details(fingerprint)
    return None if details is None else details["field"]


def current_fingerprint_mismatch_details(fingerprint: RuntimeFileFingerprint):
    if not isinstance(fingerprint, RuntimeFileFingerprint):
        raise _schema_error("fingerprint must be a RuntimeFileFingerprint")
    path = Path(fingerprint.path)
    try:
        current = fingerprint_file(path, fingerprint.roles)
    except RuntimeSourceObservationError:
        return {
            "field": "missing",
            "expected": fingerprint.to_dict(),
            "current": None,
        }
    if current.size != fingerprint.size:
        return _fingerprint_field_mismatch("size", fingerprint, current)
    if current.mtime_ns != fingerprint.mtime_ns:
        return _fingerprint_field_mismatch("mtime_ns", fingerprint, current)
    if current.sha256 != fingerprint.sha256:
        return _fingerprint_field_mismatch("sha256", fingerprint, current)
    return None


def format_fingerprint_mismatch(subject: str, fingerprint: RuntimeFileFingerprint, details=None):
    details = details or current_fingerprint_mismatch_details(fingerprint)
    if details is None:
        return f"{subject} is current for {fingerprint.path}"
    role_text = ",".join(fingerprint.roles)
    field = details["field"]
    if field == "missing":
        return (
            f"{subject} is stale for {fingerprint.path} "
            f"(roles={role_text}): file is missing; "
            f"expected size={fingerprint.size} "
            f"mtime_ns={fingerprint.mtime_ns} "
            f"sha256={fingerprint.sha256}"
        )
    return (
        f"{subject} is stale for {fingerprint.path} "
        f"(roles={role_text}): {field} mismatch; "
        f"expected {field}={details['expected'][field]}; "
        f"current {field}={details['current'][field]}"
    )


def _fingerprint_field_mismatch(field: str, expected: RuntimeFileFingerprint, current: RuntimeFileFingerprint):
    return {
        "field": field,
        "expected": expected.to_dict(),
        "current": current.to_dict(),
    }


def _validate_fingerprint_coverage(entrypoint: str, processes, sources, files):
    roles_by_path = {fingerprint.path: set(fingerprint.roles) for fingerprint in files}
    _require_fingerprint_role(roles_by_path, entrypoint, "entrypoint", "entrypoint")
    for event in sources:
        if event.status == 0:
            _require_fingerprint_role(
                roles_by_path,
                event.resolved_path,
                "source",
                "sources[].resolved_path",
            )
        if not _is_process_command_call_site(event, processes):
            _require_fingerprint_role(
                roles_by_path,
                event.call_site.file,
                "call-site",
                "sources[].call_site.file",
            )
        if event.function_call is not None:
            _require_fingerprint_role(
                roles_by_path,
                event.function_call.file,
                "call-site",
                "sources[].function_call.file",
            )


def _validate_xtrace_links(sources, xtrace):
    if not xtrace:
        if any(event.xtrace_index is not None for event in sources):
            raise _schema_error("sources[].xtrace_index must be null when xtrace is empty")
        return

    referenced = []
    for event in sources:
        if event.xtrace_index is None:
            raise _schema_error("sources[].xtrace_index is required when xtrace provenance is present")
        if event.xtrace_index >= len(xtrace):
            raise _schema_error("sources[].xtrace_index must reference an existing xtrace source command")
        command = xtrace[event.xtrace_index]
        if command.process_index != event.process_index:
            raise _schema_error("sources[].xtrace_index must reference the same process")
        if not event.source_identity:
            raise _schema_error("sources[].source_identity is required when xtrace provenance is present")
        if event.source_identity != command.source_identity:
            raise _schema_error("sources[].source_identity must match linked xtrace source command")
        referenced.append(event.xtrace_index)

    if sorted(referenced) != list(range(len(xtrace))):
        raise _schema_error("xtrace source commands must be referenced exactly once by sources")


def _is_process_command_call_site(event: RuntimeSourceEvent, processes):
    process = processes[event.process_index]
    return event.call_site.file == process.entrypoint and process.command != process.entrypoint


def _require_fingerprint_role(roles_by_path, path: str, role: str, label: str):
    roles = roles_by_path.get(path)
    if role not in (roles or ()):
        raise _schema_error(f"{label} must have a file fingerprint with role {role}")


def validate_observation(data):
    if not isinstance(data, dict):
        raise _schema_error("runtime source observation must be a JSON object")
    return RuntimeSourceObservation.from_dict(data)


def load_observation(path: str | os.PathLike):
    observation_path = Path(path)
    if not observation_path.is_file():
        raise RuntimeSourceObservationError(
            f"runtime source observation file does not exist: {observation_path}",
            code="runtime.observation.missing",
        )

    try:
        data = json.loads(observation_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeSourceObservationError(
            f"invalid runtime source observation JSON: {observation_path}: {exc}",
            code="runtime.observation.invalid_json",
        ) from exc
    return validate_observation(data)


def write_observation(path: str | os.PathLike, observation: RuntimeSourceObservation):
    observation = _coerce_observation(observation)
    observation_path = Path(path)
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(json.dumps(observation.to_dict(), indent=2) + "\n", encoding="utf-8")
    return observation_path


def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)


def _require_keys(data, expected_keys, label: str):
    if not isinstance(data, dict):
        raise _schema_error(f"{label} must be an object")

    missing = sorted(expected_keys - set(data))
    if missing:
        raise _schema_error(f"{label} missing required keys: {', '.join(missing)}")

    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise _schema_error(f"{label} has unknown keys: {', '.join(unknown)}")


def _object_list(value, label: str):
    if not isinstance(value, list):
        raise _schema_error(f"{label} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _schema_error(f"{label}[{index}] must be an object")
    return value


def _string_list(value, label: str):
    if not isinstance(value, list):
        raise _schema_error(f"{label} must be a list")
    return tuple(_exact_string(item, f"{label}[]") for item in value)


def _sequence(value, label: str):
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise _schema_error(f"{label} must be a sequence")
    return tuple(value)


def _absolute_path(value, label: str):
    value = _nonempty_string(value, label)
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        raise _schema_error(f"{label} must be an absolute path")
    return str(candidate.resolve(strict=False))


def _environment_key(value, label: str):
    value = _nonempty_string(value, label)
    if "=" in value:
        raise _schema_error(f"{label} values must not contain '='")
    return value


def _file_role(value, label: str):
    value = _nonempty_string(value, label)
    if value not in FILE_FINGERPRINT_ROLES:
        raise _schema_error(f"{label} contains unsupported role: {value}")
    return value


def _sha256_hex(value, label: str):
    value = _nonempty_string(value, label)
    if value != value.lower():
        raise _schema_error(f"{label} must be a lowercase SHA-256 hex digest")
    if len(value) != SHA256_HEX_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise _schema_error(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FINGERPRINT_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_string(value, label: str):
    if not isinstance(value, str):
        raise _schema_error(f"{label} values must be strings")
    if "\0" in value:
        raise _schema_error(f"{label} values must not contain NUL bytes")
    return value


def _nonempty_string(value, label: str):
    value = _exact_string(value, label)
    if not value:
        raise _schema_error(f"{label} must not be empty")
    return value


def _positive_int(value, label: str):
    value = _integer(value, label)
    if value < 1:
        raise _schema_error(f"{label} must be greater than 0")
    return value


def _nonnegative_int(value, label: str):
    value = _integer(value, label)
    if value < 0:
        raise _schema_error(f"{label} must be greater than or equal to 0")
    return value


def _optional_positive_number(value, label: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _schema_error(f"{label} must be a positive number or null")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise _schema_error(f"{label} must be a positive number or null")
    return float(value)


def _integer(value, label: str):
    if not isinstance(value, int) or isinstance(value, bool):
        raise _schema_error(f"{label} must be an integer")
    return value


def _schema_error(message: str):
    return RuntimeSourceObservationError(message)
