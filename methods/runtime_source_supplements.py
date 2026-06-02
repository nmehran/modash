from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch,
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
                    {"arguments": list(entry["arguments"])}
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
        source_word = _source_word_from_command(event.call_site_command)
        if not source_word:
            continue

        variable = _variable_candidate(source_word, event.resolved_path, entrypoint_directory)
        if variable is not None:
            name, value = variable
            existing = variables.get(name)
            if existing is not None and existing != value:
                raise RuntimeSupplementGenerationError(
                    f"conflicting observed values for source supplement variable {name}",
                    code="runtime.supplement.variable_conflict",
                )
            variables[name] = value

        if _is_positional_source_word(source_word):
            function_name = _enclosing_function_name(event.call_site_file, event.call_site_line)
            if function_name:
                arguments = [
                    _review_path(event.resolved_path, entrypoint_directory),
                    *event.arguments,
                ]
                functions.setdefault(function_name, [])
                entry = {"arguments": arguments}
                if entry not in functions[function_name]:
                    functions[function_name].append(entry)

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
        mismatch = current_fingerprint_mismatch(fingerprint)
        if mismatch is not None:
            raise RuntimeSupplementGenerationError(
                f"runtime source observation is stale for {fingerprint.path}: {mismatch} mismatch",
                code="runtime.supplement.stale_observation",
            )


def _source_word_from_command(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None

    for index, word in enumerate(words[:-1]):
        command_name = strip_shell_word_quotes(_strip_shell_punctuation(word))
        if command_name not in SOURCE_COMMANDS:
            continue
        return _clean_source_word(words[index + 1])
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


def _enclosing_function_name(path: str, line: int):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if line < 1 or line > len(lines):
        return None

    active_name = None
    active_depth = 0
    for line_number, text in enumerate(lines, start=1):
        if active_name is None:
            match = FUNCTION_DEFINITION_PATTERN.match(text)
            if match:
                active_name = match.group(1)
                active_depth = _brace_delta(text)
                if line_number == line:
                    return active_name
                if active_depth <= 0:
                    active_name = None
                    active_depth = 0
        else:
            active_depth += _brace_delta(text)

        if active_name is not None and line_number == line:
            return active_name
        if active_name is not None and active_depth <= 0:
            active_name = None
            active_depth = 0
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
