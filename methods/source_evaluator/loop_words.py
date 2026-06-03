from __future__ import annotations

# Extracted SourceEvaluator methods. Shared names come from source_evaluator.shared.
from methods.source_evaluator.shared import *  # noqa: F401,F403


class SourceEvaluatorLoopWordMixin:
    def _resolve_loop_words(self, node: ForLoop, state: EvaluationState):
        if not node.is_exact:
            raise self._unsupported_loop_words(node, "unsupported loop word list")

        raw_words = self._loop_raw_words(node)
        if len(raw_words) != len(node.words):
            raise self._unsupported_loop_words(node, "unsupported loop word list syntax")

        words = []
        for word, raw_word in zip(node.words, raw_words):
            words.extend(self._expand_loop_word(word, raw_word, node, state))

        return words

    @staticmethod
    def _loop_words_need_exact_replacement(node: ForLoop):
        return '$(' in node.words_text or '`' in node.words_text

    def _loop_raw_words(self, node: ForLoop):
        try:
            return tuple(parse_shell_words_preserving_quotes(node.words_text))
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, "unsupported loop word list syntax") from exc

    def _expand_loop_word(self, word: str, raw_word: str, node: ForLoop, state: EvaluationState):
        if self._raw_word_is_single_quoted(raw_word):
            return [word]

        positional_words = self._expand_positional_loop_word(raw_word, state)
        if positional_words is not None:
            return positional_words

        if '$(' in word or '`' in word:
            return self._resolve_command_substitution_loop_word(word, raw_word, node, state)

        array_match = ARRAY_EXPANSION_PATTERN.match(word)
        if array_match:
            array_name = array_match.group(1)
            values = state.arrays.get(array_name)
            if values is None:
                raise self._unsupported_loop_words(node, f"loop word list references unknown array: {array_name}")
            return list(values)

        if (
            has_unquoted_glob(raw_word)
            or has_unquoted_brace_expansion(raw_word)
            or has_unquoted_extglob(raw_word)
        ):
            try:
                glob_word = resolve_variable_references(word, state.resolver_context())
                glob_word = os.path.expandvars(glob_word)
                return self._loop_glob_match_words(
                    expand_glob_word(
                        glob_word,
                        state.resolver_context(),
                        node.text,
                        raw_pattern=raw_word,
                        allow_missing_literal=True,
                    ),
                    state,
                )
            except FailglobExpansionError:
                raise
            except UnsupportedSourceError as exc:
                raise self._unsupported_loop_words(node, str(exc)) from exc

        if has_unquoted_glob(word):
            raise self._unsupported_loop_words(node, "unsupported quoted loop glob")

        for match in SCALAR_REFERENCE_PATTERN.finditer(word):
            variable_name = match.group(1) or match.group(2)
            if variable_name in state.ambiguous_variables:
                raise self._unsupported_loop_words(node, f"loop word list references branch-dependent variable: {variable_name}")
            if variable_name not in state.runtime_variables:
                raise self._unsupported_loop_words(node, f"loop word list references unknown variable: {variable_name}")

        if '$' in word:
            resolved_word = resolve_variable_references(word, state.runtime_context())

            if "$" in resolved_word:
                raise self._unsupported_loop_words(node, "loop word list contains unresolved scalar expansion")

            if self._raw_word_is_unquoted_scalar(raw_word):
                return self._split_scalar_loop_word(resolved_word, node, state)

            if any(char.isspace() for char in resolved_word) and not self._raw_word_is_double_quoted(raw_word):
                raise self._unsupported_loop_words(
                    node,
                    "unsupported loop word list contains whitespace after scalar expansion",
                )
            if has_unquoted_glob(raw_word) or has_unquoted_glob(resolved_word):
                raise self._unsupported_loop_words(
                    node,
                    "unsupported loop word list requires scalar glob expansion",
                )
            return [resolved_word]

        return [word]

    def _expand_positional_loop_word(self, raw_word: str, state: EvaluationState):
        if raw_word in {'"$@"', '"${@}"'}:
            if state.ambiguous_positionals:
                return None
            return list(state.positional_arguments)
        if raw_word in {'"$*"', '"${*}"'}:
            if state.ambiguous_positionals:
                return None
            return [self._joined_positionals(state)]
        return None

    def _resolve_command_substitution_loop_word(self, word: str, raw_word: str, node, state: EvaluationState):
        if '`' in raw_word or '`' in word:
            raise self._unsupported_loop_words(node, "loop word list uses backticks")

        expression = raw_word if '$(' in raw_word else word
        try:
            inner_command = extract_exact_command_substitution(expression)
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc
        if not inner_command:
            raise self._unsupported_loop_words(node, "loop word list is runtime-dynamic")

        if '$(' in inner_command or '`' in inner_command:
            raise self._unsupported_loop_words(node, "loop word list uses nested command substitution")

        try:
            output = self._evaluate_safe_word_list_command(inner_command, node, state)
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc

        if self._raw_word_is_double_quoted(raw_word):
            stripped_output = output.rstrip('\n')
            if not stripped_output:
                return []
            if '\n' in stripped_output:
                raise self._unsupported_loop_words(node, "quoted command substitution produced multiple lines")
            return [stripped_output]

        return self._split_word_list_output(output.rstrip('\n'), node, state)

    def _split_word_list_output(self, output: str, node, state: EvaluationState):
        words = []
        for field in self._split_ifs_fields_for_node(output, node, state):
            if has_unquoted_glob(field):
                try:
                    words.extend(
                        self._loop_glob_match_words(
                            expand_glob_word(
                                field,
                                state.resolver_context(),
                                node.text,
                                raw_pattern=field,
                                allow_missing_literal=True,
                            ),
                            state,
                        )
                    )
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
            else:
                words.append(field)
        return words

    def _evaluate_safe_word_list_command(self, inner_command: str, node, state: EvaluationState):
        if has_unsupported_shell_operator(inner_command):
            raise self._unsupported_loop_words(node, "unsupported command substitution syntax")

        words = parse_shell_words_preserving_quotes(inner_command)
        if not words:
            raise self._unsupported_loop_words(node, "empty command substitution")

        command_name = strip_shell_word_quotes(words[0])
        if command_name == "cat":
            return self._evaluate_cat_word_list(words, node, state)
        if command_name == "find":
            return self._evaluate_find_word_list(words, node, state)
        if command_name == "printf":
            return self._evaluate_printf_word_list(words, node, state)
        if command_name == "sort":
            return self._evaluate_sort_word_list(words, node, state)
        if command_name == "head":
            return self._evaluate_head_word_list(words, node, state)
        if command_name == "grep":
            return self._evaluate_grep_word_list(words, node, state)
        if command_name == "realpath":
            return self._evaluate_realpath_word_list(words, node, state)
        if command_name in {"dirname", "basename"}:
            return self._evaluate_path_transform_word_list(command_name, words, node, state)
        raise self._unsupported_loop_words(node, f"unsupported command substitution: {command_name}")

    def _evaluate_cat_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported cat command substitution")
        output = []
        for raw_path in words[1:]:
            path_word = strip_shell_word_quotes(raw_path)
            if path_word.startswith("-"):
                raise self._unsupported_loop_words(node, "unsupported cat command substitution option")
            path = self._word_list_path(path_word, node, state)
            if not path.is_file():
                raise self._unsupported_loop_words(node, "unsupported cat command substitution path")
            output.append(self._read_text_preserving_newlines(path))
        return ''.join(output)

    def _evaluate_find_word_list(self, words: list[str], node, state: EvaluationState):
        stripped_words = [strip_shell_word_quotes(word) for word in words]
        try:
            parsed_find = SOURCE_RESOLVER.parse_find_command(stripped_words, state.resolver_context())
        except UnsupportedSourceError as exc:
            raise self._unsupported_loop_words(node, str(exc)) from exc
        if not parsed_find:
            raise self._unsupported_loop_words(node, "unsupported find command substitution")

        roots, filters = parsed_find
        root_words = self._find_root_words(stripped_words)
        matches = self._find_word_list_matches(root_words, roots, filters, node, state)
        return self._lines_output(matches)

    @staticmethod
    def _find_root_words(words: list[str]):
        roots = []
        index = 1
        while index < len(words) and not words[index].startswith("-"):
            roots.append(words[index])
            index += 1
        return roots or ["."]

    def _find_word_list_matches(self, root_words: list[str], roots: list[str], filters: dict, node,
                                state: EvaluationState):
        matches = []
        for root_word, root in zip(root_words, roots):
            display_root = self._resolve_exact_runtime_word(root_word, node, state, "loop word list")
            for directory, dirnames, filenames in os.walk(root):
                relative_directory = os.path.relpath(directory, root)
                directory_depth = 0 if relative_directory == os.curdir else len(relative_directory.split(os.sep))
                maxdepth = filters['maxdepth']
                if maxdepth is not None and directory_depth >= maxdepth:
                    dirnames[:] = []

                for filename in filenames:
                    candidate = os.path.join(directory, filename)
                    candidate_depth = directory_depth + 1
                    if candidate_depth < filters['mindepth']:
                        continue
                    if maxdepth is not None and candidate_depth > maxdepth:
                        continue
                    if not os.path.isfile(candidate):
                        continue
                    display_path = self._find_display_path(display_root, root, candidate)
                    if filters['name'] and not any(fnmatch(filename, pattern) for pattern in filters['name']):
                        continue
                    if filters['path'] and not any(fnmatch(display_path, pattern) for pattern in filters['path']):
                        continue

                    matches.append(display_path)
                    if filters.get('quit'):
                        return matches
        return matches

    @staticmethod
    def _find_display_path(display_root: str, resolved_root: str, candidate: str):
        relative = os.path.relpath(candidate, resolved_root)
        if relative == os.curdir:
            return display_root
        return os.path.join(display_root, relative)

    def _evaluate_printf_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported printf command substitution")
        format_word = strip_shell_word_quotes(words[1])
        if format_word not in {"%s\\n", "%s\n"}:
            raise self._unsupported_loop_words(node, "unsupported printf command substitution format")
        values = [
            self._resolve_exact_runtime_word(strip_shell_word_quotes(word), node, state, "loop word list")
            for word in words[2:]
        ]
        return self._lines_output(values)

    def _evaluate_sort_word_list(self, words: list[str], node, state: EvaluationState):
        unique = False
        path_words = []
        for raw_word in words[1:]:
            word = strip_shell_word_quotes(raw_word)
            if word == "-u":
                unique = True
                continue
            if word.startswith("-"):
                raise self._unsupported_loop_words(node, "unsupported sort command substitution option")
            path_words.append(raw_word)
        if not path_words:
            raise self._unsupported_loop_words(node, "unsupported sort command substitution without file operands")

        lines = []
        for _, path in self._word_list_path_pairs(path_words, node, state):
            lines.extend(self._command_output_lines(self._read_text_preserving_newlines(path)))
        sorted_lines = sorted(lines)
        if unique:
            sorted_lines = list(dict.fromkeys(sorted_lines))
        return self._lines_output(sorted_lines)

    def _evaluate_head_word_list(self, words: list[str], node, state: EvaluationState):
        count = None
        index = 1
        if index < len(words):
            first = strip_shell_word_quotes(words[index])
            if first == "-n":
                if index + 1 >= len(words):
                    raise self._unsupported_loop_words(node, "unsupported head command substitution count")
                count = self._head_count(strip_shell_word_quotes(words[index + 1]), node)
                index += 2
            elif re.fullmatch(r'-\d+', first):
                count = self._head_count(first[1:], node)
                index += 1
        if count is None:
            count = 10

        path_words = words[index:]
        if len(path_words) != 1:
            raise self._unsupported_loop_words(node, "unsupported head command substitution operands")
        _, path = self._word_list_path_pairs(path_words, node, state)[0]
        return ''.join(self._read_text_preserving_newlines(path).splitlines(keepends=True)[:count])

    def _evaluate_grep_word_list(self, words: list[str], node, state: EvaluationState):
        literal = False
        extended_regex = False
        list_matches = False
        index = 1
        while index < len(words):
            option = strip_shell_word_quotes(words[index])
            if not option.startswith("-") or option == "-":
                break
            if option == "--":
                index += 1
                break
            for flag in option[1:]:
                if flag == "l":
                    list_matches = True
                elif flag == "F":
                    literal = True
                elif flag == "E":
                    extended_regex = True
                else:
                    raise self._unsupported_loop_words(node, "unsupported grep command substitution option")
            index += 1

        if not list_matches or literal == extended_regex:
            raise self._unsupported_loop_words(node, "unsupported grep command substitution mode")
        if index >= len(words):
            raise self._unsupported_loop_words(node, "unsupported grep command substitution pattern")
        pattern = strip_shell_word_quotes(words[index])
        path_words = words[index + 1:]
        if not path_words:
            raise self._unsupported_loop_words(node, "unsupported grep command substitution without file operands")

        regex = None
        if extended_regex:
            self._ensure_supported_regex_pattern(pattern, node.text, "grep regex")
            regex = re.compile(pattern)

        output = []
        for display_word, path in self._word_list_path_pairs(path_words, node, state):
            content = self._read_text_preserving_newlines(path)
            matched = pattern in content if literal else bool(regex.search(content))
            if matched:
                output.append(display_word)
        return self._lines_output(output)

    def _evaluate_realpath_word_list(self, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, "unsupported realpath command substitution without operands")
        paths = self._word_list_path_pairs(words[1:], node, state)
        return self._lines_output([str(path.resolve()) for _, path in paths])

    def _evaluate_path_transform_word_list(self, command_name: str, words: list[str], node, state: EvaluationState):
        if len(words) < 2:
            raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution without operands")
        index = 1
        option_like_operands = False
        if strip_shell_word_quotes(words[index]) == "--":
            index += 1
            option_like_operands = True
        operand_words = words[index:]
        if not operand_words:
            raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution without operands")
        for word in operand_words:
            if not option_like_operands and strip_shell_word_quotes(word).startswith("-"):
                raise self._unsupported_loop_words(node, f"unsupported {command_name} command substitution option")

        values = [
            self._resolve_exact_runtime_word(strip_shell_word_quotes(word), node, state, "loop word list")
            for word in operand_words
        ]
        if command_name == "basename":
            if len(values) > 2:
                raise self._unsupported_loop_words(node, "unsupported basename command substitution operands")
            return self._lines_output([shell_utility_basename(*values)])

        transform = shell_utility_dirname
        return self._lines_output([transform(value) for value in values])

    def _word_list_path_pairs(self, raw_words: list[str], node, state: EvaluationState):
        pairs = []
        for raw_word in raw_words:
            stripped = strip_shell_word_quotes(raw_word)
            if has_unquoted_glob(raw_word) or has_unquoted_brace_expansion(raw_word) or has_unquoted_extglob(raw_word):
                try:
                    for match in expand_glob_word(stripped, state.resolver_context(), node.text, raw_pattern=raw_word):
                        pairs.append((match.word, Path(match.path)))
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
                continue
            path = self._word_list_path(stripped, node, state)
            if not path.is_file():
                raise self._unsupported_loop_words(node, "unsupported command substitution path")
            pairs.append((self._resolve_exact_runtime_word(stripped, node, state, "loop word list"), path))
        return pairs

    @staticmethod
    def _head_count(value: str, node):
        if not re.fullmatch(r'\d+', value):
            raise SourceEvaluatorLoopWordMixin._unsupported_loop_words(node, "unsupported head command substitution count")
        return int(value)

    @staticmethod
    def _read_text_preserving_newlines(path: Path):
        with path.open("r", newline="") as file:
            return file.read()

    @staticmethod
    def _command_output_lines(content: str):
        if not content:
            return []
        lines = content.split("\n")
        return lines[:-1] if content.endswith("\n") else lines

    @staticmethod
    def _lines_output(lines: list[str]):
        return "\n".join(lines) + ("\n" if lines else "")

    def _word_list_path(self, word: str, node, state: EvaluationState):
        resolved = self._resolve_exact_runtime_word(word, node, state, "loop word list")
        path = Path(resolved)
        if not path.is_absolute():
            path = state.cwd / path
        return path.resolve()

    @staticmethod
    def _resolve_exact_runtime_word(word: str, node, state: EvaluationState, label: str):
        for match in SCALAR_REFERENCE_PATTERN.finditer(word):
            variable_name = match.group(1) or match.group(2)
            if variable_name in state.ambiguous_variables:
                raise unsupported_source_error(
                    str(node.location.path),
                    node.location.line - 1,
                    node.text,
                    node.text,
                    "unsupported.source.loop-word-list",
                    f"unsupported {label} references branch-dependent variable: {variable_name}",
                    "Use exact values before source-aware loop evaluation.",
                )
        resolved = resolve_variable_references(word, state.runtime_context())
        resolved = os.path.expandvars(resolved)
        if "$" in resolved:
            raise unsupported_source_error(
                str(node.location.path),
                node.location.line - 1,
                node.text,
                node.text,
                "unsupported.source.loop-word-list",
                f"unsupported {label} contains unresolved scalar expansion",
                "Use exact values before source-aware loop evaluation.",
            )
        return strip_shell_word_quotes(resolved)

    def _split_scalar_loop_word(self, resolved_word: str, node: ForLoop, state: EvaluationState):
        words = []
        for field in self._split_ifs_fields_for_node(resolved_word, node, state):
            if has_unquoted_glob(field):
                try:
                    words.extend(
                        self._loop_glob_match_words(
                            expand_glob_word(
                                field,
                                state.resolver_context(),
                                node.text,
                                raw_pattern=field,
                                allow_missing_literal=True,
                            ),
                            state,
                        )
                    )
                except UnsupportedSourceError as exc:
                    raise self._unsupported_loop_words(node, str(exc)) from exc
            else:
                words.append(field)
        return words

    @staticmethod
    def _loop_glob_match_words(matches, state: EvaluationState):
        words = []
        for match in matches:
            if not match.exists:
                state.missing_source_words.add(match.word)
            words.append(match.word)
        return words

    def _split_ifs_fields_for_node(self, text: str, node, state: EvaluationState):
        if "IFS" in state.ambiguous_variables:
            if isinstance(node, ArrayAssignment):
                raise self._unsupported_array_assignment(
                    node,
                    "array word splitting references branch-dependent IFS",
                )
            raise self._unsupported_loop_words(node, "loop word splitting references branch-dependent IFS")
        return self._split_ifs_fields(text, state.runtime_variables.get("IFS", DEFAULT_IFS))

    @staticmethod
    def _split_ifs_fields(text: str, ifs: str):
        if ifs == "":
            return [text] if text else []

        ifs_whitespace = ''.join(char for char in ifs if char in " \t\n")
        ifs_other = ''.join(dict.fromkeys(char for char in ifs if char not in " \t\n"))

        if not ifs_other:
            stripped = text.strip(ifs_whitespace)
            if not stripped:
                return []
            return [
                field
                for field in re.split(f"[{re.escape(ifs_whitespace)}]+", stripped)
                if field
            ]

        delimiter_pattern_parts = [f"[{re.escape(ifs_other)}]"]
        if ifs_whitespace:
            delimiter_pattern_parts = [
                f"[{re.escape(ifs_whitespace)}]*[{re.escape(ifs_other)}][{re.escape(ifs_whitespace)}]*",
                f"[{re.escape(ifs_whitespace)}]+",
            ]
            text = text.strip(ifs_whitespace)
        fields = re.split("|".join(delimiter_pattern_parts), text)
        while fields and fields[-1] == "":
            fields.pop()
        return fields

    @staticmethod
    def _raw_word_is_unquoted_scalar(raw_word: str):
        stripped = raw_word.strip()
        return not stripped.startswith(('"', "'")) and bool(SCALAR_WORD_PATTERN.match(stripped))

    @staticmethod
    def _raw_word_is_single_quoted(raw_word: str):
        stripped = raw_word.strip()
        return len(stripped) >= 2 and stripped[0] == stripped[-1] == "'"

    @staticmethod
    def _raw_word_is_double_quoted(raw_word: str):
        stripped = raw_word.strip()
        return len(stripped) >= 2 and stripped[0] == stripped[-1] == '"'

    @staticmethod
    def _unsupported_loop_words(node, message: str):
        if "loop word" not in message:
            message = f"unsupported loop word list: {message}"
        return unsupported_source_error(
            str(node.location.path),
            node.location.line - 1,
            node.text,
            node.text,
            "unsupported.source.loop-word-list",
            message,
            "Use a literal finite list, known scalar variables, exact command substitutions, or an exact ${array[@]} expansion.",
        )

