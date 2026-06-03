from __future__ import annotations

import ast
import copy
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path

from methods.shell_line import get_commands
from methods.source_diagnostics import unsupported_source_error, with_source_diagnostic
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
    contains_source_command,
    contains_nested_source_command,
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
    source_command_index,
    source_command_invocation,
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
CONTROL_SOURCE_CONDITION_PATTERN = re.compile(
    r'^(?:if|elif|while|until)\s+(.+?)(?:\s*;\s*(?:then|do)\s*)?$',
    re.S,
)
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
SOURCE_OVERRIDE_EXHAUSTED = object()


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
        return EvaluationState(
            cwd=self.cwd,
            variables=copy.deepcopy(self.variables),
            runtime_variables=copy.deepcopy(self.runtime_variables),
            arrays=copy.deepcopy(self.arrays),
            associative_arrays=copy.deepcopy(self.associative_arrays),
            functions=copy.deepcopy(self.functions),
            function_variants=copy.deepcopy(self.function_variants),
            shell_options=set(self.shell_options),
            glob_options=set(self.glob_options),
            missing_source_words=set(self.missing_source_words),
            bash_source_stack=self.bash_source_stack,
            occurrence_context=self.occurrence_context,
            condition_context=self.condition_context,
            ambiguous_cwd=self.ambiguous_cwd,
            ambiguous_variables=set(self.ambiguous_variables),
            ambiguous_arrays=set(self.ambiguous_arrays),
            ambiguous_functions=set(self.ambiguous_functions),
            ambiguous_shell_options=self.ambiguous_shell_options,
            ambiguous_glob_options=self.ambiguous_glob_options,
            function_call_stack=self.function_call_stack,
            positional_arguments=self.positional_arguments,
            ambiguous_positionals=self.ambiguous_positionals,
            positional_assignment_generation=self.positional_assignment_generation,
            source_argument_frame_dirty_stack=self.source_argument_frame_dirty_stack,
            local_scopes=copy.deepcopy(self.local_scopes),
            last_status=self.last_status,
            loop_depth=self.loop_depth,
            source_depth=self.source_depth,
            function_body_depth=self.function_body_depth,
        )

    def conditional_copy(self):
        state = self.child_shell_copy()
        state.occurrence_context = OccurrenceModel.CONDITIONAL
        return state

    def copy_from(self, other: EvaluationState):
        self.cwd = other.cwd
        self.variables = copy.deepcopy(other.variables)
        self.runtime_variables = copy.deepcopy(other.runtime_variables)
        self.arrays = copy.deepcopy(other.arrays)
        self.associative_arrays = copy.deepcopy(other.associative_arrays)
        self.functions = copy.deepcopy(other.functions)
        self.function_variants = copy.deepcopy(other.function_variants)
        self.shell_options = set(other.shell_options)
        self.glob_options = set(other.glob_options)
        self.missing_source_words = set(other.missing_source_words)
        self.bash_source_stack = other.bash_source_stack
        self.occurrence_context = other.occurrence_context
        self.condition_context = other.condition_context
        self.ambiguous_cwd = other.ambiguous_cwd
        self.ambiguous_variables = set(other.ambiguous_variables)
        self.ambiguous_arrays = set(other.ambiguous_arrays)
        self.ambiguous_functions = set(other.ambiguous_functions)
        self.ambiguous_shell_options = other.ambiguous_shell_options
        self.ambiguous_glob_options = other.ambiguous_glob_options
        self.function_call_stack = other.function_call_stack
        self.positional_arguments = other.positional_arguments
        self.ambiguous_positionals = other.ambiguous_positionals
        self.positional_assignment_generation = other.positional_assignment_generation
        self.source_argument_frame_dirty_stack = other.source_argument_frame_dirty_stack
        self.local_scopes = copy.deepcopy(other.local_scopes)
        self.last_status = other.last_status
        self.loop_depth = other.loop_depth
        self.source_depth = other.source_depth
        self.function_body_depth = other.function_body_depth


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
class ConditionAtom:
    text: str
    offset: int
    separator: str = ""
    negated: bool = False
    source_command: str | None = None
    source_expression: str | None = None
    source_offset: int | None = None


@dataclass(frozen=True)
class SourceOverride:
    path: str
    line: int
    command: str
    resolved_path: str
    arguments: tuple[str, ...] = ()
    replacement_kind: str = "source"
    source_value: str | None = None
    graph_index: int = -1
    function_call: tuple[str, str, int, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class ConditionWords:
    words: tuple[str, ...]
    kind: str


class SourceEvaluator:
    """Evaluate source effects for the supported IR subset without executing Bash."""

    def __init__(
        self,
        frontend: ParserFrontend | None = None,
        mode: str = "executable",
        source_supplement: SourceSupplement | None = None,
        source_overrides=(),
    ):
        self.frontend = frontend or LineParserFrontend()
        self.mode = mode
        self.source_supplement = source_supplement or empty_source_supplement()
        self.source_overrides = self._source_override_map(source_overrides)
        self._source_override_indexes = Counter()
        self._function_dispatch_signature_indexes = Counter()
        self.events: list[SourceEvent] = []
        self.disabled_sources: list[DisabledSourceSite] = []
        self.line_replacements: list[LineReplacement] = []
        self.retained_helper_source_sites: list[RetainedHelperSourceSite] = []
        self._retained_helper_stack: list[str] = []
        self._source_line_cache: dict[Path, tuple[str, ...]] = {}

    def evaluate(self, entrypoint: str | Path):
        entrypoint = Path(entrypoint).resolve()
        initial_variables = {
            **self.source_supplement.variables,
            '0': str(entrypoint),
            'BASH_SOURCE': str(entrypoint),
        }
        state = EvaluationState(
            cwd=entrypoint.parent,
            variables=copy.deepcopy(initial_variables),
            runtime_variables=copy.deepcopy(initial_variables),
            shell_options=set(DEFAULT_ENABLED_SHOPT_OPTIONS),
            bash_source_stack=(entrypoint,),
        )
        self.events = []
        self.disabled_sources = []
        self.line_replacements = []
        self.retained_helper_source_sites = []
        self._retained_helper_stack = []
        self._source_override_indexes.clear()
        self._function_dispatch_signature_indexes.clear()
        self._evaluate_file(entrypoint, state, ())
        self._ensure_retained_helpers_resolved()
        self._ensure_source_overrides_consumed()
        return EvaluationResult(
            events=self._with_occurrence_models(self.events),
            disabled_sources=tuple(self.disabled_sources),
            line_replacements=tuple(self.line_replacements),
            final_state=state.snapshot(),
        )

    @staticmethod
    def _source_override_map(source_overrides):
        overrides = {}
        for override in source_overrides or ():
            key = (
                Path(override.path).resolve(strict=False),
                override.line,
                SourceEvaluator._source_override_command_key(override.command),
            )
            overrides.setdefault(key, []).append(override)
        return {key: tuple(values) for key, values in overrides.items()}

    @staticmethod
    def _source_override_command_key(command: str):
        command = command.strip()
        match = CONTROL_SOURCE_CONDITION_PATTERN.fullmatch(command)
        if match is None:
            return command
        try:
            atoms = SourceEvaluator._source_logical_condition_atoms_from_text(match.group(1).strip())
        except UnsupportedSourceError:
            return command
        source_atoms = [atom for atom in atoms if atom.source_command is not None]
        if len(source_atoms) != 1:
            return command
        atom = source_atoms[0]
        return f"{atom.source_command} {atom.source_expression}"

    def _evaluate_file(
        self,
        path: Path,
        state: EvaluationState,
        stack: tuple[Path, ...],
        *,
        as_source: bool = False,
    ):
        path = path.resolve()
        if path in stack:
            chain = " -> ".join(str(item) for item in (*stack, path))
            raise RecursionError(f"Circular source dependency while evaluating: {chain}")
        current_stack = (*stack, path)

        content = path.read_text()
        ir = self.frontend.parse(path, content)
        previous_bash_source = state.variables.get('BASH_SOURCE')
        previous_runtime_bash_source = state.runtime_variables.get('BASH_SOURCE')
        previous_stack = state.bash_source_stack
        previous_source_depth = state.source_depth
        previous_function_body_depth = state.function_body_depth
        state.variables['BASH_SOURCE'] = str(path)
        state.runtime_variables['BASH_SOURCE'] = str(path)
        state.bash_source_stack = (*previous_stack, path) if previous_stack[-1:] != (path,) else previous_stack
        if as_source:
            state.source_depth += 1
            state.function_body_depth = 0

        try:
            self._evaluate_nodes(ir.nodes, state, current_stack)
            return bool(ir.nodes)
        finally:
            if previous_bash_source is None:
                state.variables.pop('BASH_SOURCE', None)
            else:
                state.variables['BASH_SOURCE'] = previous_bash_source
            if previous_runtime_bash_source is None:
                state.runtime_variables.pop('BASH_SOURCE', None)
            else:
                state.runtime_variables['BASH_SOURCE'] = previous_runtime_bash_source
            state.bash_source_stack = previous_stack
            state.source_depth = previous_source_depth
            state.function_body_depth = previous_function_body_depth

    def _evaluate_nodes(self, nodes, state: EvaluationState, stack: tuple[Path, ...]):
        nodes = tuple(nodes)
        aborted_lines = set()
        for index, node in enumerate(nodes):
            try:
                if (node.location.path, node.location.line) in aborted_lines:
                    continue
                if isinstance(node, Assignment):
                    self._apply_assignment(node, state)
                elif isinstance(node, ArrayAssignment):
                    self._apply_array_assignment(node, state)
                elif isinstance(node, CdCommand):
                    self._apply_cd(node, state)
                elif isinstance(node, SetCommand):
                    self._apply_set(node, state)
                elif isinstance(node, FunctionDef):
                    self._apply_function_def(node, state, stack)
                elif isinstance(node, ForLoop):
                    self._apply_for_loop(node, state, stack)
                elif isinstance(node, CStyleForLoop):
                    self._apply_c_style_for_loop(node, state, stack)
                elif isinstance(node, WhileLoop):
                    self._apply_while_loop(node, state, stack)
                elif isinstance(node, IfBlock):
                    self._apply_if_block(node, state, stack)
                elif isinstance(node, CaseBlock):
                    self._apply_case_block(node, state, stack)
                elif isinstance(node, SourceSite):
                    self._apply_source_site(node, state, stack)
                elif isinstance(node, RawCommand):
                    self._apply_raw_command(node, state, stack)
            except (FunctionReturnSignal, SourceReturnSignal):
                self._disable_unreachable_sources(nodes[index + 1:], "return")
                raise
            except FunctionSourceExpansionAbortSignal:
                self._disable_unreachable_sources(nodes[index + 1:], "source expansion failure")
                raise
            except LineAbortSignal as signal:
                aborted_lines.add((signal.path, signal.line))

    def _apply_assignment(self, node: Assignment, state: EvaluationState):
        if node.prefix == "local" and state.local_scopes:
            SourceEvaluator._capture_local_variable(node.name, state)

        shopt_snapshot = self._assignment_shopt_snapshot_value(node, state)
        if shopt_snapshot is not None:
            state.variables[node.name] = shopt_snapshot
            state.runtime_variables[node.name] = shopt_snapshot
            state.ambiguous_variables.discard(node.name)
            state.last_status = 0
            return

        arithmetic_value = self._assignment_arithmetic_value(node, state)
        if arithmetic_value is not None:
            state.variables[node.name] = arithmetic_value
            state.runtime_variables[node.name] = arithmetic_value
            state.ambiguous_variables.discard(node.name)
            state.last_status = 0
            return

        runtime_context = state.runtime_context()
        runtime_value = resolve_variable_references(node.value, runtime_context)
        runtime_value = os.path.expandvars(runtime_value)
        runtime_value = resolve_shell_path_commands(runtime_value, str(state.cwd))
        runtime_value = self._decode_ansi_c_quoted_word(strip_matching_quotes(runtime_value))

        context = state.resolver_context()
        value = self._decode_ansi_c_quoted_word(strip_matching_quotes(resolve_variable_references(node.value, context)))
        resolved_value, _ = resolve_command(value, context)
        state.variables[node.name] = resolved_value
        state.runtime_variables[node.name] = runtime_value
        state.ambiguous_variables.discard(node.name)
        state.last_status = 0

    @staticmethod
    def _decode_ansi_c_quoted_word(value: str):
        if len(value) >= 3 and value.startswith("$'") and value.endswith("'"):
            body = value[2:-1]
            try:
                return bytes(body, "utf-8").decode("unicode_escape")
            except UnicodeDecodeError as exc:
                raise UnsupportedSourceError(f"unsupported ANSI-C quoted value: {value}") from exc
        return value

    @staticmethod
    def _assignment_shopt_snapshot_value(node: Assignment, state: EvaluationState):
        match = re.fullmatch(r'\$\(shopt\s+-p\s+([a-zA-Z_]\w*)\)', node.value.strip())
        if not match:
            return None

        option = match.group(1)
        if option not in GLOB_SHOPT_OPTIONS | SHOPT_SHELL_OPTIONS:
            return None

        enabled = option in state.glob_options or option in state.shell_options
        action = "-s" if enabled else "-u"
        return f"shopt {action} {option}"

    def _assignment_arithmetic_value(self, node: Assignment, state: EvaluationState):
        value = node.value.strip()
        if not value.startswith("$((") or not value.endswith("))"):
            return None
        expression = value[3:-2].strip()
        result = self._evaluate_arithmetic_expression(expression, state, node.text)
        if result is None:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.arithmetic",
                "unsupported arithmetic assignment",
                "Arithmetic assignments must resolve exactly.",
            )
        return str(result)

    @staticmethod
    def _capture_local_variable(name: str, state: EvaluationState):
        SourceEvaluator._capture_variable_in_scope(name, state.local_scopes[-1], state)

    @staticmethod
    def _capture_variable_in_scope(
        name: str,
        scope: dict[str, tuple[bool, str | None, bool, str | None, bool]],
        state: EvaluationState,
    ):
        if name in scope:
            return
        has_value = name in state.variables
        has_runtime_value = name in state.runtime_variables
        scope[name] = (
            has_value,
            state.variables.get(name),
            has_runtime_value,
            state.runtime_variables.get(name),
            name in state.ambiguous_variables,
        )

    @staticmethod
    def _restore_local_scope(
        local_scope: dict[str, tuple[bool, str | None, bool, str | None, bool]],
        state: EvaluationState,
    ):
        for name, (
            had_value,
            previous_value,
            had_runtime_value,
            previous_runtime_value,
            was_ambiguous,
        ) in reversed(local_scope.items()):
            if had_value and previous_value is not None:
                state.variables[name] = previous_value
            else:
                state.variables.pop(name, None)

            if had_runtime_value and previous_runtime_value is not None:
                state.runtime_variables[name] = previous_runtime_value
            else:
                state.runtime_variables.pop(name, None)

            if was_ambiguous:
                state.ambiguous_variables.add(name)
            else:
                state.ambiguous_variables.discard(name)

    def _apply_array_assignment(self, node: ArrayAssignment, state: EvaluationState):
        if not node.is_exact:
            state.ambiguous_arrays.add(node.name)
            state.last_status = 0
            return

        if node.associative_values:
            if node.operation == "assign":
                state.associative_arrays[node.name] = {}
            target = state.associative_arrays.setdefault(node.name, {})
            for key, value in node.associative_values:
                target[self._resolve_array_word(key, node, state)] = self._resolve_array_word(value, node, state)
            state.arrays.pop(node.name, None)
            state.ambiguous_arrays.discard(node.name)
            state.last_status = 0
            return

        values = self._resolve_array_values(node, state)
        if self.mode == "executable" and any('$(' in raw_value for raw_value in node.raw_values):
            operator = "+=" if node.operation == "append" else "="
            self._record_line_replacement(
                node.location,
                node.text,
                f"{node.name}{operator}({self._shell_quote_words(values)})",
            )
        if node.operation == "assign":
            state.arrays[node.name] = values
            state.associative_arrays.pop(node.name, None)
        elif node.operation == "append":
            existing = state.arrays.get(node.name, ())
            state.arrays[node.name] = (*existing, *values)
            state.associative_arrays.pop(node.name, None)
        elif node.operation == "set":
            self._set_indexed_array_value(node, values, state)

        state.ambiguous_arrays.discard(node.name)
        state.last_status = 0

    def _resolve_array_values(self, node: ArrayAssignment, state: EvaluationState):
        values = []
        raw_values = node.raw_values or node.values
        for value, raw_value in zip(node.values, raw_values):
            values.extend(self._expand_array_assignment_word(value, raw_value, node, state))
        return tuple(values)

    def _expand_array_assignment_word(self, value: str, raw_value: str, node: ArrayAssignment,
                                      state: EvaluationState):
        if '$(' in raw_value or '$(' in value:
            return tuple(self._resolve_command_substitution_loop_word(value, raw_value, node, state))

        resolved = self._resolve_array_word(value, node, state)
        if self._raw_word_is_unquoted_scalar(raw_value):
            return tuple(self._split_array_scalar_word(resolved, node, state))
        if has_unquoted_glob(raw_value):
            try:
                return tuple(
                    match.word
                    for match in expand_glob_word(resolved, state.resolver_context(), node.text, raw_pattern=raw_value)
                )
            except UnsupportedSourceError as exc:
                raise self._unsupported_array_assignment(node, str(exc)) from exc
        return (resolved,)

    def _resolve_array_word(self, value: str, node: ArrayAssignment, state: EvaluationState):
        if '`' in value:
            raise self._unsupported_array_assignment(node, "unsupported array assignment backticks")
        for match in SCALAR_REFERENCE_PATTERN.finditer(value):
            variable_name = match.group(1) or match.group(2)
            if variable_name in state.ambiguous_variables:
                raise self._unsupported_array_assignment(
                    node,
                    f"array assignment references branch-dependent variable: {variable_name}",
                )
            if variable_name not in state.runtime_variables:
                raise self._unsupported_array_assignment(
                    node,
                    f"array assignment references unknown variable: {variable_name}",
                )
        resolved = resolve_variable_references(value, state.runtime_context())
        resolved = os.path.expandvars(resolved)
        if "$" in resolved:
            raise self._unsupported_array_assignment(node, "array assignment contains unresolved scalar expansion")
        return strip_matching_quotes(resolved)

    def _split_array_scalar_word(self, resolved_word: str, node: ArrayAssignment, state: EvaluationState):
        words = []
        for field in self._split_ifs_fields_for_node(resolved_word, node, state):
            if has_unquoted_glob(field):
                try:
                    words.extend(
                        match.word
                        for match in expand_glob_word(field, state.resolver_context(), node.text, raw_pattern=field)
                    )
                except UnsupportedSourceError as exc:
                    raise self._unsupported_array_assignment(node, str(exc)) from exc
            else:
                words.append(field)
        return words

    def _set_indexed_array_value(self, node: ArrayAssignment, values: tuple[str, ...], state: EvaluationState):
        if len(values) != 1:
            raise self._unsupported_array_assignment(node, "indexed array assignment must resolve to one value")
        index = self._resolve_array_index(node.index or "", node, state)
        existing = list(state.arrays.get(node.name, ()))
        if index < 0:
            raise self._unsupported_array_assignment(node, "negative array indexes are unsupported")
        if index >= len(existing):
            existing.extend("" for _ in range(index - len(existing) + 1))
        if node.operation == "append":
            existing[index] = existing[index] + values[0]
        else:
            existing[index] = values[0]
        state.arrays[node.name] = tuple(existing)
        state.associative_arrays.pop(node.name, None)

    @staticmethod
    def _resolve_array_index(index_expression: str, node, state: EvaluationState):
        index_expression = strip_matching_quotes(index_expression.strip())
        if re.fullmatch(r'\d+', index_expression):
            return int(index_expression)
        resolved = resolve_variable_references(index_expression, state.runtime_context())
        resolved = os.path.expandvars(strip_matching_quotes(resolved))
        if not re.fullmatch(r'\d+', resolved):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.array-index",
                "unsupported array index expression",
                "Array indexes must resolve to exact non-negative integers.",
            )
        return int(resolved)

    @staticmethod
    def _unsupported_array_assignment(node: ArrayAssignment, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.array-assignment",
            f"unsupported array assignment: {message}",
            "Array assignments must resolve to exact finite values.",
        )

    def _apply_function_def(self, node: FunctionDef, state: EvaluationState, stack: tuple[Path, ...]):
        state.functions[node.name] = node
        state.function_variants.pop(node.name, None)
        state.ambiguous_functions.discard(node.name)
        state.last_status = 0
        if self.mode != "executable":
            return

        retained_sites = self._retained_helper_source_sites(node, state, stack)
        if not retained_sites:
            return
        self.retained_helper_source_sites.extend(retained_sites)

    def _apply_retained_helper_signatures(
        self,
        function_def: FunctionDef,
        signatures: tuple[tuple[str, ...], ...],
        state: EvaluationState,
        stack: tuple[Path, ...],
    ):
        uses_first_positional_source = self._retained_helper_uses_first_positional_source(function_def)
        for signature in signatures:
            if not signature:
                raise self._unsupported_retained_helper(
                    function_def.name,
                    function_def.location,
                    function_def.text,
                    f"unsupported retained source helper argument count: {len(signature)}",
                    "Retained helper supplements must provide at least one source path argument.",
                )
            if uses_first_positional_source and len(signature) != 1:
                raise self._unsupported_retained_helper(
                    function_def.name,
                    function_def.location,
                    function_def.text,
                    f"unsupported retained source helper argument count: {len(signature)}",
                    'Retained helpers using source "$1" must provide exactly one source path argument.',
                )

        call_node = RawCommand(
            function_def.location,
            f"{function_def.name} <source-supplement>",
        )
        for signature in dict.fromkeys(signatures):
            retained_state = state.child_shell_copy()
            self._retained_helper_stack.append(function_def.name)
            try:
                self._apply_function_call_variant(
                    function_def,
                    function_def.name,
                    signature,
                    [],
                    call_node,
                    retained_state,
                    stack,
                )
            except UnsupportedSourceError as exc:
                raise with_source_diagnostic(
                    exc,
                    str(function_def.location.path),
                    function_def.location.line - 1,
                    function_def.text,
                    function_def.text,
                    "unsupported.source.retained-helper",
                ) from exc
            finally:
                self._retained_helper_stack.pop()

    def _retained_helper_uses_first_positional_source(self, function_def: FunctionDef):
        first_positional_expressions = {'"$1"', '"${1}"'}

        def collect(nodes):
            for node in nodes:
                if isinstance(node, SourceSite):
                    if node.source_expression.strip() in first_positional_expressions:
                        return True
                    continue
                if isinstance(node, IfBlock):
                    for branch in node.branches:
                        site_spec = self._retained_helper_source_condition_site(node, branch)
                        if site_spec and site_spec[1] in first_positional_expressions:
                            return True
                        if collect(branch.body):
                            return True
                    continue
                if isinstance(node, FunctionDef):
                    continue
                if isinstance(node, ForLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, CStyleForLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, WhileLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, CaseBlock):
                    for arm in node.arms:
                        if collect(arm.body):
                            return True
            return False

        return collect(function_def.body)

    def _retained_helper_source_sites(self, function_def: FunctionDef, state: EvaluationState,
                                      stack: tuple[Path, ...] = ()):
        site_specs = []

        def collect(nodes):
            for node in nodes:
                if isinstance(node, SourceSite):
                    source_expression = node.source_expression.strip()
                    if self._is_retained_helper_positional_source_expression(source_expression):
                        site_specs.append((
                            node.location,
                            source_expression,
                            self._source_site_text(node),
                            node.text,
                        ))
                    continue

                if isinstance(node, IfBlock):
                    for branch in node.branches:
                        site_spec = self._retained_helper_source_condition_site(node, branch)
                        if site_spec is not None:
                            site_specs.append(site_spec)
                        collect(branch.body)
                    continue

                if isinstance(node, FunctionDef):
                    continue
                if isinstance(node, ForLoop):
                    collect(node.body)
                elif isinstance(node, CStyleForLoop):
                    collect(node.body)
                elif isinstance(node, WhileLoop):
                    collect(node.body)
                elif isinstance(node, CaseBlock):
                    for arm in node.arms:
                        collect(arm.body)

        collect(function_def.body)
        if not site_specs:
            return []

        definition_state = state.child_shell_copy()
        return [
            RetainedHelperSourceSite(
                function_name=function_def.name,
                function_def=function_def,
                definition_state=definition_state,
                stack=stack,
                location=location,
                source_expression=source_expression,
                source_site=source_site,
                fragment=fragment,
            )
            for location, source_expression, source_site, fragment in site_specs
        ]

    def _retained_helper_source_condition_site(
        self,
        node: IfBlock,
        branch,
    ):
        if branch.keyword != "if" or branch.condition is None:
            return None

        match = re.fullmatch(r'(!\s*)?((?:source)|\.)\s+(.+)', branch.condition.strip(), re.S)
        if not match:
            return None

        _, command_name, source_expression = match.groups()
        source_expression = source_expression.strip()
        if not self._is_retained_helper_positional_source_expression(source_expression):
            return None

        column = self._source_condition_column(node, command_name)
        location = SourceLocation(node.location.path, node.location.line, column)
        return (
            location,
            source_expression,
            f"{command_name} {source_expression}".strip(),
            node.text,
        )

    def _ensure_retained_helpers_resolved(self):
        if self.mode != "executable" or not self.retained_helper_source_sites:
            return

        processed_functions = set()
        resolved_sites = self._retained_resolved_site_keys()
        index = 0
        while index < len(self.retained_helper_source_sites):
            site = self.retained_helper_source_sites[index]
            if self._retained_site_key(site) in resolved_sites:
                index += 1
                continue

            signatures = self.source_supplement.function_signatures(site.function_name)
            function_key = (
                site.function_def.location.path.resolve(),
                site.function_def.location.line,
                site.function_name,
            )
            if signatures and function_key not in processed_functions:
                self._apply_retained_helper_signatures(
                    site.function_def,
                    signatures,
                    site.definition_state,
                    site.stack,
                )
                processed_functions.add(function_key)
                resolved_sites = self._retained_resolved_site_keys()
                continue

            raise self._unsupported_retained_helper(
                site.function_name,
                site.location,
                site.fragment,
                f"unsupported retained source helper: {site.function_name}",
                "Provide a source supplement with finite allowed helper arguments.",
            )

    def _retained_resolved_site_keys(self):
        return {
            (
                event.location.path.resolve(),
                event.location.line,
                event.location.column,
                event.source_site,
            )
            for event in self.events
        }

    @staticmethod
    def _retained_site_key(site: RetainedHelperSourceSite):
        return (
            site.location.path.resolve(),
            site.location.line,
            site.location.column,
            site.source_site,
        )

    @staticmethod
    def _unsupported_retained_helper(
        function_name: str,
        location: SourceLocation,
        fragment: str,
        message: str,
        hint: str,
    ):
        return unsupported_source_error(
            str(location.path),
            location.line - 1,
            fragment,
            fragment,
            "unsupported.source.retained-helper",
            message,
            hint,
            details={"supplement_skeleton": supplement_skeleton(function_name=function_name)},
        )

    @staticmethod
    def _apply_cd(node: CdCommand, state: EvaluationState):
        if state.ambiguous_cwd:
            SourceEvaluator._ensure_cd_state_can_resolve(node, state)
        context = state.resolver_context()
        state.cwd = Path(change_directory(node.path_expression, context))
        state.ambiguous_cwd = False
        state.last_status = 0

    def _apply_set(self, node: SetCommand, state: EvaluationState):
        status = 0
        index = 0
        while index < len(node.arguments):
            argument = node.arguments[index]
            if argument == "--":
                break
            if not argument.startswith(("-", "+")):
                break
            if argument in {'-o', '+o'} and index + 1 < len(node.arguments):
                option = node.arguments[index + 1]
                if option not in VALID_SET_OPTIONS:
                    status = 2
                    break
                if argument == '-o':
                    state.shell_options.add(option)
                else:
                    state.shell_options.discard(option)
                index += 2
                continue

            if len(argument) > 1 and argument[0] in {'-', '+'}:
                enabled = argument[0] == '-'
                if argument in {'-o', '+o'}:
                    break
                if any(flag not in VALID_SET_FLAGS for flag in argument[1:]):
                    status = 2
                    break
                for flag in argument[1:]:
                    option = SHELL_OPTION_FLAGS.get(flag)
                    if not option:
                        continue
                    if enabled:
                        state.shell_options.add(option)
                    else:
                        state.shell_options.discard(option)
            index += 1
        if status == 0:
            try:
                positional_arguments = self._set_command_positional_arguments(node, state)
            except UnsupportedSourceError:
                self._mark_positionals_ambiguous(state, source_argument_escape=True)
            else:
                if positional_arguments is not None:
                    self._set_positionals(positional_arguments, state, source_argument_escape=True)
        state.last_status = status

    def _set_command_positional_arguments(self, node: SetCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError as exc:
            raise self._unsupported_positional_mutation(node, "unsupported set syntax") from exc

        if not words or strip_shell_word_quotes(words[0]) != "set":
            return None

        index = 1
        while index < len(words):
            argument = strip_shell_word_quotes(words[index])
            if argument == "--":
                return self._resolve_positional_assignment_words(words[index + 1:], node, state)
            if not argument.startswith(("-", "+")):
                return self._resolve_positional_assignment_words(words[index:], node, state)
            if argument in {'-o', '+o'}:
                index += 2
                continue
            if argument in {'-', '+'}:
                return None
            index += 1
        return None

    def _resolve_positional_assignment_words(self, words: list[str], node, state: EvaluationState):
        arguments = []
        for word in words:
            stripped = word.strip()
            if stripped in {'"$@"', '"${@}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_positional_mutation(
                        node,
                        "unsupported ambiguous positional assignment expansion",
                    )
                arguments.extend(state.positional_arguments)
                continue
            if stripped in {'"$*"', '"${*}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_positional_mutation(
                        node,
                        "unsupported ambiguous positional assignment expansion",
                    )
                arguments.append(self._joined_positionals(state))
                continue
            if re.search(r'(?<!\\)\$(?:\{?[@*]\}?)', stripped):
                raise self._unsupported_positional_mutation(
                    node,
                    "unsupported positional assignment expansion",
                )
            arguments.append(self._resolve_function_exact_word(
                word,
                node,
                state,
                "unsupported.source.positionals",
                "unsupported dynamic positional assignment",
                "unsupported unresolved positional assignment",
                "Positional assignments must resolve exactly for source-aware lowering.",
            ))
        return tuple(arguments)

    @staticmethod
    def _set_positionals(
        arguments: tuple[str, ...],
        state: EvaluationState,
        *,
        source_argument_escape: bool = False,
        mark_source_argument_frame: bool = True,
    ):
        previous_names = {
            name
            for mapping in (state.variables, state.runtime_variables)
            for name in mapping
            if name.isdigit() and int(name) > 0
        }
        current_names = {str(index) for index in range(1, len(arguments) + 1)}
        for index, argument in enumerate(arguments, start=1):
            name = str(index)
            state.variables[name] = argument
            state.runtime_variables[name] = argument
            state.ambiguous_variables.discard(name)
        for name in previous_names - current_names:
            state.variables.pop(name, None)
            state.runtime_variables.pop(name, None)
            state.ambiguous_variables.discard(name)
        state.positional_arguments = tuple(arguments)
        state.ambiguous_positionals = False
        if source_argument_escape:
            state.positional_assignment_generation += 1
            if mark_source_argument_frame:
                SourceEvaluator._mark_current_source_argument_frame_dirty(state)

    @staticmethod
    def _mark_positionals_ambiguous(
        state: EvaluationState,
        *,
        source_argument_escape: bool = False,
        mark_source_argument_frame: bool = True,
    ):
        positional_names = {
            name
            for mapping in (state.variables, state.runtime_variables)
            for name in mapping
            if name.isdigit() and int(name) > 0
        }
        for name in positional_names:
            state.variables.pop(name, None)
            state.runtime_variables.pop(name, None)
            state.ambiguous_variables.discard(name)
        state.positional_arguments = ()
        state.ambiguous_positionals = True
        if source_argument_escape:
            state.positional_assignment_generation += 1
            if mark_source_argument_frame:
                SourceEvaluator._mark_current_source_argument_frame_dirty(state)

    @staticmethod
    def _push_source_argument_frame(state: EvaluationState):
        state.source_argument_frame_dirty_stack = (*state.source_argument_frame_dirty_stack, False)

    @staticmethod
    def _pop_source_argument_frame(state: EvaluationState):
        dirty = state.source_argument_frame_dirty_stack[-1]
        state.source_argument_frame_dirty_stack = state.source_argument_frame_dirty_stack[:-1]
        return dirty

    @staticmethod
    def _mark_current_source_argument_frame_dirty(state: EvaluationState):
        if not state.source_argument_frame_dirty_stack:
            return
        stack = list(state.source_argument_frame_dirty_stack)
        stack[-1] = True
        state.source_argument_frame_dirty_stack = tuple(stack)

    @staticmethod
    def _clear_current_source_argument_frame_dirty(state: EvaluationState):
        if not state.source_argument_frame_dirty_stack:
            return
        stack = list(state.source_argument_frame_dirty_stack)
        stack[-1] = False
        state.source_argument_frame_dirty_stack = tuple(stack)

    @staticmethod
    def _unsupported_positional_mutation(node, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.positionals",
            message,
            "Positional mutation must be exact for source-aware lowering.",
        )

    def _apply_for_loop(self, node: ForLoop, state: EvaluationState, stack: tuple[Path, ...]):
        try:
            words = self._resolve_loop_words(node, state)
        except FailglobExpansionError as exc:
            if self.mode == "context":
                return
            if not self._node_list_may_source(node.body):
                state.last_status = 1
                if state.function_body_depth > 0:
                    raise FunctionSourceExpansionAbortSignal()
                return
            self._record_for_loop_expansion_failure(node, exc.pattern, state)
            return
        except UnsupportedSourceError:
            if self.mode == "context":
                return
            if not self._node_list_may_source(node.body):
                self._apply_source_free_unknown_loop_body(node.body, state, stack)
                return
            raise

        if self.mode == "executable" and words and self._loop_words_need_exact_replacement(node):
            self._record_line_replacement(
                node.location,
                node.words_text,
                self._shell_quote_words(tuple(words)),
            )

        if not words:
            self._disable_unreachable_sources(node.body, f"for {node.variable} in {node.words_text}")
            return

        for word in words:
            state.variables[node.variable] = word
            state.runtime_variables[node.variable] = word
            state.ambiguous_variables.discard(node.variable)
            state.loop_depth += 1
            try:
                self._evaluate_nodes(node.body, state, stack)
            except LoopContinueSignal:
                continue
            except LoopBreakSignal:
                break
            finally:
                state.loop_depth -= 1

    def _apply_c_style_for_loop(self, node: CStyleForLoop, state: EvaluationState, stack: tuple[Path, ...]):
        try:
            self._apply_c_style_arithmetic_list(node.init, node, state)
        except UnsupportedSourceError as exc:
            if self.mode == "context":
                self._evaluate_context_loop_body(node.body, state, stack)
                return
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.arithmetic",
            ) from exc

        for iteration in range(MAX_MODELED_LOOP_ITERATIONS):
            try:
                condition_status = (
                    "true"
                    if not node.condition
                    else self._evaluate_arithmetic_condition(node.condition, state, node.text)
                )
            except UnsupportedSourceError as exc:
                if self.mode == "context":
                    self._evaluate_context_loop_body(node.body, state, stack)
                    return
                raise with_source_diagnostic(
                    exc,
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-condition",
                ) from exc

            if condition_status == "unknown":
                if not self._node_list_may_source(node.body):
                    self._apply_source_free_unknown_loop_body(node.body, state, stack)
                    return
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-condition",
                    "unsupported unknown C-style for condition",
                    "C-style for conditions must resolve exactly before source-aware lowering.",
                )
            if condition_status == "false":
                if iteration == 0:
                    self._disable_unreachable_sources(node.body, f"for (( {node.condition} ))")
                return

            state.loop_depth += 1
            try:
                self._evaluate_nodes(node.body, state, stack)
            except LoopContinueSignal:
                pass
            except LoopBreakSignal:
                return
            finally:
                state.loop_depth -= 1

            try:
                self._apply_c_style_arithmetic_list(node.update, node, state)
            except UnsupportedSourceError as exc:
                if self.mode == "context":
                    return
                raise with_source_diagnostic(
                    exc,
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.arithmetic",
                ) from exc

        raise unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.loop-iteration",
            "unsupported C-style for loop exceeds modeled iteration limit",
            "Use finite loops whose source effects resolve within the modeled iteration limit.",
        )

    def _apply_c_style_arithmetic_list(self, expression_list: str, node: CStyleForLoop, state: EvaluationState):
        for expression in self._split_c_style_arithmetic_list(expression_list):
            if self._apply_arithmetic_mutation(expression, node, state):
                continue
            value = self._evaluate_arithmetic_expression(expression, state, node.text)
            if value is None:
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.arithmetic",
                    "unsupported C-style for arithmetic expression",
                    "C-style for arithmetic clauses must resolve exactly.",
                )
            state.last_status = 0 if value else 1

    @staticmethod
    def _split_c_style_arithmetic_list(expression_list: str):
        expressions = []
        current = []
        depth = 0
        for char in expression_list:
            if char == "(":
                depth += 1
            elif char == ")" and depth > 0:
                depth -= 1
            elif char == "," and depth == 0:
                expression = ''.join(current).strip()
                if expression:
                    expressions.append(expression)
                current = []
                continue
            current.append(char)
        expression = ''.join(current).strip()
        if expression:
            expressions.append(expression)
        return expressions

    def _evaluate_context_loop_body(self, body, state: EvaluationState, stack: tuple[Path, ...]):
        if self._node_list_may_source(body):
            loop_state = state.conditional_copy()
            self._evaluate_nodes(body, loop_state, stack)

    def _apply_source_free_unknown_loop_body(self, body, state: EvaluationState, stack: tuple[Path, ...]):
        base_state = state.child_shell_copy()
        loop_state = state.child_shell_copy()
        loop_state.loop_depth += 1
        try:
            self._evaluate_nodes(body, loop_state, stack)
        except (LoopBreakSignal, LoopContinueSignal):
            pass
        finally:
            loop_state.loop_depth -= 1
        self._merge_possible_states(state, [base_state, loop_state])

    def _apply_while_loop(self, node: WhileLoop, state: EvaluationState, stack: tuple[Path, ...]):
        try:
            read_words = self._read_loop_words(node, state)
        except UnsupportedSourceError as exc:
            if self.mode == "context":
                self._evaluate_context_loop_body(node.body, state, stack)
                return
            if not self._node_list_may_source(node.body):
                self._apply_source_free_unknown_loop_body(node.body, state, stack)
                return
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.loop-word-list",
            ) from exc
        if read_words is not None:
            if self.mode == "executable":
                self._record_read_loop_replacements(node, read_words)
            if not read_words.values:
                self._disable_unreachable_sources(node.body, f"{node.keyword} {node.condition}")
                return
            loop_state = state.child_shell_copy() if read_words.child_shell else state
            for value in read_words.values:
                loop_state.variables[read_words.variable] = value
                loop_state.runtime_variables[read_words.variable] = value
                loop_state.ambiguous_variables.discard(read_words.variable)
                loop_state.loop_depth += 1
                try:
                    self._evaluate_nodes(node.body, loop_state, stack)
                except LoopContinueSignal:
                    continue
                except LoopBreakSignal:
                    break
                finally:
                    loop_state.loop_depth -= 1
            return

        for iteration in range(MAX_MODELED_LOOP_ITERATIONS):
            try:
                condition_status = self._evaluate_condition(node.condition, state, node, stack)
            except UnsupportedSourceError as exc:
                if self.mode == "context":
                    if self._node_list_may_source(node.body):
                        loop_state = state.conditional_copy()
                        self._evaluate_nodes(node.body, loop_state, stack)
                    return
                if not self._node_list_may_source(node.body):
                    self._apply_source_free_unknown_loop_body(node.body, state, stack)
                    return
                raise with_source_diagnostic(
                    exc,
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-condition",
                ) from exc

            should_run = condition_status == "true" if node.keyword == "while" else condition_status == "false"
            if condition_status == "unknown":
                if not self._node_list_may_source(node.body):
                    self._apply_source_free_unknown_loop_body(node.body, state, stack)
                    return
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-condition",
                    f"unsupported unknown {node.keyword} condition",
                    "Loop conditions must be exact before source-aware lowering.",
                )
            if not should_run:
                if iteration == 0:
                    self._disable_unreachable_sources(node.body, f"{node.keyword} {node.condition}")
                return

            state.loop_depth += 1
            try:
                self._evaluate_nodes(node.body, state, stack)
            except LoopContinueSignal:
                continue
            except LoopBreakSignal:
                return
            finally:
                state.loop_depth -= 1

        raise unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.loop-iteration",
            f"unsupported {node.keyword} loop exceeds modeled iteration limit",
            "Use finite loops whose source effects resolve within the modeled iteration limit.",
        )

    def _read_loop_words(self, node: WhileLoop, state: EvaluationState):
        if node.keyword != "while":
            return None
        if not node.trailing.startswith("<") and not node.producer:
            return None

        read_condition, include_incomplete, nonempty_word = self._split_read_loop_nonempty_tail(node.condition)
        condition_words = parse_shell_words_preserving_quotes(read_condition)
        if not condition_words:
            return None

        read_ifs = DEFAULT_IFS
        index = 0
        if condition_words[index].startswith("IFS="):
            read_ifs = self._read_loop_ifs_value(condition_words[index])
            index += 1
        if index >= len(condition_words) or strip_shell_word_quotes(condition_words[index]) != "read":
            return None
        index += 1

        while index < len(condition_words) and condition_words[index].startswith("-"):
            option = strip_shell_word_quotes(condition_words[index])
            if option == "-r":
                index += 1
                continue
            raise self._unsupported_loop_condition(node, f"unsupported read option: {option}")

        if index != len(condition_words) - 1:
            raise self._unsupported_loop_condition(node, "unsupported read loop condition")

        variable = strip_shell_word_quotes(condition_words[index])
        if not re.fullmatch(r'[a-zA-Z_]\w*', variable):
            raise self._unsupported_loop_condition(node, "unsupported read loop variable")
        if nonempty_word is not None and self._read_loop_nonempty_variable(nonempty_word) != variable:
            raise self._unsupported_loop_condition(node, "unsupported read loop nonempty guard")

        values = []
        child_shell, lines = self._read_loop_input_lines(node, state, include_incomplete)
        for line in lines:
            value = self._read_loop_value(line, read_ifs)
            values.append(value)
        return ReadLoopWords(variable, tuple(values), child_shell=child_shell)

    def _read_loop_input_lines(self, node: WhileLoop, state: EvaluationState, include_incomplete: bool):
        if node.producer:
            output = self._evaluate_safe_word_list_command(node.producer, node, state)
            return (
                self._producer_read_loop_uses_child_shell(node, state),
                self._read_loop_lines_from_content(output, include_incomplete),
            )

        process_substitution = self._read_loop_process_substitution(node.trailing)
        if process_substitution is not None:
            output = self._evaluate_safe_word_list_command(process_substitution, node, state)
            return False, self._read_loop_lines_from_content(output, include_incomplete)

        trailing_words = parse_shell_words_preserving_quotes(node.trailing)
        if len(trailing_words) != 2 or trailing_words[0] != "<":
            raise self._unsupported_loop_condition(node, "unsupported read loop redirection")

        input_path = self._word_list_path(strip_shell_word_quotes(trailing_words[1]), node, state)
        if not input_path.is_file():
            raise self._unsupported_loop_condition(node, "unsupported read loop input path")
        return False, self._read_loop_lines(input_path, include_incomplete)

    @staticmethod
    def _read_loop_process_substitution(trailing: str):
        match = re.fullmatch(r'<\s*<\((.*)\)\s*', trailing)
        return match.group(1).strip() if match else None

    def _producer_read_loop_uses_child_shell(self, node: WhileLoop, state: EvaluationState):
        if state.ambiguous_shell_options:
            raise self._unsupported_loop_condition(node, "unsupported read loop producer with ambiguous shell options")
        return "lastpipe" not in state.shell_options or "monitor" in state.shell_options

    @staticmethod
    def _split_read_loop_nonempty_tail(condition: str):
        match = re.match(
            r'^(.*?)\s*\|\|\s*(?:(?:\[\[\s+-n\s+(.+?)\s*\]\])|(?:\[\s+-n\s+(.+?)\s*\]))\s*$',
            condition,
        )
        if not match:
            return condition, False, None
        return match.group(1).strip(), True, (match.group(2) or match.group(3)).strip()

    @staticmethod
    def _read_loop_nonempty_variable(word: str):
        stripped = strip_shell_word_quotes(word.strip())
        match = re.fullmatch(r'\$(?:\{([a-zA-Z_]\w*)\}|([a-zA-Z_]\w*))', stripped)
        return (match.group(1) or match.group(2)) if match else None

    def _read_loop_ifs_value(self, word: str):
        _, value = word.split("=", 1)
        decoded = self._decode_ansi_c_quoted_word(value)
        return decoded if decoded != value else strip_shell_word_quotes(value)

    @staticmethod
    def _read_loop_lines(path: Path, include_incomplete: bool):
        with path.open("r", newline="") as file:
            content = file.read()
        return SourceEvaluator._read_loop_lines_from_content(content, include_incomplete)

    @staticmethod
    def _read_loop_lines_from_content(content: str, include_incomplete: bool):
        if not content:
            return []
        lines = content.split("\n")
        if content.endswith("\n"):
            return lines[:-1]
        return lines if include_incomplete else lines[:-1]

    @staticmethod
    def _read_loop_value(line: str, read_ifs: str):
        if read_ifs == "":
            return line
        ifs_whitespace = ''.join(char for char in read_ifs if char in " \t\n")
        return line.strip(ifs_whitespace) if ifs_whitespace else line

    def _resolve_loop_words(self, node: ForLoop, state: EvaluationState):
        if not node.is_exact:
            raise self._unsupported_loop_words(node, "unsupported loop word list")

        raw_words = self._loop_raw_words(node)
        if len(raw_words) != len(node.words):
            raise self._unsupported_loop_words(node, "unsupported loop word list syntax")

        words = []
        for word, raw_word in zip(node.words, raw_words):
            words.extend(self._expand_loop_word(word, raw_word, node, state))

        return words

    @staticmethod
    def _loop_words_need_exact_replacement(node: ForLoop):
        return '$(' in node.words_text or '`' in node.words_text

    def _loop_raw_words(self, node: ForLoop):
        try:
            return tuple(parse_shell_words_preserving_quotes(node.words_text))
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, "unsupported loop word list syntax") from exc

    def _expand_loop_word(self, word: str, raw_word: str, node: ForLoop, state: EvaluationState):
        if self._raw_word_is_single_quoted(raw_word):
            return [word]

        positional_words = self._expand_positional_loop_word(raw_word, state)
        if positional_words is not None:
            return positional_words

        if '$(' in word or '`' in word:
            return self._resolve_command_substitution_loop_word(word, raw_word, node, state)

        array_match = ARRAY_EXPANSION_PATTERN.match(word)
        if array_match:
            array_name = array_match.group(1)
            values = state.arrays.get(array_name)
            if values is None:
                raise self._unsupported_loop_words(node, f"loop word list references unknown array: {array_name}")
            return list(values)

        if (
            has_unquoted_glob(raw_word)
            or has_unquoted_brace_expansion(raw_word)
            or has_unquoted_extglob(raw_word)
        ):
            try:
                glob_word = resolve_variable_references(word, state.resolver_context())
                glob_word = os.path.expandvars(glob_word)
                return self._loop_glob_match_words(
                    expand_glob_word(
                        glob_word,
                        state.resolver_context(),
                        node.text,
                        raw_pattern=raw_word,
                        allow_missing_literal=True,
                    ),
                    state,
                )
            except FailglobExpansionError:
                raise
            except UnsupportedSourceError as exc:
                raise self._unsupported_loop_words(node, str(exc)) from exc

        if has_unquoted_glob(word):
            raise self._unsupported_loop_words(node, "unsupported quoted loop glob")

        for match in SCALAR_REFERENCE_PATTERN.finditer(word):
            variable_name = match.group(1) or match.group(2)
            if variable_name in state.ambiguous_variables:
                raise self._unsupported_loop_words(node, f"loop word list references branch-dependent variable: {variable_name}")
            if variable_name not in state.runtime_variables:
                raise self._unsupported_loop_words(node, f"loop word list references unknown variable: {variable_name}")

        if '$' in word:
            resolved_word = resolve_variable_references(word, state.runtime_context())

            if "$" in resolved_word:
                raise self._unsupported_loop_words(node, "loop word list contains unresolved scalar expansion")

            if self._raw_word_is_unquoted_scalar(raw_word):
                return self._split_scalar_loop_word(resolved_word, node, state)

            if any(char.isspace() for char in resolved_word) and not self._raw_word_is_double_quoted(raw_word):
                raise self._unsupported_loop_words(
                    node,
                    "unsupported loop word list contains whitespace after scalar expansion",
                )
            if has_unquoted_glob(raw_word) or has_unquoted_glob(resolved_word):
                raise self._unsupported_loop_words(
                    node,
                    "unsupported loop word list requires scalar glob expansion",
                )
            return [resolved_word]

        return [word]

    def _expand_positional_loop_word(self, raw_word: str, state: EvaluationState):
        if raw_word in {'"$@"', '"${@}"'}:
            if state.ambiguous_positionals:
                return None
            return list(state.positional_arguments)
        if raw_word in {'"$*"', '"${*}"'}:
            if state.ambiguous_positionals:
                return None
            return [self._joined_positionals(state)]
        return None

    def _resolve_command_substitution_loop_word(self, word: str, raw_word: str, node, state: EvaluationState):
        if '`' in raw_word or '`' in word:
            raise self._unsupported_loop_words(node, "loop word list uses backticks")

        expression = raw_word if '$(' in raw_word else word
        try:
            inner_command = extract_exact_command_substitution(expression)
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc
        if not inner_command:
            raise self._unsupported_loop_words(node, "loop word list is runtime-dynamic")

        if '$(' in inner_command or '`' in inner_command:
            raise self._unsupported_loop_words(node, "loop word list uses nested command substitution")

        try:
            output = self._evaluate_safe_word_list_command(inner_command, node, state)
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc

        if self._raw_word_is_double_quoted(raw_word):
            stripped_output = output.rstrip('\n')
            if not stripped_output:
                return []
            if '\n' in stripped_output:
                raise self._unsupported_loop_words(node, "quoted command substitution produced multiple lines")
            return [stripped_output]

        return self._split_word_list_output(output.rstrip('\n'), node, state)

    def _split_word_list_output(self, output: str, node, state: EvaluationState):
        words = []
        for field in self._split_ifs_fields_for_node(output, node, state):
            if has_unquoted_glob(field):
                try:
                    words.extend(
                        self._loop_glob_match_words(
                            expand_glob_word(
                                field,
                                state.resolver_context(),
                                node.text,
                                raw_pattern=field,
                                allow_missing_literal=True,
                            ),
                            state,
                        )
                    )
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
            else:
                words.append(field)
        return words

    def _evaluate_safe_word_list_command(self, inner_command: str, node, state: EvaluationState):
        if has_unsupported_shell_operator(inner_command):
            raise self._unsupported_loop_words(node, "unsupported command substitution syntax")

        words = parse_shell_words_preserving_quotes(inner_command)
        if not words:
            raise self._unsupported_loop_words(node, "empty command substitution")

        command_name = strip_shell_word_quotes(words[0])
        if command_name == "cat":
            return self._evaluate_cat_word_list(words, node, state)
        if command_name == "find":
            return self._evaluate_find_word_list(words, node, state)
        if command_name == "printf":
            return self._evaluate_printf_word_list(words, node, state)
        if command_name == "sort":
            return self._evaluate_sort_word_list(words, node, state)
        if command_name == "head":
            return self._evaluate_head_word_list(words, node, state)
        if command_name == "grep":
            return self._evaluate_grep_word_list(words, node, state)
        if command_name == "realpath":
            return self._evaluate_realpath_word_list(words, node, state)
        if command_name in {"dirname", "basename"}:
            return self._evaluate_path_transform_word_list(command_name, words, node, state)
        raise self._unsupported_loop_words(node, f"unsupported command substitution: {command_name}")

    def _evaluate_cat_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported cat command substitution")
        output = []
        for raw_path in words[1:]:
            path_word = strip_shell_word_quotes(raw_path)
            if path_word.startswith("-"):
                raise self._unsupported_loop_words(node, "unsupported cat command substitution option")
            path = self._word_list_path(path_word, node, state)
            if not path.is_file():
                raise self._unsupported_loop_words(node, "unsupported cat command substitution path")
            output.append(self._read_text_preserving_newlines(path))
        return ''.join(output)

    def _evaluate_find_word_list(self, words: list[str], node, state: EvaluationState):
        stripped_words = [strip_shell_word_quotes(word) for word in words]
        try:
            parsed_find = SOURCE_RESOLVER.parse_find_command(stripped_words, state.resolver_context())
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc
        if not parsed_find:
            raise self._unsupported_loop_words(node, "unsupported find command substitution")

        roots, filters = parsed_find
        root_words = self._find_root_words(stripped_words)
        matches = self._find_word_list_matches(root_words, roots, filters, node, state)
        return self._lines_output(matches)

    @staticmethod
    def _find_root_words(words: list[str]):
        roots = []
        index = 1
        while index < len(words) and not words[index].startswith("-"):
            roots.append(words[index])
            index += 1
        return roots or ["."]

    def _find_word_list_matches(self, root_words: list[str], roots: list[str], filters: dict, node,
                                state: EvaluationState):
        matches = []
        for root_word, root in zip(root_words, roots):
            display_root = self._resolve_exact_runtime_word(root_word, node, state, "loop word list")
            for directory, dirnames, filenames in os.walk(root):
                relative_directory = os.path.relpath(directory, root)
                directory_depth = 0 if relative_directory == os.curdir else len(relative_directory.split(os.sep))
                maxdepth = filters['maxdepth']
                if maxdepth is not None and directory_depth >= maxdepth:
                    dirnames[:] = []

                for filename in filenames:
                    candidate = os.path.join(directory, filename)
                    candidate_depth = directory_depth + 1
                    if candidate_depth < filters['mindepth']:
                        continue
                    if maxdepth is not None and candidate_depth > maxdepth:
                        continue
                    if not os.path.isfile(candidate):
                        continue
                    display_path = self._find_display_path(display_root, root, candidate)
                    if filters['name'] and not any(fnmatch(filename, pattern) for pattern in filters['name']):
                        continue
                    if filters['path'] and not any(fnmatch(display_path, pattern) for pattern in filters['path']):
                        continue

                    matches.append(display_path)
                    if filters.get('quit'):
                        return matches
        return matches

    @staticmethod
    def _find_display_path(display_root: str, resolved_root: str, candidate: str):
        relative = os.path.relpath(candidate, resolved_root)
        if relative == os.curdir:
            return display_root
        return os.path.join(display_root, relative)

    def _evaluate_printf_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported printf command substitution")
        format_word = strip_shell_word_quotes(words[1])
        if format_word not in {"%s\\n", "%s\n"}:
            raise self._unsupported_loop_words(node, "unsupported printf command substitution format")
        values = [
            self._resolve_exact_runtime_word(strip_shell_word_quotes(word), node, state, "loop word list")
            for word in words[2:]
        ]
        return self._lines_output(values)

    def _evaluate_sort_word_list(self, words: list[str], node, state: EvaluationState):
        unique = False
        path_words = []
        for raw_word in words[1:]:
            word = strip_shell_word_quotes(raw_word)
            if word == "-u":
                unique = True
                continue
            if word.startswith("-"):
                raise self._unsupported_loop_words(node, "unsupported sort command substitution option")
            path_words.append(raw_word)
        if not path_words:
            raise self._unsupported_loop_words(node, "unsupported sort command substitution without file operands")

        lines = []
        for _, path in self._word_list_path_pairs(path_words, node, state):
            lines.extend(self._command_output_lines(self._read_text_preserving_newlines(path)))
        sorted_lines = sorted(lines)
        if unique:
            sorted_lines = list(dict.fromkeys(sorted_lines))
        return self._lines_output(sorted_lines)

    def _evaluate_head_word_list(self, words: list[str], node, state: EvaluationState):
        count = None
        index = 1
        if index < len(words):
            first = strip_shell_word_quotes(words[index])
            if first == "-n":
                if index + 1 >= len(words):
                    raise self._unsupported_loop_words(node, "unsupported head command substitution count")
                count = self._head_count(strip_shell_word_quotes(words[index + 1]), node)
                index += 2
            elif re.fullmatch(r'-\d+', first):
                count = self._head_count(first[1:], node)
                index += 1
        if count is None:
            count = 10

        path_words = words[index:]
        if len(path_words) != 1:
            raise self._unsupported_loop_words(node, "unsupported head command substitution operands")
        _, path = self._word_list_path_pairs(path_words, node, state)[0]
        return ''.join(self._read_text_preserving_newlines(path).splitlines(keepends=True)[:count])

    def _evaluate_grep_word_list(self, words: list[str], node, state: EvaluationState):
        literal = False
        extended_regex = False
        list_matches = False
        index = 1
        while index < len(words):
            option = strip_shell_word_quotes(words[index])
            if not option.startswith("-") or option == "-":
                break
            if option == "--":
                index += 1
                break
            for flag in option[1:]:
                if flag == "l":
                    list_matches = True
                elif flag == "F":
                    literal = True
                elif flag == "E":
                    extended_regex = True
                else:
                    raise self._unsupported_loop_words(node, "unsupported grep command substitution option")
            index += 1

        if not list_matches or literal == extended_regex:
            raise self._unsupported_loop_words(node, "unsupported grep command substitution mode")
        if index >= len(words):
            raise self._unsupported_loop_words(node, "unsupported grep command substitution pattern")
        pattern = strip_shell_word_quotes(words[index])
        path_words = words[index + 1:]
        if not path_words:
            raise self._unsupported_loop_words(node, "unsupported grep command substitution without file operands")

        regex = None
        if extended_regex:
            self._ensure_supported_regex_pattern(pattern, node.text, "grep regex")
            regex = re.compile(pattern)

        output = []
        for display_word, path in self._word_list_path_pairs(path_words, node, state):
            content = self._read_text_preserving_newlines(path)
            matched = pattern in content if literal else bool(regex.search(content))
            if matched:
                output.append(display_word)
        return self._lines_output(output)

    def _evaluate_realpath_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported realpath command substitution without operands")
        paths = self._word_list_path_pairs(words[1:], node, state)
        return self._lines_output([str(path.resolve()) for _, path in paths])

    def _evaluate_path_transform_word_list(self, command_name: str, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution without operands")
        index = 1
        option_like_operands = False
        if strip_shell_word_quotes(words[index]) == "--":
            index += 1
            option_like_operands = True
        operand_words = words[index:]
        if not operand_words:
            raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution without operands")
        for word in operand_words:
            if not option_like_operands and strip_shell_word_quotes(word).startswith("-"):
                raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution option")

        values = [
            self._resolve_exact_runtime_word(strip_shell_word_quotes(word), node, state, "loop word list")
            for word in operand_words
        ]
        if command_name == "basename":
            if len(values) > 2:
                raise self._unsupported_loop_words(node, "unsupported basename command substitution operands")
            return self._lines_output([shell_utility_basename(*values)])

        transform = shell_utility_dirname
        return self._lines_output([transform(value) for value in values])

    def _word_list_path_pairs(self, raw_words: list[str], node, state: EvaluationState):
        pairs = []
        for raw_word in raw_words:
            stripped = strip_shell_word_quotes(raw_word)
            if has_unquoted_glob(raw_word) or has_unquoted_brace_expansion(raw_word) or has_unquoted_extglob(raw_word):
                try:
                    for match in expand_glob_word(stripped, state.resolver_context(), node.text, raw_pattern=raw_word):
                        pairs.append((match.word, Path(match.path)))
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
                continue
            path = self._word_list_path(stripped, node, state)
            if not path.is_file():
                raise self._unsupported_loop_words(node, "unsupported command substitution path")
            pairs.append((self._resolve_exact_runtime_word(stripped, node, state, "loop word list"), path))
        return pairs

    @staticmethod
    def _head_count(value: str, node):
        if not re.fullmatch(r'\d+', value):
            raise SourceEvaluator._unsupported_loop_words(node, "unsupported head command substitution count")
        return int(value)

    @staticmethod
    def _read_text_preserving_newlines(path: Path):
        with path.open("r", newline="") as file:
            return file.read()

    @staticmethod
    def _command_output_lines(content: str):
        if not content:
            return []
        lines = content.split("\n")
        return lines[:-1] if content.endswith("\n") else lines

    @staticmethod
    def _lines_output(lines: list[str]):
        return "\n".join(lines) + ("\n" if lines else "")

    def _word_list_path(self, word: str, node, state: EvaluationState):
        resolved = self._resolve_exact_runtime_word(word, node, state, "loop word list")
        path = Path(resolved)
        if not path.is_absolute():
            path = state.cwd / path
        return path.resolve()

    @staticmethod
    def _resolve_exact_runtime_word(word: str, node, state: EvaluationState, label: str):
        for match in SCALAR_REFERENCE_PATTERN.finditer(word):
            variable_name = match.group(1) or match.group(2)
            if variable_name in state.ambiguous_variables:
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-word-list",
                    f"unsupported {label} references branch-dependent variable: {variable_name}",
                    "Use exact values before source-aware loop evaluation.",
                )
        resolved = resolve_variable_references(word, state.runtime_context())
        resolved = os.path.expandvars(resolved)
        if "$" in resolved:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.loop-word-list",
                f"unsupported {label} contains unresolved scalar expansion",
                "Use exact values before source-aware loop evaluation.",
            )
        return strip_shell_word_quotes(resolved)

    def _split_scalar_loop_word(self, resolved_word: str, node: ForLoop, state: EvaluationState):
        words = []
        for field in self._split_ifs_fields_for_node(resolved_word, node, state):
            if has_unquoted_glob(field):
                try:
                    words.extend(
                        self._loop_glob_match_words(
                            expand_glob_word(
                                field,
                                state.resolver_context(),
                                node.text,
                                raw_pattern=field,
                                allow_missing_literal=True,
                            ),
                            state,
                        )
                    )
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
            else:
                words.append(field)
        return words

    @staticmethod
    def _loop_glob_match_words(matches, state: EvaluationState):
        words = []
        for match in matches:
            if not match.exists:
                state.missing_source_words.add(match.word)
            words.append(match.word)
        return words

    def _split_ifs_fields_for_node(self, text: str, node, state: EvaluationState):
        if "IFS" in state.ambiguous_variables:
            if isinstance(node, ArrayAssignment):
                raise self._unsupported_array_assignment(
                    node,
                    "array word splitting references branch-dependent IFS",
                )
            raise self._unsupported_loop_words(node, "loop word splitting references branch-dependent IFS")
        return self._split_ifs_fields(text, state.runtime_variables.get("IFS", DEFAULT_IFS))

    @staticmethod
    def _split_ifs_fields(text: str, ifs: str):
        if ifs == "":
            return [text] if text else []

        ifs_whitespace = ''.join(char for char in ifs if char in " \t\n")
        ifs_other = ''.join(dict.fromkeys(char for char in ifs if char not in " \t\n"))

        if not ifs_other:
            stripped = text.strip(ifs_whitespace)
            if not stripped:
                return []
            return [
                field
                for field in re.split(f"[{re.escape(ifs_whitespace)}]+", stripped)
                if field
            ]

        delimiter_pattern_parts = [f"[{re.escape(ifs_other)}]"]
        if ifs_whitespace:
            delimiter_pattern_parts = [
                f"[{re.escape(ifs_whitespace)}]*[{re.escape(ifs_other)}][{re.escape(ifs_whitespace)}]*",
                f"[{re.escape(ifs_whitespace)}]+",
            ]
            text = text.strip(ifs_whitespace)
        fields = re.split("|".join(delimiter_pattern_parts), text)
        while fields and fields[-1] == "":
            fields.pop()
        return fields

    @staticmethod
    def _raw_word_is_unquoted_scalar(raw_word: str):
        stripped = raw_word.strip()
        return not stripped.startswith(('"', "'")) and bool(SCALAR_WORD_PATTERN.match(stripped))

    @staticmethod
    def _raw_word_is_single_quoted(raw_word: str):
        stripped = raw_word.strip()
        return len(stripped) >= 2 and stripped[0] == stripped[-1] == "'"

    @staticmethod
    def _raw_word_is_double_quoted(raw_word: str):
        stripped = raw_word.strip()
        return len(stripped) >= 2 and stripped[0] == stripped[-1] == '"'

    @staticmethod
    def _unsupported_loop_words(node, message: str):
        if "loop word" not in message:
            message = f"unsupported loop word list: {message}"
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.loop-word-list",
            message,
            "Use a literal finite list, known scalar variables, exact command substitutions, or an exact ${array[@]} expansion.",
        )

    @staticmethod
    def _unsupported_loop_condition(node: WhileLoop, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.loop-condition",
            message,
            "while/until loops must have exact bounded conditions or a supported read redirection.",
        )

    def _apply_if_block(self, node: IfBlock, state: EvaluationState, stack: tuple[Path, ...]):
        if any(branch.condition and self._condition_text_may_source(branch.condition) for branch in node.branches):
            self._apply_source_condition_if_block(node, state, stack)
            return

        outer_occurrence_context = state.occurrence_context
        outer_condition_context = state.condition_context
        statuses = []
        for branch in node.branches:
            if branch.condition is None:
                statuses.append("else")
                continue
            try:
                statuses.append(self._evaluate_condition(
                    branch.condition,
                    state,
                    node,
                    stack,
                    branch,
                ))
            except UnsupportedSourceError as exc:
                if self.mode == "context" or not self._raw_command_may_source(branch.condition):
                    statuses.append("unknown")
                    continue
                raise with_source_diagnostic(
                    exc,
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.if-condition",
                ) from exc

        if self.mode == "context":
            self._apply_context_if_block(node, state, stack, statuses)
            state.occurrence_context = outer_occurrence_context
            state.condition_context = outer_condition_context
            return

        base_state = state.child_shell_copy()
        branch_outcomes = []
        branch_reachability = self._if_branch_reachability(statuses)
        occurrence_model = (
            OccurrenceModel.MUTUALLY_EXCLUSIVE
            if len(node.branches) > 1
            else OccurrenceModel.CONDITIONAL
        )
        for branch, is_reachable in zip(node.branches, branch_reachability):
            if not is_reachable:
                self._disable_unreachable_sources(branch.body, branch.condition or "else")
                branch_outcomes.append(EvaluationOutcome(base_state.child_shell_copy()))
                continue

            branch_state = state.child_shell_copy()
            branch_state.occurrence_context = occurrence_model
            branch_state.condition_context = branch.condition or "else"
            return_signal = None
            try:
                self._evaluate_nodes(branch.body, branch_state, stack)
            except (FunctionReturnSignal, SourceReturnSignal) as signal:
                return_signal = signal
            branch_outcomes.append(EvaluationOutcome(branch_state, return_signal))

        possible_outcomes = self._possible_if_outcomes(statuses, base_state, branch_outcomes)
        try:
            self._apply_possible_outcomes(node, state, possible_outcomes)
        finally:
            state.occurrence_context = outer_occurrence_context
            state.condition_context = outer_condition_context

    def _apply_source_condition_if_block(self, node: IfBlock, state: EvaluationState, stack: tuple[Path, ...]):
        outer_occurrence_context = state.occurrence_context
        outer_condition_context = state.condition_context
        occurrence_model = (
            OccurrenceModel.MUTUALLY_EXCLUSIVE
            if len(node.branches) > 1
            else OccurrenceModel.CONDITIONAL
        )

        active_outcomes = [EvaluationOutcome(state.child_shell_copy())]
        completed_outcomes = []

        try:
            for branch in node.branches:
                branch_context = branch.condition or "else"
                if not active_outcomes:
                    self._disable_branch_condition_sources(branch, branch_context)
                    self._disable_unreachable_sources(branch.body, branch_context)
                    continue

                if branch.condition is None:
                    for outcome in active_outcomes:
                        completed_outcomes.append(
                            self._evaluate_if_branch_body(
                                branch,
                                outcome.state,
                                stack,
                                occurrence_model,
                                branch_context,
                            )
                        )
                    active_outcomes = []
                    continue

                next_active_outcomes = []
                body_reachable = False
                for outcome in active_outcomes:
                    condition_state = outcome.state.child_shell_copy()
                    condition_state.condition_context = branch.condition
                    try:
                        status = self._evaluate_condition(
                            branch.condition,
                            condition_state,
                            node,
                            stack,
                            branch,
                        )
                    except SourceConditionExpansionFailureSignal as signal:
                        state.copy_from(condition_state)
                        self._record_if_block_expansion_failure(node, signal.pattern, state)
                        return
                    except UnsupportedSourceError as exc:
                        if (
                            self.mode == "context"
                            or not self._condition_has_source_atom(branch.condition)
                        ):
                            status = "unknown"
                        else:
                            raise with_source_diagnostic(
                                exc,
                                str((branch.condition_location or node.location).path),
                                (branch.condition_location or node.location).line - 1,
                                branch.condition_text or node.text,
                                branch.condition,
                                "unsupported.source.if-condition",
                            ) from exc

                    if status in {"true", "unknown"}:
                        body_reachable = True
                        completed_outcomes.append(
                            self._evaluate_if_branch_body(
                                branch,
                                condition_state,
                                stack,
                                occurrence_model,
                                branch_context,
                            )
                        )
                    if status in {"false", "unknown"}:
                        next_active_outcomes.append(EvaluationOutcome(condition_state))

                if not body_reachable:
                    self._disable_unreachable_sources(branch.body, branch_context)
                active_outcomes = next_active_outcomes

            completed_outcomes.extend(active_outcomes)
            if self.mode == "context":
                selected = [outcome for outcome in completed_outcomes if outcome.return_signal is None]
                selected = selected or completed_outcomes
                self._merge_possible_states(state, [outcome.state for outcome in selected])
                return
            self._apply_possible_outcomes(node, state, completed_outcomes)
        finally:
            state.occurrence_context = outer_occurrence_context
            state.condition_context = outer_condition_context

    def _evaluate_if_branch_body(
        self,
        branch,
        input_state: EvaluationState,
        stack: tuple[Path, ...],
        occurrence_model: OccurrenceModel,
        condition_context: str,
    ):
        branch_state = input_state.child_shell_copy()
        branch_state.occurrence_context = occurrence_model
        branch_state.condition_context = condition_context
        return_signal = None
        try:
            self._evaluate_nodes(branch.body, branch_state, stack)
        except (FunctionReturnSignal, SourceReturnSignal) as signal:
            return_signal = signal
        return EvaluationOutcome(branch_state, return_signal)

    def _apply_context_if_block(
        self,
        node: IfBlock,
        state: EvaluationState,
        stack: tuple[Path, ...],
        statuses: list[str],
    ):
        branch_outcomes = []
        occurrence_model = (
            OccurrenceModel.MUTUALLY_EXCLUSIVE
            if len(node.branches) > 1
            else OccurrenceModel.CONDITIONAL
        )

        base_state = state.child_shell_copy()
        branch_reachability = self._if_branch_reachability(statuses)
        for index, branch in enumerate(node.branches):
            is_reachable = branch_reachability[index]
            if not is_reachable:
                branch_outcomes.append(EvaluationOutcome(base_state.child_shell_copy()))
                continue

            branch_state = base_state.child_shell_copy()
            branch_state.occurrence_context = occurrence_model
            branch_state.condition_context = branch.condition or "else"
            return_signal = None
            try:
                self._evaluate_nodes(branch.body, branch_state, stack)
            except (FunctionReturnSignal, SourceReturnSignal) as signal:
                return_signal = signal
            branch_outcomes.append(EvaluationOutcome(branch_state, return_signal))

        possible_outcomes = self._possible_if_outcomes(statuses, base_state, branch_outcomes)
        continuing_outcomes = [outcome for outcome in possible_outcomes if outcome.return_signal is None]
        returning_outcomes = [outcome for outcome in possible_outcomes if outcome.return_signal is not None]
        self._merge_possible_states(
            state,
            [outcome.state for outcome in continuing_outcomes or returning_outcomes],
        )

    @staticmethod
    def _if_branch_reachability(statuses: list[str]):
        reachable = []
        fallthrough_possible = True

        for status in statuses:
            if not fallthrough_possible or status == "false":
                reachable.append(False)
                continue

            reachable.append(True)
            if status in {"true", "else"}:
                fallthrough_possible = False

        return reachable

    @staticmethod
    def _possible_if_outcomes(
        statuses: list[str],
        base_state: EvaluationState,
        branch_outcomes: list[EvaluationOutcome],
    ):
        if not statuses:
            return [EvaluationOutcome(base_state)]

        if "unknown" not in statuses:
            for status, branch_outcome in zip(statuses, branch_outcomes):
                if status in {"true", "else"}:
                    return [branch_outcome]
            return [EvaluationOutcome(base_state)]

        possible_outcomes = []
        fallthrough_possible = True
        for status, branch_outcome in zip(statuses, branch_outcomes):
            if not fallthrough_possible:
                break
            if status == "false":
                continue
            if status == "true":
                possible_outcomes.append(branch_outcome)
                fallthrough_possible = False
            elif status == "else":
                possible_outcomes.append(branch_outcome)
                fallthrough_possible = False
            else:
                possible_outcomes.append(branch_outcome)

        if fallthrough_possible:
            possible_outcomes.append(EvaluationOutcome(base_state))
        return possible_outcomes

    def _apply_possible_outcomes(self, node, state: EvaluationState, outcomes: list[EvaluationOutcome]):
        returning_outcomes = [outcome for outcome in outcomes if outcome.return_signal is not None]
        continuing_outcomes = [outcome for outcome in outcomes if outcome.return_signal is None]
        return_kind = "source" if (
            returning_outcomes
            and isinstance(returning_outcomes[0].return_signal, SourceReturnSignal)
        ) else "function"

        if returning_outcomes and continuing_outcomes:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-control",
                f"unsupported branch-dependent {return_kind} return",
                f"Make {return_kind} return flow exact before later source-aware effects.",
            )

        selected_outcomes = returning_outcomes or continuing_outcomes
        if returning_outcomes:
            first_status = returning_outcomes[0].return_signal.status
            if any(
                outcome.return_signal.status != first_status
                or type(outcome.return_signal) is not type(returning_outcomes[0].return_signal)
                for outcome in returning_outcomes
            ):
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.function-control",
                    f"unsupported branch-dependent {return_kind} return",
                    f"Make {return_kind} return status exact before later source-aware effects.",
                )
        self._merge_possible_states(state, [outcome.state for outcome in selected_outcomes])
        if returning_outcomes:
            raise returning_outcomes[0].return_signal

    def _apply_case_block(self, node: CaseBlock, state: EvaluationState, stack: tuple[Path, ...]):
        outer_occurrence_context = state.occurrence_context
        outer_condition_context = state.condition_context
        try:
            subject_value = self._case_subject_value(node.subject, state)
            self._validate_case_patterns(node, state)
        except UnsupportedSourceError as exc:
            if self.mode == "context":
                subject_value = None
            else:
                raise self._unsupported_case(
                    node,
                    exc.code or "unsupported.source.case",
                    str(exc),
                    exc.hint,
                ) from exc

        if self.mode == "executable":
            self._ensure_case_terminators_supported(node)

        if self.mode == "context":
            self._apply_context_case_block(node, state, stack, subject_value)
            state.occurrence_context = outer_occurrence_context
            state.condition_context = outer_condition_context
            return

        if subject_value is None:
            try:
                self._apply_unknown_case_block(node, state, stack)
            finally:
                state.occurrence_context = outer_occurrence_context
                state.condition_context = outer_condition_context
            return

        base_state = state.child_shell_copy()
        reachable_arms = self._case_arm_reachability(node, subject_value, state)
        for arm, is_reachable in zip(node.arms, reachable_arms):
            if not is_reachable:
                self._disable_unreachable_sources(arm.body, self._case_arm_condition(node, arm))

        possible_outcomes = [
            self._evaluate_case_execution_path(node, state, stack, reachable_arms)
        ] if any(reachable_arms) else [EvaluationOutcome(base_state)]
        try:
            self._apply_possible_outcomes(node, state, possible_outcomes)
        finally:
            state.occurrence_context = outer_occurrence_context
            state.condition_context = outer_condition_context

    def _evaluate_case_execution_path(
        self,
        node: CaseBlock,
        state: EvaluationState,
        stack: tuple[Path, ...],
        reachable_arms: list[bool],
    ):
        arm_state = state.child_shell_copy()
        occurrence_model = self._case_occurrence_model(node)
        return_signal = None

        for arm, is_reachable in zip(node.arms, reachable_arms):
            if not is_reachable:
                continue
            arm_state.occurrence_context = occurrence_model
            arm_state.condition_context = self._case_arm_condition(node, arm)
            try:
                self._evaluate_nodes(arm.body, arm_state, stack)
            except (FunctionReturnSignal, SourceReturnSignal) as signal:
                return_signal = signal
                break

        return EvaluationOutcome(arm_state, return_signal)

    def _apply_context_case_block(
        self,
        node: CaseBlock,
        state: EvaluationState,
        stack: tuple[Path, ...],
        subject_value: str | None,
    ):
        occurrence_model = self._case_occurrence_model(node)
        arm_outcomes = []

        for arm in node.arms:
            arm_state = state.child_shell_copy()
            arm_state.occurrence_context = occurrence_model
            arm_state.condition_context = self._case_arm_condition(node, arm)
            return_signal = None
            try:
                self._evaluate_nodes(arm.body, arm_state, stack)
            except (FunctionReturnSignal, SourceReturnSignal) as signal:
                return_signal = signal
            arm_outcomes.append(EvaluationOutcome(arm_state, return_signal))

        if subject_value is None:
            possible_outcomes = arm_outcomes
            if not self._case_has_default_arm(node, state):
                possible_outcomes.append(EvaluationOutcome(state.child_shell_copy()))
        else:
            reachable_arms = self._case_arm_reachability(node, subject_value, state)
            possible_outcomes = [
                arm_outcome
                for arm_outcome, is_reachable in zip(arm_outcomes, reachable_arms)
                if is_reachable
            ] or [EvaluationOutcome(state.child_shell_copy())]

        selected_states = [
            outcome.state
            for outcome in possible_outcomes
            if outcome.return_signal is None
        ] or [outcome.state for outcome in possible_outcomes]
        self._merge_possible_states(state, selected_states)

    def _apply_unknown_case_block(self, node: CaseBlock, state: EvaluationState, stack: tuple[Path, ...]):
        arm_outcomes = []
        occurrence_model = self._case_occurrence_model(node)

        for arm in node.arms:
            arm_state = state.child_shell_copy()
            arm_state.occurrence_context = occurrence_model
            arm_state.condition_context = self._case_arm_condition(node, arm)
            return_signal = None
            try:
                self._evaluate_nodes(arm.body, arm_state, stack)
            except (FunctionReturnSignal, SourceReturnSignal) as signal:
                return_signal = signal
            arm_outcomes.append(EvaluationOutcome(arm_state, return_signal))

        possible_outcomes = arm_outcomes
        if not self._case_has_default_arm(node, state):
            possible_outcomes.append(EvaluationOutcome(state.child_shell_copy()))
        self._apply_possible_outcomes(node, state, possible_outcomes)

    def _case_subject_value(self, subject: str, state: EvaluationState):
        subject = subject.strip()
        if self._is_single_quoted_word(subject):
            return subject[1:-1]
        if self._contains_case_command_substitution(subject):
            raise UnsupportedSourceError(
                f"unsupported dynamic case subject: {subject}",
                code="unsupported.source.case-subject",
                hint="Use a literal, known scalar variable, or environment-provided subject.",
            )
        if ARRAY_INDEX_PATTERN.search(subject):
            raise UnsupportedSourceError(
                f"unsupported array case subject: {subject}",
                code="unsupported.source.case-subject",
                hint="Array case subjects need explicit array semantics.",
            )

        value = self._condition_value(subject, state)
        if value is not None:
            return value

        if "'" in subject:
            return None

        expanded = os.path.expandvars(strip_matching_quotes(subject))
        return None if "$" in expanded else expanded

    @staticmethod
    def _is_single_quoted_word(value: str):
        return len(value) >= 2 and value[0] == value[-1] == "'" and value.count("'") == 2

    @staticmethod
    def _contains_case_command_substitution(text: str):
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

            if not in_single_quote and (text.startswith("$(", index) or char == "`"):
                return True

            index += 1
        return False

    def _validate_case_patterns(self, node: CaseBlock, state: EvaluationState):
        for arm in node.arms:
            for pattern in arm.patterns:
                self._validate_case_pattern(pattern, state)

    def _validate_case_pattern(self, pattern: str, state: EvaluationState):
        stripped_pattern = pattern.strip()
        if self._contains_case_command_substitution(stripped_pattern):
            raise UnsupportedSourceError(
                f"unsupported dynamic case pattern: {stripped_pattern}",
                code="unsupported.source.case-pattern",
                hint="Use literal case patterns in the modeled subset.",
            )
        if has_unquoted_extglob(stripped_pattern) and "extglob" not in state.glob_options:
            raise UnsupportedSourceError(
                f"unsupported disabled extglob case pattern: {stripped_pattern}",
                code="unsupported.source.case-pattern",
                hint="Enable extglob exactly before source-bearing case patterns that use extglob syntax.",
            )
        self._case_pattern_regex(stripped_pattern, state)

    def _ensure_case_terminators_supported(self, node: CaseBlock):
        for arm in node.arms:
            if arm.terminator not in {";;", ";&", ";;&"}:
                raise self._unsupported_case(
                    node,
                    "unsupported.source.case-terminator",
                    f"unsupported case terminator: {arm.terminator}",
                    "Case fallthrough terminators need explicit fallthrough semantics.",
                )

    def _case_arm_reachability(self, node: CaseBlock, subject_value: str, state: EvaluationState):
        reachable = [False] * len(node.arms)
        mode = "test"

        for index, arm in enumerate(node.arms):
            is_reachable = mode == "execute" or (
                mode == "test" and self._case_arm_matches(arm, subject_value, state)
            )
            reachable[index] = is_reachable
            if not is_reachable:
                continue

            if arm.terminator == ";;":
                break
            if arm.terminator == ";&":
                mode = "execute"
                continue
            mode = "test"
        return reachable

    @staticmethod
    def _case_occurrence_model(node: CaseBlock):
        if len(node.arms) <= 1:
            return OccurrenceModel.CONDITIONAL
        if any(arm.terminator != ";;" for arm in node.arms):
            return OccurrenceModel.CONDITIONAL
        return OccurrenceModel.MUTUALLY_EXCLUSIVE

    def _case_arm_matches(self, arm, subject_value: str, state: EvaluationState):
        return any(self._case_pattern_matches(pattern, subject_value, state) for pattern in arm.patterns)

    def _case_pattern_matches(self, pattern: str, subject_value: str, state: EvaluationState):
        try:
            regex = self._case_pattern_regex(pattern.strip(), state)
        except UnsupportedSourceError:
            return False
        return bool(regex.fullmatch(subject_value))

    def _case_pattern_regex(self, pattern: str, state: EvaluationState):
        flags = re.S | (re.I if "nocasematch" in state.shell_options else 0)
        return re.compile(rf'\A{self._case_pattern_regex_source(pattern, state)}\Z', flags)

    def _case_pattern_regex_source(
        self,
        pattern: str,
        state: EvaluationState,
        *,
        allow_variables: bool = True,
    ):
        output = []
        in_single_quote = False
        in_double_quote = False
        index = 0

        while index < len(pattern):
            char = pattern[index]
            if allow_variables and char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                index += 1
                continue

            if allow_variables and char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                index += 1
                continue

            if char == "\\" and not in_single_quote:
                if index + 1 >= len(pattern):
                    output.append(re.escape("\\"))
                    index += 1
                    continue
                output.append(re.escape(pattern[index + 1]))
                index += 2
                continue

            if (
                allow_variables
                and not in_single_quote
                and char == "$"
                and (match := SCALAR_REFERENCE_PATTERN.match(pattern, index))
            ):
                name = match.group(1) or match.group(2)
                value = self._case_pattern_variable_value(name, state, pattern)
                if in_double_quote:
                    output.append(re.escape(value))
                else:
                    output.append(
                        self._case_pattern_regex_source(
                            value,
                            state,
                            allow_variables=False,
                        )
                    )
                index = match.end()
                continue

            if in_single_quote or in_double_quote:
                output.append(re.escape(char))
                index += 1
                continue

            operator = extglob_operator_at(pattern, index)
            if operator is not None:
                if "extglob" not in state.glob_options:
                    raise UnsupportedSourceError(
                        f"unsupported disabled extglob case pattern: {pattern}",
                        code="unsupported.source.case-pattern",
                        hint="Enable extglob exactly before source-bearing case patterns that use extglob syntax.",
                    )
                body, group_end = self._case_extglob_body(pattern, index)
                alternatives = split_extglob_alternatives(body)
                if not alternatives:
                    raise UnsupportedSourceError(
                        f"unsupported empty extglob case pattern: {pattern}",
                        code="unsupported.source.case-pattern",
                        hint="Extglob case patterns must have at least one alternative.",
                    )
                alternative_sources = [
                    self._case_pattern_regex_source(
                        alternative,
                        state,
                        allow_variables=allow_variables,
                    )
                    for alternative in alternatives
                ]
                alternative_group = "|".join(alternative_sources)
                if operator == "@":
                    output.append(f"(?:{alternative_group})")
                elif operator == "?":
                    output.append(f"(?:(?:{alternative_group}))?")
                elif operator == "*":
                    output.append(f"(?:(?:{alternative_group}))*")
                elif operator == "+":
                    output.append(f"(?:(?:{alternative_group}))+")
                elif operator == "!":
                    rest_source = self._case_pattern_regex_source(
                        pattern[group_end + 1:],
                        state,
                        allow_variables=allow_variables,
                    )
                    output.append(f"(?!(?:{alternative_group}){rest_source}\\Z).*?")
                index = group_end + 1
                continue

            if char == "*":
                output.append(".*")
                index += 1
                continue
            if char == "?":
                output.append(".")
                index += 1
                continue
            if char == "[":
                translated, next_index = self._case_bracket_regex(pattern, index)
                output.append(translated)
                index = next_index
                continue

            output.append(re.escape(char))
            index += 1

        if in_single_quote or in_double_quote:
            raise UnsupportedSourceError(
                f"unsupported unterminated quote in case pattern: {pattern}",
                code="unsupported.source.case-pattern",
                hint="Case patterns must have balanced quotes.",
            )
        return ''.join(output)

    @staticmethod
    def _case_extglob_body(pattern: str, operator_index: int):
        try:
            return read_extglob_body(pattern, operator_index + 2)
        except UnsupportedPatternError as exc:
            raise UnsupportedSourceError(
                f"unsupported extglob case pattern: {pattern} ({exc})",
                code="unsupported.source.case-pattern",
                hint="Extglob case patterns must be balanced.",
            ) from exc

    @staticmethod
    def _case_pattern_variable_value(name: str, state: EvaluationState, pattern: str):
        if name in state.ambiguous_variables:
            raise UnsupportedSourceError(
                f"unsupported branch-dependent variable case pattern: {pattern}",
                code="unsupported.source.case-pattern",
                hint="Variable-expanded case patterns must be exact.",
            )
        if name in state.runtime_variables:
            return state.runtime_variables[name]
        if name in os.environ:
            return os.environ[name]
        raise UnsupportedSourceError(
            f"unsupported unresolved variable case pattern: {pattern}",
            code="unsupported.source.case-pattern",
            hint="Variable-expanded case patterns must be exact.",
        )

    def _case_bracket_regex(self, pattern: str, start: int):
        end = self._case_bracket_end(pattern, start)
        if end is None:
            return re.escape("["), start + 1
        content = pattern[start + 1:end]
        return self._translate_case_bracket_content(content, pattern), end + 1

    @staticmethod
    def _case_bracket_end(pattern: str, start: int):
        index = start + 1
        if index < len(pattern) and pattern[index] in {"!", "^"}:
            index += 1
        if index < len(pattern) and pattern[index] == "]":
            index += 1
        while index < len(pattern):
            if pattern.startswith("[:", index):
                class_end = pattern.find(":]", index + 2)
                if class_end >= 0:
                    index = class_end + 2
                    continue
            if pattern[index] == "]":
                return index
            index += 1
        return None

    def _translate_case_bracket_content(self, content: str, pattern: str):
        if not content:
            raise UnsupportedSourceError(
                f"unsupported empty bracket case pattern: {pattern}",
                code="unsupported.source.case-pattern",
                hint="Case bracket patterns must be exact.",
            )
        if "[." in content or "[=" in content:
            raise UnsupportedSourceError(
                f"unsupported collating case pattern: {pattern}",
                code="unsupported.source.case-pattern",
                hint="Collating symbols and equivalence classes need explicit locale semantics.",
            )

        negated = content[0] in {"!", "^"}
        if negated:
            content = content[1:]
        translated = self._translate_case_posix_classes(content, pattern)
        return f"[{'^' if negated else ''}{self._case_regex_class_body(translated)}]"

    @staticmethod
    def _translate_case_posix_classes(content: str, pattern: str):
        posix_classes = {
            "alnum": "0-9A-Za-z",
            "alpha": "A-Za-z",
            "blank": " \t",
            "cntrl": r"\x00-\x1f\x7f",
            "digit": "0-9",
            "graph": "!-~",
            "lower": "a-z",
            "print": " -~",
            "punct": r"!\"#$%&'()*+,./:;<=>?@[\\\]^_`{|}~-",
            "space": r" \t\r\n\v\f",
            "upper": "A-Z",
            "xdigit": "0-9A-Fa-f",
        }

        def replace(match):
            name = match.group(1)
            if name not in posix_classes:
                raise UnsupportedSourceError(
                    f"unsupported POSIX class case pattern: {pattern}",
                    code="unsupported.source.case-pattern",
                    hint="Use a supported POSIX character class in source-bearing case patterns.",
                )
            return posix_classes[name]

        return re.sub(r'\[:([a-zA-Z_]+):\]', replace, content)

    @staticmethod
    def _case_regex_class_body(content: str):
        output = []
        for index, char in enumerate(content):
            if char == "\\":
                output.append(r"\\")
            elif char == "]":
                output.append(r"\]")
            elif char == "^" and index == 0:
                output.append(r"\^")
            else:
                output.append(char)
        return ''.join(output)

    def _case_has_default_arm(self, node: CaseBlock, state: EvaluationState):
        return any(
            any(self._case_pattern_is_catchall(pattern, state) for pattern in arm.patterns)
            for arm in node.arms
        )

    def _case_pattern_is_catchall(self, pattern: str, state: EvaluationState):
        try:
            regex_source = self._case_pattern_regex_source(pattern.strip(), state)
        except UnsupportedSourceError:
            return False
        return bool(regex_source) and regex_source.replace(".*", "") == ""

    @staticmethod
    def _case_arm_condition(node: CaseBlock, arm):
        return f"case {node.subject} in {'|'.join(arm.patterns)}"

    @staticmethod
    def _unsupported_case(node: CaseBlock, code: str, message: str, hint: str | None = None):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            code,
            message,
            hint,
        )

    @staticmethod
    def _merge_possible_states(target: EvaluationState, possible_states: list[EvaluationState]):
        if not possible_states:
            return
        if len(possible_states) == 1:
            target.copy_from(possible_states[0])
            return

        first = possible_states[0]
        target.cwd = first.cwd
        target.ambiguous_cwd = any(state.ambiguous_cwd for state in possible_states) or any(
            state.cwd != first.cwd for state in possible_states
        )

        target.ambiguous_variables.clear()
        SourceEvaluator._merge_state_mapping(
            target.variables,
            [state.variables for state in possible_states],
            target.ambiguous_variables,
            [state.ambiguous_variables for state in possible_states],
            clear_ambiguous=False,
        )
        SourceEvaluator._merge_state_mapping(
            target.runtime_variables,
            [state.runtime_variables for state in possible_states],
            target.ambiguous_variables,
            [state.ambiguous_variables for state in possible_states],
            clear_ambiguous=False,
        )
        SourceEvaluator._merge_state_mapping(
            target.arrays,
            [state.arrays for state in possible_states],
            target.ambiguous_arrays,
            [state.ambiguous_arrays for state in possible_states],
        )
        SourceEvaluator._merge_state_mapping(
            target.associative_arrays,
            [state.associative_arrays for state in possible_states],
            target.ambiguous_arrays,
            [state.ambiguous_arrays for state in possible_states],
            clear_ambiguous=False,
        )
        SourceEvaluator._merge_function_state(target, possible_states)

        first_shell_options = first.shell_options
        if any(state.ambiguous_shell_options for state in possible_states) or any(
            state.shell_options != first_shell_options for state in possible_states
        ):
            target.ambiguous_shell_options = True
        else:
            target.shell_options = set(first_shell_options)
            target.ambiguous_shell_options = False

        first_glob_options = first.glob_options
        if any(state.ambiguous_glob_options for state in possible_states) or any(
            state.glob_options != first_glob_options for state in possible_states
        ):
            target.ambiguous_glob_options = True
        else:
            target.glob_options = set(first_glob_options)
            target.ambiguous_glob_options = False

        target.missing_source_words = set().union(*(state.missing_source_words for state in possible_states))

        first_last_status = first.last_status
        target.last_status = (
            first_last_status
            if all(state.last_status == first_last_status for state in possible_states)
            else None
        )
        first_positionals = first.positional_arguments
        positionals_converged = (
            not any(state.ambiguous_positionals for state in possible_states)
            and all(state.positional_arguments == first_positionals for state in possible_states)
        )
        target.positional_arguments = first_positionals if positionals_converged else ()
        target.ambiguous_positionals = not positionals_converged
        target.positional_assignment_generation = max(
            state.positional_assignment_generation
            for state in possible_states
        )
        max_frame_depth = max(
            len(state.source_argument_frame_dirty_stack)
            for state in possible_states
        )
        target.source_argument_frame_dirty_stack = tuple(
            any(
                index < len(state.source_argument_frame_dirty_stack)
                and state.source_argument_frame_dirty_stack[index]
                for state in possible_states
            )
            for index in range(max_frame_depth)
        )

    @staticmethod
    def _merge_state_mapping(target: dict, state_mappings: list[dict], ambiguous: set[str],
                             ambiguous_sets: list[set[str]], clear_ambiguous: bool = True):
        merged = {}
        if clear_ambiguous:
            ambiguous.clear()
        keys = set().union(*(mapping.keys() for mapping in state_mappings), *ambiguous_sets)
        for key in keys:
            values = [mapping.get(key) for mapping in state_mappings]
            if key in set().union(*ambiguous_sets) or any(value != values[0] for value in values[1:]):
                ambiguous.add(key)
                continue
            if values[0] is not None:
                merged[key] = copy.deepcopy(values[0])
        target.clear()
        target.update(merged)

    @staticmethod
    def _merge_function_state(target: EvaluationState, possible_states: list[EvaluationState]):
        target.functions.clear()
        target.function_variants.clear()
        target.ambiguous_functions.clear()

        keys = set().union(
            *(state.functions.keys() for state in possible_states),
            *(state.function_variants.keys() for state in possible_states),
            *(state.ambiguous_functions for state in possible_states),
        )
        for key in keys:
            if any(key in state.ambiguous_functions for state in possible_states):
                target.ambiguous_functions.add(key)
                continue

            variants_by_signature = {}
            missing = False
            for state in possible_states:
                variants = state.function_variants.get(key)
                if variants is None:
                    function_def = state.functions.get(key)
                    variants = (function_def,) if function_def is not None else ()
                if not variants:
                    missing = True
                    continue
                for function_def in variants:
                    signature_variants = variants_by_signature.setdefault(
                        SourceEvaluator._function_signature(function_def),
                        [],
                    )
                    if function_def not in signature_variants:
                        signature_variants.append(function_def)

            if missing or len(variants_by_signature) != 1:
                target.ambiguous_functions.add(key)
                continue

            variants = tuple(next(iter(variants_by_signature.values())))
            target.functions[key] = variants[0]
            if len(variants) > 1:
                target.function_variants[key] = variants

    @staticmethod
    def _function_signature(function_def: FunctionDef):
        return (
            "function",
            function_def.name,
            tuple(SourceEvaluator._node_signature(node) for node in function_def.body),
        )

    @staticmethod
    def _node_signature(node):
        if isinstance(node, Assignment):
            return ("assignment", node.name, node.value, node.prefix)
        if isinstance(node, ArrayAssignment):
            return (
                "array",
                node.name,
                node.values,
                node.is_exact,
                node.operation,
                node.index,
                node.associative_values,
                node.raw_values,
            )
        if isinstance(node, CdCommand):
            return ("cd", node.path_expression)
        if isinstance(node, SetCommand):
            return ("set", node.arguments)
        if isinstance(node, FunctionDef):
            return SourceEvaluator._function_signature(node)
        if isinstance(node, ForLoop):
            return (
                "for",
                node.variable,
                node.words,
                node.words_text,
                node.is_exact,
                tuple(SourceEvaluator._node_signature(child) for child in node.body),
            )
        if isinstance(node, CStyleForLoop):
            return (
                "c-for",
                node.init,
                node.condition,
                node.update,
                tuple(SourceEvaluator._node_signature(child) for child in node.body),
            )
        if isinstance(node, WhileLoop):
            return (
                "while",
                node.keyword,
                node.condition,
                node.trailing,
                node.producer,
                node.end_location.line if node.end_location else None,
                tuple(SourceEvaluator._node_signature(child) for child in node.body),
            )
        if isinstance(node, IfBlock):
            return (
                "if",
                tuple(
                    (
                        branch.keyword,
                        branch.condition,
                        tuple(SourceEvaluator._node_signature(child) for child in branch.body),
                    )
                    for branch in node.branches
                ),
            )
        if isinstance(node, CaseBlock):
            return (
                "case",
                node.subject,
                tuple(
                    (
                        arm.patterns,
                        arm.terminator,
                        tuple(SourceEvaluator._node_signature(child) for child in arm.body),
                    )
                    for arm in node.arms
                ),
            )
        if isinstance(node, SourceSite):
            return (
                "source",
                node.command_name,
                node.source_expression,
                node.separator,
                node.is_control_flow,
            )
        if isinstance(node, RawCommand):
            return ("raw", node.text)
        return (type(node).__name__, node.text)

    def _evaluate_condition(
        self,
        condition: str,
        state: EvaluationState,
        node=None,
        stack: tuple[Path, ...] | None = None,
        branch=None,
    ):
        condition = condition.strip()
        if not condition:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        if node is not None and stack is not None and self._condition_text_may_source(condition):
            source_status = self._evaluate_source_logical_condition(
                condition,
                node,
                state,
                stack,
                branch,
            )
            if source_status is not None:
                return source_status
        if '$(' in condition or '`' in condition:
            raise UnsupportedSourceError(f"unsupported dynamic if condition: {condition}")

        if condition.startswith("((") and condition.endswith("))"):
            return self._evaluate_arithmetic_condition(condition[2:-2].strip(), state, condition)

        try:
            condition_words = self._condition_words(condition)
        except UnsupportedSourceError:
            return self._evaluate_command_condition(condition, state)

        if not condition_words.words:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        return self._evaluate_condition_tokens(condition_words, state, condition)

    @staticmethod
    def _source_condition_column(node, command_name: str):
        match = re.search(rf'(?<!\S){re.escape(command_name)}(?=\s|$)', node.text)
        if match:
            return node.location.column + match.start()
        command_index = source_command_index(node.text)
        return node.location.column if command_index is None else node.location.column + command_index

    def _evaluate_source_logical_condition(
        self,
        condition: str,
        node,
        state: EvaluationState,
        stack: tuple[Path, ...],
        branch=None,
    ):
        atoms = self._source_logical_condition_atoms(condition)
        if not any(atom.source_command for atom in atoms):
            if self._raw_command_may_source(condition):
                raise UnsupportedSourceError(f"unsupported source if condition: {condition}")
            return None

        status = self._status_from_last_status(state.last_status)
        for atom in atoms:
            if atom.separator == "&&" and status == "false":
                self._disable_condition_atom_source(node, condition, atom, branch, "&& previous command status")
                state.last_status = 1
                continue
            if atom.separator == "||" and status == "true":
                self._disable_condition_atom_source(node, condition, atom, branch, "|| previous command status")
                state.last_status = 0
                continue

            if status == "unknown" and atom.separator in {"&&", "||"} and atom.source_command is None:
                state.last_status = None
                continue

            if atom.source_command is not None:
                source_node = self._condition_source_node(node, atom, branch)
                self._apply_source_site(source_node, state, stack)
                if atom.negated:
                    state.last_status = self._negated_last_status(state.last_status)
                status = self._status_from_last_status(state.last_status)
                continue

            status = self._evaluate_logical_condition_command_atom(atom, state, condition)
            if atom.negated:
                status = self._condition_not(status)
            state.last_status = self._last_status_from_condition_status(status)

        return status

    def _source_logical_condition_atoms(self, condition: str):
        return self._source_logical_condition_atoms_from_text(condition)

    @staticmethod
    def _source_logical_condition_atoms_from_text(condition: str):
        if '$(' in condition or '`' in condition:
            raise UnsupportedSourceError(f"unsupported dynamic if condition: {condition}")

        segments = SourceEvaluator._split_logical_condition_segments(condition)
        atoms = []
        for separator, text, offset in segments:
            atom = SourceEvaluator._parse_logical_condition_atom(text, offset, separator, condition)
            atoms.append(atom)
        return tuple(atoms)

    @staticmethod
    def _split_logical_condition_segments(condition: str):
        segments = []
        start = 0
        separator = ""
        in_single_quote = False
        in_double_quote = False
        in_double_bracket = False
        escaped = False
        paren_depth = 0
        index = 0

        while index < len(condition):
            char = condition[index]
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
            if not in_single_quote and not in_double_quote and condition.startswith("[[", index):
                in_double_bracket = True
                index += 2
                continue
            if in_double_bracket:
                if not in_single_quote and not in_double_quote and condition.startswith("]]", index):
                    in_double_bracket = False
                    index += 2
                    continue
                index += 1
                continue
            if not in_single_quote and not in_double_quote:
                if char == "(":
                    paren_depth += 1
                elif char == ")" and paren_depth:
                    paren_depth -= 1
                elif char == ";":
                    raise UnsupportedSourceError(f"unsupported if condition list: {condition}")
                elif paren_depth == 0 and condition.startswith(("&&", "||"), index):
                    atom_text = condition[start:index]
                    stripped_offset = start + len(atom_text) - len(atom_text.lstrip())
                    stripped_text = atom_text.strip()
                    if not stripped_text:
                        raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
                    segments.append((separator, stripped_text, stripped_offset))
                    separator = condition[index:index + 2]
                    index += 2
                    start = index
                    continue
                elif char == "|":
                    raise UnsupportedSourceError(f"unsupported if condition pipeline: {condition}")
            index += 1

        atom_text = condition[start:]
        stripped_offset = start + len(atom_text) - len(atom_text.lstrip())
        stripped_text = atom_text.strip()
        if not stripped_text:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        segments.append((separator, stripped_text, stripped_offset))
        return tuple(segments)

    @staticmethod
    def _parse_logical_condition_atom(text: str, offset: int, separator: str, condition: str):
        negated = False
        command_text = text
        command_offset = offset
        while command_text == "!" or command_text.startswith("! "):
            negated = not negated
            if command_text == "!":
                raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
            stripped = command_text[1:]
            command_offset += 1 + len(stripped) - len(stripped.lstrip())
            command_text = stripped.lstrip()

        source_match = re.fullmatch(r'((?:source)|\.)\s+(.+)', command_text, re.S)
        if source_match:
            command_name, source_expression = source_match.groups()
            source_expression = source_expression.strip()
            if not source_expression:
                raise UnsupportedSourceError(f"unsupported empty source condition: {condition}")
            if has_unsupported_shell_operator(source_expression):
                raise UnsupportedSourceError(f"unsupported source if condition: {condition}")
            return ConditionAtom(
                text=command_text,
                offset=command_offset,
                separator=separator,
                negated=negated,
                source_command=command_name,
                source_expression=source_expression,
                source_offset=command_offset + source_match.start(1),
            )

        if contains_source_command(command_text) or contains_nested_source_command(command_text):
            raise UnsupportedSourceError(f"unsupported source if condition: {condition}")

        return ConditionAtom(
            text=command_text,
            offset=command_offset,
            separator=separator,
            negated=negated,
        )

    def _evaluate_logical_condition_command_atom(
        self,
        atom: ConditionAtom,
        state: EvaluationState,
        condition: str,
    ):
        try:
            return self._evaluate_condition(atom.text, state)
        except UnsupportedSourceError:
            if self._raw_command_may_source(atom.text):
                raise
            return "unknown"

    @staticmethod
    def _status_from_last_status(status: int | None):
        if status is None:
            return "unknown"
        return "true" if status == 0 else "false"

    @staticmethod
    def _last_status_from_condition_status(status: str):
        if status == "true":
            return 0
        if status == "false":
            return 1
        return None

    @staticmethod
    def _negated_last_status(status: int | None):
        if status is None:
            return None
        return 1 if status == 0 else 0

    def _condition_source_node(self, node, atom: ConditionAtom, branch=None):
        location = self._condition_atom_location(node, atom, branch)
        return SourceSite(
            location=location,
            text=f"{atom.source_command} {atom.source_expression}",
            command_name=atom.source_command,
            source_expression=atom.source_expression,
            separator=atom.separator,
            is_control_flow=False,
            is_condition_source=True,
        )

    @staticmethod
    def _condition_atom_location(node, atom: ConditionAtom, branch=None):
        base_location = getattr(branch, "condition_location", None) or node.location
        if atom.source_offset is None:
            return base_location
        return SourceLocation(
            base_location.path,
            base_location.line,
            base_location.column + atom.source_offset,
        )

    def _disable_condition_atom_source(self, node, condition: str, atom: ConditionAtom, branch, reason: str):
        if atom.source_command is None:
            return
        location = self._condition_atom_location(node, atom, branch)
        source_site = f"{atom.source_command} {atom.source_expression}".strip()
        self.disabled_sources.append(DisabledSourceSite(
            location=location,
            source_expression=atom.source_expression.strip(),
            source_site=source_site,
            replacement_kind="source",
            condition=reason,
        ))

    def _disable_branch_condition_sources(self, branch, condition: str):
        if not branch.condition:
            return
        location = branch.condition_location
        if location is None:
            return
        try:
            atoms = self._source_logical_condition_atoms(branch.condition)
        except UnsupportedSourceError:
            atoms = ()
        disabled_direct_source = False
        for atom in atoms:
            if atom.source_command is None:
                continue
            disabled_direct_source = True
            source_site = f"{atom.source_command} {atom.source_expression}".strip()
            self.disabled_sources.append(DisabledSourceSite(
                location=self._condition_atom_location(None, atom, branch),
                source_expression=atom.source_expression.strip(),
                source_site=source_site,
                replacement_kind="source",
                condition=condition,
            ))
        if not disabled_direct_source and self._raw_command_contains_literal_source(branch.condition):
            self.disabled_sources.append(DisabledSourceSite(
                location=location,
                source_expression=branch.condition.strip(),
                source_site=branch.condition.strip(),
                replacement_kind="command",
                condition=condition,
            ))

    @staticmethod
    def _condition_text_may_source(condition: str):
        return bool(
            re.search(r'(^|[\s!(&|])(?:source|\.)\s+', condition)
            or SourceEvaluator._raw_command_may_source(condition)
        )

    def _condition_has_source_atom(self, condition: str):
        try:
            return any(atom.source_command for atom in self._source_logical_condition_atoms(condition))
        except UnsupportedSourceError:
            return self._condition_text_may_source(condition)

    def _evaluate_condition_tokens(self, condition_words: ConditionWords, state: EvaluationState, condition: str):
        result, index = self._parse_condition_or(condition_words, 0, state, condition)
        if index != len(condition_words.words):
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")
        return result

    def _parse_condition_or(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        left, index = self._parse_condition_and(condition_words, index, state, condition)
        while index < len(words) and words[index] == "||":
            right, index = self._parse_condition_and(condition_words, index + 1, state, condition)
            left = self._condition_or(left, right)
        return left, index

    def _parse_condition_and(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        left, index = self._parse_condition_not(condition_words, index, state, condition)
        while index < len(words) and words[index] == "&&":
            right, index = self._parse_condition_not(condition_words, index + 1, state, condition)
            left = self._condition_and(left, right)
        return left, index

    def _parse_condition_not(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        if index >= len(words):
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")
        if words[index] == "!":
            result, next_index = self._parse_condition_not(condition_words, index + 1, state, condition)
            return self._condition_not(result), next_index
        if words[index] == "(":
            result, next_index = self._parse_condition_or(condition_words, index + 1, state, condition)
            if next_index >= len(words) or words[next_index] != ")":
                raise UnsupportedSourceError(f"unsupported if condition grouping: {condition}")
            return result, next_index + 1
        return self._parse_condition_atom(condition_words, index, state, condition)

    def _parse_condition_atom(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        if index >= len(words) or words[index] in {")", "&&", "||"}:
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")

        if words[index] in CONDITION_UNARY_FILE_OPERATORS | CONDITION_UNARY_STRING_OPERATORS:
            if index + 1 >= len(words):
                raise UnsupportedSourceError(f"unsupported if condition: {condition}")
            return self._evaluate_condition_unary(
                words[index],
                words[index + 1],
                state,
                condition,
                condition_words.kind,
            ), index + 2

        if index + 1 < len(words) and words[index + 1] in CONDITION_BINARY_OPERATORS:
            if index + 2 >= len(words):
                raise UnsupportedSourceError(f"unsupported if condition: {condition}")
            return self._evaluate_condition_binary(
                words[index],
                words[index + 1],
                words[index + 2],
                state,
                condition,
                condition_words.kind,
            ), index + 3

        value = self._condition_value(words[index], state)
        if value is None:
            return "unknown", index + 1
        return ("true" if bool(value) else "false"), index + 1

    def _evaluate_condition_unary(
        self,
        operator: str,
        operand: str,
        state: EvaluationState,
        condition: str,
        condition_kind: str,
    ):
        if operator in CONDITION_UNARY_FILE_OPERATORS:
            if condition_kind != "double-bracket" and (
                has_unquoted_glob(operand) or has_unquoted_extglob(operand)
            ):
                return self._evaluate_condition_glob_unary(operator, operand, state, condition)
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.exists()
            if operator == "-f":
                result = path.is_file()
            elif operator == "-d":
                result = path.is_dir()
            elif operator == "-r":
                result = os.access(path, os.R_OK)
            return "true" if result else "false"

        value = self._condition_value(operand, state)
        if value is None:
            return "unknown"
        result = bool(value) if operator == "-n" else not bool(value)
        return "true" if result else "false"

    def _evaluate_condition_glob_unary(
        self,
        operator: str,
        operand: str,
        state: EvaluationState,
        condition: str,
    ):
        if operator not in {"-f", "-r"}:
            raise UnsupportedSourceError(f"unsupported glob if condition: {condition}")
        if state.ambiguous_cwd or state.ambiguous_shell_options or state.ambiguous_glob_options:
            raise UnsupportedSourceError(f"unsupported branch-dependent glob if condition: {condition}")
        if "GLOBIGNORE" in state.ambiguous_variables:
            raise UnsupportedSourceError(f"unsupported GLOBIGNORE glob if condition: {condition}")
        if "noglob" in state.shell_options:
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
            return "true" if result else "false"
        if has_unquoted_brace_expansion(operand):
            raise UnsupportedSourceError(f"unsupported brace glob if condition: {condition}")

        resolved = self._condition_value(operand, state)
        if resolved is None:
            return "unknown"

        try:
            matches = expand_glob_word(resolved, state.resolver_context(), condition, raw_pattern=operand)
        except UnsupportedSourceError as exc:
            if "unsupported unmatched source glob" not in str(exc) and "unsupported GLOBIGNORE source pattern" not in str(exc):
                raise
            if "nullglob" in state.glob_options:
                return "true"
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
            return "true" if result else "false"

        if not matches:
            return "true"
        if len(matches) != 1:
            raise UnsupportedSourceError(f"unsupported multi-match glob if condition: {condition}")

        path = Path(matches[0].path).resolve()
        result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
        return "true" if result else "false"

    def _evaluate_condition_binary(
        self,
        left_token: str,
        operator: str,
        right_token: str,
        state: EvaluationState,
        condition: str,
        condition_kind: str,
    ):
        if operator in CONDITION_STRING_OPERATORS:
            is_double_bracket = condition_kind == "double-bracket"
            if not is_double_bracket and (has_unquoted_glob(left_token) or has_unquoted_glob(right_token)):
                raise UnsupportedSourceError(f"unsupported glob if condition: {condition}")
            left = self._condition_value(left_token, state)
            right = self._condition_value(right_token, state)
            if left is None or right is None:
                return "unknown"
            if is_double_bracket and self._condition_rhs_is_pattern(right_token, right):
                try:
                    result = shell_pattern_matches(
                        right,
                        left,
                        extglob=True,
                        nocase="nocasematch" in state.shell_options,
                    )
                except UnsupportedPatternError as exc:
                    raise UnsupportedSourceError(f"unsupported pattern if condition: {condition} ({exc})") from exc
            else:
                if is_double_bracket and "nocasematch" in state.shell_options:
                    result = left.lower() == right.lower()
                else:
                    result = left == right
            if operator == "!=":
                result = not result
            return "true" if result else "false"

        if operator in CONDITION_INTEGER_OPERATORS:
            left = self._condition_integer_value(left_token, state, condition)
            right = self._condition_integer_value(right_token, state, condition)
            if left is None or right is None:
                return "unknown"
            comparisons = {
                "-eq": left == right,
                "-ne": left != right,
                "-gt": left > right,
                "-ge": left >= right,
                "-lt": left < right,
                "-le": left <= right,
            }
            return "true" if comparisons[operator] else "false"

        if operator == "=~":
            if not condition.strip().startswith("[["):
                raise UnsupportedSourceError(f"unsupported regex if condition: {condition}")
            return self._evaluate_condition_regex(left_token, right_token, state, condition)

        raise UnsupportedSourceError(f"unsupported if condition: {condition}")

    @staticmethod
    def _condition_rhs_is_pattern(raw_token: str, resolved_value: str):
        if has_unquoted_glob(raw_token) or has_unquoted_extglob(raw_token):
            return True
        if SourceEvaluator._raw_word_is_single_quoted(raw_token) or SourceEvaluator._raw_word_is_double_quoted(raw_token):
            return False
        return has_unquoted_glob(resolved_value) or has_unquoted_extglob(resolved_value)

    @staticmethod
    def _condition_and(left: str, right: str):
        if left == "false" or right == "false":
            return "false"
        if left == "true" and right == "true":
            return "true"
        return "unknown"

    @staticmethod
    def _condition_or(left: str, right: str):
        if left == "true" or right == "true":
            return "true"
        if left == "false" and right == "false":
            return "false"
        return "unknown"

    @staticmethod
    def _condition_not(result: str):
        if result == "true":
            return "false"
        if result == "false":
            return "true"
        return "unknown"

    def _evaluate_condition_regex(self, left_token: str, right_token: str, state: EvaluationState, condition: str):
        left = self._condition_value(left_token, state)
        pattern = self._condition_value(right_token, state)
        if left is None or pattern is None:
            return "unknown"
        if self._raw_word_is_single_quoted(right_token) or self._raw_word_is_double_quoted(right_token):
            pattern = re.escape(pattern)
        self._ensure_supported_regex_pattern(pattern, condition)
        try:
            return "true" if re.search(pattern, left) else "false"
        except re.error as exc:
            raise UnsupportedSourceError(f"unsupported regex if condition: {condition} ({exc})") from exc

    def _evaluate_command_condition(self, condition: str, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(condition)
        except UnsupportedSourceError as exc:
            raise UnsupportedSourceError(f"unsupported if condition syntax: {condition}") from exc
        if not words:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")

        command_name = strip_shell_word_quotes(words[0])
        if command_name in {":", "true"} and len(words) == 1:
            return "true"
        if command_name == "false" and len(words) == 1:
            return "false"
        if command_name == "grep":
            return self._evaluate_grep_condition(words, state, condition)
        if command_name == "shopt":
            return self._evaluate_shopt_query_condition(words, state, condition)
        raise UnsupportedSourceError(f"unsupported command if condition: {condition}")

    def _evaluate_shopt_query_condition(self, words: list[str], state: EvaluationState, condition: str):
        if len(words) < 3 or strip_shell_word_quotes(words[1]) != "-q":
            raise UnsupportedSourceError(f"unsupported shopt if condition: {condition}")
        if state.ambiguous_shell_options or state.ambiguous_glob_options:
            return "unknown"

        for option_word in words[2:]:
            option = strip_shell_word_quotes(option_word)
            if option not in KNOWN_SHOPT_OPTIONS:
                raise UnsupportedSourceError(f"unsupported shopt option in if condition: {condition}")
            enabled = (
                option in state.glob_options
                if option in GLOB_SHOPT_OPTIONS
                else option in state.shell_options
            )
            if not enabled:
                return "false"
        return "true"

    def _evaluate_grep_condition(self, words: list[str], state: EvaluationState, condition: str):
        options = set()
        index = 1
        while index < len(words):
            option = strip_shell_word_quotes(words[index])
            if option == "--":
                index += 1
                break
            if not option.startswith("-") or option == "-":
                break
            for flag in option[1:]:
                if flag not in {"q", "E", "F", "s"}:
                    raise UnsupportedSourceError(f"unsupported grep option in if condition: {condition}")
                options.add(flag)
            index += 1

        if "q" not in options:
            raise UnsupportedSourceError(f"unsupported grep if condition without -q: {condition}")
        if {"E", "F"} <= options:
            raise UnsupportedSourceError(f"unsupported grep if condition with both -E and -F: {condition}")
        if len(words) - index != 2:
            raise UnsupportedSourceError(f"unsupported grep if condition arguments: {condition}")

        pattern = self._condition_value(words[index], state)
        path = self._condition_path(words[index + 1], state, condition)
        if pattern is None or path is None:
            return "unknown"
        if not path.is_file():
            return "false"

        if "F" in options:
            matched = self._file_contains_literal(path, pattern)
        elif "E" in options:
            self._ensure_supported_regex_pattern(pattern, condition, "grep regex")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise UnsupportedSourceError(f"unsupported grep regex in if condition: {condition} ({exc})") from exc
            matched = self._file_matches_regex(path, regex)
        else:
            if GREP_LITERAL_META_PATTERN.search(pattern):
                raise UnsupportedSourceError(f"unsupported basic-regex grep if condition: {condition}")
            matched = self._file_contains_literal(path, pattern)

        return "true" if matched else "false"

    @staticmethod
    def _ensure_supported_regex_pattern(pattern: str, condition: str, label: str = "regex"):
        if POSIX_CLASS_PATTERN.search(pattern):
            raise UnsupportedSourceError(f"unsupported POSIX {label} in if condition: {condition}")
        if PYTHON_ONLY_REGEX_PATTERN.search(pattern) or LAZY_REGEX_QUANTIFIER_PATTERN.search(pattern):
            raise UnsupportedSourceError(f"unsupported Python-specific {label} in if condition: {condition}")

    @staticmethod
    def _file_contains_literal(path: Path, needle: str):
        with path.open('r', errors='ignore') as file:
            return any(needle in line for line in file)

    @staticmethod
    def _file_matches_regex(path: Path, regex):
        with path.open('r', errors='ignore') as file:
            return any(regex.search(line) for line in file)

    def _evaluate_arithmetic_condition(self, expression: str, state: EvaluationState, condition: str):
        if not expression:
            raise UnsupportedSourceError(f"unsupported empty arithmetic if condition: {condition}")
        value = self._evaluate_arithmetic_expression(expression, state, condition)
        if value is None:
            return "unknown"
        return "true" if bool(value) else "false"

    def _evaluate_arithmetic_expression(self, expression: str, state: EvaluationState, condition: str):
        normalized = self._normalize_arithmetic_expression(expression)
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise UnsupportedSourceError(f"unsupported arithmetic if condition: {condition}") from exc
        return self._evaluate_arithmetic_ast(tree.body, state, condition)

    @staticmethod
    def _normalize_arithmetic_expression(expression: str):
        normalized = re.sub(r'\$\{([a-zA-Z_]\w*)\}', r'\1', expression)
        normalized = re.sub(r'\$([a-zA-Z_]\w*)', r'\1', normalized)
        normalized = normalized.replace("&&", " and ")
        normalized = normalized.replace("||", " or ")
        normalized = re.sub(r'(?<![=!<>])!(?!=)', ' not ', normalized)
        normalized = re.sub(r'(?<!/)/(?!/)', '//', normalized)
        return normalized

    def _evaluate_arithmetic_ast(self, node, state: EvaluationState, condition: str):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
            return int(node.value)

        if isinstance(node, ast.Name):
            return self._arithmetic_name_value(node.id, state, condition)

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not, ast.Invert)):
            operand = self._evaluate_arithmetic_ast(node.operand, state, condition)
            if operand is None:
                return None
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Invert):
                return ~operand
            return 0 if bool(operand) else 1

        if isinstance(node, ast.BinOp) and isinstance(
            node.op,
            (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor),
        ):
            left = self._evaluate_arithmetic_ast(node.left, state, condition)
            right = self._evaluate_arithmetic_ast(node.right, state, condition)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.LShift, ast.RShift)) and right < 0:
                raise UnsupportedSourceError(f"unsupported negative arithmetic shift in if condition: {condition}")
            if isinstance(node.op, ast.LShift):
                return left << right
            if isinstance(node.op, ast.RShift):
                return left >> right
            if isinstance(node.op, ast.BitAnd):
                return left & right
            if isinstance(node.op, ast.BitOr):
                return left | right
            if isinstance(node.op, ast.BitXor):
                return left ^ right
            if right == 0:
                raise UnsupportedSourceError(f"unsupported arithmetic division by zero in if condition: {condition}")
            if isinstance(node.op, ast.FloorDiv):
                return int(left / right)
            return left % right

        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [self._evaluate_arithmetic_ast(value, state, condition) for value in node.values]
            if any(value is None for value in values):
                return None
            if isinstance(node.op, ast.And):
                return int(all(bool(value) for value in values))
            return int(any(bool(value) for value in values))

        if isinstance(node, ast.Compare):
            left = self._evaluate_arithmetic_ast(node.left, state, condition)
            if left is None:
                return None
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._evaluate_arithmetic_ast(comparator, state, condition)
                if right is None:
                    return None
                if not self._arithmetic_compare(left, operator, right, condition):
                    return 0
                left = right
            return 1

        raise UnsupportedSourceError(f"unsupported arithmetic if condition: {condition}")

    @staticmethod
    def _arithmetic_compare(left: int, operator, right: int, condition: str):
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        if isinstance(operator, ast.GtE):
            return left >= right
        raise UnsupportedSourceError(f"unsupported arithmetic comparison in if condition: {condition}")

    @staticmethod
    def _arithmetic_name_value(name: str, state: EvaluationState, condition: str):
        if name in state.ambiguous_variables:
            return None
        raw_value = state.runtime_variables.get(name, os.environ.get(name, "0"))
        raw_value = strip_matching_quotes(str(raw_value))
        if not re.fullmatch(r'[+-]?\d+', raw_value):
            raise UnsupportedSourceError(f"unsupported non-integer arithmetic variable in if condition: {condition}")
        return int(raw_value)

    @staticmethod
    def _condition_words(condition: str):
        stripped = condition.strip()
        if stripped.startswith("[[") and stripped.endswith("]]"):
            stripped = stripped[2:-2].strip()
            kind = "double-bracket"
        elif stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1].strip()
            kind = "single-bracket"
        elif stripped.startswith("test "):
            stripped = stripped[5:].strip()
            kind = "test"
        else:
            raise UnsupportedSourceError(f"unsupported if condition syntax: {condition}")
        return ConditionWords(tuple(parse_shell_words_preserving_quotes(stripped)), kind)

    @staticmethod
    def _condition_value(value: str, state: EvaluationState):
        if strip_matching_quotes(value.strip()) == "$#":
            return None if state.ambiguous_positionals else str(len(state.positional_arguments))

        variable_names = [match.group(1) or match.group(2) for match in SCALAR_REFERENCE_PATTERN.finditer(value)]
        if any(name in state.ambiguous_variables for name in variable_names):
            return None
        if any(name not in state.runtime_variables and f"${name}" in value for name in variable_names):
            return None

        resolved = resolve_variable_references(value, state.runtime_context())
        if SCALAR_REFERENCE_PATTERN.search(resolved):
            return None
        resolved = os.path.expandvars(resolved)
        return strip_matching_quotes(resolved)

    def _condition_integer_value(self, value: str, state: EvaluationState, condition: str):
        resolved = self._condition_value(value, state)
        if resolved is None:
            return None
        if not re.fullmatch(r'[+-]?\d+', resolved):
            raise UnsupportedSourceError(f"unsupported integer if condition: {condition}")
        return int(resolved)

    @staticmethod
    def _condition_path(value: str, state: EvaluationState, condition: str):
        if state.ambiguous_cwd:
            raise UnsupportedSourceError(f"unsupported branch-dependent cwd in if condition: {condition}")
        resolved = SourceEvaluator._condition_value(value, state)
        if resolved is None:
            return None
        resolved = resolve_shell_path_commands(resolved, str(state.cwd))
        path = Path(resolved)
        if not path.is_absolute():
            path = state.cwd / path
        return path.resolve()

    def _apply_source_site(self, node: SourceSite, state: EvaluationState, stack: tuple[Path, ...]):
        if self._source_site_skipped_by_known_status(node, state):
            return

        if len(self._pipeline_segments(node.text)) > 1:
            raw_node = RawCommand(
                location=node.location,
                text=node.text,
                separator=node.separator,
            )
            if self._apply_child_shell_sources(raw_node, state, stack):
                return

        if not self._is_plain_source_site(node) and self.mode == "executable":
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.command-unresolved",
                "unsupported unresolved source command",
                "Only direct source and dot commands can be lowered in executable mode.",
            )

        if node.is_control_flow and self.mode == "executable":
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.control-flow",
                "unsupported source in control flow",
                "Control-flow source sites need modeled branch semantics before executable lowering.",
            )
        is_context_control_flow = node.is_control_flow and self.mode == "context"
        source_site = self._source_site_text(node)
        source_override = self._source_override_for_node(node)

        if source_override is SOURCE_OVERRIDE_EXHAUSTED:
            if state.loop_depth > 0:
                state.last_status = 1
                raise LoopContinueSignal()
            if state.occurrence_context == OccurrenceModel.CONDITIONAL:
                self._disable_unreachable_sources([node], "trusted runtime graph conditional source")
                state.last_status = 1
                return
            source_override = None

        if source_override is not None:
            source_value = (
                source_override.source_value
                if source_override.source_value is not None
                else node.source_expression.strip()
            )
            invocation = SourceInvocation(
                ResolvedSource(
                    path=source_override.resolved_path,
                    source_expression=node.source_expression.strip(),
                    source_site=source_site,
                    replacement_kind=source_override.replacement_kind,
                    source_value=source_value,
                ),
                source_arguments=source_override.arguments or None,
            )
        else:
            try:
                self._ensure_source_state_can_resolve(node, node.source_expression, state)
                resolved_expression = self._expand_array_indexes(node.source_expression, node, state)
                invocation = self._resolve_source_invocation(
                    resolved_expression,
                    node,
                    state,
                )
            except UnsupportedSourceError:
                if self.mode == "context":
                    return
                raise

        resolved_source = invocation.source
        source_arguments = invocation.source_arguments

        if not resolved_source:
            if self.mode == "context":
                return
            variable_names = self._unresolved_word_variables([node.source_expression], state)
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.unresolved",
                "unsupported unresolved source",
                "Use a statically resolvable source path for IR evaluation.",
                details={"supplement_skeleton": supplement_skeleton(variable_names=variable_names)},
            )

        source_path = Path(resolved_source.path)
        source_value = (
            resolved_source.source_value
            if resolved_source.source_value is not None
            else self._source_runtime_value(resolved_source.source_expression, state)
        )
        replacement_kind = self._source_site_replacement_kind(node)
        if self._is_missing_source(resolved_source):
            replacement_kind = resolved_source.replacement_kind
            if self.mode == "context":
                return
            self._record_missing_source(
                source_path,
                node,
                node.source_expression,
                source_site,
                state,
                replacement_kind,
                source_value,
            )
            return
        if self._is_source_expansion_failure(resolved_source):
            replacement_kind = resolved_source.replacement_kind
            if self.mode == "context":
                return
            self._record_source_expansion_failure(
                source_path,
                node,
                node.source_expression,
                source_site,
                state,
                replacement_kind,
                source_value,
            )
            return

        if self._source_site_has_unknown_status_guard(node, state):
            base_state = state.child_shell_copy()
            branch_state = state.conditional_copy()
            self._record_event(
                source_path,
                node,
                node.source_expression,
                source_site,
                ExecutionModel.PARENT_SOURCE,
                replacement_kind,
                state,
                occurrence_model=OccurrenceModel.CONDITIONAL,
                source_value=source_value,
                source_arguments=source_arguments,
            )
            branch_state.last_status = self._evaluate_sourced_file(
                source_path,
                branch_state,
                stack,
                source_arguments=source_arguments,
            )
            self._merge_possible_states(state, [base_state, branch_state])
            return

        if is_context_control_flow:
            branch_state = state.conditional_copy()
            self._record_event(
                source_path,
                node,
                node.source_expression,
                source_site,
                ExecutionModel.PARENT_SOURCE,
                replacement_kind,
                state,
                occurrence_model=OccurrenceModel.CONDITIONAL,
                source_value=source_value,
                source_arguments=source_arguments,
            )
            branch_state.last_status = self._evaluate_sourced_file(
                source_path,
                branch_state,
                stack,
                source_arguments=source_arguments,
            )
            return

        self._record_and_descend(
            source_path,
            node,
            node.source_expression,
            source_site,
            state,
            stack,
            ExecutionModel.PARENT_SOURCE,
            replacement_kind,
            source_value,
            source_arguments,
        )

    def _source_override_for_node(self, node: SourceSite):
        key = (
            node.location.path.resolve(strict=False),
            node.location.line,
            node.text.strip(),
        )
        overrides = self.source_overrides.get(key)
        if not overrides:
            return None
        index = self._source_override_indexes[key]
        if index >= len(overrides):
            return SOURCE_OVERRIDE_EXHAUSTED
        self._source_override_indexes[key] += 1
        return overrides[index]

    def _ensure_source_overrides_consumed(self):
        for key, overrides in sorted(self.source_overrides.items(), key=lambda item: (str(item[0][0]), item[0][1], item[0][2])):
            consumed = self._source_override_indexes[key]
            if consumed >= len(overrides):
                continue
            path, line, command = key
            remaining = len(overrides) - consumed
            plural = "edge" if remaining == 1 else "edges"
            raise UnsupportedSourceError(
                "trusted runtime graph replay did not consume "
                f"{remaining} source {plural}: {path}:{line}: {command}",
                code="unsupported.source.graph-unconsumed",
            )

    def _resolve_source_invocation(
        self,
        resolved_expression: str,
        node: SourceSite,
        state: EvaluationState,
    ):
        positional_source = self._resolve_positional_source_expression(
            resolved_expression,
            node,
            state,
        )
        if positional_source is not None:
            return SourceInvocation(
                positional_source,
                source_arguments=positional_source.source_arguments,
            )

        if self._source_expression_needs_word_expansion(resolved_expression):
            return self._resolve_expanded_source_invocation(resolved_expression, node, state)

        source_site = self._source_site_text(node)
        path_expression, source_arguments = self._split_source_expression_arguments(
            resolved_expression,
            node,
            state,
        )
        try:
            resolved_source = SOURCE_RESOLVER.resolve_source_expression(
                path_expression,
                source_site,
                state.resolver_context(),
            )
        except UnsupportedSourceError as exc:
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.resolution",
            ) from exc

        if not resolved_source:
            return SourceInvocation(resolved_source, source_arguments=source_arguments)
        if resolved_source.replacement_kind == MISSING_SOURCE_NO_FILENAME and source_arguments:
            raise self._unsupported_source_argument(
                node,
                "unsupported nullglob source argument shift",
                "Nullglob source sites with later words becoming the filename are not modeled yet.",
            )

        return SourceInvocation(
            resolved_source,
            source_arguments=self._merge_source_arguments(
                resolved_source.source_arguments,
                source_arguments,
            ),
        )

    def _resolve_expanded_source_invocation(
        self,
        source_expression: str,
        node: SourceSite,
        state: EvaluationState,
    ):
        source_site = self._source_site_text(node)
        resolver_context = state.resolver_context()
        try:
            expanded_words = self._expand_source_command_words(source_expression, node, state, resolver_context)
        except FailglobExpansionError as exc:
            if self.mode == "context":
                return SourceInvocation(None)
            if self._source_site_has_unknown_status_guard(node, state):
                raise self._unsupported_source_expansion_failure(
                    node,
                    "unsupported conditional failglob source expansion",
                    "Failglob after an unknown &&/|| guard cannot be lowered without changing later line behavior.",
                ) from exc
            if node.is_condition_source:
                raise SourceConditionExpansionFailureSignal(exc.pattern) from exc
            return SourceInvocation(
                source_expansion_failure_result(
                    exc.pattern,
                    node.source_expression,
                    source_site,
                    resolver_context,
                    replacement_kind=(
                        SOURCE_EXPANSION_FAILURE_RETURN
                        if state.function_body_depth > 0
                        else SOURCE_EXPANSION_FAILURE
                    ),
                )
            )

        if not expanded_words:
            return SourceInvocation(
                ResolvedSource(
                    path=str(state.cwd),
                    source_expression=node.source_expression.strip(),
                    source_site=source_site,
                    replacement_kind=MISSING_SOURCE_NO_FILENAME,
                    source_value="",
                )
            )

        source_word, *argument_words = expanded_words
        if not source_word.exists:
            return SourceInvocation(
                ResolvedSource(
                    path=source_word.path or str(state.cwd / source_word.word),
                    source_expression=node.source_expression.strip(),
                    source_site=source_site,
                    replacement_kind=MISSING_SOURCE,
                    source_value=source_word.word,
                ),
                source_arguments=tuple(word.word for word in argument_words) or None,
            )

        if source_word.path is not None and not source_word.is_file:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.resolution",
                "unsupported non-file source glob match",
                "The expanded source filename must resolve to a regular file.",
            )

        if source_word.path is not None:
            resolved_source = ResolvedSource(
                path=source_word.path,
                source_expression=node.source_expression.strip(),
                source_site=source_site,
                source_value=source_word.word,
            )
        else:
            resolved_source = SOURCE_RESOLVER.resolve_source_expression(
                self._shell_quote(source_word.word),
                source_site,
                resolver_context,
            )
            if resolved_source is not None:
                resolved_source = replace(
                    resolved_source,
                    source_expression=node.source_expression.strip(),
                    source_site=source_site,
                    source_value=source_word.word,
                )

        if not resolved_source:
            return SourceInvocation(None)

        return SourceInvocation(
            resolved_source,
            source_arguments=tuple(word.word for word in argument_words) or None,
        )

    @staticmethod
    def _source_expression_needs_word_expansion(source_expression: str):
        try:
            words = parse_shell_words_preserving_quotes(source_expression)
        except UnsupportedSourceError:
            return False
        return any(
            has_unquoted_glob(word)
            or has_unquoted_brace_expansion(word)
            or has_unquoted_extglob(word)
            for word in words
        )

    def _expand_source_command_words(
        self,
        source_expression: str,
        node: SourceSite,
        state: EvaluationState,
        resolver_context: dict,
    ):
        try:
            raw_words = parse_shell_words_preserving_quotes(source_expression)
        except UnsupportedSourceError as exc:
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.argument",
            ) from exc

        expanded_words: list[ExpandedSourceWord] = []
        for raw_word in raw_words:
            if (
                has_unquoted_glob(raw_word)
                or has_unquoted_brace_expansion(raw_word)
                or has_unquoted_extglob(raw_word)
            ):
                resolved_word = resolve_variable_references(raw_word, resolver_context)
                resolved_word = os.path.expandvars(resolved_word)
                resolved_word = strip_shell_word_quotes(resolved_word)
                matches = expand_glob_word(
                    resolved_word,
                    resolver_context,
                    node.text,
                    raw_pattern=raw_word,
                    allow_missing_literal=True,
                    require_files=False,
                )
                expanded_words.extend(
                    ExpandedSourceWord(
                        word=match.word,
                        path=match.path,
                        exists=match.exists,
                        is_file=match.is_file,
                    )
                    for match in matches
                )
                continue

            if not expanded_words and raw_word.strip() in QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS:
                raise self._unsupported_positional_source(
                    node,
                    state,
                    "unsupported shifted positional source expression",
                    "Nullglob source shifting to $@/$* is not modeled.",
                )

            expanded_words.extend(
                ExpandedSourceWord(word=argument)
                for argument in self._resolve_source_argument_words([raw_word], node, state)
            )

        return expanded_words

    @staticmethod
    def _unsupported_source_expansion_failure(node: SourceSite, message: str, hint: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.expansion-failure",
            message,
            hint,
        )

    def _split_source_expression_arguments(
        self,
        source_expression: str,
        node: SourceSite,
        state: EvaluationState,
    ):
        try:
            words = parse_shell_words_preserving_quotes(source_expression)
        except UnsupportedSourceError as exc:
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.argument",
            ) from exc

        if len(words) <= 1:
            return source_expression, None

        return words[0], self._resolve_source_argument_words(words[1:], node, state)

    def _resolve_source_argument_words(self, words: list[str], node: SourceSite, state: EvaluationState):
        arguments = []
        for word in words:
            stripped = word.strip()
            if stripped in {'"$@"', '"${@}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_source_argument(
                        node,
                        "unsupported ambiguous positional source argument expansion",
                        "Positional source arguments must resolve exactly.",
                    )
                arguments.extend(state.positional_arguments)
                continue
            if stripped in {'"$*"', '"${*}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_source_argument(
                        node,
                        "unsupported ambiguous positional source argument expansion",
                        "Positional source arguments must resolve exactly.",
                    )
                arguments.append(self._joined_positionals(state))
                continue
            if re.search(r'(?<!\\)\$(?:\{?[@*]\}?)', stripped):
                raise self._unsupported_source_argument(
                    node,
                    "unsupported positional source argument expansion",
                    "Only quoted standalone $@/$* source arguments are supported.",
                )
            arguments.append(self._resolve_source_argument_word(word, node, state))
        return tuple(arguments)

    @staticmethod
    def _merge_source_arguments(
        resolver_arguments: tuple[str, ...] | None,
        explicit_arguments: tuple[str, ...] | None,
    ):
        if resolver_arguments is None:
            return explicit_arguments
        if explicit_arguments is None:
            return resolver_arguments
        return (*resolver_arguments, *explicit_arguments)

    def _resolve_source_argument_word(self, word: str, node: SourceSite, state: EvaluationState):
        if self._raw_word_is_single_quoted(word):
            return strip_shell_word_quotes(word)

        if '$(' in word or '`' in word:
            raise self._unsupported_source_argument(
                node,
                "unsupported dynamic source argument",
                "Source arguments must resolve to exact strings without command substitution.",
            )

        resolved = resolve_variable_references(word, state.runtime_context())
        resolved = os.path.expandvars(resolved)
        if "$" in resolved:
            raise self._unsupported_source_argument(
                node,
                "unsupported unresolved source argument",
                "Source arguments must resolve to exact strings.",
            )

        value = strip_shell_word_quotes(resolved)
        if "$" in word and not self._word_has_quotes(word) and re.search(r'\s', value):
            raise self._unsupported_source_argument(
                node,
                "unsupported word-splitting source argument",
                "Quote source arguments whose resolved value contains whitespace.",
            )
        return value

    @staticmethod
    def _joined_positionals(state: EvaluationState):
        ifs = state.runtime_variables.get("IFS", DEFAULT_IFS)
        separator = ifs[0] if ifs else ""
        return separator.join(state.positional_arguments)

    @staticmethod
    def _word_has_quotes(word: str):
        return "'" in word or '"' in word

    @staticmethod
    def _unsupported_source_argument(node: SourceSite, message: str, hint: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.argument",
            message,
            hint,
        )

    def _resolve_positional_source_expression(
        self,
        source_expression: str,
        node: SourceSite,
        state: EvaluationState,
    ):
        expression = source_expression.strip()
        if not self._is_quoted_all_positionals_source_expression(expression):
            if re.search(r'(?<!\\)\$(?:\{?[@*]\}?)', expression):
                try:
                    words = parse_shell_words_preserving_quotes(expression)
                except UnsupportedSourceError:
                    words = []
                if len(words) <= 1:
                    raise self._unsupported_positional_source(
                        node,
                        state,
                        "unsupported positional source expression",
                        "Only quoted standalone $@/$* source expressions are supported.",
                    )
            return None

        if not state.function_call_stack:
            raise self._unsupported_positional_source(
                node,
                state,
                "unsupported top-level positional source expression",
                "Quoted $@/$* source expressions are supported only inside modeled local helper calls.",
            )

        arguments = state.positional_arguments
        if not arguments:
            raise self._unsupported_positional_source(
                node,
                state,
                "unsupported positional source argument count: 0",
                "Quoted $@/$* helper sources must bind to at least one source path argument.",
            )
        if expression in {'"$*"', '"${*}"'} and len(arguments) != 1:
            raise self._unsupported_positional_source(
                node,
                state,
                f"unsupported positional source argument count: {len(arguments)}",
                "Quoted $* helper sources must bind to exactly one source path argument.",
            )

        source_site = self._source_site_text(node)
        quoted_argument = self._shell_quote(arguments[0])
        try:
            resolved_source = SOURCE_RESOLVER.resolve_source_expression(
                quoted_argument,
                source_site,
                state.resolver_context(),
            )
        except UnsupportedSourceError as exc:
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-positionals",
            ) from exc

        if not resolved_source:
            raise self._unsupported_positional_source(
                node,
                state,
                "unsupported unresolved positional source argument",
                "The helper source argument must resolve to an existing source file.",
            )

        return ResolvedSource(
            path=resolved_source.path,
            source_expression=node.source_expression.strip(),
            source_site=source_site,
            execution_model=resolved_source.execution_model,
            confidence=resolved_source.confidence,
            replacement_kind=resolved_source.replacement_kind,
            source_value=arguments[0],
            source_arguments=arguments[1:] or None,
            source_column=resolved_source.source_column,
        )

    @staticmethod
    def _is_quoted_all_positionals_source_expression(source_expression: str):
        return source_expression.strip() in QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS

    @staticmethod
    def _is_retained_helper_positional_source_expression(source_expression: str):
        return source_expression.strip() in RETAINED_HELPER_POSITIONAL_SOURCE_EXPRESSIONS

    def _source_site_replacement_kind(self, node: SourceSite):
        if (
            self._retained_helper_stack
            and self._is_retained_helper_positional_source_expression(node.source_expression)
        ):
            return "retained-source"
        return "source"

    @staticmethod
    def _source_site_text(node: SourceSite):
        return node.source_site.strip() or f"{node.command_name} {node.source_expression.strip()}".strip()

    @staticmethod
    def _is_missing_source(resolved_source: ResolvedSource):
        return is_missing_source_replacement_kind(resolved_source.replacement_kind)

    @staticmethod
    def _is_source_expansion_failure(resolved_source: ResolvedSource):
        return is_source_expansion_failure_replacement_kind(resolved_source.replacement_kind)

    def _record_missing_source(
        self,
        source_path: Path,
        node: SourceSite,
        source_expression: str,
        source_site: str,
        state: EvaluationState,
        replacement_kind: str,
        source_value: str | None,
    ):
        if self._source_site_has_unknown_status_guard(node, state):
            base_state = state.child_shell_copy()
            branch_state = state.conditional_copy()
            self._record_event(
                source_path,
                node,
                source_expression,
                source_site,
                ExecutionModel.PARENT_SOURCE,
                replacement_kind,
                state,
                occurrence_model=OccurrenceModel.CONDITIONAL,
                source_value=source_value,
            )
            branch_state.last_status = missing_source_status(replacement_kind)
            self._merge_possible_states(state, [base_state, branch_state])
            return

        self._record_event(
            source_path,
            node,
            source_expression,
            source_site,
            ExecutionModel.PARENT_SOURCE,
            replacement_kind,
            state,
            source_value=source_value,
        )
        state.last_status = missing_source_status(replacement_kind)

    def _record_source_expansion_failure(
        self,
        source_path: Path,
        node: SourceSite,
        source_expression: str,
        source_site: str,
        state: EvaluationState,
        replacement_kind: str,
        source_value: str | None,
    ):
        self._record_event(
            source_path,
            node,
            source_expression,
            source_site,
            ExecutionModel.PARENT_SOURCE,
            replacement_kind,
            state,
            source_value=source_value,
        )
        state.last_status = 1
        if replacement_kind == SOURCE_EXPANSION_FAILURE_RETURN:
            raise FunctionSourceExpansionAbortSignal()
        raise LineAbortSignal(node.location.path, node.location.line)

    @staticmethod
    def _unsupported_positional_source(node: SourceSite, state: EvaluationState, message: str, hint: str):
        function_name = state.function_call_stack[-1] if state.function_call_stack else None
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.function-positionals",
            message,
            hint,
            details={"supplement_skeleton": supplement_skeleton(function_name=function_name)},
        )

    @staticmethod
    def _source_site_has_unknown_status_guard(node: SourceSite, state: EvaluationState):
        return node.separator in {"&&", "||"} and state.last_status is None

    def _source_site_skipped_by_known_status(self, node: SourceSite, state: EvaluationState):
        if node.separator == "&&" and state.last_status not in {None, 0}:
            self._disable_unreachable_sources([node], "&& previous command status")
            return True
        if node.separator == "||" and state.last_status == 0:
            self._disable_unreachable_sources([node], "|| previous command status")
            return True
        return False

    def _apply_raw_command(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        if self._raw_command_skipped_by_known_status(node, state):
            return

        if self._apply_local_declaration(node, state):
            return

        if self._apply_function_call(node, state, stack):
            return

        if self._apply_loop_control(node, state):
            return

        if self._raw_function_return_command(node):
            if state.function_body_depth > 0:
                raise FunctionReturnSignal(self._function_return_status(node, state), node)
            if state.source_depth > 0:
                raise SourceReturnSignal(self._function_return_status(node, state), node)

        if self._raw_function_shift_command(node):
            self._apply_function_shift(node, state)
            return

        if self._apply_array_population_command(node, state):
            return

        if self._apply_arithmetic_command(node, state):
            return

        if self._apply_exact_non_source_eval(node, state):
            return

        if self._apply_child_shell_sources(node, state, stack):
            return

        exact_status = self._raw_exact_status_command(node, state)
        if exact_status is not None:
            state.last_status = exact_status
            return

        shopt_status = self._apply_shopt(node, state)
        if shopt_status is not None:
            state.last_status = shopt_status
            return

        try:
            if contains_source_command(node.text):
                self._ensure_source_state_can_resolve(node, node.text, state)
            resolved_sources = SOURCE_RESOLVER.resolve_command_level_sources(
                node.text,
                state.resolver_context(),
                self.mode,
            )
        except UnsupportedSourceError as exc:
            if self.mode == "context":
                return
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.command-resolution",
            ) from exc

        if not resolved_sources and self._raw_command_may_source(node.text) and self.mode == "executable":
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.command-unresolved",
                "unsupported unresolved source command",
                "Use a direct source command or a supported dynamic source expression.",
            )

        source_status = 0 if resolved_sources else None
        for resolved_source in resolved_sources:
            execution_model = ExecutionModel(resolved_source.execution_model)
            source_path = Path(resolved_source.path)
            self._record_event(
                source_path,
                node,
                resolved_source.source_expression,
                resolved_source.source_site,
                execution_model,
                resolved_source.replacement_kind,
                state,
                source_value=resolved_source.source_value,
            )
            if execution_model == ExecutionModel.CHILD_SHELL:
                child_state = state.child_shell_copy()
                source_status = self._evaluate_sourced_file(source_path, child_state, stack)
            else:
                source_status = self._evaluate_sourced_file(source_path, state, stack)
        state.last_status = source_status

    def _apply_local_declaration(self, node: RawCommand, state: EvaluationState):
        if state.function_body_depth <= 0 or not state.local_scopes:
            return False
        if contains_source_command(node.text) or contains_nested_source_command(node.text):
            return False
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError:
            return False
        if not words or strip_shell_word_quotes(words[0]) != "local":
            return False

        for word in words[1:]:
            if word.startswith("-"):
                return False
            if "=" in word:
                name, value = word.split("=", 1)
                if not re.fullmatch(r'[a-zA-Z_]\w*', name):
                    return False
                SourceEvaluator._capture_local_variable(name, state)
                try:
                    resolved = self._resolve_function_exact_word(
                        value,
                        node,
                        state,
                        "unsupported.source.local",
                        "unsupported dynamic local declaration",
                        "unsupported unresolved local declaration",
                        "Local declarations must be exact for source-aware function evaluation.",
                    )
                except UnsupportedSourceError:
                    state.variables.pop(name, None)
                    state.runtime_variables.pop(name, None)
                    state.ambiguous_variables.add(name)
                else:
                    state.variables[name] = resolved
                    state.runtime_variables[name] = resolved
                    state.ambiguous_variables.discard(name)
            else:
                name = strip_shell_word_quotes(word)
                if not re.fullmatch(r'[a-zA-Z_]\w*', name):
                    return False
                SourceEvaluator._capture_local_variable(name, state)
                state.variables.pop(name, None)
                state.runtime_variables.pop(name, None)
                state.ambiguous_variables.discard(name)
        state.last_status = 0
        return True

    def _apply_child_shell_sources(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        try:
            source_commands = self._child_shell_source_commands(node.text)
        except UnsupportedSourceError as exc:
            if self.mode == "context":
                return False
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
            ) from exc
        if not source_commands:
            return False

        context_states: dict[tuple[str, int], EvaluationState] = {}
        for source_command in source_commands:
            context_state = context_states.setdefault(source_command.context_id, state.child_shell_copy())
            source_node = SourceSite(
                location=SourceLocation(node.location.path, node.location.line, source_command.column),
                text=source_command.resolve_source_site or source_command.source_site,
                command_name=source_command.command_name,
                source_expression=source_command.source_expression,
                source_site=source_command.source_site,
            )
            try:
                source_path, source_value, source_arguments = self._resolve_child_shell_source(
                    source_node,
                    context_state,
                )
            except UnsupportedSourceError:
                if self.mode == "context":
                    return False
                raise
            event_index = len(self.events)
            self._record_event(
                source_path,
                source_node,
                source_command.source_expression,
                source_command.source_site,
                ExecutionModel.CHILD_SHELL,
                source_command.replacement_kind,
                context_state,
                source_value=source_command.source_value or source_value,
                source_arguments=source_arguments,
            )
            context_state.last_status, sync_positionals = self._evaluate_sourced_file(
                source_path,
                context_state,
                stack,
                source_arguments=source_arguments,
            )
            self.events[event_index] = replace(
                self.events[event_index],
                sync_positionals=sync_positionals,
            )

        state.last_status = None
        return True

    def _resolve_child_shell_source(self, node: SourceSite, state: EvaluationState):
        source_override = self._source_override_for_node(node)
        if source_override is SOURCE_OVERRIDE_EXHAUSTED:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
                "runtime source graph override exhausted",
                "The trusted runtime graph did not contain enough matching child-shell source events.",
            )
        if source_override is not None:
            return (
                Path(source_override.resolved_path),
                node.source_expression.strip(),
                source_override.arguments or None,
            )

        try:
            self._ensure_source_state_can_resolve(node, node.source_expression, state)
            resolved_expression = self._expand_array_indexes(node.source_expression, node, state)
            invocation = self._resolve_source_invocation(resolved_expression, node, state)
        except UnsupportedSourceError as exc:
            raise with_source_diagnostic(
                exc,
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
            ) from exc

        if not invocation.source:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
                "unsupported unresolved child-shell source",
                "Child-shell source sites must resolve to exact source paths.",
            )
        if self._is_missing_source(invocation.source):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
                "unsupported missing child-shell source",
                "Missing-source runtime lowering is supported only for parent-shell source sites.",
            )
        if self._is_source_expansion_failure(invocation.source):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.child-shell",
                "unsupported child-shell source expansion failure",
                "Failglob source expansion failure lowering is supported only for parent-shell source sites.",
            )

        source_path = Path(invocation.source.path)
        source_value = invocation.source.source_value or self._source_runtime_value(
            invocation.source.source_expression,
            state,
        )
        return source_path, source_value, invocation.source_arguments

    @classmethod
    def _child_shell_source_commands(cls, command: str):
        return (
            *cls._subshell_source_commands(command),
            *cls._command_substitution_source_commands(command),
            *cls._process_substitution_source_commands(command),
            *cls._bash_c_source_commands(command),
            *cls._pipeline_source_commands(command),
        )

    @classmethod
    def _subshell_source_commands(cls, command: str):
        commands = []
        for body, body_start, context_index in cls._subshell_bodies(command):
            commands.extend(cls._direct_source_commands_in_body(
                body,
                body_start,
                ("subshell", context_index),
            ))
        return tuple(commands)

    @classmethod
    def _command_substitution_source_commands(cls, command: str):
        commands = []
        for body, body_start, context_index in cls._command_substitution_bodies(command):
            commands.extend(cls._direct_source_commands_in_body(
                body,
                body_start,
                ("command-substitution", context_index),
            ))
        return tuple(commands)

    @classmethod
    def _process_substitution_source_commands(cls, command: str):
        commands = []
        for body, body_start, context_index in cls._process_substitution_bodies(command):
            commands.extend(cls._direct_source_commands_in_body(
                body,
                body_start,
                ("process-substitution", context_index),
            ))
        return tuple(commands)

    @classmethod
    def _bash_c_source_commands(cls, command: str):
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            return ()
        if not words:
            return ()

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index + 2 >= len(words):
            return ()

        command_name = strip_shell_word_quotes(words[index])
        if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"} or words[index + 1] != "-c":
            return ()

        if len(words) < index + 3:
            return ()
        payload_word = words[index + 2]
        if payload_word.startswith('"') and "$" in payload_word:
            raise UnsupportedSourceError("unsupported parent-expanded bash -c payload")

        payload = strip_shell_word_quotes(payload_word)
        source_commands = cls._direct_source_commands_in_body(payload, 0, ("bash-c", 1))
        if not source_commands:
            return ()
        if len(source_commands) != 1:
            raise UnsupportedSourceError("unsupported multi-source bash -c payload")

        source_command = source_commands[0]
        source_expression = source_command.source_expression
        extra_words = tuple(words[index + 3:])
        if extra_words:
            source_expression = cls._bash_c_positional_source_expression(source_expression, extra_words)
            if source_expression is None:
                raise UnsupportedSourceError("unsupported bash -c source arguments")
        elif re.search(r'[$`*?\[]', source_expression):
            raise UnsupportedSourceError("unsupported dynamic bash -c source expression")

        command_start = command.find(words[index])
        if command_start < 0:
            command_start = 0
        return (ChildShellSourceCommand(
            context_id=("bash-c", 1),
            command_name=source_command.command_name,
            source_expression=source_expression,
            source_site=command.strip(),
            column=command_start + 1,
            replacement_kind="bash-c-source",
            resolve_source_site=source_command.source_site,
            source_value=source_command.source_site,
        ),)

    @staticmethod
    def _bash_c_positional_source_expression(source_expression: str, extra_words: tuple[str, ...]):
        stripped = strip_shell_word_quotes(source_expression)
        match = re.fullmatch(r'\$(?:\{([1-9][0-9]*)\}|([1-9][0-9]*))', stripped)
        if not match:
            return None
        position = int(match.group(1) or match.group(2))
        if position >= len(extra_words):
            return None
        return extra_words[position]

    @classmethod
    def _pipeline_source_commands(cls, command: str):
        segments = cls._pipeline_segments(command)
        if len(segments) <= 1:
            return ()

        commands = []
        final_index = len(segments) - 1
        for index, (segment, segment_start) in enumerate(segments):
            source_commands = cls._direct_source_commands_in_body(
                segment,
                segment_start,
                ("pipeline", index + 1),
            )
            if not source_commands:
                continue
            if index == final_index:
                raise UnsupportedSourceError(
                    "unsupported source in final pipeline segment; lastpipe-sensitive semantics are not modeled"
                )
            commands.extend(source_commands)
        return tuple(commands)

    @classmethod
    def _subshell_bodies(cls, command: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0
        context_index = 0

        while index < len(command):
            char = command[index]
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

            if in_single_quote or in_double_quote:
                index += 1
                continue

            if (
                char == "("
                and not command.startswith("((", index)
                and not cls._is_array_assignment_paren(command, index)
                and (index == 0 or command[index - 1] not in "$<>")
            ):
                body, end_index = cls._read_balanced_body(command, index + 1)
                if end_index is None:
                    index += 1
                    continue
                context_index += 1
                yield body, index + 1, context_index
                index = end_index + 1
                continue

            index += 1

    @classmethod
    def _command_substitution_bodies(cls, command: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0
        context_index = 0

        while index < len(command):
            char = command[index]
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

            if in_single_quote:
                index += 1
                continue

            if command.startswith("$((", index):
                body, end_index = cls._read_balanced_body(command, index + 3)
                index = end_index + 1 if end_index is not None else index + 3
                continue

            if command.startswith("$(", index):
                body, end_index = cls._read_balanced_body(command, index + 2)
                if end_index is None:
                    index += 2
                    continue
                context_index += 1
                yield body, index + 2, context_index
                index = end_index + 1
                continue

            index += 1

    @classmethod
    def _process_substitution_bodies(cls, command: str):
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0
        context_index = 0

        while index < len(command):
            char = command[index]
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

            if in_single_quote or in_double_quote:
                index += 1
                continue

            if command.startswith("<(", index) or command.startswith(">(", index):
                body, end_index = cls._read_balanced_body(command, index + 2)
                if end_index is None:
                    index += 2
                    continue
                context_index += 1
                yield body, index + 2, context_index
                index = end_index + 1
                continue

            index += 1

    @staticmethod
    def _pipeline_segments(command: str):
        segments = []
        current_start = 0
        in_single_quote = False
        in_double_quote = False
        escaped = False
        paren_depth = 0
        index = 0

        while index < len(command):
            char = command[index]
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

            if not in_single_quote and not in_double_quote:
                if command.startswith("||", index):
                    index += 2
                    continue

                if char == "(":
                    paren_depth += 1
                elif char == ")" and paren_depth:
                    paren_depth -= 1

                if char == "|" and paren_depth == 0 and not command.startswith("||", index):
                    segment = command[current_start:index].strip()
                    if segment:
                        leading = len(command[current_start:index]) - len(command[current_start:index].lstrip())
                        segments.append((segment, current_start + leading))
                    current_start = index + 1
                    index += 1
                    continue

            index += 1

        tail = command[current_start:].strip()
        if tail:
            leading = len(command[current_start:]) - len(command[current_start:].lstrip())
            segments.append((tail, current_start + leading))
        return tuple(segments)

    @classmethod
    def _direct_source_commands_in_body(cls, body: str, body_start: int, context_id: tuple[str, int]):
        source_commands = []
        search_start = 0
        for command in get_commands(body):
            command_start = body.find(command, search_start)
            if command_start < 0:
                command_start = search_start
            search_start = command_start + len(command)
            source_command = cls._direct_source_command(command, body_start + command_start, context_id)
            if source_command is not None:
                source_commands.append(source_command)
        return tuple(source_commands)

    @staticmethod
    def _direct_source_command(command: str, command_start: int, context_id: tuple[str, int]):
        invocation = source_command_invocation(command)
        if invocation is None:
            return None
        if invocation.command_start_index != 0:
            return None
        if invocation.wrapped:
            words = [strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(command)]
            if not words or words[0] not in {"builtin", "command"}:
                return None

        source_expression = invocation.source_expression
        if not source_expression:
            return None
        return ChildShellSourceCommand(
            context_id=context_id,
            command_name=invocation.command_name,
            source_expression=source_expression,
            source_site=invocation.source_site,
            column=command_start + invocation.source_site_column_offset + 1,
        )

    @staticmethod
    def _read_balanced_body(text: str, start_index: int):
        body = []
        in_single_quote = False
        in_double_quote = False
        escaped = False
        depth = 1
        index = start_index

        while index < len(text):
            char = text[index]
            if escaped:
                body.append(char)
                escaped = False
                index += 1
                continue

            if char == "\\" and not in_single_quote:
                body.append(char)
                escaped = True
                index += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                body.append(char)
                index += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                body.append(char)
                index += 1
                continue

            if not in_single_quote and not in_double_quote:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        return "".join(body), index

            body.append(char)
            index += 1

        return None, None

    @staticmethod
    def _is_array_assignment_paren(text: str, paren_index: int):
        if paren_index == 0 or text[paren_index - 1] != "=":
            return False

        word_start = paren_index - 2
        while word_start >= 0 and not text[word_start].isspace() and text[word_start] not in ";&|":
            word_start -= 1

        assignment_name = text[word_start + 1:paren_index - 1]
        return bool(re.fullmatch(r"[a-zA-Z_]\w*(?:\[[^\]]+\])?\+?", assignment_name))

    def _apply_exact_non_source_eval(self, node: RawCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError:
            return False
        if not words:
            return False

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index >= len(words) or strip_shell_word_quotes(words[index]) != "eval":
            return False

        payload = " ".join(words[index + 1:])
        payload = resolve_variable_references(payload, state.runtime_context())
        payload = os.path.expandvars(strip_matching_quotes(payload))
        if "$" in payload or "`" in payload:
            return False
        if contains_source_command(payload) or contains_nested_source_command(payload):
            return False

        payload_node = RawCommand(node.location, payload)
        shopt_status = self._apply_shopt(payload_node, state)
        if shopt_status is not None:
            state.last_status = shopt_status
        else:
            state.last_status = self._raw_exact_status_command(payload_node, state)
            if not payload.strip():
                state.last_status = 0
        return True

    def _apply_function_call(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError:
            return False
        if not words:
            return False

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index >= len(words):
            return False

        function_name, exact_dispatch = self._resolve_function_name(words[index], node, state)
        if not exact_dispatch:
            graph_dispatch = self._graph_backed_dynamic_function_dispatch(words[index + 1:], node, state)
            if graph_dispatch is not None:
                function_name, function_def, variants, arguments = graph_dispatch
                prefix_words = words[:index]
                if len(variants) != 1:
                    return self._apply_function_variants(
                        variants,
                        function_name,
                        arguments,
                        prefix_words,
                        node,
                        state,
                        stack,
                    )
                self._apply_function_call_variant(
                    function_def,
                    function_name,
                    arguments,
                    prefix_words,
                    node,
                    state,
                    stack,
                )
                return True
            if self._state_has_source_relevant_functions(state):
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.function-dispatch",
                    "unsupported dynamic function dispatch",
                    "Function dispatch must resolve exactly when source-relevant functions are in scope.",
                )
            return False

        if function_name in state.ambiguous_functions:
            variants = state.function_variants.get(function_name)
            function_def = state.functions.get(function_name)
            if variants is not None and not self._function_variants_may_source(variants):
                return False
            if variants is None and function_def is not None and not self._node_list_may_source(function_def.body):
                return False
            if variants is None and function_def is None and self.source_overrides:
                return False
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-dispatch",
                f"unsupported branch-dependent function call: {function_name}",
                "Define source-relevant functions consistently before calling them.",
            )
        function_def = state.functions.get(function_name)
        if function_def is None:
            return False

        variants = state.function_variants.get(function_name, (function_def,))
        if (
            self.mode == "executable"
            and node.separator in {"&&", "||"}
            and state.last_status is None
            and self._function_variants_may_source(variants)
        ):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-guard",
                f"unsupported unknown guarded source-relevant function call: {function_name}",
                "Source-relevant function calls behind unknown &&/|| guards must be modeled explicitly before lowering.",
            )

        if function_name in state.function_call_stack:
            if not self._graph_backed_recursion_allowed(function_name, state):
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.function-recursion",
                    f"unsupported recursive function call: {function_name}",
                    "Recursive source effects need an explicit bounded recursion model.",
                )

        try:
            arguments = self._resolve_function_arguments(function_name, words[index + 1:], node, state)
        except UnsupportedSourceError:
            if self.mode == "context" or not self._function_variants_may_source(variants):
                return False
            raise
        prefix_words = words[:index]
        if len(variants) == 1:
            self._apply_function_call_variant(
                variants[0],
                function_name,
                arguments,
                prefix_words,
                node,
                state,
                stack,
            )
            return True

        return self._apply_function_variants(
            variants,
            function_name,
            arguments,
            prefix_words,
            node,
            state,
            stack,
        )

    def _apply_function_variants(
        self,
        variants: tuple[FunctionDef, ...],
        function_name: str,
        arguments: tuple[str, ...],
        prefix_words: list[str],
        node: RawCommand,
        state: EvaluationState,
        stack: tuple[Path, ...],
    ):
        base_state = state.child_shell_copy()
        outcomes = []
        for variant in variants:
            variant_state = base_state.child_shell_copy()
            variant_state.occurrence_context = OccurrenceModel.MUTUALLY_EXCLUSIVE
            self._apply_function_call_variant(
                variant,
                function_name,
                arguments,
                prefix_words,
                node,
                variant_state,
                stack,
            )
            outcomes.append(EvaluationOutcome(variant_state))

        self._merge_possible_states(state, [outcome.state for outcome in outcomes])
        return True

    def _graph_backed_dynamic_function_dispatch(
        self,
        argument_words: list[str],
        node: RawCommand,
        state: EvaluationState,
    ):
        if not self.source_overrides:
            return None

        observed_call = self._next_unconsumed_function_call_for_node(node, state)
        if observed_call is not None:
            return observed_call

        candidates = []
        for function_name, function_def in sorted(state.functions.items()):
            variants = state.function_variants.get(function_name, (function_def,))
            if not self._function_variants_may_source(variants):
                continue
            signatures = self.source_supplement.function_signatures(function_name)
            if not signatures:
                continue
            matching_signatures = tuple(
                signature for signature in signatures if len(signature) == len(argument_words)
            )
            candidates.append((function_name, function_def, variants, matching_signatures))

        next_override = self._next_unconsumed_source_override()
        if next_override is not None:
            matched_candidates = tuple(
                candidate
                for candidate in candidates
                if self._function_variants_contain_source_override(candidate[2], next_override)
            )
            if matched_candidates:
                candidates = list(matched_candidates)

        if len(candidates) > 1:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-dispatch",
                "ambiguous graph-backed dynamic function dispatch",
                "Trusted graph replay can bind a dynamic source helper only when one finite observed helper signature matches.",
            )
        if len(candidates) == 1:
            function_name, function_def, variants, signatures = candidates[0]
            try:
                arguments = self._resolve_function_exact_arguments(argument_words, node, state)
            except UnsupportedSourceError:
                arguments = self._graph_backed_dynamic_function_arguments(function_name, signatures)
                if arguments is None:
                    return None
            return function_name, function_def, variants, arguments
        return None

    def _next_unconsumed_function_call_for_node(self, node: RawCommand, state: EvaluationState):
        next_override = self._next_unconsumed_source_override()
        if next_override is None or next_override.function_call is None:
            return None
        function_name, path, line, arguments = next_override.function_call
        if node.location.path.resolve(strict=False) != Path(path).resolve(strict=False):
            return None
        if node.location.line != line:
            return None
        function_def = state.functions.get(function_name)
        if function_def is None:
            return None
        variants = state.function_variants.get(function_name, (function_def,))
        return function_name, function_def, variants, arguments

    def _next_unconsumed_source_override(self):
        next_item = None
        for key, overrides in self.source_overrides.items():
            consumed = self._source_override_indexes[key]
            if consumed >= len(overrides):
                continue
            override = overrides[consumed]
            graph_index = override.graph_index
            if graph_index < 0:
                graph_index = 1_000_000_000
            item = (graph_index, override)
            if next_item is None or item[0] < next_item[0]:
                next_item = item
        return None if next_item is None else next_item[1]

    def _function_variants_contain_source_override(self, variants: tuple[FunctionDef, ...], override: SourceOverride):
        return any(self._node_list_contains_source_override(variant.body, override) for variant in variants)

    def _node_list_contains_source_override(self, nodes, override: SourceOverride):
        for node in nodes:
            if isinstance(node, SourceSite) and self._source_site_matches_override(node, override):
                return True
            if isinstance(node, IfBlock):
                for branch in node.branches:
                    if self._condition_matches_source_override(
                        branch.condition,
                        branch.condition_location or node.location,
                        override,
                    ):
                        return True
                    if self._node_list_contains_source_override(branch.body, override):
                        return True
                continue
            if isinstance(node, WhileLoop):
                if self._condition_matches_source_override(node.condition, node.location, override):
                    return True
                if self._node_list_contains_source_override(node.body, override):
                    return True
                continue
            if isinstance(node, ForLoop):
                if self._node_list_contains_source_override(node.body, override):
                    return True
                continue
            if isinstance(node, CStyleForLoop):
                if self._node_list_contains_source_override(node.body, override):
                    return True
                continue
            if isinstance(node, CaseBlock):
                if any(self._node_list_contains_source_override(arm.body, override) for arm in node.arms):
                    return True
                continue
            if isinstance(node, FunctionDef):
                if self._node_list_contains_source_override(node.body, override):
                    return True
        return False

    def _source_site_matches_override(self, site: SourceSite, override: SourceOverride):
        return (
            site.location.path.resolve(strict=False) == Path(override.path).resolve(strict=False)
            and site.location.line == override.line
            and self._source_override_command_key(site.text) == self._source_override_command_key(override.command)
        )

    def _condition_matches_source_override(self, condition: str | None, location: SourceLocation, override: SourceOverride):
        if not condition:
            return False
        if location.path.resolve(strict=False) != Path(override.path).resolve(strict=False):
            return False
        if location.line != override.line:
            return False
        try:
            atoms = self._source_logical_condition_atoms_from_text(condition)
        except UnsupportedSourceError:
            return False
        override_key = self._source_override_command_key(override.command)
        return any(
            atom.source_command is not None
            and self._source_override_command_key(f"{atom.source_command} {atom.source_expression}") == override_key
            for atom in atoms
        )

    def _graph_backed_dynamic_function_arguments(self, function_name: str, signatures: tuple[tuple[str, ...], ...]):
        key = function_name
        index = self._function_dispatch_signature_indexes[key]
        if index >= len(signatures):
            return None
        self._function_dispatch_signature_indexes[key] += 1
        return signatures[index]

    def _graph_backed_recursion_allowed(self, function_name: str, state: EvaluationState):
        if not self.source_overrides:
            return False
        current_depth = sum(1 for name in state.function_call_stack if name == function_name)
        observed_source_budget = sum(len(overrides) for overrides in self.source_overrides.values())
        return current_depth <= observed_source_budget + 1

    def _apply_function_call_variant(
        self,
        function_def: FunctionDef,
        function_name: str,
        arguments: tuple[str, ...],
        prefix_words: list[str],
        call_node: RawCommand,
        state: EvaluationState,
        stack: tuple[Path, ...],
    ):
        prefix_scope = {}
        self._apply_function_assignment_prefixes(prefix_words, prefix_scope, call_node, state)
        previous_positional_arguments = state.positional_arguments
        previous_ambiguous_positionals = state.ambiguous_positionals
        previous_positional_assignment_generation = state.positional_assignment_generation
        previous_positionals = self._push_function_positionals(arguments, state)
        state.ambiguous_positionals = False
        previous_call_stack = state.function_call_stack
        previous_function_body_depth = state.function_body_depth
        state.function_call_stack = (*state.function_call_stack, function_name)
        state.positional_arguments = arguments
        state.function_body_depth += 1
        state.local_scopes.append({})
        return_status = None
        source_expansion_abort = False
        try:
            try:
                self._evaluate_nodes(function_def.body, state, stack)
            except FunctionReturnSignal as signal:
                return_status = signal.status
            except FunctionSourceExpansionAbortSignal:
                return_status = 1
                source_expansion_abort = True
                if previous_function_body_depth > 0:
                    self._record_function_abort_call_replacement(call_node, nested=True)
                    raise
        finally:
            local_scope = state.local_scopes.pop()
            self._restore_local_scope(local_scope, state)
            self._restore_function_positionals(previous_positionals, len(arguments), state)
            state.positional_arguments = previous_positional_arguments
            state.ambiguous_positionals = previous_ambiguous_positionals
            state.positional_assignment_generation = previous_positional_assignment_generation
            self._restore_local_scope(prefix_scope, state)
            state.function_call_stack = previous_call_stack
            state.function_body_depth = previous_function_body_depth
        if return_status is not None:
            state.last_status = return_status
            if source_expansion_abort:
                self._record_function_abort_call_replacement(call_node, nested=False)
                raise LineAbortSignal(call_node.location.path, call_node.location.line)
        return return_status

    def _record_function_abort_call_replacement(self, call_node: RawCommand, *, nested: bool):
        replacement = f"{{ {call_node.text}; return 1; }}" if nested else f"{call_node.text} #"
        self._record_line_replacement(call_node.location, call_node.text, replacement)

    def _resolve_function_name(self, word: str, node: RawCommand, state: EvaluationState):
        if "$" not in word:
            return strip_shell_word_quotes(word), True

        try:
            return self._resolve_function_exact_word(
                word,
                node,
                state,
                "unsupported.source.function-dispatch",
                "unsupported dynamic function dispatch",
                "unsupported unresolved function dispatch",
                "Function dispatch must resolve to a known local function before source-aware evaluation.",
            ), True
        except UnsupportedSourceError:
            return strip_shell_word_quotes(word), False

    def _state_has_source_relevant_functions(self, state: EvaluationState):
        return any(
            self._node_list_may_source(function_def.body)
            for function_def in state.functions.values()
        ) or any(
            self._node_list_may_source(function_def.body)
            for variants in state.function_variants.values()
            for function_def in variants
        )

    def _function_variants_may_source(self, variants: tuple[FunctionDef, ...]):
        return any(self._node_list_may_source(function_def.body) for function_def in variants)

    def _apply_function_assignment_prefixes(self, words: list[str], scope: dict, node: RawCommand,
                                            state: EvaluationState):
        for word in words:
            match = re.match(r'^([a-zA-Z_]\w*)(\+?)=(.*)$', word, re.S)
            if not match:
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.function-assignment",
                    "unsupported function assignment prefix",
                    "Function assignment prefixes must be exact scalar assignments.",
                )
            name, append_operator, value = match.groups()
            self._capture_variable_in_scope(name, scope, state)
            resolved = self._resolve_function_exact_word(
                value,
                node,
                state,
                "unsupported.source.function-assignment",
                "unsupported dynamic function assignment prefix",
                "unsupported unresolved function assignment prefix",
                "Function assignment prefixes must be exact for source-aware function evaluation.",
            )
            if append_operator:
                resolved = state.runtime_variables.get(name, "") + resolved
            state.variables[name] = resolved
            state.runtime_variables[name] = resolved
            state.ambiguous_variables.discard(name)

    def _resolve_function_arguments(self, function_name: str, words: list[str], node: RawCommand, state: EvaluationState):
        try:
            return self._resolve_function_exact_arguments(words, node, state)
        except UnsupportedSourceError as exc:
            supplemented = self._supplemented_function_arguments(function_name, len(words), node)
            if supplemented is not None:
                return supplemented
            variable_names = self._unresolved_word_variables(words, state)
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.function-argument",
                str(exc),
                "Provide exact function arguments or a source supplement for the missing source values.",
                details={
                    "supplement_skeleton": supplement_skeleton(
                        variable_names=variable_names,
                        function_name=function_name,
                    ),
                },
            ) from exc

    def _resolve_function_exact_arguments(self, words: list[str], node: RawCommand, state: EvaluationState):
        arguments = []
        for word in words:
            stripped = word.strip()
            if stripped in {'"$@"', '"${@}"'}:
                if state.ambiguous_positionals:
                    raise unsupported_source_error(
                        str(node.location.path),
                        node.location.line - 1,
                        node.text,
                        node.text,
                        "unsupported.source.function-argument",
                        "unsupported ambiguous positional function argument expansion",
                        "Function arguments must resolve exactly for source-aware function evaluation.",
                    )
                arguments.extend(state.positional_arguments)
                continue
            if stripped in {'"$*"', '"${*}"'}:
                if state.ambiguous_positionals:
                    raise unsupported_source_error(
                        str(node.location.path),
                        node.location.line - 1,
                        node.text,
                        node.text,
                        "unsupported.source.function-argument",
                        "unsupported ambiguous positional function argument expansion",
                        "Function arguments must resolve exactly for source-aware function evaluation.",
                    )
                arguments.append(self._joined_positionals(state))
                continue
            if re.search(r'(?<!\\)\$(?:\{?[@*]\}?)', stripped):
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.function-argument",
                    "unsupported positional function argument expansion",
                    "Only quoted standalone $@/$* function arguments are supported.",
                )
            arguments.append(SourceEvaluator._resolve_function_exact_word(
                word,
                node,
                state,
                "unsupported.source.function-argument",
                "unsupported dynamic function argument",
                "unsupported unresolved function argument",
                "Function arguments must be exact for source-aware function evaluation.",
            ))
        return tuple(arguments)

    def _supplemented_function_arguments(self, function_name: str, word_count: int, node: RawCommand):
        signatures = tuple(
            signature
            for signature in self.source_supplement.function_signatures(function_name)
            if len(signature) == word_count
        )
        if not signatures:
            return None
        if len(signatures) == 1:
            return signatures[0]
        raise unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.function-argument",
            f"ambiguous source supplement signatures for function {function_name}",
            "Provide exactly one supplement signature for unresolved helper call arguments.",
            details={"supplement_skeleton": supplement_skeleton(function_name=function_name)},
        )

    @staticmethod
    def _unresolved_word_variables(words: list[str], state: EvaluationState):
        names = set()
        for word in words:
            for match in SCALAR_REFERENCE_PATTERN.finditer(word):
                name = match.group(1) or match.group(2)
                if name not in state.runtime_variables:
                    names.add(name)
        return names

    @staticmethod
    def _resolve_function_exact_word(word: str, node: RawCommand, state: EvaluationState, code: str,
                                     dynamic_message: str, unresolved_message: str, hint: str):
        if SourceEvaluator._raw_word_is_single_quoted(word):
            return strip_shell_word_quotes(word)

        if '$(' in word or '`' in word:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                code,
                dynamic_message,
                hint,
            )
        resolved = resolve_variable_references(word, state.runtime_context())
        resolved = os.path.expandvars(resolved)
        if "$" in resolved:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                code,
                unresolved_message,
                hint,
            )
        return strip_matching_quotes(resolved)

    @staticmethod
    def _push_function_positionals(arguments: tuple[str, ...], state: EvaluationState):
        positional_names = {str(index) for index in range(1, len(arguments) + 1)}
        positional_names.update(
            name
            for mapping in (state.variables, state.runtime_variables)
            for name in mapping
            if name.isdigit()
        )
        previous = {
            name: (
                name in state.variables,
                state.variables.get(name),
                name in state.runtime_variables,
                state.runtime_variables.get(name),
                name in state.ambiguous_variables,
            )
            for name in positional_names
        }
        for index, argument in enumerate(arguments, start=1):
            name = str(index)
            state.variables[name] = argument
            state.runtime_variables[name] = argument
            state.ambiguous_variables.discard(name)
        for name in positional_names - {str(index) for index in range(1, len(arguments) + 1)}:
            state.variables.pop(name, None)
            state.runtime_variables.pop(name, None)
            state.ambiguous_variables.discard(name)
        return previous

    @staticmethod
    def _restore_function_positionals(previous_positionals, argument_count: int, state: EvaluationState):
        for index in range(1, argument_count + 1):
            state.ambiguous_variables.discard(str(index))
        for name, (
            had_value,
            previous_value,
            had_runtime_value,
            previous_runtime_value,
            was_ambiguous,
        ) in previous_positionals.items():
            if had_value and previous_value is not None:
                state.variables[name] = previous_value
            else:
                state.variables.pop(name, None)
            if had_runtime_value and previous_runtime_value is not None:
                state.runtime_variables[name] = previous_runtime_value
            else:
                state.runtime_variables.pop(name, None)
            if was_ambiguous:
                state.ambiguous_variables.add(name)
            else:
                state.ambiguous_variables.discard(name)

    @staticmethod
    def _raw_function_return_command(node: RawCommand):
        stripped = node.text.strip()
        return bool(re.match(r'^return(?:\s|$)', stripped))

    @staticmethod
    def _raw_function_shift_command(node: RawCommand):
        stripped = node.text.strip()
        return bool(re.match(r'^shift(?:\s|$)', stripped))

    def _raw_exact_status_command(self, node: RawCommand, state: EvaluationState):
        stripped = node.text.strip()
        if contains_source_command(stripped) or contains_nested_source_command(stripped):
            return None

        bracket_status = self._raw_bracket_status(stripped, state)
        if bracket_status is not None:
            return bracket_status
        if has_unsupported_shell_operator(stripped):
            return None

        try:
            words = parse_shell_words_preserving_quotes(stripped)
        except UnsupportedSourceError:
            return None
        if not words:
            return 0

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index >= len(words):
            return 0

        command_name = strip_shell_word_quotes(words[index])
        if command_name in {":", "true", "echo"}:
            return 0
        if command_name == "false":
            return 1
        if command_name == "printf" and self._printf_status_is_exact_success(words[index + 1:]):
            return 0
        return None

    @staticmethod
    def _printf_status_is_exact_success(arguments: list[str]):
        if not arguments:
            return False

        index = 0
        first = strip_shell_word_quotes(arguments[index])
        if first == "--":
            index += 1
            if index >= len(arguments):
                return False
            first = strip_shell_word_quotes(arguments[index])
        if first == "-v":
            return False
        if first.startswith("-"):
            return False

        format_word = arguments[index]
        if "$" in format_word or "`" in format_word:
            return False
        return SourceEvaluator._printf_format_is_string_only(strip_shell_word_quotes(format_word))

    @staticmethod
    def _printf_format_is_string_only(format_value: str):
        index = 0
        while index < len(format_value):
            if format_value[index] != "%":
                index += 1
                continue

            index += 1
            if index >= len(format_value):
                return False
            if format_value[index] == "%":
                index += 1
                continue
            if format_value[index] == "(":
                return False

            while index < len(format_value) and format_value[index] in "#0- +":
                index += 1
            if index < len(format_value) and format_value[index] == "*":
                return False
            while index < len(format_value) and format_value[index].isdigit():
                index += 1
            if index < len(format_value) and format_value[index] == ".":
                index += 1
                if index < len(format_value) and format_value[index] == "*":
                    return False
                while index < len(format_value) and format_value[index].isdigit():
                    index += 1
            if index >= len(format_value) or format_value[index] not in {"b", "c", "q", "Q", "s"}:
                return False
            index += 1
        return True

    def _raw_bracket_status(self, stripped: str, state: EvaluationState):
        if not (
            (stripped.startswith("[[") and stripped.endswith("]]"))
            or (stripped.startswith("[") and stripped.endswith("]"))
        ):
            return None
        include_guard_status = self._raw_include_guard_status(stripped, state)
        if include_guard_status is not None:
            return include_guard_status
        try:
            result = self._evaluate_condition(stripped, state)
        except UnsupportedSourceError:
            return None
        if result == "true":
            return 0
        if result == "false":
            return 1
        return None

    @staticmethod
    def _raw_include_guard_status(stripped: str, state: EvaluationState):
        match = re.fullmatch(
            r'\[\[?\s+-n\s+"?\$(?:\{([a-zA-Z_]\w*)\}|([a-zA-Z_]\w*))"?\s+\]?\]',
            stripped,
        )
        if not match:
            return None
        name = match.group(1) or match.group(2)
        if name in state.ambiguous_variables:
            return None
        value = state.runtime_variables.get(name, os.environ.get(name, ""))
        return 0 if value else 1

    def _raw_command_skipped_by_known_status(self, node: RawCommand, state: EvaluationState):
        if node.separator == "&&" and state.last_status not in {None, 0}:
            self._disable_unreachable_sources([node], "&& previous command status")
            return True
        if node.separator == "||" and state.last_status == 0:
            self._disable_unreachable_sources([node], "|| previous command status")
            return True
        return False

    def _function_return_status(self, node: RawCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError as exc:
            raise self._unsupported_function_control(node, "unsupported function return syntax") from exc

        if len(words) > 2 or not words or words[0] != "return":
            raise self._unsupported_function_control(node, "unsupported function return syntax")
        if len(words) == 1:
            if state.last_status is None:
                raise self._unsupported_function_control(node, "unsupported implicit return status")
            return state.last_status % 256

        status_text = self._resolve_function_control_word(words[1], node, state, "return")
        if not re.fullmatch(r'[+-]?\d+', status_text):
            raise self._unsupported_function_control(node, "unsupported non-integer function return status")
        return int(status_text) % 256

    def _apply_function_shift(self, node: RawCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError as exc:
            raise self._unsupported_function_control(node, "unsupported function shift syntax") from exc

        if len(words) > 2 or not words or words[0] != "shift":
            raise self._unsupported_function_control(node, "unsupported function shift syntax")

        if len(words) == 1:
            count = 1
        else:
            count_text = self._resolve_function_control_word(words[1], node, state, "shift")
            if not re.fullmatch(r'\d+', count_text):
                raise self._unsupported_function_control(node, "unsupported non-integer function shift count")
            count = int(count_text)

        if state.ambiguous_positionals:
            state.last_status = 0 if count == 0 else None
            return

        argument_count = len(state.positional_arguments)
        if count == 0:
            state.last_status = 0
            return
        if count > argument_count:
            state.last_status = 1
            return

        self._set_positionals(state.positional_arguments[count:], state)
        state.last_status = 0

    def _resolve_function_control_word(self, word: str, node: RawCommand, state: EvaluationState, command: str):
        return self._resolve_function_exact_word(
            word,
            node,
            state,
            "unsupported.source.function-control",
            f"unsupported dynamic function {command}",
            f"unsupported unresolved function {command}",
            "Function control arguments must be exact for source-aware function evaluation.",
        )

    @staticmethod
    def _unsupported_function_control(node: RawCommand, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.function-control",
            message,
            "Function return/shift semantics must be exact for source-aware lowering.",
        )

    def _apply_loop_control(self, node: RawCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError:
            return False
        if not words:
            return False

        command = strip_shell_word_quotes(words[0])
        if command not in {"break", "continue"}:
            return False
        if len(words) > 2 or (len(words) == 2 and strip_shell_word_quotes(words[1]) != "1"):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.loop-control",
                f"unsupported {command} depth",
                "Only local break/continue control is modeled.",
            )
        if state.loop_depth <= 0:
            state.last_status = 1
            return True
        if node.separator in {"&&", "||"} and state.last_status is None:
            state.last_status = 1 if node.separator == "&&" else 0
            return True
        if command == "break":
            raise LoopBreakSignal()
        raise LoopContinueSignal()

    def _apply_array_population_command(self, node: RawCommand, state: EvaluationState):
        stripped = node.text.strip()
        if not re.match(r'^(?:mapfile|readarray)\b', stripped):
            return False

        try:
            words = parse_shell_words_preserving_quotes(stripped)
        except UnsupportedSourceError as exc:
            raise self._unsupported_array_population(node, "unsupported array population syntax") from exc

        if not words or strip_shell_word_quotes(words[0]) not in {"mapfile", "readarray"}:
            return False

        strip_newline = False
        index = 1
        while index < len(words) and words[index].startswith("-"):
            option = strip_shell_word_quotes(words[index])
            if option == "-t":
                strip_newline = True
                index += 1
                continue
            raise self._unsupported_array_population(node, f"unsupported array population option: {option}")

        if not strip_newline:
            raise self._unsupported_array_population(node, "mapfile/readarray without -t is unsupported")
        if index >= len(words) or not re.fullmatch(r'[a-zA-Z_]\w*', strip_shell_word_quotes(words[index])):
            raise self._unsupported_array_population(node, "unsupported array population target")

        name = strip_shell_word_quotes(words[index])
        index += 1
        if index != len(words) - 2 or words[index] != "<":
            raise self._unsupported_array_population(node, "unsupported array population redirection")

        input_path = self._word_list_path(strip_shell_word_quotes(words[index + 1]), node, state)
        if not input_path.is_file():
            raise self._unsupported_array_population(node, "unsupported array population input path")

        values = tuple(input_path.read_text().splitlines())
        if self.mode == "executable":
            self._record_line_replacement(
                node.location,
                node.text,
                f"{name}=({self._shell_quote_words(values)})",
            )

        state.arrays[name] = values
        state.associative_arrays.pop(name, None)
        state.ambiguous_arrays.discard(name)
        state.last_status = 0
        return True

    @staticmethod
    def _unsupported_array_population(node: RawCommand, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.array-population",
            message,
            "Use mapfile/readarray -t ARRAY < exact_file for modeled dynamic arrays.",
        )

    def _apply_arithmetic_command(self, node: RawCommand, state: EvaluationState):
        stripped = node.text.strip()
        if not stripped.startswith("((") or not stripped.endswith("))"):
            return False

        expression = stripped[2:-2].strip()
        if self._apply_arithmetic_mutation(expression, node, state):
            return True

        value = self._evaluate_arithmetic_expression(expression, state, stripped)
        if value is None:
            raise self._unsupported_arithmetic_command(node)
        state.last_status = 0 if value else 1
        return True

    def _apply_arithmetic_mutation(self, expression: str, node: RawCommand, state: EvaluationState):
        expression = expression.strip()
        if match := re.fullmatch(r'([a-zA-Z_]\w*)(\+\+|--)', expression):
            name, operator = match.groups()
            current = self._arithmetic_name_value(name, state, node.text)
            if current is None:
                raise self._unsupported_arithmetic_command(node)
            state.runtime_variables[name] = str(current + (1 if operator == "++" else -1))
            state.variables[name] = state.runtime_variables[name]
            state.ambiguous_variables.discard(name)
            state.last_status = 0 if current else 1
            return True

        if match := re.fullmatch(r'(\+\+|--)([a-zA-Z_]\w*)', expression):
            operator, name = match.groups()
            current = self._arithmetic_name_value(name, state, node.text)
            if current is None:
                raise self._unsupported_arithmetic_command(node)
            new_value = current + (1 if operator == "++" else -1)
            state.runtime_variables[name] = str(new_value)
            state.variables[name] = state.runtime_variables[name]
            state.ambiguous_variables.discard(name)
            state.last_status = 0 if new_value else 1
            return True

        if match := re.fullmatch(r'([a-zA-Z_]\w*)\s*([+\-*/%]?=)\s*(.+)', expression):
            name, operator, rhs_expression = match.groups()
            current = self._arithmetic_name_value(name, state, node.text)
            rhs = self._evaluate_arithmetic_expression(rhs_expression, state, node.text)
            if current is None or rhs is None:
                raise self._unsupported_arithmetic_command(node)
            if operator == "=":
                new_value = rhs
            elif operator == "+=":
                new_value = current + rhs
            elif operator == "-=":
                new_value = current - rhs
            elif operator == "*=":
                new_value = current * rhs
            elif operator == "/=":
                if rhs == 0:
                    raise self._unsupported_arithmetic_command(node)
                new_value = int(current / rhs)
            elif operator == "%=":
                if rhs == 0:
                    raise self._unsupported_arithmetic_command(node)
                new_value = current % rhs
            else:
                raise self._unsupported_arithmetic_command(node)
            state.runtime_variables[name] = str(new_value)
            state.variables[name] = state.runtime_variables[name]
            state.ambiguous_variables.discard(name)
            state.last_status = 0 if new_value else 1
            return True

        return False

    @staticmethod
    def _unsupported_arithmetic_command(node: RawCommand):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.arithmetic",
            "unsupported arithmetic command",
            "Arithmetic loop mutations must resolve exactly.",
        )

    @staticmethod
    def _apply_shopt(node: RawCommand, state: EvaluationState):
        stripped_text = node.text.strip()
        if not stripped_text.startswith("shopt "):
            return None

        try:
            words = parse_shell_words_preserving_quotes(stripped_text)
        except UnsupportedSourceError:
            return None

        if len(words) < 2 or words[0] != "shopt":
            return None

        action = words[1]
        if action not in {"-s", "-u"}:
            return None
        if len(words) == 2:
            return 0

        status = 0
        for option in words[2:]:
            option = strip_shell_word_quotes(option)
            if option not in KNOWN_SHOPT_OPTIONS:
                status = 1
                continue
            if option not in GLOB_SHOPT_OPTIONS:
                if action == "-s":
                    state.shell_options.add(option)
                else:
                    state.shell_options.discard(option)
                continue
            if action == "-s":
                state.glob_options.add(option)
            else:
                state.glob_options.discard(option)
        return status

    def _record_and_descend(self, source_path: Path, node: SourceSite, source_expression: str, source_site: str,
                            state: EvaluationState, stack: tuple[Path, ...], execution_model: ExecutionModel,
                            replacement_kind: str, source_value: str | None = None,
                            source_arguments: tuple[str, ...] | None = None):
        event_index = len(self.events)
        self._record_event(
            source_path, node, source_expression, source_site, execution_model, replacement_kind, state,
            source_value=source_value,
            source_arguments=source_arguments,
        )
        state.last_status, sync_positionals = self._evaluate_sourced_file(
            source_path,
            state,
            stack,
            source_arguments=source_arguments,
        )
        self.events[event_index] = replace(
            self.events[event_index],
            sync_positionals=sync_positionals,
        )

    def _evaluate_sourced_file(
        self,
        source_path: Path,
        state: EvaluationState,
        stack: tuple[Path, ...],
        source_arguments: tuple[str, ...] | None = None,
    ):
        previous_positional_arguments = None
        previous_positionals = None
        previous_ambiguous_positionals = None
        return_status = 0
        sync_positionals = False
        source_argument_frame_active = bool(state.source_argument_frame_dirty_stack)
        if source_argument_frame_active:
            self._clear_current_source_argument_frame_dirty(state)
        if source_arguments is not None:
            previous_positional_arguments = state.positional_arguments
            previous_ambiguous_positionals = state.ambiguous_positionals
            previous_positionals = self._push_function_positionals(source_arguments, state)
            state.positional_arguments = source_arguments
            state.ambiguous_positionals = False
            self._push_source_argument_frame(state)
        try:
            try:
                had_nodes = self._evaluate_file(source_path, state, stack, as_source=True)
            except SourceReturnSignal as signal:
                return_status = signal.status
            else:
                if not had_nodes:
                    return_status = 0
                else:
                    return_status = state.last_status
            if source_arguments is None:
                sync_positionals = (
                    source_argument_frame_active
                    and bool(state.source_argument_frame_dirty_stack)
                    and state.source_argument_frame_dirty_stack[-1]
                )
        finally:
            if source_arguments is not None:
                final_positional_arguments = state.positional_arguments
                final_ambiguous_positionals = state.ambiguous_positionals
                frame_dirty = self._pop_source_argument_frame(state)
                sync_positionals = frame_dirty
                self._restore_function_positionals(previous_positionals, len(source_arguments), state)
                state.positional_arguments = previous_positional_arguments
                state.ambiguous_positionals = previous_ambiguous_positionals
                if frame_dirty:
                    mark_parent_frame = not state.source_argument_frame_dirty_stack
                    if final_ambiguous_positionals:
                        self._mark_positionals_ambiguous(
                            state,
                            source_argument_escape=True,
                            mark_source_argument_frame=mark_parent_frame,
                        )
                    else:
                        self._set_positionals(
                            final_positional_arguments,
                            state,
                            source_argument_escape=True,
                            mark_source_argument_frame=mark_parent_frame,
                        )
        return return_status, sync_positionals

    def _record_event(self, source_path: Path, node, source_expression: str, source_site: str,
                      execution_model: ExecutionModel, replacement_kind: str, state: EvaluationState,
                      occurrence_model: OccurrenceModel | None = None, source_value: str | None = None,
                      source_arguments: tuple[str, ...] | None = None):
        self.events.append(SourceEvent(
            path=source_path.resolve(),
            location=node.location,
            source_expression=source_expression.strip(),
            source_site=source_site.strip(),
            execution_model=execution_model,
            occurrence_model=occurrence_model or state.occurrence_context,
            replacement_kind=replacement_kind,
            source_value=source_value,
            source_arguments=source_arguments,
            state_before=state.snapshot(),
            condition=state.condition_context,
        ))

    def _record_read_loop_replacements(self, node: WhileLoop, read_words: ReadLoopWords):
        if node.end_location is None:
            return
        variable = read_words.variable
        values = read_words.values
        header_match = re.match(r'^(.*?;\s*do)\b', node.text)
        header_text = header_match.group(1) if header_match else node.text
        inline_do = bool(header_match)
        replacement_prefix = "( " if read_words.child_shell else ""
        self._record_line_replacement(
            node.location,
            header_text,
            (
                f"{replacement_prefix}for {variable} in {self._shell_quote_words(values)}; do"
                if inline_do
                else f"{replacement_prefix}for {variable} in {self._shell_quote_words(values)}"
            ),
        )
        if read_words.child_shell:
            self._record_line_replacement(
                node.end_location,
                "done",
                "done )",
            )
        elif node.trailing:
            self._record_line_replacement(
                node.end_location,
                f"done {node.trailing}",
                "done",
            )

    def _record_if_block_expansion_failure(self, node: IfBlock, pattern: str, state: EvaluationState):
        end_location = node.end_location or node.location
        return_from_function = state.function_body_depth > 0
        self._record_control_block_expansion_failure(
            node.location,
            end_location,
            pattern,
            return_from_function=return_from_function,
        )
        state.last_status = 1
        if return_from_function:
            raise FunctionSourceExpansionAbortSignal()

    def _record_for_loop_expansion_failure(self, node: ForLoop, pattern: str, state: EvaluationState):
        if node.end_location is None:
            raise self._unsupported_loop_words(node, "unsupported failglob loop without exact end location")

        self._record_line_replacement(node.location, node.words_text, "")
        self._disable_unreachable_sources(node.body, f"for {node.variable} in {node.words_text}")
        failure = self._source_expansion_failure_inline(
            pattern,
            return_from_function=state.function_body_depth > 0,
        )
        done_replacement = f"done; {failure}"
        if node.trailing:
            done_replacement = f"{done_replacement} #"
            done_fragment = f"done {node.trailing}"
        else:
            done_fragment = "done"
        self._record_line_replacement(node.end_location, done_fragment, done_replacement)

        state.last_status = 1
        if state.function_body_depth > 0:
            raise FunctionSourceExpansionAbortSignal()

    def _record_control_block_expansion_failure(
        self,
        start_location: SourceLocation,
        end_location: SourceLocation,
        pattern: str,
        *,
        return_from_function: bool = False,
    ):
        path = start_location.path
        if start_location.line == end_location.line:
            old = self._source_line_text(path, start_location.line)
            replacement = self._source_expansion_failure_inline(
                pattern,
                return_from_function=return_from_function,
            )
            if not return_from_function:
                replacement = f"{replacement} #"
            self._record_line_replacement(start_location, old, replacement)
            return

        self._record_line_replacement(
            start_location,
            self._source_line_text(path, start_location.line),
            self._source_expansion_failure_inline(pattern, return_from_function=return_from_function),
        )
        for line in range(start_location.line + 1, end_location.line):
            old = self._source_line_text(path, line)
            if old:
                self._record_line_replacement(SourceLocation(path, line, 1), old, ":")

        end_replacement = "return 1" if return_from_function else "( exit 1 )"
        self._record_line_replacement(
            end_location,
            self._source_line_text(path, end_location.line),
            end_replacement,
        )

    def _source_line_text(self, path: Path, line: int):
        lines = self._source_line_cache.get(path)
        if lines is None:
            lines = tuple(path.read_text().splitlines())
            self._source_line_cache[path] = lines
        try:
            return lines[line - 1].strip()
        except IndexError:
            return ""

    def _source_expansion_failure_inline(self, pattern: str, *, return_from_function: bool = False):
        commands = [
            "printf '%s: line %s: no match: %s\\n' "
            f'"${{BASH_SOURCE[0]}}" "${{LINENO}}" {self._shell_quote(pattern)} >&2',
            "( exit 1 )",
        ]
        if return_from_function:
            commands.append("return 1")
        return "{ " + "; ".join(commands) + "; }"

    def _record_line_replacement(self, location: SourceLocation, old: str, new: str):
        self.line_replacements.append(LineReplacement(location, old.strip(), new.strip()))

    @staticmethod
    def _shell_quote_words(words: tuple[str, ...]):
        return " ".join(SourceEvaluator._shell_quote(word) for word in words)

    @staticmethod
    def _shell_quote(value: str):
        return "'" + value.replace("'", "'\"'\"'") + "'"

    def _disable_unreachable_sources(self, nodes, condition: str):
        for node in nodes:
            if isinstance(node, SourceSite):
                self.disabled_sources.append(DisabledSourceSite(
                    location=node.location,
                    source_expression=node.source_expression.strip(),
                    source_site=self._source_site_text(node),
                    replacement_kind="source",
                    condition=condition,
                ))
            elif isinstance(node, RawCommand):
                stripped_text = node.text.strip()
                if self._raw_command_contains_literal_source(node.text):
                    self.disabled_sources.append(DisabledSourceSite(
                        location=node.location,
                        source_expression=stripped_text,
                        source_site=stripped_text,
                        replacement_kind="command",
                        condition=condition,
                    ))
            elif isinstance(node, FunctionDef):
                self._disable_unreachable_sources(node.body, condition)
            elif isinstance(node, ForLoop):
                self._disable_unreachable_sources(node.body, condition)
            elif isinstance(node, CStyleForLoop):
                self._disable_unreachable_sources(node.body, condition)
            elif isinstance(node, WhileLoop):
                self._disable_unreachable_sources(node.body, condition)
            elif isinstance(node, IfBlock):
                for branch in node.branches:
                    self._disable_unreachable_sources(branch.body, branch.condition or "else")
            elif isinstance(node, CaseBlock):
                for arm in node.arms:
                    self._disable_unreachable_sources(arm.body, self._case_arm_condition(node, arm))

    def _nodes_may_source(self, arms):
        for arm in arms:
            if self._node_list_may_source(arm.body):
                return True
        return False

    def _if_block_may_source(self, node: IfBlock):
        for branch in node.branches:
            if branch.condition and self._raw_command_may_source(branch.condition):
                return True
            if self._node_list_may_source(branch.body):
                return True
        return False

    def _node_list_may_source(self, nodes):
        for node in nodes:
            if isinstance(node, SourceSite):
                return True
            if isinstance(node, RawCommand) and self._raw_command_may_source(node.text):
                return True
            if isinstance(node, FunctionDef) and self._node_list_may_source(node.body):
                return True
            if isinstance(node, ForLoop) and self._node_list_may_source(node.body):
                return True
            if isinstance(node, CStyleForLoop) and self._node_list_may_source(node.body):
                return True
            if isinstance(node, WhileLoop) and self._node_list_may_source(node.body):
                return True
            if isinstance(node, IfBlock):
                if self._if_block_may_source(node):
                    return True
            if isinstance(node, CaseBlock) and self._nodes_may_source(node.arms):
                return True
        return False

    @staticmethod
    def _raw_command_may_source(command: str):
        if command.strip() in {"(", ")", "{", "}"}:
            return False
        if SourceEvaluator._continued_source_free_fragment(command):
            return False
        return bool(
            contains_source_command(command)
            or contains_nested_source_command(command)
            or SourceEvaluator._raw_command_payload_may_source(command)
            or SourceEvaluator._raw_command_may_expand_to_source(command)
        )

    @staticmethod
    def _raw_command_contains_literal_source(command: str):
        if command.strip() in {"(", ")", "{", "}"}:
            return False
        if SourceEvaluator._continued_source_free_fragment(command):
            return False
        return bool(
            contains_source_command(command)
            or contains_nested_source_command(command)
            or SourceEvaluator._raw_command_payload_may_source(command)
        )

    @staticmethod
    def _continued_source_free_fragment(command: str):
        return command.rstrip().endswith("\\") and not re.search(r'\bsource\b|(?:^|[\s;&|])\.\s+', command)

    @staticmethod
    def _raw_command_payload_may_source(command: str):
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            return bool(
                re.search(r'^\s*(?:[a-zA-Z_]\w*(?:\+)?=\S+\s+)*(?:eval|bash|/bin/bash|/usr/bin/bash)\b', command)
                and re.search(r'\bsource\b|(?:^|[\s;&|])\.', command)
            )

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index >= len(words):
            return False

        command_name = words[index]
        if command_name == "eval":
            payload = strip_matching_quotes(" ".join(words[index + 1:]))
            return contains_source_command(payload) or contains_nested_source_command(payload)

        if command_name in {"bash", "/bin/bash", "/usr/bin/bash"} and len(words) > index + 2 and words[index + 1] == "-c":
            payload = strip_matching_quotes(words[index + 2])
            return contains_source_command(payload) or contains_nested_source_command(payload)

        return False

    @staticmethod
    def _raw_command_may_expand_to_source(command: str):
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            if command.rstrip().endswith("\\") and not (
                contains_source_command(command) or contains_nested_source_command(command)
            ):
                return False
            return bool(
                '$' in command
                and re.search(r'^\s*(?:[a-zA-Z_]\w*(?:\+)?=\S+\s+)*(?:eval|bash|/bin/bash|/usr/bin/bash)\b', command)
            )

        index = 0
        while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
            index += 1
        if index >= len(words):
            return False

        command_name = words[index]
        if command_name == "eval":
            return any("$" in word for word in words[index + 1:])

        if command_name in {"bash", "/bin/bash", "/usr/bin/bash"}:
            return (
                len(words) > index + 2
                and words[index + 1] == "-c"
                and "$" in words[index + 2]
            )

        return False

    @staticmethod
    def _ensure_source_state_can_resolve(node, source_expression: str, state: EvaluationState):
        if state.ambiguous_cwd:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.branch-state",
                "unsupported source after branch-dependent cwd",
                "Reset cwd with an exact cd before the next source, or keep branch cwd effects convergent.",
            )

        if (has_unquoted_glob(source_expression) or has_unquoted_extglob(source_expression)) and (
            state.ambiguous_shell_options
            or state.ambiguous_glob_options
            or "GLOBIGNORE" in state.ambiguous_variables
        ):
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.branch-state",
                "unsupported glob source after branch-dependent shell state",
                "Keep glob-affecting shell options and GLOBIGNORE exact before sourcing a glob.",
            )

        variable_names = {match.group(1) or match.group(2) for match in SCALAR_REFERENCE_PATTERN.finditer(source_expression)}
        ambiguous_variables = sorted(variable_names & state.ambiguous_variables)
        if ambiguous_variables:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.branch-state",
                f"unsupported source after branch-dependent variable: {', '.join(ambiguous_variables)}",
                "Assign the same source-relevant value on every branch before sourcing it.",
            )

        array_names = {match.group(1) for match in ARRAY_ANY_INDEX_PATTERN.finditer(source_expression)}
        ambiguous_arrays = sorted(array_names & state.ambiguous_arrays)
        if ambiguous_arrays:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.branch-state",
                f"unsupported source after branch-dependent array: {', '.join(ambiguous_arrays)}",
                "Assign the same source-relevant array values on every branch before sourcing them.",
            )

    @staticmethod
    def _ensure_cd_state_can_resolve(node: CdCommand, state: EvaluationState):
        candidate = resolve_variable_references(node.path_expression, state.runtime_context())
        if "$" in candidate:
            candidate = ""
        candidate = os.path.expandvars(strip_matching_quotes(candidate))
        candidate = resolve_shell_path_commands(candidate, None)
        if candidate and os.path.isabs(candidate):
            return

        raise unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.branch-state",
            "unsupported relative cd after branch-dependent cwd",
            "Use an absolute cd target before the next source, or keep branch cwd effects convergent.",
        )

    @staticmethod
    def _expand_array_indexes(source_expression: str, node: SourceSite, state: EvaluationState):
        def replace(match):
            name, index_text = match.groups()
            if index_text == "@":
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.array-index",
                    "unsupported array source expression",
                    "Only exact array indexes can be resolved by the IR evaluator.",
                )

            associative_values = state.associative_arrays.get(name)
            if associative_values is not None:
                key = SourceEvaluator._resolve_array_key(index_text, node, state)
                if key not in associative_values:
                    raise unsupported_source_error(
                        str(node.location.path),
                        node.location.line - 1,
                        node.text,
                        node.text,
                        "unsupported.source.array-index",
                        "unsupported associative array source expression",
                        "Associative array source indexes must resolve to existing exact keys.",
                    )
                return associative_values[key]

            values = state.arrays.get(name)
            index = SourceEvaluator._resolve_array_index(index_text, node, state)
            if values is None or index >= len(values):
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.array-index",
                    "unsupported array source expression",
                    "Only exact array indexes can be resolved by the IR evaluator.",
                )
            return values[index]

        return ARRAY_ANY_INDEX_PATTERN.sub(replace, source_expression)

    @staticmethod
    def _resolve_array_key(index_expression: str, node, state: EvaluationState):
        index_expression = strip_matching_quotes(index_expression.strip())
        resolved = resolve_variable_references(index_expression, state.runtime_context())
        resolved = os.path.expandvars(strip_matching_quotes(resolved))
        if "$" in resolved:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.array-index",
                "unsupported associative array source expression",
                "Associative array indexes must resolve to exact keys.",
            )
        return resolved

    @staticmethod
    def _source_runtime_value(source_expression: str, state: EvaluationState):
        context = state.runtime_context()
        resolved_expression = resolve_variable_references(source_expression, context)
        return strip_matching_quotes(resolved_expression)

    @staticmethod
    def _is_plain_source_site(node: SourceSite):
        stripped_text = node.text.strip()
        for separator in ("&&", "||", ";"):
            if stripped_text.startswith(separator):
                stripped_text = stripped_text[len(separator):].strip()
                break
        invocation = source_command_invocation(stripped_text)
        if invocation is not None:
            if invocation.command_start_index != 0:
                return False
            if not invocation.wrapped:
                return True
            words = [strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(stripped_text)]
            return words[0] in {"builtin", "command"}
        return (
            stripped_text.startswith("source ")
            or stripped_text.startswith(". ")
            or stripped_text == "."
        )

    @staticmethod
    def _with_occurrence_models(events: list[SourceEvent]):
        path_counts = Counter(event.path for event in events)
        return tuple(
            SourceEvent(
                path=event.path,
                location=event.location,
                source_expression=event.source_expression,
                source_site=event.source_site,
                execution_model=event.execution_model,
                occurrence_model=(
                    OccurrenceModel.REPEATED
                    if path_counts[event.path] > 1 and event.occurrence_model == OccurrenceModel.ONCE
                    else event.occurrence_model
                ),
                replacement_kind=event.replacement_kind,
                source_value=event.source_value,
                source_arguments=event.source_arguments,
                state_before=event.state_before,
                condition=event.condition,
                sync_positionals=event.sync_positionals,
            )
            for event in events
        )
