from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorRetainedHelperMixin:
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

