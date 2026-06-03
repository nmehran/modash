from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorAssignmentMixin:
    def _apply_assignment(self, node: Assignment, state: EvaluationState):
        if node.prefix == "local" and state.local_scopes:
            self._capture_local_variable(node.name, state)

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
        SourceEvaluatorAssignmentMixin._capture_variable_in_scope(name, state.local_scopes[-1], state)

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

