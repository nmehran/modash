from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ExecutionModel(str, Enum):
    PARENT_SOURCE = "parent-source"
    CHILD_SHELL = "child-shell"
    CONTEXT_ONLY = "context-only"
    UNSUPPORTED = "unsupported"


class OccurrenceModel(str, Enum):
    ONCE = "once"
    REPEATED = "repeated"
    CONDITIONAL = "conditional"
    MUTUALLY_EXCLUSIVE = "mutually-exclusive"


@dataclass(frozen=True)
class SourceLocation:
    path: Path
    line: int
    column: int = 1


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: DiagnosticSeverity
    location: SourceLocation
    fragment: str
    message: str
    hint: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StateSnapshot:
    cwd: Path
    variables: dict[str, str] = field(default_factory=dict)
    arrays: dict[str, tuple[str, ...]] = field(default_factory=dict)
    associative_arrays: dict[str, dict[str, str]] = field(default_factory=dict)
    shell_options: frozenset[str] = field(default_factory=frozenset)
    glob_options: frozenset[str] = field(default_factory=frozenset)
    bash_source_stack: tuple[str, ...] = field(default_factory=tuple)
    function_call_stack: tuple[str, ...] = field(default_factory=tuple)
    positional_assignment_generation: int = 0


@dataclass(frozen=True)
class SourceEvent:
    path: Path
    location: SourceLocation
    source_expression: str
    source_site: str
    execution_model: ExecutionModel
    occurrence_model: OccurrenceModel
    replacement_kind: str = "source"
    source_value: str | None = None
    source_arguments: tuple[str, ...] | None = None
    source_argument_words: tuple[str, ...] | None = None
    state_before: StateSnapshot | None = None
    condition: str | None = None
    sync_positionals: bool = False


@dataclass(frozen=True)
class DisabledSourceSite:
    location: SourceLocation
    source_expression: str
    source_site: str
    replacement_kind: str
    condition: str | None = None


@dataclass(frozen=True)
class LineReplacement:
    location: SourceLocation
    old: str
    new: str


@dataclass(frozen=True)
class EvaluationResult:
    events: tuple[SourceEvent, ...]
    disabled_sources: tuple[DisabledSourceSite, ...] = ()
    line_replacements: tuple[LineReplacement, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    final_state: StateSnapshot | None = None


@dataclass(frozen=True)
class IRNode:
    location: SourceLocation
    text: str


@dataclass(frozen=True)
class RawCommand(IRNode):
    separator: str = ""


@dataclass(frozen=True)
class Assignment(IRNode):
    name: str
    value: str
    prefix: str = ""


@dataclass(frozen=True)
class ArrayAssignment(IRNode):
    name: str
    values: tuple[str, ...]
    is_exact: bool = True
    operation: str = "assign"
    index: str | None = None
    associative_values: tuple[tuple[str, str], ...] = ()
    raw_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class CdCommand(IRNode):
    path_expression: str


@dataclass(frozen=True)
class SetCommand(IRNode):
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class FunctionDef(IRNode):
    name: str
    body: tuple[IRNode, ...]


@dataclass(frozen=True)
class ForLoop(IRNode):
    variable: str
    words: tuple[str, ...]
    body: tuple[IRNode, ...]
    words_text: str
    is_exact: bool = True
    end_location: SourceLocation | None = None
    trailing: str = ""


@dataclass(frozen=True)
class CStyleForLoop(IRNode):
    init: str
    condition: str
    update: str
    body: tuple[IRNode, ...]


@dataclass(frozen=True)
class WhileLoop(IRNode):
    keyword: str
    condition: str
    body: tuple[IRNode, ...]
    trailing: str = ""
    end_location: SourceLocation | None = None
    producer: str = ""


@dataclass(frozen=True)
class IfBranch:
    condition: str | None
    body: tuple[IRNode, ...]
    keyword: str
    condition_location: SourceLocation | None = None
    condition_text: str = ""


@dataclass(frozen=True)
class IfBlock(IRNode):
    branches: tuple[IfBranch, ...]
    end_location: SourceLocation | None = None


@dataclass(frozen=True)
class CaseArm:
    patterns: tuple[str, ...]
    body: tuple[IRNode, ...]
    terminator: str = ";;"


@dataclass(frozen=True)
class CaseBlock(IRNode):
    subject: str
    arms: tuple[CaseArm, ...]


@dataclass(frozen=True)
class SourceSite(IRNode):
    command_name: str
    source_expression: str
    source_site: str = ""
    separator: str = ""
    is_control_flow: bool = False
    is_condition_source: bool = False


@dataclass(frozen=True)
class ScriptIR:
    path: Path
    nodes: tuple[IRNode, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def source_sites(self) -> tuple[SourceSite, ...]:
        def collect(nodes):
            sites = []
            for node in nodes:
                if isinstance(node, SourceSite):
                    sites.append(node)
                elif isinstance(node, FunctionDef):
                    sites.extend(collect(node.body))
                elif isinstance(node, ForLoop):
                    sites.extend(collect(node.body))
                elif isinstance(node, CStyleForLoop):
                    sites.extend(collect(node.body))
                elif isinstance(node, WhileLoop):
                    sites.extend(collect(node.body))
                elif isinstance(node, IfBlock):
                    for branch in node.branches:
                        sites.extend(collect(branch.body))
                elif isinstance(node, CaseBlock):
                    for arm in node.arms:
                        sites.extend(collect(arm.body))
            return sites

        return tuple(collect(self.nodes))
