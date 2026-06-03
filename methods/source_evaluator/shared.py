from __future__ import annotations

import ast
import copy
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path

from methods.shell.line import get_commands
from methods.shell.scan import (
    command_substitution_bodies,
    is_array_assignment_paren,
    process_substitution_bodies,
    read_balanced_body,
    subshell_bodies,
    top_level_pipeline_segments,
)
from methods.source_diagnostics import unsupported_source_error, with_source_diagnostic
from methods.source_conditions import (
    ConditionAtom,
    condition_status_and,
    condition_status_not,
    condition_status_or,
    source_logical_condition_atoms_from_text,
)
from methods.source_commands import (
    contains_nested_source_command,
    contains_source_command,
    shell_quote_words as quote_shell_words,
    shell_single_quote,
    source_command_index,
    source_command_invocation,
)
from methods.source_effects import (
    ArrayAssignment,
    Assignment,
    CaseBlock,
    CdCommand,
    CStyleForLoop,
    DisabledSourceSite,
    EvaluationResult,
    ExecutionModel,
    FunctionDef,
    ForLoop,
    IfBlock,
    LineReplacement,
    OccurrenceModel,
    RawCommand,
    SetCommand,
    SourceEvent,
    SourceSite,
    SourceLocation,
    StateSnapshot,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend, ParserFrontend
from methods.source_patterns import (
    UnsupportedPatternError,
    extglob_operator_at,
    read_extglob_body,
    shell_pattern_matches,
    split_extglob_alternatives,
)
from methods.source_resolver import (
    FailglobExpansionError,
    MISSING_SOURCE,
    MISSING_SOURCE_NO_FILENAME,
    SOURCE_EXPANSION_FAILURE,
    SOURCE_EXPANSION_FAILURE_RETURN,
    ResolvedSource,
    UnsupportedSourceError,
    extract_exact_command_substitution,
    expand_glob_word,
    has_unsupported_shell_operator,
    has_unquoted_brace_expansion,
    has_unquoted_extglob,
    has_unquoted_glob,
    is_missing_source_replacement_kind,
    is_source_expansion_failure_replacement_kind,
    missing_source_status,
    parse_shell_words_preserving_quotes,
    source_expansion_failure_result,
    strip_shell_word_quotes,
)
from methods.source_supplements import SourceSupplement, empty_source_supplement, supplement_skeleton
from methods.sources import (
    SOURCE_RESOLVER,
    change_directory,
    resolve_command,
    resolve_shell_path_commands,
    resolve_variable_references,
    shell_utility_basename,
    shell_utility_dirname,
)
from methods.regex.utilities import strip_matching_quotes

ARRAY_INDEX_PATTERN = re.compile(r'\$\{([a-zA-Z_]\w*)\[(\d+)\]\}')
ARRAY_ANY_INDEX_PATTERN = re.compile(r'\$\{([a-zA-Z_]\w*)\[([^\]]+)\]\}')
ARRAY_EXPANSION_PATTERN = re.compile(r'^\$\{([a-zA-Z_]\w*)\[@\]\}$')
SCALAR_REFERENCE_PATTERN = re.compile(r'\$(?:\{([a-zA-Z_]\w*|[0-9]+)\}|([a-zA-Z_]\w*|[0-9]+))')
SCALAR_WORD_PATTERN = re.compile(r'^\$(?:\{([a-zA-Z_]\w*|[0-9]+)\}|([a-zA-Z_]\w*|[0-9]+))$')
ASSIGNMENT_WORD_PATTERN = re.compile(r'^[a-zA-Z_]\w*(?:\+)?=.*$')
DEFAULT_IFS = " \t\n"
MAX_MODELED_LOOP_ITERATIONS = 256
SHELL_OPTION_FLAGS = {
    'e': 'errexit',
    'E': 'errtrace',
    'f': 'noglob',
    'm': 'monitor',
    'u': 'nounset',
}
VALID_SET_FLAGS = frozenset("abefhkmnptuvxBCEHPT")
VALID_SET_OPTIONS = frozenset({
    'allexport',
    'braceexpand',
    'emacs',
    'errexit',
    'errtrace',
    'functrace',
    'hashall',
    'histexpand',
    'history',
    'ignoreeof',
    'interactive-comments',
    'keyword',
    'monitor',
    'noclobber',
    'noexec',
    'noglob',
    'nolog',
    'notify',
    'nounset',
    'onecmd',
    'physical',
    'pipefail',
    'posix',
    'privileged',
    'verbose',
    'vi',
    'xtrace',
})
SHOPT_SHELL_OPTIONS = frozenset({
    'lastpipe',
})
DEFAULT_ENABLED_SHOPT_OPTIONS = frozenset({
    'checkwinsize',
    'cmdhist',
    'complete_fullquote',
    'extquote',
    'force_fignore',
    'globasciiranges',
    'globskipdots',
    'hostcomplete',
    'interactive_comments',
    'patsub_replacement',
    'progcomp',
    'promptvars',
    'sourcepath',
})
GLOB_SHOPT_OPTIONS = frozenset({
    'dotglob',
    'extglob',
    'failglob',
    'globstar',
    'nocaseglob',
    'nullglob',
})
KNOWN_SHOPT_OPTIONS = frozenset({
    'assoc_expand_once',
    'autocd',
    'cdable_vars',
    'cdspell',
    'checkhash',
    'checkjobs',
    'checkwinsize',
    'cmdhist',
    'compat31',
    'compat32',
    'compat40',
    'compat41',
    'compat42',
    'compat43',
    'compat44',
    'complete_fullquote',
    'direxpand',
    'dirspell',
    'execfail',
    'expand_aliases',
    'extdebug',
    'extquote',
    'force_fignore',
    'globasciiranges',
    'globskipdots',
    'gnu_errfmt',
    'histappend',
    'histreedit',
    'histverify',
    'hostcomplete',
    'huponexit',
    'inherit_errexit',
    'interactive_comments',
    'lithist',
    'localvar_inherit',
    'localvar_unset',
    'login_shell',
    'mailwarn',
    'no_empty_cmd_completion',
    'nocasematch',
    'noexpand_translation',
    'patsub_replacement',
    'progcomp',
    'progcomp_alias',
    'promptvars',
    'restricted_shell',
    'shift_verbose',
    'sourcepath',
    'varredir_close',
    'xpg_echo',
}) | SHOPT_SHELL_OPTIONS | GLOB_SHOPT_OPTIONS
CONDITION_UNARY_FILE_OPERATORS = frozenset({'-e', '-f', '-d', '-r'})
CONDITION_UNARY_STRING_OPERATORS = frozenset({'-n', '-z'})
CONDITION_STRING_OPERATORS = frozenset({'=', '==', '!='})
CONDITION_INTEGER_OPERATORS = frozenset({'-eq', '-ne', '-gt', '-ge', '-lt', '-le'})
CONDITION_BINARY_OPERATORS = (
    CONDITION_STRING_OPERATORS
    | CONDITION_INTEGER_OPERATORS
    | frozenset({'=~'})
)
GREP_LITERAL_META_PATTERN = re.compile(r'[.\[\\*^$]')
POSIX_CLASS_PATTERN = re.compile(r'\[\[:[a-zA-Z_]+:\]\]')
PYTHON_ONLY_REGEX_PATTERN = re.compile(r'\(\?|\\[AbBdDsSwWZ]')
LAZY_REGEX_QUANTIFIER_PATTERN = re.compile(r'(?:[*+?]|\{[0-9]+(?:,[0-9]*)?\})\?')
QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS = frozenset({'"$@"', '"${@}"', '"$*"', '"${*}"'})
RETAINED_HELPER_POSITIONAL_SOURCE_EXPRESSIONS = QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS | frozenset({
    '"$1"',
    '"${1}"',
})


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
