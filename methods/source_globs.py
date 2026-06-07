import glob
import os
import re

from methods.source_errors import FailglobExpansionError, UnsupportedSourceError
from methods.source_patterns import UnsupportedPatternError, shell_pattern_matches
from methods.source_types import GlobMatch, ResolvedSource
from methods.source_words import (
    has_unquoted_brace_expansion,
    has_unquoted_extglob,
    has_unquoted_glob,
    strip_shell_word_quotes,
)

UNSUPPORTED_GLOB_OPTIONS = frozenset()
MISSING_SOURCE = "missing-source"
MISSING_SOURCE_NO_FILENAME = "missing-source-no-filename"
MISSING_SOURCE_INVALID_OPTION = "missing-source-invalid-option"
MISSING_SOURCE_REPLACEMENT_KINDS = frozenset({
    MISSING_SOURCE,
    MISSING_SOURCE_NO_FILENAME,
    MISSING_SOURCE_INVALID_OPTION,
})
SOURCE_EXPANSION_FAILURE = "source-expansion-failure"
SOURCE_EXPANSION_FAILURE_RETURN = "source-expansion-failure-return"
SOURCE_EXPANSION_FAILURE_REPLACEMENT_KINDS = frozenset({
    SOURCE_EXPANSION_FAILURE,
    SOURCE_EXPANSION_FAILURE_RETURN,
})


def is_missing_source_replacement_kind(replacement_kind: str):
    return replacement_kind in MISSING_SOURCE_REPLACEMENT_KINDS


def is_source_expansion_failure_replacement_kind(replacement_kind: str):
    return replacement_kind in SOURCE_EXPANSION_FAILURE_REPLACEMENT_KINDS


def missing_source_status(replacement_kind: str):
    if replacement_kind == MISSING_SOURCE:
        return 1
    if replacement_kind == MISSING_SOURCE_NO_FILENAME:
        return 2
    if replacement_kind == MISSING_SOURCE_INVALID_OPTION:
        return 2
    raise ValueError(f"unknown missing-source replacement kind: {replacement_kind}")


def _brace_expand(pattern: str, raw_pattern: str, source_site: str):
    if not has_unquoted_brace_expansion(raw_pattern):
        return [pattern]
    return _brace_expand_pattern(pattern, source_site)


def _brace_expand_pattern(pattern: str, source_site: str):
    start = pattern.find("{")
    if start < 0:
        return [pattern]

    depth = 0
    end = -1
    for index in range(start, len(pattern)):
        char = pattern[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break

    if end < 0:
        raise UnsupportedSourceError(f"unsupported brace source pattern: {source_site.strip()}")

    body = pattern[start + 1:end]
    if "{" in body or "}" in body:
        raise UnsupportedSourceError(f"unsupported nested brace source pattern: {source_site.strip()}")
    sequence_options = _brace_sequence_options(body)
    if sequence_options is not None:
        options = sequence_options
    elif "," in body:
        options = body.split(",")
    else:
        return [pattern]

    expanded = []
    for option in options:
        expanded.extend(_brace_expand_pattern(f"{pattern[:start]}{option}{pattern[end + 1:]}", source_site))
    return expanded


def _brace_sequence_options(body: str):
    match = re.fullmatch(r'(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?', body)
    if match:
        start_text, end_text, step_text = match.groups()
        start = int(start_text)
        end = int(end_text)
        if step_text is None:
            step = 1 if start <= end else -1
        else:
            step = abs(int(step_text))
            if step == 0:
                return None
            if start > end:
                step = -step
        width = max(len(start_text.lstrip("-")), len(end_text.lstrip("-")))
        zero_padded = (
            len(start_text.lstrip("-")) > 1 and start_text.lstrip("-").startswith("0")
        ) or (
            len(end_text.lstrip("-")) > 1 and end_text.lstrip("-").startswith("0")
        )
        stop = end + (1 if step > 0 else -1)
        values = []
        for value in range(start, stop, step):
            if zero_padded:
                sign = "-" if value < 0 else ""
                values.append(f"{sign}{abs(value):0{width}d}")
            else:
                values.append(str(value))
        return values

    match = re.fullmatch(r'([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?', body)
    if match:
        start_text, end_text, step_text = match.groups()
        start = ord(start_text)
        end = ord(end_text)
        if step_text is None:
            step = 1 if start <= end else -1
        else:
            step = abs(int(step_text))
            if step == 0:
                return None
            if start > end:
                step = -step
        stop = end + (1 if step > 0 else -1)
        return [chr(value) for value in range(start, stop, step)]

    return None


def _glob_matches(pattern: str, current_directory: str, glob_options: set[str], include_hidden: bool):
    if (
        include_hidden
        or 'nocaseglob' in glob_options
        or 'extglob' in glob_options
        or _has_escaped_pattern_meta(pattern)
    ):
        return _manual_glob_matches(pattern, current_directory, glob_options, include_hidden)

    recursive = 'globstar' in glob_options
    if os.path.isabs(pattern):
        return sorted(glob.glob(pattern, recursive=recursive))
    return sorted(glob.glob(
        pattern,
        root_dir=current_directory,
        recursive=recursive,
    ))


def _has_escaped_pattern_meta(pattern: str):
    return bool(re.search(r'\\[*?\[\]@!+()|]', pattern))


def _manual_glob_matches(pattern: str, current_directory: str, glob_options: set[str], include_hidden: bool):
    absolute_pattern = pattern if os.path.isabs(pattern) else os.path.join(current_directory, pattern)
    absolute_pattern = os.path.normpath(absolute_pattern)
    root, pattern_parts = _glob_static_root(absolute_pattern)
    if not os.path.isdir(root):
        return []

    recursive = 'globstar' in glob_options and '**' in pattern_parts
    max_depth = None if recursive else len(pattern_parts)
    matches = []

    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        relative_directory = os.path.relpath(directory, root)
        directory_parts = [] if relative_directory == os.curdir else relative_directory.split(os.sep)

        if max_depth is not None and len(directory_parts) >= max_depth:
            dirnames[:] = []

        for name in [*dirnames, *filenames]:
            candidate_parts = [*directory_parts, name]
            if max_depth is not None and len(candidate_parts) > max_depth:
                continue
            if not _glob_parts_match(pattern_parts, candidate_parts, glob_options, include_hidden):
                continue
            candidate = os.path.join(root, *candidate_parts)
            matches.append(candidate if os.path.isabs(pattern) else _relative_glob_word(candidate, current_directory, pattern))

    return sorted(matches)


def _pattern_with_quoted_literals(pattern: str, raw_pattern: str):
    if "$" in raw_pattern or strip_shell_word_quotes(raw_pattern) != pattern:
        return pattern

    output = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    index = 0
    metacharacters = set("*?[]@!+()|")

    while index < len(raw_pattern):
        char = raw_pattern[index]
        if escaped:
            output.append(f"\\{char}" if char in metacharacters else char)
            escaped = False
            index += 1
            continue

        if char == "\\" and not in_single_quote:
            escaped = True
            index += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            index += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            index += 1
            continue

        if (in_single_quote or in_double_quote) and char in metacharacters:
            output.append(f"\\{char}")
        else:
            output.append(char)
        index += 1

    return "".join(output)


def _glob_static_root(absolute_pattern: str):
    drive, tail = os.path.splitdrive(absolute_pattern)
    parts = [part for part in tail.split(os.sep) if part]
    root_parts = []
    while parts and not _glob_segment_has_magic(parts[0]):
        root_parts.append(parts.pop(0))

    root = drive + os.sep + os.path.join(*root_parts) if root_parts else drive + os.sep
    return os.path.normpath(root), parts


def _glob_segment_has_magic(segment: str):
    return any(char in segment for char in "*?[") or has_unquoted_extglob(segment)


def _glob_parts_match(pattern_parts: list[str], candidate_parts: list[str], glob_options: set[str],
                      include_hidden: bool):
    if not pattern_parts:
        return not candidate_parts

    pattern = pattern_parts[0]
    if pattern == "**" and "globstar" in glob_options:
        if _glob_parts_match(pattern_parts[1:], candidate_parts, glob_options, include_hidden):
            return True
        if not candidate_parts:
            return False
        if _hidden_glob_segment_blocked(pattern, candidate_parts[0], include_hidden):
            return False
        return _glob_parts_match(pattern_parts, candidate_parts[1:], glob_options, include_hidden)

    if not candidate_parts:
        return False
    if _hidden_glob_segment_blocked(pattern, candidate_parts[0], include_hidden):
        return False
    if not _glob_segment_matches(pattern, candidate_parts[0], glob_options):
        return False
    return _glob_parts_match(pattern_parts[1:], candidate_parts[1:], glob_options, include_hidden)


def _hidden_glob_segment_blocked(pattern: str, candidate: str, include_hidden: bool):
    return candidate.startswith(".") and not include_hidden and not pattern.startswith(".")


def _glob_segment_matches(pattern: str, candidate: str, glob_options: set[str]):
    return shell_pattern_matches(
        pattern,
        candidate,
        extglob='extglob' in glob_options,
        nocase='nocaseglob' in glob_options,
    )


def _relative_glob_word(path: str, current_directory: str, pattern: str):
    relative = os.path.relpath(path, current_directory)
    if pattern.startswith("./") and not relative.startswith(os.pardir):
        return f"./{relative}"
    return relative


def _globignore_patterns(context: dict):
    globignore = context.get('runtime_vars', context.get('vars', {})).get('GLOBIGNORE', '')
    if not globignore:
        return []
    return [pattern for pattern in globignore.split(":") if pattern]


def _apply_globignore(matches: list[str], patterns: list[str], glob_options: set[str]):
    if not patterns:
        return matches
    return [
        match
        for match in matches
        if not any(
            shell_pattern_matches(
                pattern,
                match,
                extglob='extglob' in glob_options,
                nocase='nocaseglob' in glob_options,
            )
            for pattern in patterns
        )
    ]


def _missing_glob_match(word: str, current_directory: str):
    path = word if os.path.isabs(word) else os.path.join(current_directory, word)
    return GlobMatch(word=word, path=os.path.abspath(path), exists=False, is_file=False)


def _missing_source_result(word: str, source_expression: str, source_site: str, context: dict, status_kind: str):
    return ResolvedSource(
        path=_missing_glob_match(word or ".", context['current_directory']).path,
        source_expression=source_expression.strip(),
        source_site=source_site.strip(),
        replacement_kind=status_kind,
        source_value=word,
    )


def source_expansion_failure_result(
    word: str,
    source_expression: str,
    source_site: str,
    context: dict,
    replacement_kind: str = SOURCE_EXPANSION_FAILURE,
):
    return ResolvedSource(
        path=_missing_glob_match(word or ".", context['current_directory']).path,
        source_expression=source_expression.strip(),
        source_site=source_site.strip(),
        replacement_kind=replacement_kind,
        source_value=word,
    )


def _has_pathname_expansion_pattern(pattern: str):
    return has_unquoted_glob(pattern) or has_unquoted_extglob(pattern)


def expand_glob_word(
    pattern: str,
    context: dict,
    source_site: str,
    raw_pattern: str | None = None,
    allow_missing_literal: bool = False,
    require_files: bool = True,
):
    raw_pattern = raw_pattern if raw_pattern is not None else pattern

    glob_options = set(context.get('glob_options', set()))
    enabled_unsupported_options = sorted(glob_options & UNSUPPORTED_GLOB_OPTIONS)
    if enabled_unsupported_options:
        option_list = ', '.join(enabled_unsupported_options)
        raise UnsupportedSourceError(f"unsupported glob shell option {option_list}: {source_site.strip()}")

    if 'noglob' in context.get('shell_options', set()):
        raise UnsupportedSourceError(f"unsupported noglob source pattern: {source_site.strip()}")
    if has_unquoted_extglob(raw_pattern) and 'extglob' not in glob_options:
        raise UnsupportedSourceError(f"unsupported disabled extglob source pattern: {source_site.strip()}")

    current_directory = context['current_directory']
    globignore_patterns = _globignore_patterns(context)
    include_hidden = 'dotglob' in glob_options or bool(globignore_patterns)
    matches = []
    matching_pattern = _pattern_with_quoted_literals(pattern, raw_pattern)
    try:
        for expanded_pattern in _brace_expand(matching_pattern, raw_pattern, source_site):
            has_pathname_pattern = _has_pathname_expansion_pattern(expanded_pattern)
            if has_pathname_pattern:
                pattern_matches = _glob_matches(expanded_pattern, current_directory, glob_options, include_hidden)
            else:
                literal_path = (
                    expanded_pattern
                    if os.path.isabs(expanded_pattern)
                    else os.path.join(current_directory, expanded_pattern)
                )
                pattern_matches = [expanded_pattern] if os.path.exists(literal_path) else []
            if not pattern_matches:
                if 'failglob' in glob_options and has_pathname_pattern:
                    raise FailglobExpansionError(expanded_pattern, source_site)
                if 'nullglob' in glob_options and has_pathname_pattern:
                    continue
                if allow_missing_literal:
                    matches.append(_missing_glob_match(expanded_pattern, current_directory))
                    continue
            filtered_matches = (
                _apply_globignore(pattern_matches, globignore_patterns, glob_options)
                if has_pathname_pattern
                else pattern_matches
            )
            if not filtered_matches and pattern_matches and 'nullglob' not in glob_options:
                if 'failglob' in glob_options:
                    raise FailglobExpansionError(expanded_pattern, source_site)
                if allow_missing_literal:
                    matches.append(_missing_glob_match(expanded_pattern, current_directory))
                    continue
                raise UnsupportedSourceError(f"unsupported GLOBIGNORE source pattern: {source_site.strip()}")
            matches.extend(filtered_matches)
    except UnsupportedPatternError as exc:
        raise UnsupportedSourceError(f"unsupported source pattern: {source_site.strip()} ({exc})") from exc

    if not matches:
        if 'failglob' in glob_options and _has_pathname_expansion_pattern(matching_pattern):
            raise FailglobExpansionError(matching_pattern, source_site)
        if 'nullglob' in glob_options:
            return ()
        if allow_missing_literal:
            return (_missing_glob_match(matching_pattern, current_directory),)
        raise UnsupportedSourceError(f"unsupported unmatched source glob: {source_site.strip()}")

    glob_matches = []
    for match in matches:
        if isinstance(match, GlobMatch):
            glob_matches.append(match)
            continue
        path = match if os.path.isabs(match) else os.path.join(current_directory, match)
        resolved_path = os.path.abspath(path)
        is_file = os.path.isfile(resolved_path)
        if require_files and not is_file:
            raise UnsupportedSourceError(f"unsupported non-file source glob match: {source_site.strip()}")
        glob_matches.append(GlobMatch(word=match, path=resolved_path, is_file=is_file))

    return tuple(glob_matches)

