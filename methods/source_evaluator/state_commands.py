from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorStateCommandMixin:
    def _apply_cd(self, node: CdCommand, state: EvaluationState):
        if state.ambiguous_cwd:
            self._ensure_cd_state_can_resolve(node, state)
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
