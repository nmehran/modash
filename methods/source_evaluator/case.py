from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorCaseMixin:
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

