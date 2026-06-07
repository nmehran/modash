from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from methods.source_effects import FunctionDef, OccurrenceModel, RawCommand, SourceLocation, StateSnapshot
from methods.source_resolver import ResolvedSource


@dataclass
class EvaluationState:
    cwd: Path
    variables: dict[str, str] = field(default_factory=dict)
    runtime_variables: dict[str, str] = field(default_factory=dict)
    arrays: dict[str, tuple[str, ...]] = field(default_factory=dict)
    associative_arrays: dict[str, dict[str, str]] = field(default_factory=dict)
    functions: dict[str, FunctionDef] = field(default_factory=dict)
    function_variants: dict[str, tuple[FunctionDef, ...]] = field(default_factory=dict)
    shell_options: set[str] = field(default_factory=set)
    glob_options: set[str] = field(default_factory=set)
    missing_source_words: set[str] = field(default_factory=set)
    bash_source_stack: tuple[Path, ...] = ()
    occurrence_context: OccurrenceModel = OccurrenceModel.ONCE
    condition_context: str | None = None
    ambiguous_cwd: bool = False
    ambiguous_variables: set[str] = field(default_factory=set)
    ambiguous_arrays: set[str] = field(default_factory=set)
    ambiguous_functions: set[str] = field(default_factory=set)
    ambiguous_shell_options: bool = False
    ambiguous_glob_options: bool = False
    function_call_stack: tuple[str, ...] = ()
    positional_arguments: tuple[str, ...] = ()
    ambiguous_positionals: bool = False
    positional_assignment_generation: int = 0
    source_argument_frame_dirty_stack: tuple[bool, ...] = ()
    local_scopes: list[dict[str, tuple[bool, str | None, bool, str | None, bool]]] = field(default_factory=list)
    last_status: int | None = 0
    loop_depth: int = 0
    source_depth: int = 0
    function_body_depth: int = 0

    def set_positionals(
        self,
        arguments: tuple[str, ...],
        *,
        source_argument_escape: bool = False,
        mark_source_argument_frame: bool = True,
    ):
        previous_names = self._positive_positional_variable_names()
        current_names = {str(index) for index in range(1, len(arguments) + 1)}
        for index, argument in enumerate(arguments, start=1):
            name = str(index)
            self.variables[name] = argument
            self.runtime_variables[name] = argument
            self.ambiguous_variables.discard(name)
        for name in previous_names - current_names:
            self.variables.pop(name, None)
            self.runtime_variables.pop(name, None)
            self.ambiguous_variables.discard(name)
        self.positional_arguments = tuple(arguments)
        self.ambiguous_positionals = False
        self._record_source_argument_escape(
            source_argument_escape=source_argument_escape,
            mark_source_argument_frame=mark_source_argument_frame,
        )

    def mark_positionals_ambiguous(
        self,
        *,
        source_argument_escape: bool = False,
        mark_source_argument_frame: bool = True,
    ):
        for name in self._positive_positional_variable_names():
            self.variables.pop(name, None)
            self.runtime_variables.pop(name, None)
            self.ambiguous_variables.discard(name)
        self.positional_arguments = ()
        self.ambiguous_positionals = True
        self._record_source_argument_escape(
            source_argument_escape=source_argument_escape,
            mark_source_argument_frame=mark_source_argument_frame,
        )

    def push_source_argument_frame(self):
        self.source_argument_frame_dirty_stack = (*self.source_argument_frame_dirty_stack, False)

    def pop_source_argument_frame(self):
        dirty = self.source_argument_frame_dirty_stack[-1]
        self.source_argument_frame_dirty_stack = self.source_argument_frame_dirty_stack[:-1]
        return dirty

    def mark_current_source_argument_frame_dirty(self):
        if not self.source_argument_frame_dirty_stack:
            return
        stack = list(self.source_argument_frame_dirty_stack)
        stack[-1] = True
        self.source_argument_frame_dirty_stack = tuple(stack)

    def clear_current_source_argument_frame_dirty(self):
        if not self.source_argument_frame_dirty_stack:
            return
        stack = list(self.source_argument_frame_dirty_stack)
        stack[-1] = False
        self.source_argument_frame_dirty_stack = tuple(stack)

    def _record_source_argument_escape(
        self,
        *,
        source_argument_escape: bool,
        mark_source_argument_frame: bool,
    ):
        if not source_argument_escape:
            return
        self.positional_assignment_generation += 1
        if mark_source_argument_frame:
            self.mark_current_source_argument_frame_dirty()

    def _positive_positional_variable_names(self):
        return {
            name
            for mapping in (self.variables, self.runtime_variables)
            for name in mapping
            if name.isdigit() and int(name) > 0
        }

    def resolver_context(self):
        return {
            'vars': self.variables,
            'runtime_vars': self.runtime_variables,
            'current_directory': str(self.cwd),
            'shell_options': self.shell_options,
            'glob_options': self.glob_options,
            'missing_source_words': self.missing_source_words,
        }

    def runtime_context(self):
        return {
            'vars': self.runtime_variables,
            'current_directory': str(self.cwd),
            'shell_options': self.shell_options,
            'glob_options': self.glob_options,
            'missing_source_words': self.missing_source_words,
        }

    def snapshot(self):
        return StateSnapshot(
            cwd=self.cwd,
            variables=dict(self.variables),
            arrays=dict(self.arrays),
            associative_arrays=copy.deepcopy(self.associative_arrays),
            shell_options=frozenset(self.shell_options),
            glob_options=frozenset(self.glob_options),
            bash_source_stack=self.bash_source_stack,
            positional_assignment_generation=self.positional_assignment_generation,
        )

    def child_shell_copy(self):
        return copy.deepcopy(self)

    def conditional_copy(self):
        state = self.child_shell_copy()
        state.occurrence_context = OccurrenceModel.CONDITIONAL
        return state

    def copy_from(self, other: EvaluationState):
        self.__dict__.clear()
        self.__dict__.update(copy.deepcopy(other.__dict__))


@dataclass
class FunctionReturnSignal(Exception):
    status: int
    node: RawCommand


@dataclass
class SourceReturnSignal(Exception):
    status: int
    node: RawCommand


class LoopBreakSignal(Exception):
    pass


class LoopContinueSignal(Exception):
    pass


@dataclass
class LineAbortSignal(Exception):
    path: Path
    line: int


class SourceConditionExpansionFailureSignal(Exception):
    def __init__(self, pattern: str):
        super().__init__(pattern)
        self.pattern = pattern


class FunctionSourceExpansionAbortSignal(Exception):
    pass


@dataclass
class ReadLoopWords:
    variable: str
    values: tuple[str, ...]
    child_shell: bool = False


@dataclass
class EvaluationOutcome:
    state: EvaluationState
    return_signal: FunctionReturnSignal | SourceReturnSignal | None = None


@dataclass(frozen=True)
class RetainedHelperSourceSite:
    function_name: str
    function_def: FunctionDef
    definition_state: EvaluationState
    stack: tuple[Path, ...]
    location: SourceLocation
    source_expression: str
    source_site: str
    fragment: str


@dataclass(frozen=True)
class SourceInvocation:
    source: ResolvedSource | None
    source_arguments: tuple[str, ...] | None = None
    source_argument_words: tuple[str, ...] | None = None
    source_arguments_dynamic: bool = False


@dataclass(frozen=True)
class ExpandedSourceWord:
    word: str
    path: str | None = None
    exists: bool = True
    is_file: bool = True


@dataclass(frozen=True)
class ChildShellSourceCommand:
    context_id: tuple[str, int]
    command_name: str
    source_expression: str
    source_site: str
    column: int
    replacement_kind: str = "source"
    resolve_source_site: str | None = None
    source_value: str | None = None


@dataclass(frozen=True)
class ConditionWords:
    words: tuple[str, ...]
    kind: str
