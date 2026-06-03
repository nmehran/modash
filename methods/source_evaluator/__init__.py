from __future__ import annotations

from methods.source_evaluator.shared import *  # noqa: F401,F403

# These mixins keep the evaluator split by concern while preserving the single
# SourceEvaluator public surface. They are imported after the shared constants,
# dataclasses, and signal types above are defined because the mixins reference
# those names during module import.
from methods.source_evaluator.assignments import SourceEvaluatorAssignmentMixin
from methods.source_evaluator.case import SourceEvaluatorCaseMixin
from methods.source_evaluator.commands import SourceEvaluatorCommandMixin
from methods.source_evaluator.conditions import SourceEvaluatorConditionMixin
from methods.source_evaluator.condition_tests import SourceEvaluatorConditionTestMixin
from methods.source_evaluator.functions import SourceEvaluatorFunctionMixin
from methods.source_evaluator.loops import SourceEvaluatorLoopMixin
from methods.source_evaluator.retained_helpers import SourceEvaluatorRetainedHelperMixin
from methods.source_evaluator.sources import SourceEvaluatorSourceSiteMixin
from methods.source_evaluator.state_commands import SourceEvaluatorStateCommandMixin
from methods.source_evaluator.support import SourceEvaluatorSupportMixin


class SourceEvaluator(
    SourceEvaluatorAssignmentMixin,
    SourceEvaluatorCaseMixin,
    SourceEvaluatorCommandMixin,
    SourceEvaluatorConditionMixin,
    SourceEvaluatorConditionTestMixin,
    SourceEvaluatorFunctionMixin,
    SourceEvaluatorLoopMixin,
    SourceEvaluatorRetainedHelperMixin,
    SourceEvaluatorSourceSiteMixin,
    SourceEvaluatorStateCommandMixin,
    SourceEvaluatorSupportMixin,
):
    """Evaluate source effects for the supported IR subset without executing Bash."""

    def __init__(
        self,
        frontend: ParserFrontend | None = None,
        mode: str = "executable",
        source_supplement: SourceSupplement | None = None,
    ):
        self.frontend = frontend or LineParserFrontend()
        self.mode = mode
        self.source_supplement = source_supplement or empty_source_supplement()
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
        self._evaluate_file(entrypoint, state, ())
        self._ensure_retained_helpers_resolved()
        return EvaluationResult(
            events=self._with_occurrence_models(self.events),
            disabled_sources=tuple(self.disabled_sources),
            line_replacements=tuple(self.line_replacements),
            final_state=state.snapshot(),
        )

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
