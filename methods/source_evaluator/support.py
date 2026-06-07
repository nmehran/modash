from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorSupportMixin:
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
            state.clear_current_source_argument_frame_dirty()
        if source_arguments is not None:
            previous_positional_arguments = state.positional_arguments
            previous_ambiguous_positionals = state.ambiguous_positionals
            previous_positionals = self._push_function_positionals(source_arguments, state)
            state.positional_arguments = source_arguments
            state.ambiguous_positionals = False
            state.push_source_argument_frame()
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
                frame_dirty = state.pop_source_argument_frame()
                sync_positionals = frame_dirty
                self._restore_function_positionals(previous_positionals, len(source_arguments), state)
                state.positional_arguments = previous_positional_arguments
                state.ambiguous_positionals = previous_ambiguous_positionals
                if frame_dirty:
                    mark_parent_frame = not state.source_argument_frame_dirty_stack
                    if final_ambiguous_positionals:
                        state.mark_positionals_ambiguous(
                            source_argument_escape=True,
                            mark_source_argument_frame=mark_parent_frame,
                        )
                    else:
                        state.set_positionals(
                            final_positional_arguments,
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
        return quote_shell_words(words, always_quote=True)

    @staticmethod
    def _shell_quote(value: str):
        return shell_single_quote(value)

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
        if SourceEvaluatorSupportMixin._continued_source_free_fragment(command):
            return False
        return bool(
            contains_source_command(command)
            or contains_nested_source_command(command)
            or SourceEvaluatorSupportMixin._raw_command_payload_may_source(command)
            or SourceEvaluatorSupportMixin._raw_command_may_expand_to_source(command)
        )

    @staticmethod
    def _raw_command_contains_literal_source(command: str):
        if command.strip() in {"(", ")", "{", "}"}:
            return False
        if SourceEvaluatorSupportMixin._continued_source_free_fragment(command):
            return False
        return bool(
            contains_source_command(command)
            or contains_nested_source_command(command)
            or SourceEvaluatorSupportMixin._raw_command_payload_may_source(command)
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

    def _expand_array_indexes(self, source_expression: str, node: SourceSite, state: EvaluationState):
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
                key = self._resolve_array_key(index_text, node, state)
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
            index = self._resolve_array_index(index_text, node, state)
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
            prefix_end = invocation.command_start_index if invocation.wrapped else invocation.source_index
            if (
                invocation.command_start_index != 0
                and not SourceEvaluatorSupportMixin._plain_source_prefix_words(
                    invocation.words[:prefix_end]
                )
            ):
                return False
            if not invocation.wrapped:
                return True
            words = [strip_shell_word_quotes(word) for word in parse_shell_words_preserving_quotes(stripped_text)]
            return (
                0 <= invocation.command_start_index < len(words)
                and words[invocation.command_start_index] in {"builtin", "command"}
            )
        return (
            stripped_text.startswith("source ")
            or stripped_text.startswith(". ")
            or stripped_text == "."
        )

    @staticmethod
    def _plain_source_prefix_words(words):
        index = 0
        while index < len(words):
            word = words[index]
            if word == "!":
                index += 1
                continue
            if ASSIGNMENT_WORD_PATTERN.match(word):
                index += 1
                continue
            if re.fullmatch(r"(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|)", word):
                index += 2
                continue
            if re.match(r"^(?:[0-9]+)?(?:>|>>|<|<>|>&|<&|&>|>\|).+", word):
                index += 1
                continue
            return False
        return True

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
