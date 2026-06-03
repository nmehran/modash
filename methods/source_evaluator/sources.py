from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorSourceSiteMixin:
    def _apply_source_site(self, node: SourceSite, state: EvaluationState, stack: tuple[Path, ...]):
        if self._source_site_skipped_by_known_status(node, state):
            return

        if len(top_level_pipeline_segments(node.text)) > 1:
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
