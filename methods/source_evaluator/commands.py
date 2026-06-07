from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorCommandMixin:
    def _apply_raw_command(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        if self._raw_command_skipped_by_known_status(node, state):
            return

        for handler in self._pre_source_command_handlers():
            if handler(node, state, stack):
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
                source_arguments=resolved_source.source_arguments,
                source_argument_words=resolved_source.source_argument_words,
            )
            if execution_model == ExecutionModel.CHILD_SHELL:
                child_state = state.child_shell_copy()
                source_status = self._evaluate_sourced_file(
                    source_path,
                    child_state,
                    stack,
                    source_arguments=resolved_source.source_arguments,
                )
            else:
                source_status = self._evaluate_sourced_file(
                    source_path,
                    state,
                    stack,
                    source_arguments=resolved_source.source_arguments,
                )
        state.last_status = source_status

    def _pre_source_command_handlers(self):
        return (
            self._apply_local_declaration,
            self._apply_function_call,
            self._apply_loop_control,
            self._apply_return_command,
            self._apply_shift_command,
            self._apply_array_population_command,
            self._apply_arithmetic_command,
            self._apply_exact_non_source_eval,
            self._apply_child_shell_sources,
        )

    def _apply_local_declaration(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
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
                self._capture_local_variable(name, state)
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
                self._capture_local_variable(name, state)
                state.variables.pop(name, None)
                state.runtime_variables.pop(name, None)
                state.ambiguous_variables.discard(name)
        state.last_status = 0
        return True

    def _apply_return_command(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        if not self._raw_function_return_command(node):
            return False
        if state.function_body_depth > 0:
            raise FunctionReturnSignal(self._function_return_status(node, state), node)
        if state.source_depth > 0:
            raise SourceReturnSignal(self._function_return_status(node, state), node)
        return False

    def _apply_shift_command(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
        if not self._raw_function_shift_command(node):
            return False
        self._apply_function_shift(node, state)
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
        for body, body_start, context_index in subshell_bodies(command):
            commands.extend(cls._direct_source_commands_in_body(
                body,
                body_start,
                ("subshell", context_index),
            ))
        return tuple(commands)

    @classmethod
    def _command_substitution_source_commands(cls, command: str):
        commands = []
        for body, body_start, context_index in command_substitution_bodies(command):
            commands.extend(cls._direct_source_commands_in_body(
                body,
                body_start,
                ("command-substitution", context_index),
            ))
        return tuple(commands)

    @classmethod
    def _process_substitution_source_commands(cls, command: str):
        commands = []
        for body, body_start, context_index in process_substitution_bodies(command):
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
        segments = top_level_pipeline_segments(command)
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


    def _apply_exact_non_source_eval(self, node: RawCommand, state: EvaluationState, stack: tuple[Path, ...]):
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
