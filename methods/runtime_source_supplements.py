from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch_details,
    format_fingerprint_mismatch,
    load_observation,
    validate_observation,
)
from methods.runtime_source_graph import (
    ensure_graph_fingerprints_current,
    load_observed_source_graph,
    validate_observed_source_graph,
)
from methods.source_resolver import parse_shell_words_preserving_quotes, strip_shell_word_quotes
from methods.source_supplements import SUPPLEMENT_VERSION, source_supplement_from_payload

SOURCE_COMMANDS = frozenset({"source", "."})
VARIABLE_REFERENCE_PATTERN = re.compile(r'\$(?:{([a-zA-Z_]\w*)(?::?-[^}]*)?}|([a-zA-Z_]\w*))')
POSITIONAL_SOURCE_WORDS = frozenset({"$@", "${@}", "$*", "${*}", "$1", "${1}"})
FUNCTION_DEFINITION_PATTERN = re.compile(
    r'^\s*(?:function\s+)?([a-zA-Z_]\w*)\s*(?:\(\s*\))?\s*\{'
)


class RuntimeSupplementGenerationError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.supplement.invalid"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class GeneratedSourceSupplement:
    payload: dict

    def to_dict(self):
        return {
            "version": SUPPLEMENT_VERSION,
            "variables": dict(self.payload.get("variables", {})),
            "functions": {
                name: [
                    {
                        "arguments": list(entry["arguments"]),
                        **({"source_index": entry["source_index"]} if entry.get("source_index", 0) else {}),
                    }
                    for entry in entries
                ]
                for name, entries in self.payload.get("functions", {}).items()
            },
        }


@dataclass(frozen=True)
class _SupplementSourceEvent:
    call_site_file: str
    call_site_line: int
    call_site_command: str
    resolved_path: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _FunctionSourceAlias:
    function_name: str | None
    has_function_local_assignment: bool = False
    has_first_positional_alias: bool = False
    shifted_after_alias: bool = False
    variable_positions: tuple[tuple[str, int], ...] = ()

    def position_for_variable(self, name: str):
        return dict(self.variable_positions).get(name)


@dataclass(frozen=True)
class _HelperSignature:
    arguments: tuple[str, ...]
    source_index: int = 0


def generate_source_supplement(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_observation_matches_entrypoint(entrypoint_path, observation)
    if validate_fingerprints:
        _ensure_observation_fingerprints_current(observation)

    return _generate_source_supplement_from_events(
        entrypoint_path,
        (
            _SupplementSourceEvent(
                call_site_file=event.call_site.file,
                call_site_line=event.call_site.line,
                call_site_command=event.call_site.command,
                resolved_path=event.resolved_path,
                arguments=event.arguments,
            )
            for event in observation.sources
        ),
    )


def generate_source_supplement_from_graph(entrypoint: str | os.PathLike, graph, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    graph = _coerce_graph(graph)
    _ensure_graph_matches_entrypoint(entrypoint_path, graph)
    if validate_fingerprints:
        ensure_graph_fingerprints_current(graph)

    return _generate_source_supplement_from_events(
        entrypoint_path,
        (
            _SupplementSourceEvent(
                call_site_file=edge["call_site"]["file"],
                call_site_line=edge["call_site"]["line"],
                call_site_command=edge["call_site"]["command"],
                resolved_path=edge["resolved_path"],
                arguments=tuple(edge["arguments"]),
            )
            for edge in graph["edges"]
        ),
    )


def _generate_source_supplement_from_events(entrypoint_path: Path, source_events):
    entrypoint_directory = entrypoint_path.parent
    variables = {}
    functions: dict[str, list[dict[str, list[str]]]] = {}

    for event in source_events:
        invocation = _source_invocation_from_command(event.call_site_command)
        if invocation is None:
            continue
        source_word, source_arg_words = invocation
        alias = _function_source_alias(event.call_site_file, event.call_site_line, source_word)
        signature_arguments = _helper_signature_arguments(event, source_word, source_arg_words, alias, entrypoint_directory)

        variable = None if alias.has_function_local_assignment else _variable_candidate(
            source_word,
            event.resolved_path,
            entrypoint_directory,
        )
        if variable is not None:
            name, value = variable
            existing = variables.get(name)
            if existing is not None and existing != value:
                raise RuntimeSupplementGenerationError(
                    f"conflicting observed values for source supplement variable {name}",
                    code="runtime.supplement.variable_conflict",
                )
            variables[name] = value

        if alias.function_name and signature_arguments is not None:
            functions.setdefault(alias.function_name, [])
            entry = {"arguments": list(signature_arguments.arguments)}
            if signature_arguments.source_index:
                entry["source_index"] = signature_arguments.source_index
            if entry not in functions[alias.function_name]:
                functions[alias.function_name].append(entry)

    payload = {
        "version": SUPPLEMENT_VERSION,
        "variables": dict(sorted(variables.items())),
        "functions": {
            name: sorted(entries, key=lambda item: item["arguments"])
            for name, entries in sorted(functions.items())
        },
    }
    load_source_supplement_from_payload(payload, entrypoint_directory)
    return GeneratedSourceSupplement(payload)


def generate_source_supplement_from_observation_file(entrypoint: str | os.PathLike, observation_path: str | os.PathLike):
    return generate_source_supplement(entrypoint, load_observation(observation_path))


def generate_source_supplement_from_graph_file(entrypoint: str | os.PathLike, graph_path: str | os.PathLike):
    return generate_source_supplement_from_graph(entrypoint, load_observed_source_graph(graph_path))


def write_generated_supplement(supplement: GeneratedSourceSupplement | dict, path: str | os.PathLike):
    payload = _coerce_supplement(supplement).to_dict()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def load_source_supplement_from_payload(payload: dict, entrypoint_directory: str | os.PathLike):
    return source_supplement_from_payload(payload, entrypoint_directory)


def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)


def _coerce_graph(graph):
    return validate_observed_source_graph(graph)


def _coerce_supplement(supplement):
    if isinstance(supplement, GeneratedSourceSupplement):
        return supplement
    if isinstance(supplement, dict):
        return GeneratedSourceSupplement(supplement)
    raise RuntimeSupplementGenerationError("generated source supplement must be an object")


def _ensure_observation_matches_entrypoint(entrypoint_path: Path, observation: RuntimeSourceObservation):
    observed_entrypoint = Path(observation.entrypoint).resolve(strict=False)
    if observed_entrypoint != entrypoint_path:
        raise RuntimeSupplementGenerationError(
            f"observation entrypoint does not match requested entrypoint: {observed_entrypoint}",
            code="runtime.supplement.entrypoint_mismatch",
        )


def _ensure_graph_matches_entrypoint(entrypoint_path: Path, graph: dict):
    observed_entrypoint = Path(graph["entrypoint"]).resolve(strict=False)
    if observed_entrypoint != entrypoint_path:
        raise RuntimeSupplementGenerationError(
            f"runtime source graph entrypoint does not match requested entrypoint: {observed_entrypoint}",
            code="runtime.supplement.entrypoint_mismatch",
        )


def _ensure_observation_fingerprints_current(observation: RuntimeSourceObservation):
    for fingerprint in observation.files:
        mismatch = current_fingerprint_mismatch_details(fingerprint)
        if mismatch is not None:
            raise RuntimeSupplementGenerationError(
                format_fingerprint_mismatch("runtime source observation", fingerprint, mismatch),
                code="runtime.supplement.stale_observation",
            )


def _source_word_from_command(command: str):
    invocation = _source_invocation_from_command(command)
    return invocation[0] if invocation is not None else None


def _source_invocation_from_command(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None

    for index, word in enumerate(words[:-1]):
        command_name = strip_shell_word_quotes(_strip_shell_punctuation(word))
        if command_name not in SOURCE_COMMANDS:
            continue
        source_word = _clean_source_word(words[index + 1])
        source_arguments = []
        for argument in words[index + 2:]:
            cleaned = _clean_source_word(argument)
            if not cleaned:
                break
            if cleaned in {"then", "do", "else", "fi", "done", "}"}:
                break
            if cleaned in {"&&", "||", "|"}:
                break
            source_arguments.append(cleaned)
        return source_word, tuple(source_arguments)
    return None


def _clean_source_word(word: str):
    word = _strip_shell_punctuation(word)
    return strip_shell_word_quotes(word)


def _strip_shell_punctuation(word: str):
    while word.endswith(";"):
        word = word[:-1]
    return word


def _variable_candidate(source_word: str, resolved_path: str, entrypoint_directory: Path):
    matches = list(VARIABLE_REFERENCE_PATTERN.finditer(source_word))
    if len(matches) != 1:
        return None
    match = matches[0]
    if match.start() != 0:
        return None

    name = match.group(1) or match.group(2)
    suffix = source_word[match.end():]
    if "$" in suffix or "`" in suffix:
        return None

    resolved = Path(resolved_path).resolve(strict=False).as_posix()
    if suffix:
        if not suffix.startswith("/"):
            return None
        if not resolved.endswith(suffix):
            return None
        value_path = resolved[:-len(suffix)]
    else:
        value_path = resolved

    if not value_path:
        return None
    return name, _review_path(value_path, entrypoint_directory)


def _is_positional_source_word(source_word: str):
    return source_word in POSITIONAL_SOURCE_WORDS


def _helper_signature_arguments(
    event: _SupplementSourceEvent,
    source_word: str,
    source_arg_words,
    alias: _FunctionSourceAlias,
    entrypoint_directory: Path,
):
    if _is_positional_source_word(source_word):
        return _HelperSignature((
            _review_path(event.resolved_path, entrypoint_directory),
            *event.arguments,
        ))

    source_position = _helper_word_position(source_word, alias)
    if source_position is None:
        return None

    if len(source_arg_words) == 1 and _is_variadic_positional_word(source_arg_words[0]):
        if alias.shifted_after_alias:
            return _HelperSignature((
                _review_path(event.resolved_path, entrypoint_directory),
                *event.arguments,
            ))
        return None

    signature = {source_position: _review_path(event.resolved_path, entrypoint_directory)}
    event_argument_index = 0
    for word in source_arg_words:
        position = _helper_word_position(word, alias)
        if position is None:
            return None
        if event_argument_index >= len(event.arguments):
            return None
        signature[position] = event.arguments[event_argument_index]
        event_argument_index += 1
    if event_argument_index != len(event.arguments):
        return None

    if 1 not in signature:
        case_value = _case_arm_signature_value(event.call_site_command)
        if case_value is not None:
            signature[1] = case_value

    signature_length = max(signature)
    if any(index not in signature for index in range(1, signature_length + 1)):
        return None
    return _HelperSignature(
        tuple(signature[index] for index in range(1, signature_length + 1)),
        source_index=source_position - 1,
    )


def _helper_word_position(word: str, alias: _FunctionSourceAlias):
    if position := _positional_word_index(word):
        return position
    variable_name = _exact_variable_reference_name(word)
    if variable_name is None:
        return None
    return alias.position_for_variable(variable_name)


def _is_variadic_positional_word(word: str):
    return word in {"$@", "${@}", "$*", "${*}"}


def _positional_word_index(word: str):
    match = re.fullmatch(r'\$(?:\{([1-9][0-9]*)\}|([1-9][0-9]*))', word)
    if match:
        return int(match.group(1) or match.group(2))
    return None


def _case_arm_signature_value(command: str):
    match = re.match(r'\s*([a-zA-Z0-9_./:+-]+)\)\s+', command)
    if not match:
        return None
    return match.group(1)


def _function_source_alias(path: str, line: int, source_word: str):
    function_context = _enclosing_function_context(path, line)
    function_name = function_context[0] if function_context is not None else None
    variable_name = _exact_variable_reference_name(source_word)
    if function_context is None or variable_name is None:
        return _FunctionSourceAlias(function_name=function_name)

    words = _function_words_before_source(function_context[1], source_word)
    shifted = False
    has_function_local_assignment = False
    aliased_from_first_positional = False
    shifted_after_alias = False
    shifted_count = 0
    variable_positions = {}
    index = 0
    while index < len(words):
        word = words[index]
        if word == "shift":
            amount, index = _consume_shift(words, index)
            if amount != "1":
                aliased_from_first_positional = False
                shifted_after_alias = False
                shifted = True
                continue
            shifted = True
            shifted_count += 1
            if aliased_from_first_positional:
                shifted_after_alias = True
            continue

        assignment = _assignment_from_word(word)
        index += 1
        if assignment is None:
            continue
        name, value = assignment
        if name != variable_name:
            if position := _positional_word_index(value):
                variable_positions[name] = position + shifted_count
            else:
                variable_positions.pop(name, None)
            continue
        has_function_local_assignment = True
        if position := _positional_word_index(value):
            variable_positions[name] = position + shifted_count
        else:
            variable_positions.pop(name, None)
        if not shifted and _is_first_positional_word(value):
            aliased_from_first_positional = True
            shifted_after_alias = False
        else:
            aliased_from_first_positional = False
            shifted_after_alias = False

    return _FunctionSourceAlias(
        function_name=function_name,
        has_function_local_assignment=has_function_local_assignment,
        has_first_positional_alias=aliased_from_first_positional,
        shifted_after_alias=shifted_after_alias,
        variable_positions=tuple(sorted(variable_positions.items())),
    )


def _consume_shift(words, index):
    next_index = index + 1
    if next_index >= len(words):
        return "1", next_index
    word = words[next_index]
    if word.isdigit() or _looks_like_explicit_shift_count(word):
        return word, next_index + 1
    return "1", next_index


def _looks_like_explicit_shift_count(word: str):
    return word.startswith("$") or bool(re.fullmatch(r'[+-]\d+', word))


def _exact_variable_reference_name(word: str):
    if re.fullmatch(r'\$[a-zA-Z_]\w*', word):
        return word[1:]
    match = re.fullmatch(r'\$\{([a-zA-Z_]\w*)\}', word)
    if match:
        return match.group(1)
    return None


def _is_first_positional_word(word: str):
    return word in {"$1", "${1}"}


def _assignment_from_word(word: str):
    if "=" not in word:
        return None
    name, value = word.split("=", 1)
    if not re.fullmatch(r'[a-zA-Z_]\w*', name):
        return None
    return name, value


def _function_words_before_source(function_lines, source_word: str):
    words = []
    for text in function_lines:
        try:
            parsed = parse_shell_words_preserving_quotes(text)
        except Exception:
            continue
        for word in parsed:
            cleaned = _clean_source_word(word)
            if cleaned in SOURCE_COMMANDS:
                return words
            if cleaned == source_word:
                return words
            words.append(cleaned)
    return words


def _enclosing_function_name(path: str, line: int):
    context = _enclosing_function_context(path, line)
    return context[0] if context is not None else None


def _enclosing_function_context(path: str, line: int):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if line < 1 or line > len(lines):
        return None

    active_name = None
    active_depth = 0
    function_start = 0
    for line_number, text in enumerate(lines, start=1):
        if active_name is None:
            match = FUNCTION_DEFINITION_PATTERN.match(text)
            if match:
                active_name = match.group(1)
                function_start = line_number
                active_depth = _brace_delta(text)
                if line_number == line:
                    return active_name, (text,)
                if active_depth <= 0:
                    active_name = None
                    active_depth = 0
                    function_start = 0
        else:
            active_depth += _brace_delta(text)

        if active_name is not None and line_number == line:
            return active_name, tuple(lines[function_start - 1:line_number])
        if active_name is not None and active_depth <= 0:
            active_name = None
            active_depth = 0
            function_start = 0
    return None


def _brace_delta(text: str):
    return text.count("{") - text.count("}")


def _review_path(path: str, entrypoint_directory: Path):
    candidate = Path(path).resolve(strict=False)
    try:
        relative = os.path.relpath(candidate, entrypoint_directory)
    except ValueError:
        return str(candidate)
    return relative if len(relative) < len(str(candidate)) else str(candidate)
