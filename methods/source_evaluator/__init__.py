from __future__ import annotations

from methods.source_evaluator.shared import *  # noqa: F401,F403

# These mixins keep the evaluator split by concern while preserving the single
# SourceEvaluator public surface. They are imported after the shared constants,
# dataclasses, and signal types above are defined because the mixins reference
# those names during module import.
from methods.source_evaluator.commands import SourceEvaluatorCommandMixin
from methods.source_evaluator.conditions import SourceEvaluatorConditionMixin
from methods.source_evaluator.functions import SourceEvaluatorFunctionMixin
from methods.source_evaluator.loops import SourceEvaluatorLoopMixin
from methods.source_evaluator.sources import SourceEvaluatorSourceSiteMixin
from methods.source_evaluator.support import SourceEvaluatorSupportMixin


class SourceEvaluator(
    SourceEvaluatorCommandMixin,
    SourceEvaluatorConditionMixin,
    SourceEvaluatorFunctionMixin,
    SourceEvaluatorLoopMixin,
    SourceEvaluatorSourceSiteMixin,
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

    def _apply_assignment(self, node: Assignment, state: EvaluationState):
        if node.prefix == "local" and state.local_scopes:
            SourceEvaluator._capture_local_variable(node.name, state)

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
        SourceEvaluator._capture_variable_in_scope(name, state.local_scopes[-1], state)

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

    def _apply_function_def(self, node: FunctionDef, state: EvaluationState, stack: tuple[Path, ...]):
        state.functions[node.name] = node
        state.function_variants.pop(node.name, None)
        state.ambiguous_functions.discard(node.name)
        state.last_status = 0
        if self.mode != "executable":
            return

        retained_sites = self._retained_helper_source_sites(node, state, stack)
        if not retained_sites:
            return
        self.retained_helper_source_sites.extend(retained_sites)

    def _apply_retained_helper_signatures(
        self,
        function_def: FunctionDef,
        signatures: tuple[tuple[str, ...], ...],
        state: EvaluationState,
        stack: tuple[Path, ...],
    ):
        uses_first_positional_source = self._retained_helper_uses_first_positional_source(function_def)
        for signature in signatures:
            if not signature:
                raise self._unsupported_retained_helper(
                    function_def.name,
                    function_def.location,
                    function_def.text,
                    f"unsupported retained source helper argument count: {len(signature)}",
                    "Retained helper supplements must provide at least one source path argument.",
                )
            if uses_first_positional_source and len(signature) != 1:
                raise self._unsupported_retained_helper(
                    function_def.name,
                    function_def.location,
                    function_def.text,
                    f"unsupported retained source helper argument count: {len(signature)}",
                    'Retained helpers using source "$1" must provide exactly one source path argument.',
                )

        call_node = RawCommand(
            function_def.location,
            f"{function_def.name} <source-supplement>",
        )
        for signature in dict.fromkeys(signatures):
            retained_state = state.child_shell_copy()
            self._retained_helper_stack.append(function_def.name)
            try:
                self._apply_function_call_variant(
                    function_def,
                    function_def.name,
                    signature,
                    [],
                    call_node,
                    retained_state,
                    stack,
                )
            except UnsupportedSourceError as exc:
                raise with_source_diagnostic(
                    exc,
                    str(function_def.location.path),
                    function_def.location.line - 1,
                    function_def.text,
                    function_def.text,
                    "unsupported.source.retained-helper",
                ) from exc
            finally:
                self._retained_helper_stack.pop()

    def _retained_helper_uses_first_positional_source(self, function_def: FunctionDef):
        first_positional_expressions = {'"$1"', '"${1}"'}

        def collect(nodes):
            for node in nodes:
                if isinstance(node, SourceSite):
                    if node.source_expression.strip() in first_positional_expressions:
                        return True
                    continue
                if isinstance(node, IfBlock):
                    for branch in node.branches:
                        site_spec = self._retained_helper_source_condition_site(node, branch)
                        if site_spec and site_spec[1] in first_positional_expressions:
                            return True
                        if collect(branch.body):
                            return True
                    continue
                if isinstance(node, FunctionDef):
                    continue
                if isinstance(node, ForLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, CStyleForLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, WhileLoop):
                    if collect(node.body):
                        return True
                elif isinstance(node, CaseBlock):
                    for arm in node.arms:
                        if collect(arm.body):
                            return True
            return False

        return collect(function_def.body)

    def _retained_helper_source_sites(self, function_def: FunctionDef, state: EvaluationState,
                                      stack: tuple[Path, ...] = ()):
        site_specs = []

        def collect(nodes):
            for node in nodes:
                if isinstance(node, SourceSite):
                    source_expression = node.source_expression.strip()
                    if self._is_retained_helper_positional_source_expression(source_expression):
                        site_specs.append((
                            node.location,
                            source_expression,
                            self._source_site_text(node),
                            node.text,
                        ))
                    continue

                if isinstance(node, IfBlock):
                    for branch in node.branches:
                        site_spec = self._retained_helper_source_condition_site(node, branch)
                        if site_spec is not None:
                            site_specs.append(site_spec)
                        collect(branch.body)
                    continue

                if isinstance(node, FunctionDef):
                    continue
                if isinstance(node, ForLoop):
                    collect(node.body)
                elif isinstance(node, CStyleForLoop):
                    collect(node.body)
                elif isinstance(node, WhileLoop):
                    collect(node.body)
                elif isinstance(node, CaseBlock):
                    for arm in node.arms:
                        collect(arm.body)

        collect(function_def.body)
        if not site_specs:
            return []

        definition_state = state.child_shell_copy()
        return [
            RetainedHelperSourceSite(
                function_name=function_def.name,
                function_def=function_def,
                definition_state=definition_state,
                stack=stack,
                location=location,
                source_expression=source_expression,
                source_site=source_site,
                fragment=fragment,
            )
            for location, source_expression, source_site, fragment in site_specs
        ]

    def _retained_helper_source_condition_site(
        self,
        node: IfBlock,
        branch,
    ):
        if branch.keyword != "if" or branch.condition is None:
            return None

        match = re.fullmatch(r'(!\s*)?((?:source)|\.)\s+(.+)', branch.condition.strip(), re.S)
        if not match:
            return None

        _, command_name, source_expression = match.groups()
        source_expression = source_expression.strip()
        if not self._is_retained_helper_positional_source_expression(source_expression):
            return None

        column = self._source_condition_column(node, command_name)
        location = SourceLocation(node.location.path, node.location.line, column)
        return (
            location,
            source_expression,
            f"{command_name} {source_expression}".strip(),
            node.text,
        )

    def _ensure_retained_helpers_resolved(self):
        if self.mode != "executable" or not self.retained_helper_source_sites:
            return

        processed_functions = set()
        resolved_sites = self._retained_resolved_site_keys()
        index = 0
        while index < len(self.retained_helper_source_sites):
            site = self.retained_helper_source_sites[index]
            if self._retained_site_key(site) in resolved_sites:
                index += 1
                continue

            signatures = self.source_supplement.function_signatures(site.function_name)
            function_key = (
                site.function_def.location.path.resolve(),
                site.function_def.location.line,
                site.function_name,
            )
            if signatures and function_key not in processed_functions:
                self._apply_retained_helper_signatures(
                    site.function_def,
                    signatures,
                    site.definition_state,
                    site.stack,
                )
                processed_functions.add(function_key)
                resolved_sites = self._retained_resolved_site_keys()
                continue

            raise self._unsupported_retained_helper(
                site.function_name,
                site.location,
                site.fragment,
                f"unsupported retained source helper: {site.function_name}",
                "Provide a source supplement with finite allowed helper arguments.",
            )

    def _retained_resolved_site_keys(self):
        return {
            (
                event.location.path.resolve(),
                event.location.line,
                event.location.column,
                event.source_site,
            )
            for event in self.events
        }

    @staticmethod
    def _retained_site_key(site: RetainedHelperSourceSite):
        return (
            site.location.path.resolve(),
            site.location.line,
            site.location.column,
            site.source_site,
        )

    @staticmethod
    def _unsupported_retained_helper(
        function_name: str,
        location: SourceLocation,
        fragment: str,
        message: str,
        hint: str,
    ):
        return unsupported_source_error(
            str(location.path),
            location.line - 1,
            fragment,
            fragment,
            "unsupported.source.retained-helper",
            message,
            hint,
            details={"supplement_skeleton": supplement_skeleton(function_name=function_name)},
        )

    @staticmethod
    def _apply_cd(node: CdCommand, state: EvaluationState):
        if state.ambiguous_cwd:
            SourceEvaluator._ensure_cd_state_can_resolve(node, state)
        context = state.resolver_context()
        state.cwd = Path(change_directory(node.path_expression, context))
        state.ambiguous_cwd = False
        state.last_status = 0

    def _apply_set(self, node: SetCommand, state: EvaluationState):
        status = 0
        index = 0
        while index < len(node.arguments):
            argument = node.arguments[index]
            if argument == "--":
                break
            if not argument.startswith(("-", "+")):
                break
            if argument in {'-o', '+o'} and index + 1 < len(node.arguments):
                option = node.arguments[index + 1]
                if option not in VALID_SET_OPTIONS:
                    status = 2
                    break
                if argument == '-o':
                    state.shell_options.add(option)
                else:
                    state.shell_options.discard(option)
                index += 2
                continue

            if len(argument) > 1 and argument[0] in {'-', '+'}:
                enabled = argument[0] == '-'
                if argument in {'-o', '+o'}:
                    break
                if any(flag not in VALID_SET_FLAGS for flag in argument[1:]):
                    status = 2
                    break
                for flag in argument[1:]:
                    option = SHELL_OPTION_FLAGS.get(flag)
                    if not option:
                        continue
                    if enabled:
                        state.shell_options.add(option)
                    else:
                        state.shell_options.discard(option)
            index += 1
        if status == 0:
            try:
                positional_arguments = self._set_command_positional_arguments(node, state)
            except UnsupportedSourceError:
                state.mark_positionals_ambiguous(source_argument_escape=True)
            else:
                if positional_arguments is not None:
                    state.set_positionals(positional_arguments, source_argument_escape=True)
        state.last_status = status

    def _set_command_positional_arguments(self, node: SetCommand, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(node.text.strip())
        except UnsupportedSourceError as exc:
            raise self._unsupported_positional_mutation(node, "unsupported set syntax") from exc

        if not words or strip_shell_word_quotes(words[0]) != "set":
            return None

        index = 1
        while index < len(words):
            argument = strip_shell_word_quotes(words[index])
            if argument == "--":
                return self._resolve_positional_assignment_words(words[index + 1:], node, state)
            if not argument.startswith(("-", "+")):
                return self._resolve_positional_assignment_words(words[index:], node, state)
            if argument in {'-o', '+o'}:
                index += 2
                continue
            if argument in {'-', '+'}:
                return None
            index += 1
        return None

    def _resolve_positional_assignment_words(self, words: list[str], node, state: EvaluationState):
        arguments = []
        for word in words:
            stripped = word.strip()
            if stripped in {'"$@"', '"${@}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_positional_mutation(
                        node,
                        "unsupported ambiguous positional assignment expansion",
                    )
                arguments.extend(state.positional_arguments)
                continue
            if stripped in {'"$*"', '"${*}"'}:
                if state.ambiguous_positionals:
                    raise self._unsupported_positional_mutation(
                        node,
                        "unsupported ambiguous positional assignment expansion",
                    )
                arguments.append(self._joined_positionals(state))
                continue
            if re.search(r'(?<!\\)\$(?:\{?[@*]\}?)', stripped):
                raise self._unsupported_positional_mutation(
                    node,
                    "unsupported positional assignment expansion",
                )
            arguments.append(self._resolve_function_exact_word(
                word,
                node,
                state,
                "unsupported.source.positionals",
                "unsupported dynamic positional assignment",
                "unsupported unresolved positional assignment",
                "Positional assignments must resolve exactly for source-aware lowering.",
            ))
        return tuple(arguments)

    @staticmethod
    def _unsupported_positional_mutation(node, message: str):
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.positionals",
            message,
            "Positional mutation must be exact for source-aware lowering.",
        )
