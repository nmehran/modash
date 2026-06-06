from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from methods.runtime_evaluator.graph import RuntimeSourceGraphError
from methods.shell.line import get_commands
from methods.source_commands import source_command_invocation
from methods.source_resolver import (
    UnsupportedSourceError,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

RUNTIME_COMPILER_VERSION = 2
ENTRYPOINT_LOGICAL_PATH = "__entrypoint__"
PROCESS_LOGICAL_PREFIX = "__process_command__"
REPLAY_FAILURE_STATUS = 125

class RuntimeObservedCompileError(RuntimeSourceGraphError):
    def __init__(self, message: str, code: str = "runtime.compile.invalid_graph"):
        super().__init__(message, code=code)

@dataclass(frozen=True)
class _ReplayEdge:
    index: int
    process_index: int
    from_node: str
    to_node: str
    site_file: str
    site_line: int
    site_command: str
    resolved_path: str
    source_entry_status: int
    status: int
    arguments: tuple[str, ...]
    xtrace_command: str

    @property
    def is_file(self) -> bool:
        return self.to_node.startswith("file:")

    @property
    def source_value(self) -> str:
        invocation = source_command_invocation(_first_source_segment(self.xtrace_command) or "")
        if invocation is None:
            return self.resolved_path
        try:
            words = parse_shell_words_preserving_quotes(invocation.source_expression)
        except UnsupportedSourceError:
            return self.resolved_path
        if not words:
            return ""
        return strip_shell_word_quotes(words[0])

@dataclass(frozen=True)
class _SourceCandidate:
    logical_path: str
    physical_path: str | None
    line: int
    text: str
    separator: str
    ordinal: int
    base_id: str
    process_index: int = 0
    status_before: int | None = None
    repeatable: bool = False
    end_line: int | None = None
    physical_lines: tuple[str, ...] = ()

@dataclass
class _RewriteUnit:
    logical_path: str
    physical_path: str | None
    content: str
    process_index: int = 0
    candidates: tuple[_SourceCandidate, ...] = ()
    transformed: str | None = None

@dataclass(frozen=True)
class _EmbeddedFile:
    logical_path: str
    content: str

@dataclass
class _ProcessPlan:
    process_index: int
    edges: tuple[_ReplayEdge, ...]
    units: dict[str, _RewriteUnit]
    candidates: tuple[_SourceCandidate, ...]
    assignments: dict[str, list[_ReplayEdge]] = field(default_factory=dict)

@dataclass
class _CompilePlan:
    entrypoint: Path
    graph: dict
    file_units: dict[str, _RewriteUnit]
    process_plans: dict[int, _ProcessPlan]
    process_payloads: dict[int, tuple[str, str]]

def _first_source_segment(command: str) -> str | None:
    for segment in get_commands(command):
        if source_command_invocation(segment) is not None:
            return segment
    return command if source_command_invocation(command) is not None else None

def _base_id(process_index: int, logical_path: str, line: int, ordinal: int) -> str:
    return f"p{process_index}:{logical_path}:{line}:{ordinal}"

def _validate_logical_path(path: str) -> None:
    pure = PurePosixPath(path)
    if path.startswith("/") or ".." in pure.parts or not path or path.endswith("/"):
        raise RuntimeObservedCompileError(
            f"unsafe embedded logical path: {path!r}",
            code="runtime.compile.unsafe_path",
        )
