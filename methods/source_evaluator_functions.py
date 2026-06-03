from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator_shared.
from methods.source_evaluator_shared import *  # noqa: F401,F403


class SourceEvaluatorFunctionMixin:
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
