from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorConditionTestMixin:
    def _evaluate_condition(
        self,
        condition: str,
        state: EvaluationState,
        node=None,
        stack: tuple[Path, ...] | None = None,
        branch=None,
    ):
        condition = condition.strip()
        if not condition:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        if node is not None and stack is not None and self._condition_text_may_source(condition):
            source_status = self._evaluate_source_logical_condition(
                condition,
                node,
                state,
                stack,
                branch,
            )
            if source_status is not None:
                return source_status
        if '$(' in condition or '`' in condition:
            raise UnsupportedSourceError(f"unsupported dynamic if condition: {condition}")

        if condition.startswith("((") and condition.endswith("))"):
            return self._evaluate_arithmetic_condition(condition[2:-2].strip(), state, condition)

        try:
            condition_words = self._condition_words(condition)
        except UnsupportedSourceError:
            return self._evaluate_command_condition(condition, state)

        if not condition_words.words:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")
        return self._evaluate_condition_tokens(condition_words, state, condition)

    @staticmethod
    def _source_condition_column(node, command_name: str):
        match = re.search(rf'(?<!\S){re.escape(command_name)}(?=\s|$)', node.text)
        if match:
            return node.location.column + match.start()
        command_index = source_command_index(node.text)
        return node.location.column if command_index is None else node.location.column + command_index

    def _evaluate_source_logical_condition(
        self,
        condition: str,
        node,
        state: EvaluationState,
        stack: tuple[Path, ...],
        branch=None,
    ):
        atoms = self._source_logical_condition_atoms(condition)
        if not any(atom.source_command for atom in atoms):
            if self._raw_command_may_source(condition):
                raise UnsupportedSourceError(f"unsupported source if condition: {condition}")
            return None

        status = self._status_from_last_status(state.last_status)
        for atom in atoms:
            if atom.separator == "&&" and status == "false":
                self._disable_condition_atom_source(node, condition, atom, branch, "&& previous command status")
                state.last_status = 1
                continue
            if atom.separator == "||" and status == "true":
                self._disable_condition_atom_source(node, condition, atom, branch, "|| previous command status")
                state.last_status = 0
                continue

            if status == "unknown" and atom.separator in {"&&", "||"} and atom.source_command is None:
                state.last_status = None
                continue

            if atom.source_command is not None:
                source_node = self._condition_source_node(node, atom, branch)
                self._apply_source_site(source_node, state, stack)
                if atom.negated:
                    state.last_status = self._negated_last_status(state.last_status)
                status = self._status_from_last_status(state.last_status)
                continue

            status = self._evaluate_logical_condition_command_atom(atom, state, condition)
            if atom.negated:
                status = condition_status_not(status)
            state.last_status = self._last_status_from_condition_status(status)

        return status

    def _source_logical_condition_atoms(self, condition: str):
        return source_logical_condition_atoms_from_text(condition)

    def _evaluate_logical_condition_command_atom(
        self,
        atom: ConditionAtom,
        state: EvaluationState,
        condition: str,
    ):
        try:
            return self._evaluate_condition(atom.text, state)
        except UnsupportedSourceError:
            if self._raw_command_may_source(atom.text):
                raise
            return "unknown"

    @staticmethod
    def _status_from_last_status(status: int | None):
        if status is None:
            return "unknown"
        return "true" if status == 0 else "false"

    @staticmethod
    def _last_status_from_condition_status(status: str):
        if status == "true":
            return 0
        if status == "false":
            return 1
        return None

    @staticmethod
    def _negated_last_status(status: int | None):
        if status is None:
            return None
        return 1 if status == 0 else 0

    def _condition_source_node(self, node, atom: ConditionAtom, branch=None):
        location = self._condition_atom_location(node, atom, branch)
        return SourceSite(
            location=location,
            text=f"{atom.source_command} {atom.source_expression}",
            command_name=atom.source_command,
            source_expression=atom.source_expression,
            separator=atom.separator,
            is_control_flow=False,
            is_condition_source=True,
        )

    @staticmethod
    def _condition_atom_location(node, atom: ConditionAtom, branch=None):
        base_location = getattr(branch, "condition_location", None) or node.location
        if atom.source_offset is None:
            return base_location
        return SourceLocation(
            base_location.path,
            base_location.line,
            base_location.column + atom.source_offset,
        )

    def _disable_condition_atom_source(self, node, condition: str, atom: ConditionAtom, branch, reason: str):
        if atom.source_command is None:
            return
        location = self._condition_atom_location(node, atom, branch)
        source_site = f"{atom.source_command} {atom.source_expression}".strip()
        self.disabled_sources.append(DisabledSourceSite(
            location=location,
            source_expression=atom.source_expression.strip(),
            source_site=source_site,
            replacement_kind="source",
            condition=reason,
        ))

    def _disable_branch_condition_sources(self, branch, condition: str):
        if not branch.condition:
            return
        location = branch.condition_location
        if location is None:
            return
        try:
            atoms = self._source_logical_condition_atoms(branch.condition)
        except UnsupportedSourceError:
            atoms = ()
        disabled_direct_source = False
        for atom in atoms:
            if atom.source_command is None:
                continue
            disabled_direct_source = True
            source_site = f"{atom.source_command} {atom.source_expression}".strip()
            self.disabled_sources.append(DisabledSourceSite(
                location=self._condition_atom_location(None, atom, branch),
                source_expression=atom.source_expression.strip(),
                source_site=source_site,
                replacement_kind="source",
                condition=condition,
            ))
        if not disabled_direct_source and self._raw_command_contains_literal_source(branch.condition):
            self.disabled_sources.append(DisabledSourceSite(
                location=location,
                source_expression=branch.condition.strip(),
                source_site=branch.condition.strip(),
                replacement_kind="command",
                condition=condition,
            ))

    def _condition_text_may_source(self, condition: str):
        return bool(
            re.search(r'(^|[\s!(&|])(?:source|\.)\s+', condition)
            or self._raw_command_may_source(condition)
        )

    def _condition_has_source_atom(self, condition: str):
        try:
            return any(atom.source_command for atom in self._source_logical_condition_atoms(condition))
        except UnsupportedSourceError:
            return self._condition_text_may_source(condition)

    def _evaluate_condition_tokens(self, condition_words: ConditionWords, state: EvaluationState, condition: str):
        result, index = self._parse_condition_or(condition_words, 0, state, condition)
        if index != len(condition_words.words):
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")
        return result

    def _parse_condition_or(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        left, index = self._parse_condition_and(condition_words, index, state, condition)
        while index < len(words) and words[index] == "||":
            right, index = self._parse_condition_and(condition_words, index + 1, state, condition)
            left = condition_status_or(left, right)
        return left, index

    def _parse_condition_and(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        left, index = self._parse_condition_not(condition_words, index, state, condition)
        while index < len(words) and words[index] == "&&":
            right, index = self._parse_condition_not(condition_words, index + 1, state, condition)
            left = condition_status_and(left, right)
        return left, index

    def _parse_condition_not(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        if index >= len(words):
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")
        if words[index] == "!":
            result, next_index = self._parse_condition_not(condition_words, index + 1, state, condition)
            return condition_status_not(result), next_index
        if words[index] == "(":
            result, next_index = self._parse_condition_or(condition_words, index + 1, state, condition)
            if next_index >= len(words) or words[next_index] != ")":
                raise UnsupportedSourceError(f"unsupported if condition grouping: {condition}")
            return result, next_index + 1
        return self._parse_condition_atom(condition_words, index, state, condition)

    def _parse_condition_atom(self, condition_words: ConditionWords, index: int, state: EvaluationState, condition: str):
        words = condition_words.words
        if index >= len(words) or words[index] in {")", "&&", "||"}:
            raise UnsupportedSourceError(f"unsupported if condition: {condition}")

        if words[index] in CONDITION_UNARY_FILE_OPERATORS | CONDITION_UNARY_STRING_OPERATORS:
            if index + 1 >= len(words):
                raise UnsupportedSourceError(f"unsupported if condition: {condition}")
            return self._evaluate_condition_unary(
                words[index],
                words[index + 1],
                state,
                condition,
                condition_words.kind,
            ), index + 2

        if index + 1 < len(words) and words[index + 1] in CONDITION_BINARY_OPERATORS:
            if index + 2 >= len(words):
                raise UnsupportedSourceError(f"unsupported if condition: {condition}")
            return self._evaluate_condition_binary(
                words[index],
                words[index + 1],
                words[index + 2],
                state,
                condition,
                condition_words.kind,
            ), index + 3

        value = self._condition_value(words[index], state)
        if value is None:
            return "unknown", index + 1
        return ("true" if bool(value) else "false"), index + 1

    def _evaluate_condition_unary(
        self,
        operator: str,
        operand: str,
        state: EvaluationState,
        condition: str,
        condition_kind: str,
    ):
        if operator in CONDITION_UNARY_FILE_OPERATORS:
            if condition_kind != "double-bracket" and (
                has_unquoted_glob(operand) or has_unquoted_extglob(operand)
            ):
                return self._evaluate_condition_glob_unary(operator, operand, state, condition)
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.exists()
            if operator == "-f":
                result = path.is_file()
            elif operator == "-d":
                result = path.is_dir()
            elif operator == "-r":
                result = os.access(path, os.R_OK)
            return "true" if result else "false"

        value = self._condition_value(operand, state)
        if value is None:
            return "unknown"
        result = bool(value) if operator == "-n" else not bool(value)
        return "true" if result else "false"

    def _evaluate_condition_glob_unary(
        self,
        operator: str,
        operand: str,
        state: EvaluationState,
        condition: str,
    ):
        if operator not in {"-f", "-r"}:
            raise UnsupportedSourceError(f"unsupported glob if condition: {condition}")
        if state.ambiguous_cwd or state.ambiguous_shell_options or state.ambiguous_glob_options:
            raise UnsupportedSourceError(f"unsupported branch-dependent glob if condition: {condition}")
        if "GLOBIGNORE" in state.ambiguous_variables:
            raise UnsupportedSourceError(f"unsupported GLOBIGNORE glob if condition: {condition}")
        if "noglob" in state.shell_options:
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
            return "true" if result else "false"
        if has_unquoted_brace_expansion(operand):
            raise UnsupportedSourceError(f"unsupported brace glob if condition: {condition}")

        resolved = self._condition_value(operand, state)
        if resolved is None:
            return "unknown"

        try:
            matches = expand_glob_word(resolved, state.resolver_context(), condition, raw_pattern=operand)
        except UnsupportedSourceError as exc:
            if "unsupported unmatched source glob" not in str(exc) and "unsupported GLOBIGNORE source pattern" not in str(exc):
                raise
            if "nullglob" in state.glob_options:
                return "true"
            path = self._condition_path(operand, state, condition)
            if path is None:
                return "unknown"
            result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
            return "true" if result else "false"

        if not matches:
            return "true"
        if len(matches) != 1:
            raise UnsupportedSourceError(f"unsupported multi-match glob if condition: {condition}")

        path = Path(matches[0].path).resolve()
        result = path.is_file() if operator == "-f" else os.access(path, os.R_OK)
        return "true" if result else "false"

    def _evaluate_condition_binary(
        self,
        left_token: str,
        operator: str,
        right_token: str,
        state: EvaluationState,
        condition: str,
        condition_kind: str,
    ):
        if operator in CONDITION_STRING_OPERATORS:
            is_double_bracket = condition_kind == "double-bracket"
            if not is_double_bracket and (has_unquoted_glob(left_token) or has_unquoted_glob(right_token)):
                raise UnsupportedSourceError(f"unsupported glob if condition: {condition}")
            left = self._condition_value(left_token, state)
            right = self._condition_value(right_token, state)
            if left is None or right is None:
                return "unknown"
            if is_double_bracket and self._condition_rhs_is_pattern(right_token, right):
                try:
                    result = shell_pattern_matches(
                        right,
                        left,
                        extglob=True,
                        nocase="nocasematch" in state.shell_options,
                    )
                except UnsupportedPatternError as exc:
                    raise UnsupportedSourceError(f"unsupported pattern if condition: {condition} ({exc})") from exc
            else:
                if is_double_bracket and "nocasematch" in state.shell_options:
                    result = left.lower() == right.lower()
                else:
                    result = left == right
            if operator == "!=":
                result = not result
            return "true" if result else "false"

        if operator in CONDITION_INTEGER_OPERATORS:
            left = self._condition_integer_value(left_token, state, condition)
            right = self._condition_integer_value(right_token, state, condition)
            if left is None or right is None:
                return "unknown"
            comparisons = {
                "-eq": left == right,
                "-ne": left != right,
                "-gt": left > right,
                "-ge": left >= right,
                "-lt": left < right,
                "-le": left <= right,
            }
            return "true" if comparisons[operator] else "false"

        if operator == "=~":
            if not condition.strip().startswith("[["):
                raise UnsupportedSourceError(f"unsupported regex if condition: {condition}")
            return self._evaluate_condition_regex(left_token, right_token, state, condition)

        raise UnsupportedSourceError(f"unsupported if condition: {condition}")

    def _condition_rhs_is_pattern(self, raw_token: str, resolved_value: str):
        if has_unquoted_glob(raw_token) or has_unquoted_extglob(raw_token):
            return True
        if self._raw_word_is_single_quoted(raw_token) or self._raw_word_is_double_quoted(raw_token):
            return False
        return has_unquoted_glob(resolved_value) or has_unquoted_extglob(resolved_value)

    def _evaluate_condition_regex(self, left_token: str, right_token: str, state: EvaluationState, condition: str):
        left = self._condition_value(left_token, state)
        pattern = self._condition_value(right_token, state)
        if left is None or pattern is None:
            return "unknown"
        if self._raw_word_is_single_quoted(right_token) or self._raw_word_is_double_quoted(right_token):
            pattern = re.escape(pattern)
        self._ensure_supported_regex_pattern(pattern, condition)
        try:
            return "true" if re.search(pattern, left) else "false"
        except re.error as exc:
            raise UnsupportedSourceError(f"unsupported regex if condition: {condition} ({exc})") from exc

    def _evaluate_command_condition(self, condition: str, state: EvaluationState):
        try:
            words = parse_shell_words_preserving_quotes(condition)
        except UnsupportedSourceError as exc:
            raise UnsupportedSourceError(f"unsupported if condition syntax: {condition}") from exc
        if not words:
            raise UnsupportedSourceError(f"unsupported empty if condition: {condition}")

        command_name = strip_shell_word_quotes(words[0])
        if command_name in {":", "true"} and len(words) == 1:
            return "true"
        if command_name == "false" and len(words) == 1:
            return "false"
        if command_name == "grep":
            return self._evaluate_grep_condition(words, state, condition)
        if command_name == "shopt":
            return self._evaluate_shopt_query_condition(words, state, condition)
        raise UnsupportedSourceError(f"unsupported command if condition: {condition}")

    def _evaluate_shopt_query_condition(self, words: list[str], state: EvaluationState, condition: str):
        if len(words) < 3 or strip_shell_word_quotes(words[1]) != "-q":
            raise UnsupportedSourceError(f"unsupported shopt if condition: {condition}")
        if state.ambiguous_shell_options or state.ambiguous_glob_options:
            return "unknown"

        for option_word in words[2:]:
            option = strip_shell_word_quotes(option_word)
            if option not in KNOWN_SHOPT_OPTIONS:
                raise UnsupportedSourceError(f"unsupported shopt option in if condition: {condition}")
            enabled = (
                option in state.glob_options
                if option in GLOB_SHOPT_OPTIONS
                else option in state.shell_options
            )
            if not enabled:
                return "false"
        return "true"

    def _evaluate_grep_condition(self, words: list[str], state: EvaluationState, condition: str):
        options = set()
        index = 1
        while index < len(words):
            option = strip_shell_word_quotes(words[index])
            if option == "--":
                index += 1
                break
            if not option.startswith("-") or option == "-":
                break
            for flag in option[1:]:
                if flag not in {"q", "E", "F", "s"}:
                    raise UnsupportedSourceError(f"unsupported grep option in if condition: {condition}")
                options.add(flag)
            index += 1

        if "q" not in options:
            raise UnsupportedSourceError(f"unsupported grep if condition without -q: {condition}")
        if {"E", "F"} <= options:
            raise UnsupportedSourceError(f"unsupported grep if condition with both -E and -F: {condition}")
        if len(words) - index != 2:
            raise UnsupportedSourceError(f"unsupported grep if condition arguments: {condition}")

        pattern = self._condition_value(words[index], state)
        path = self._condition_path(words[index + 1], state, condition)
        if pattern is None or path is None:
            return "unknown"
        if not path.is_file():
            return "false"

        if "F" in options:
            matched = self._file_contains_literal(path, pattern)
        elif "E" in options:
            self._ensure_supported_regex_pattern(pattern, condition, "grep regex")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise UnsupportedSourceError(f"unsupported grep regex in if condition: {condition} ({exc})") from exc
            matched = self._file_matches_regex(path, regex)
        else:
            if GREP_LITERAL_META_PATTERN.search(pattern):
                raise UnsupportedSourceError(f"unsupported basic-regex grep if condition: {condition}")
            matched = self._file_contains_literal(path, pattern)

        return "true" if matched else "false"

    @staticmethod
    def _ensure_supported_regex_pattern(pattern: str, condition: str, label: str = "regex"):
        if POSIX_CLASS_PATTERN.search(pattern):
            raise UnsupportedSourceError(f"unsupported POSIX {label} in if condition: {condition}")
        if PYTHON_ONLY_REGEX_PATTERN.search(pattern) or LAZY_REGEX_QUANTIFIER_PATTERN.search(pattern):
            raise UnsupportedSourceError(f"unsupported Python-specific {label} in if condition: {condition}")

    @staticmethod
    def _file_contains_literal(path: Path, needle: str):
        with path.open('r', errors='ignore') as file:
            return any(needle in line for line in file)

    @staticmethod
    def _file_matches_regex(path: Path, regex):
        with path.open('r', errors='ignore') as file:
            return any(regex.search(line) for line in file)

    def _evaluate_arithmetic_condition(self, expression: str, state: EvaluationState, condition: str):
        if not expression:
            raise UnsupportedSourceError(f"unsupported empty arithmetic if condition: {condition}")
        value = self._evaluate_arithmetic_expression(expression, state, condition)
        if value is None:
            return "unknown"
        return "true" if bool(value) else "false"

    def _evaluate_arithmetic_expression(self, expression: str, state: EvaluationState, condition: str):
        normalized = self._normalize_arithmetic_expression(expression)
        try:
            tree = ast.parse(normalized, mode="eval")
        except SyntaxError as exc:
            raise UnsupportedSourceError(f"unsupported arithmetic if condition: {condition}") from exc
        return self._evaluate_arithmetic_ast(tree.body, state, condition)

    @staticmethod
    def _normalize_arithmetic_expression(expression: str):
        normalized = re.sub(r'\$\{([a-zA-Z_]\w*)\}', r'\1', expression)
        normalized = re.sub(r'\$([a-zA-Z_]\w*)', r'\1', normalized)
        normalized = normalized.replace("&&", " and ")
        normalized = normalized.replace("||", " or ")
        normalized = re.sub(r'(?<![=!<>])!(?!=)', ' not ', normalized)
        normalized = re.sub(r'(?<!/)/(?!/)', '//', normalized)
        return normalized

    def _evaluate_arithmetic_ast(self, node, state: EvaluationState, condition: str):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
            return int(node.value)

        if isinstance(node, ast.Name):
            return self._arithmetic_name_value(node.id, state, condition)

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not, ast.Invert)):
            operand = self._evaluate_arithmetic_ast(node.operand, state, condition)
            if operand is None:
                return None
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Invert):
                return ~operand
            return 0 if bool(operand) else 1

        if isinstance(node, ast.BinOp) and isinstance(
            node.op,
            (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.LShift, ast.RShift, ast.BitAnd, ast.BitOr, ast.BitXor),
        ):
            left = self._evaluate_arithmetic_ast(node.left, state, condition)
            right = self._evaluate_arithmetic_ast(node.right, state, condition)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.LShift, ast.RShift)) and right < 0:
                raise UnsupportedSourceError(f"unsupported negative arithmetic shift in if condition: {condition}")
            if isinstance(node.op, ast.LShift):
                return left << right
            if isinstance(node.op, ast.RShift):
                return left >> right
            if isinstance(node.op, ast.BitAnd):
                return left & right
            if isinstance(node.op, ast.BitOr):
                return left | right
            if isinstance(node.op, ast.BitXor):
                return left ^ right
            if right == 0:
                raise UnsupportedSourceError(f"unsupported arithmetic division by zero in if condition: {condition}")
            if isinstance(node.op, ast.FloorDiv):
                return int(left / right)
            return left % right

        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [self._evaluate_arithmetic_ast(value, state, condition) for value in node.values]
            if any(value is None for value in values):
                return None
            if isinstance(node.op, ast.And):
                return int(all(bool(value) for value in values))
            return int(any(bool(value) for value in values))

        if isinstance(node, ast.Compare):
            left = self._evaluate_arithmetic_ast(node.left, state, condition)
            if left is None:
                return None
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._evaluate_arithmetic_ast(comparator, state, condition)
                if right is None:
                    return None
                if not self._arithmetic_compare(left, operator, right, condition):
                    return 0
                left = right
            return 1

        raise UnsupportedSourceError(f"unsupported arithmetic if condition: {condition}")

    @staticmethod
    def _arithmetic_compare(left: int, operator, right: int, condition: str):
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        if isinstance(operator, ast.GtE):
            return left >= right
        raise UnsupportedSourceError(f"unsupported arithmetic comparison in if condition: {condition}")

    @staticmethod
    def _arithmetic_name_value(name: str, state: EvaluationState, condition: str):
        if name in state.ambiguous_variables:
            return None
        raw_value = state.runtime_variables.get(name, os.environ.get(name, "0"))
        raw_value = strip_matching_quotes(str(raw_value))
        if not re.fullmatch(r'[+-]?\d+', raw_value):
            raise UnsupportedSourceError(f"unsupported non-integer arithmetic variable in if condition: {condition}")
        return int(raw_value)

    @staticmethod
    def _condition_words(condition: str):
        stripped = condition.strip()
        if stripped.startswith("[[") and stripped.endswith("]]"):
            stripped = stripped[2:-2].strip()
            kind = "double-bracket"
        elif stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1].strip()
            kind = "single-bracket"
        elif stripped.startswith("test "):
            stripped = stripped[5:].strip()
            kind = "test"
        else:
            raise UnsupportedSourceError(f"unsupported if condition syntax: {condition}")
        return ConditionWords(tuple(parse_shell_words_preserving_quotes(stripped)), kind)

    @staticmethod
    def _condition_value(value: str, state: EvaluationState):
        if strip_matching_quotes(value.strip()) == "$#":
            return None if state.ambiguous_positionals else str(len(state.positional_arguments))

        variable_names = [match.group(1) or match.group(2) for match in SCALAR_REFERENCE_PATTERN.finditer(value)]
        if any(name in state.ambiguous_variables for name in variable_names):
            return None
        if any(name not in state.runtime_variables and f"${name}" in value for name in variable_names):
            return None

        resolved = resolve_variable_references(value, state.runtime_context())
        if SCALAR_REFERENCE_PATTERN.search(resolved):
            return None
        resolved = os.path.expandvars(resolved)
        return strip_matching_quotes(resolved)

    def _condition_integer_value(self, value: str, state: EvaluationState, condition: str):
        resolved = self._condition_value(value, state)
        if resolved is None:
            return None
        if not re.fullmatch(r'[+-]?\d+', resolved):
            raise UnsupportedSourceError(f"unsupported integer if condition: {condition}")
        return int(resolved)

    def _condition_path(self, value: str, state: EvaluationState, condition: str):
        if state.ambiguous_cwd:
            raise UnsupportedSourceError(f"unsupported branch-dependent cwd in if condition: {condition}")
        resolved = self._condition_value(value, state)
        if resolved is None:
            return None
        resolved = resolve_shell_path_commands(resolved, str(state.cwd))
        path = Path(resolved)
        if not path.is_absolute():
            path = state.cwd / path
        return path.resolve()
