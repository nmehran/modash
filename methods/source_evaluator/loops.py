from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorLoopMixin:
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
