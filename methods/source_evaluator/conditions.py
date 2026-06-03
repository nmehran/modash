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
