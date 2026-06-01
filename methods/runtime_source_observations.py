from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

OBSERVATION_VERSION = 1
TOP_LEVEL_KEYS = frozenset({
    "version",
    "entrypoint",
    "cwd",
    "argv",
    "bash",
    "trace",
    "environment",
    "sources",
})
BASH_KEYS = frozenset({"version"})
TRACE_KEYS = frozenset({"version"})
ENVIRONMENT_KEYS = frozenset({"policy", "recorded_keys"})
SOURCE_EVENT_KEYS = frozenset({"index", "call_site", "resolved_path", "arguments", "status"})
CALL_SITE_KEYS = frozenset({"file", "line", "command"})


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
class RuntimeSourceEvent:
    index: int
    call_site: SourceCallSite
    resolved_path: str
    arguments: tuple[str, ...] = field(default_factory=tuple)
    status: int = 0

    def __post_init__(self):
        object.__setattr__(self, "index", _nonnegative_int(self.index, "sources[].index"))
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
        object.__setattr__(self, "status", _nonnegative_int(self.status, "sources[].status"))

    @classmethod
    def from_dict(cls, data):
        _require_keys(data, SOURCE_EVENT_KEYS, "source event")
        return cls(
            index=data["index"],
            call_site=SourceCallSite.from_dict(data["call_site"]),
            resolved_path=data["resolved_path"],
            arguments=_string_list(data["arguments"], "sources[].arguments"),
            status=data["status"],
        )

    def to_dict(self):
        return {
            "index": self.index,
            "call_site": self.call_site.to_dict(),
            "resolved_path": self.resolved_path,
            "arguments": list(self.arguments),
            "status": self.status,
        }


@dataclass(frozen=True)
class RuntimeSourceObservation:
    entrypoint: str
    cwd: str
    argv: tuple[str, ...]
    bash: BashInfo
    trace: TraceInfo
    environment: EnvironmentInfo
    sources: tuple[RuntimeSourceEvent, ...] = field(default_factory=tuple)
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
        sources = tuple(_sequence(self.sources, "sources"))
        for expected_index, event in enumerate(sources):
            if not isinstance(event, RuntimeSourceEvent):
                raise _schema_error("sources must contain RuntimeSourceEvent values")
            if event.index != expected_index:
                raise _schema_error("sources must be indexed contiguously from 0")
        object.__setattr__(self, "sources", sources)

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
            sources=tuple(RuntimeSourceEvent.from_dict(event) for event in _object_list(data["sources"], "sources")),
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
            "sources": [event.to_dict() for event in self.sources],
        }


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


def _integer(value, label: str):
    if not isinstance(value, int) or isinstance(value, bool):
        raise _schema_error(f"{label} must be an integer")
    return value


def _schema_error(message: str):
    return RuntimeSourceObservationError(message)
