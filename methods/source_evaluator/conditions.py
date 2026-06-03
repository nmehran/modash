from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorConditionMixin:
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

    def _merge_possible_states(self, target: EvaluationState, possible_states: list[EvaluationState]):
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
        self._merge_state_mapping(
            target.variables,
            [state.variables for state in possible_states],
            target.ambiguous_variables,
            [state.ambiguous_variables for state in possible_states],
            clear_ambiguous=False,
        )
        self._merge_state_mapping(
            target.runtime_variables,
            [state.runtime_variables for state in possible_states],
            target.ambiguous_variables,
            [state.ambiguous_variables for state in possible_states],
            clear_ambiguous=False,
        )
        self._merge_state_mapping(
            target.arrays,
            [state.arrays for state in possible_states],
            target.ambiguous_arrays,
            [state.ambiguous_arrays for state in possible_states],
        )
        self._merge_state_mapping(
            target.associative_arrays,
            [state.associative_arrays for state in possible_states],
            target.ambiguous_arrays,
            [state.ambiguous_arrays for state in possible_states],
            clear_ambiguous=False,
        )
        self._merge_function_state(target, possible_states)

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

    def _merge_function_state(self, target: EvaluationState, possible_states: list[EvaluationState]):
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
                        self._function_signature(function_def),
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
            tuple(SourceEvaluatorConditionMixin._node_signature(node) for node in function_def.body),
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
            return SourceEvaluatorConditionMixin._function_signature(node)
        if isinstance(node, ForLoop):
            return (
                "for",
                node.variable,
                node.words,
                node.words_text,
                node.is_exact,
                tuple(SourceEvaluatorConditionMixin._node_signature(child) for child in node.body),
            )
        if isinstance(node, CStyleForLoop):
            return (
                "c-for",
                node.init,
                node.condition,
                node.update,
                tuple(SourceEvaluatorConditionMixin._node_signature(child) for child in node.body),
            )
        if isinstance(node, WhileLoop):
            return (
                "while",
                node.keyword,
                node.condition,
                node.trailing,
                node.producer,
                node.end_location.line if node.end_location else None,
                tuple(SourceEvaluatorConditionMixin._node_signature(child) for child in node.body),
            )
        if isinstance(node, IfBlock):
            return (
                "if",
                tuple(
                    (
                        branch.keyword,
                        branch.condition,
                        tuple(SourceEvaluatorConditionMixin._node_signature(child) for child in branch.body),
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
                        tuple(SourceEvaluatorConditionMixin._node_signature(child) for child in arm.body),
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
