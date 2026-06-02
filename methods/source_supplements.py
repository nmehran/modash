from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from methods.source_resolver import UnsupportedSourceError

SUPPLEMENT_VERSION = 1
VARIABLE_NAME_PATTERN = re.compile(r'^[a-zA-Z_]\w*$')
FUNCTION_NAME_PATTERN = re.compile(r'^[a-zA-Z_]\w*$')
TOP_LEVEL_KEYS = frozenset({"version", "variables", "functions"})
FUNCTION_ENTRY_KEYS = frozenset({"arguments"})


@dataclass(frozen=True)
class SourceSupplement:
    variables: dict[str, str] = field(default_factory=dict)
    functions: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    def function_signatures(self, name: str):
        return self.functions.get(name, ())


def empty_source_supplement():
    return SourceSupplement()


def load_source_supplement(value: str | os.PathLike | SourceSupplement | dict | None, entrypoint_directory: str | os.PathLike):
    if value is None:
        return empty_source_supplement()
    if isinstance(value, SourceSupplement):
        return value
    if isinstance(value, dict):
        return source_supplement_from_payload(value, entrypoint_directory)

    supplement_path = Path(value)
    if not supplement_path.is_file():
        raise _supplement_error(
            f"source supplement file does not exist: {supplement_path}",
            "Pass an existing JSON source supplement file.",
        )

    try:
        data = json.loads(supplement_path.read_text())
    except json.JSONDecodeError as exc:
        raise _supplement_error(
            f"invalid source supplement JSON: {supplement_path}: {exc}",
            "Use a JSON object with version 1, variables, and functions.",
        ) from exc

    return source_supplement_from_payload(data, entrypoint_directory)


def source_supplement_from_payload(data: dict, entrypoint_directory: str | os.PathLike):
    if not isinstance(data, dict):
        raise _supplement_error("invalid source supplement: top-level value must be an object")

    unknown_keys = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown_keys:
        raise _supplement_error(f"invalid source supplement: unknown top-level keys: {', '.join(unknown_keys)}")

    if data.get("version") != SUPPLEMENT_VERSION:
        raise _supplement_error("invalid source supplement: version must be 1")

    entrypoint_directory = Path(entrypoint_directory).resolve()
    variables = _load_variables(data.get("variables", {}), entrypoint_directory)
    functions = _load_functions(data.get("functions", {}), entrypoint_directory)
    return SourceSupplement(variables=variables, functions=functions)


def supplement_skeleton(variable_names=(), function_name: str | None = None):
    skeleton = {
        "version": SUPPLEMENT_VERSION,
        "variables": {},
        "functions": {},
    }
    for name in sorted(set(variable_names)):
        if VARIABLE_NAME_PATTERN.fullmatch(name):
            skeleton["variables"][name] = "<path>"
    if function_name and FUNCTION_NAME_PATTERN.fullmatch(function_name):
        skeleton["functions"][function_name] = [
            {
                "arguments": ["<source-path>"],
            }
        ]
    return skeleton


def _load_variables(raw_variables, entrypoint_directory: Path):
    if not isinstance(raw_variables, dict):
        raise _supplement_error("invalid source supplement: variables must be an object")

    variables = {}
    for name, value in raw_variables.items():
        if not isinstance(name, str) or not VARIABLE_NAME_PATTERN.fullmatch(name):
            raise _supplement_error(f"invalid source supplement variable name: {name!r}")
        variables[name] = _normalize_path_value(value, entrypoint_directory, f"variable {name}")
    return variables


def _load_functions(raw_functions, entrypoint_directory: Path):
    if not isinstance(raw_functions, dict):
        raise _supplement_error("invalid source supplement: functions must be an object")

    functions = {}
    for name, entries in raw_functions.items():
        if not isinstance(name, str) or not FUNCTION_NAME_PATTERN.fullmatch(name):
            raise _supplement_error(f"invalid source supplement function name: {name!r}")
        if not isinstance(entries, list):
            raise _supplement_error(f"invalid source supplement function entries for {name}: must be a list")

        signatures = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise _supplement_error(f"invalid source supplement function entry for {name}: must be an object")
            unknown_keys = sorted(set(entry) - FUNCTION_ENTRY_KEYS)
            if unknown_keys:
                raise _supplement_error(
                    f"invalid source supplement function entry for {name}: "
                    f"unknown keys: {', '.join(unknown_keys)}"
                )
            arguments = entry.get("arguments")
            if not isinstance(arguments, list):
                raise _supplement_error(f"invalid source supplement function entry for {name}: arguments must be a list")
            signatures.append(tuple(
                _normalize_path_value(argument, entrypoint_directory, f"function {name} source argument")
                if index == 0
                else _normalize_exact_value(argument, f"function {name} argument")
                for index, argument in enumerate(arguments)
            ))
        functions[name] = tuple(signatures)
    return functions


def _normalize_exact_value(value, label: str):
    if not isinstance(value, str):
        raise _supplement_error(f"invalid source supplement {label}: value must be a string")
    if "$(" in value or "`" in value or "$" in value:
        raise _supplement_error(f"invalid source supplement {label}: shell expansion is not allowed")
    if "\n" in value or "\r" in value:
        raise _supplement_error(f"invalid source supplement {label}: multiline values are not allowed")
    return value


def _normalize_path_value(value, entrypoint_directory: Path, label: str):
    if not isinstance(value, str):
        raise _supplement_error(f"invalid source supplement {label}: value must be a string")
    if not value:
        raise _supplement_error(f"invalid source supplement {label}: value must not be empty")
    if "$(" in value or "`" in value or "$" in value:
        raise _supplement_error(f"invalid source supplement {label}: shell expansion is not allowed")
    if "\n" in value or "\r" in value:
        raise _supplement_error(f"invalid source supplement {label}: multiline values are not allowed")

    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = entrypoint_directory / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise _supplement_error(f"invalid source supplement {label}: path does not exist: {value}")
    return str(candidate)


def _supplement_error(message: str, hint: str | None = None):
    return UnsupportedSourceError(
        message,
        code="unsupported.source.supplement",
        hint=hint or "Provide a valid JSON source supplement with version 1.",
    )
