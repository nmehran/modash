from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorLoopReadMixin:
    def _read_loop_words(self, node: WhileLoop, state: EvaluationState):
        if node.keyword != "while":
            return None
        if not node.trailing.startswith("<") and not node.producer:
            return None

        read_condition, include_incomplete, nonempty_word = self._split_read_loop_nonempty_tail(node.condition)
        condition_words = parse_shell_words_preserving_quotes(read_condition)
        if not condition_words:
            return None

        read_ifs = DEFAULT_IFS
        index = 0
        if condition_words[index].startswith("IFS="):
            read_ifs = self._read_loop_ifs_value(condition_words[index])
            index += 1
        if index >= len(condition_words) or strip_shell_word_quotes(condition_words[index]) != "read":
            return None
        index += 1

        while index < len(condition_words) and condition_words[index].startswith("-"):
            option = strip_shell_word_quotes(condition_words[index])
            if option == "-r":
                index += 1
                continue
            raise self._unsupported_loop_condition(node, f"unsupported read option: {option}")

        if index != len(condition_words) - 1:
            raise self._unsupported_loop_condition(node, "unsupported read loop condition")

        variable = strip_shell_word_quotes(condition_words[index])
        if not re.fullmatch(r'[a-zA-Z_]\w*', variable):
            raise self._unsupported_loop_condition(node, "unsupported read loop variable")
        if nonempty_word is not None and self._read_loop_nonempty_variable(nonempty_word) != variable:
            raise self._unsupported_loop_condition(node, "unsupported read loop nonempty guard")

        values = []
        child_shell, lines = self._read_loop_input_lines(node, state, include_incomplete)
        for line in lines:
            value = self._read_loop_value(line, read_ifs)
            values.append(value)
        return ReadLoopWords(variable, tuple(values), child_shell=child_shell)

    def _read_loop_input_lines(self, node: WhileLoop, state: EvaluationState, include_incomplete: bool):
        if node.producer:
            output = self._evaluate_safe_word_list_command(node.producer, node, state)
            return (
                self._producer_read_loop_uses_child_shell(node, state),
                self._read_loop_lines_from_content(output, include_incomplete),
            )

        process_substitution = self._read_loop_process_substitution(node.trailing)
        if process_substitution is not None:
            output = self._evaluate_safe_word_list_command(process_substitution, node, state)
            return False, self._read_loop_lines_from_content(output, include_incomplete)

        trailing_words = parse_shell_words_preserving_quotes(node.trailing)
        if len(trailing_words) != 2 or trailing_words[0] != "<":
            raise self._unsupported_loop_condition(node, "unsupported read loop redirection")

        input_path = self._word_list_path(strip_shell_word_quotes(trailing_words[1]), node, state)
        if not input_path.is_file():
            raise self._unsupported_loop_condition(node, "unsupported read loop input path")
        return False, self._read_loop_lines(input_path, include_incomplete)

    @staticmethod
    def _read_loop_process_substitution(trailing: str):
        match = re.fullmatch(r'<\s*<\((.*)\)\s*', trailing)
        return match.group(1).strip() if match else None

    def _producer_read_loop_uses_child_shell(self, node: WhileLoop, state: EvaluationState):
        if state.ambiguous_shell_options:
            raise self._unsupported_loop_condition(node, "unsupported read loop producer with ambiguous shell options")
        return "lastpipe" not in state.shell_options or "monitor" in state.shell_options

    @staticmethod
    def _split_read_loop_nonempty_tail(condition: str):
        match = re.match(
            r'^(.*?)\s*\|\|\s*(?:(?:\[\[\s+-n\s+(.+?)\s*\]\])|(?:\[\s+-n\s+(.+?)\s*\]))\s*$',
            condition,
        )
        if not match:
            return condition, False, None
        return match.group(1).strip(), True, (match.group(2) or match.group(3)).strip()

    @staticmethod
    def _read_loop_nonempty_variable(word: str):
        stripped = strip_shell_word_quotes(word.strip())
        match = re.fullmatch(r'\$(?:\{([a-zA-Z_]\w*)\}|([a-zA-Z_]\w*))', stripped)
        return (match.group(1) or match.group(2)) if match else None

    def _read_loop_ifs_value(self, word: str):
        _, value = word.split("=", 1)
        decoded = self._decode_ansi_c_quoted_word(value)
        return decoded if decoded != value else strip_shell_word_quotes(value)

    @staticmethod
    def _read_loop_lines(path: Path, include_incomplete: bool):
        with path.open("r", newline="") as file:
            content = file.read()
        return SourceEvaluatorLoopReadMixin._read_loop_lines_from_content(content, include_incomplete)

    @staticmethod
    def _read_loop_lines_from_content(content: str, include_incomplete: bool):
        if not content:
            return []
        lines = content.split("\n")
        if content.endswith("\n"):
            return lines[:-1]
        return lines if include_incomplete else lines[:-1]

    @staticmethod
    def _read_loop_value(line: str, read_ifs: str):
        if read_ifs == "":
            return line
        ifs_whitespace = ''.join(char for char in read_ifs if char in " \t\n")
        return line.strip(ifs_whitespace) if ifs_whitespace else line

