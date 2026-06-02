from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    load_observation,
    validate_observation,
)
from methods.source_resolver import parse_shell_words_preserving_quotes, strip_shell_word_quotes
from methods.source_supplements import SUPPLEMENT_VERSION, load_source_supplement

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


def generate_source_supplement(entrypoint: str | os.PathLike, observation):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_observation_matches_entrypoint(entrypoint_path, observation)

    entrypoint_directory = entrypoint_path.parent
    variables = {}
    functions: dict[str, list[dict[str, list[str]]]] = {}

    for event in observation.sources:
        source_word = _source_word_from_command(event.call_site.command)
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
            function_name = _enclosing_function_name(event.call_site.file, event.call_site.line)
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


def write_generated_supplement(supplement: GeneratedSourceSupplement | dict, path: str | os.PathLike):
    payload = _coerce_supplement(supplement).to_dict()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def load_source_supplement_from_payload(payload: dict, entrypoint_directory: str | os.PathLike):
    with _TemporarySupplementFile(payload) as path:
        return load_source_supplement(path, entrypoint_directory)


def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)


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


class _TemporarySupplementFile:
    def __init__(self, payload: dict):
        self.payload = payload
        self._path = None

    def __enter__(self):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        with handle:
            json.dump(self.payload, handle, indent=2)
            handle.write("\n")
        self._path = Path(handle.name)
        return self._path

    def __exit__(self, exc_type, exc, traceback):
        if self._path is not None:
            self._path.unlink(missing_ok=True)
