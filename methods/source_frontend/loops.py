from __future__ import annotations

# Extracted LineParserFrontend methods. Shared parser names come from .shared.
from .shared import *  # noqa: F401,F403


class SourceFrontendLoopMixin:
    def _parse_c_style_for_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                                line_index: int):
        match = C_FOR_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            init, condition, update, inline_body = match.groups()
        else:
            match = C_FOR_HEADER_PATTERN.match(code_line)
            if not match or line_index + 1 >= len(lines):
                return None, line_index + 1

            do_line_index = line_index + 1
            do_code_line = remove_comments(
                lines[do_line_index],
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )
            do_match = DO_LINE_PATTERN.match(do_code_line)
            if not do_match:
                return None, line_index + 1

            init, condition, update = match.groups()
            inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        next_line_index = body_start_index

        if inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                if stripped_body_line == "done" and control_depth == 0:
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        column = self._command_column(code_line, "for")
        return CStyleForLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            init=init.strip(),
            condition=condition.strip(),
            update=update.strip(),
            body=self._parse_loop_body(script_path, body_lines),
        ), next_line_index

    def _parse_for_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str], line_index: int):
        match = FOR_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            variable, words_text, inline_body = match.groups()
        else:
            match = FOR_HEADER_PATTERN.match(code_line)
            if not match or line_index + 1 >= len(lines):
                return None, line_index + 1

            do_line_index = line_index + 1
            do_code_line = remove_comments(
                lines[do_line_index],
                ['#'],
                exclusion_patterns=[r'\#\!.*'],
                escape_exclusions=False,
            )
            do_match = DO_LINE_PATTERN.match(do_code_line)
            if not do_match:
                return None, line_index + 1

            variable, words_text = match.groups()
            inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        next_line_index = body_start_index
        end_line_number = do_line_index + 1
        trailing = ""

        if inline_body is not None and inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            trailing = (done_match.group(2) or "").strip()
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                if stripped_body_line == "done" and control_depth == 0:
                    end_line_number = body_line_number
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        loop_words, is_exact = self._parse_loop_words(words_text)
        body = self._parse_loop_body(script_path, body_lines)
        column = self._command_column(code_line, "for")

        return ForLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            variable=variable,
            words=loop_words,
            body=body,
            words_text=words_text.strip(),
            is_exact=is_exact,
            end_location=SourceLocation(script_path, end_line_number, 1),
            trailing=trailing,
        ), next_line_index

    def _parse_while_loop(self, script_path: Path, line_number: int, code_line: str, lines: list[str],
                          line_index: int):
        producer = ""
        match = PIPELINE_WHILE_LOOP_PATTERN.match(code_line)
        do_line_index = line_index
        if match:
            producer, condition, inline_body = match.groups()
            keyword = "while"
        else:
            match = WHILE_LOOP_PATTERN.match(code_line)
            if match:
                keyword, condition, inline_body = match.groups()
            else:
                match = PIPELINE_WHILE_HEADER_PATTERN.match(code_line)
                if match:
                    producer, condition = match.groups()
                    keyword = "while"
                else:
                    match = WHILE_HEADER_PATTERN.match(code_line)
                    if not match or line_index + 1 >= len(lines):
                        return None, line_index + 1
                    keyword, condition = match.groups()

                if line_index + 1 >= len(lines):
                    return None, line_index + 1

                do_line_index = line_index + 1
                do_code_line = remove_comments(
                    lines[do_line_index],
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                do_match = DO_LINE_PATTERN.match(do_code_line)
                if not do_match:
                    return None, line_index + 1
                inline_body = ""

        if inline_body is None:
            inline_body = ""

        if inline_body.strip() == "":
            body_start_index = do_line_index + 1
        else:
            body_start_index = do_line_index

        if body_start_index <= line_index:
            body_start_index = line_index + 1

        body_lines = []
        trailing = ""
        end_line_number = line_number
        next_line_index = body_start_index

        if inline_body is not None and inline_body.strip():
            done_match = INLINE_DONE_PATTERN.match(inline_body.strip())
            if not done_match:
                return None, line_index + 1
            body_lines.append((do_line_index + 1, done_match.group(1).strip()))
            trailing = (done_match.group(2) or "").strip()
            end_line_number = do_line_index + 1
            next_line_index = do_line_index + 1
        else:
            body_index = body_start_index
            active_heredocs = []
            control_depth = 0
            while body_index < len(lines):
                body_line_number = body_index + 1
                body_line = lines[body_index]

                if active_heredocs:
                    body_lines.append((body_line_number, body_line))
                    if is_heredoc_end(body_line, active_heredocs[0]):
                        active_heredocs.pop(0)
                    body_index += 1
                    continue

                body_code_line = remove_comments(
                    body_line,
                    ['#'],
                    exclusion_patterns=[r'\#\!.*'],
                    escape_exclusions=False,
                )
                stripped_body_line = body_code_line.strip()
                done_match = re.match(r'^done(?:\s+(.*))?$', stripped_body_line)
                if done_match and control_depth == 0:
                    trailing = (done_match.group(1) or "").strip()
                    end_line_number = body_line_number
                    next_line_index = body_index + 1
                    break

                body_lines.append((body_line_number, body_code_line))
                active_heredocs.extend(extract_heredoc_delimiters(body_line))
                control_depth = self._next_control_depth(body_code_line, control_depth)
                body_index += 1
            else:
                return None, line_index + 1

        column = self._command_column(code_line, keyword)
        return WhileLoop(
            location=SourceLocation(script_path, line_number, column),
            text=code_line.strip(),
            keyword=keyword,
            condition=condition.strip(),
            body=self._parse_loop_body(script_path, body_lines),
            trailing=trailing,
            end_location=SourceLocation(script_path, end_line_number, 1),
            producer=producer.strip(),
        ), next_line_index

    @staticmethod
    def _parse_loop_words(words_text: str):
        try:
            raw_words = parse_shell_words_preserving_quotes(words_text)
            return tuple(strip_shell_word_quotes(word) for word in raw_words), True
        except UnsupportedSourceError:
            return (), False

    def _parse_loop_body(self, script_path: Path, body_lines):
        if not body_lines:
            return ()

        start_index = min(line_number for line_number, _ in body_lines) - 1
        end_index = max(line_number for line_number, _ in body_lines)
        lines = [""] * end_index
        for line_number, code_line in body_lines:
            lines[line_number - 1] = code_line

        return tuple(self._parse_lines(script_path, lines, start_index, end_index))
