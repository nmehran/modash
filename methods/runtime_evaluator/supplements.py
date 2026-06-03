from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from methods.source_commands import (
    SOURCE_COMMAND_NAMES,
    clean_shell_word,
    source_command_invocation,
)
from methods.source_conditions import (
    condition_status_not,
    literal_command_condition_status,
    source_logical_condition_atoms_from_text,
)
from methods.source_frontend import LineParserFrontend
from methods.source_resolver import (
    UnsupportedSourceError,
    parse_shell_words_preserving_quotes,
)
from methods.runtime_evaluator.observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch_details,
    format_fingerprint_mismatch,
    validate_observation,
)
from methods.runtime_evaluator.graph import (
    ensure_graph_fingerprints_current,
    validate_observed_source_graph,
)
from methods.source_supplements import SUPPLEMENT_VERSION, source_supplement_from_payload

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
    index: int
    call_site_file: str
    call_site_line: int
    call_site_command: str
    resolved_path: str
    arguments: tuple[str, ...]
    status: int = 0
    to_node: str = ""
    xtrace_command: str = ""


@dataclass(frozen=True)
class _FunctionSourceAlias:
    function_name: str | None
    has_function_local_assignment: bool = False
    has_first_positional_alias: bool = False
    shifted_after_alias: bool = False
    variable_positions: tuple[tuple[str, int], ...] = ()

    def position_for_variable(self, name: str):
        for variable_name, position in self.variable_positions:
            if variable_name == name:
                return position
        return None


@dataclass(frozen=True)
class _HelperSignature:
    arguments: tuple[str, ...]
    source_index: int = 0


def generate_source_supplement(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_observation_matches_entrypoint(entrypoint_path, observation)
    _ensure_successful_observation_run(observation)
    if validate_fingerprints:
        _ensure_observation_fingerprints_current(observation)
        _ensure_observation_source_presence_current(observation)

    source_events = tuple(
        _SupplementSourceEvent(
            index=event.index,
            call_site_file=event.call_site.file,
            call_site_line=event.call_site.line,
            call_site_command=event.call_site.command,
            resolved_path=event.resolved_path,
            arguments=event.arguments,
            status=event.status,
            xtrace_command=(
                observation.xtrace[event.xtrace_index].command
                if event.xtrace_index is not None
                else ""
            ),
        )
        for event in observation.sources
    )
    condition_functions, skipped_events = _condition_helper_signatures_from_events(
        source_events,
        entrypoint_path.parent,
    )
    return _generate_source_supplement_from_events(
        entrypoint_path,
        source_events,
        initial_functions=condition_functions,
        skipped_event_indexes=skipped_events,
    )


def generate_source_supplement_from_graph(entrypoint: str | os.PathLike, graph, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    graph = _coerce_graph(graph)
    _ensure_graph_matches_entrypoint(entrypoint_path, graph)
    if validate_fingerprints:
        ensure_graph_fingerprints_current(graph)

    source_events = tuple(
        _SupplementSourceEvent(
            index=edge["index"],
            call_site_file=edge["call_site"]["file"],
            call_site_line=edge["call_site"]["line"],
            call_site_command=edge["call_site"]["command"],
            resolved_path=edge["resolved_path"],
            arguments=tuple(edge["arguments"]),
            status=edge["status"],
            to_node=edge["to"],
            xtrace_command=edge["xtrace"]["command"],
        )
        for edge in graph["edges"]
    )
    condition_functions, skipped_events = _condition_helper_signatures_from_events(
        source_events,
        entrypoint_path.parent,
    )
    return _generate_source_supplement_from_events(
        entrypoint_path,
        source_events,
        initial_functions=condition_functions,
        skipped_event_indexes=skipped_events,
    )


def _generate_source_supplement_from_events(
    entrypoint_path: Path,
    source_events,
    *,
    initial_functions=None,
    skipped_event_indexes=frozenset(),
):
    entrypoint_directory = entrypoint_path.parent
    variables = {}
    functions: dict[str, list[dict[str, list[str]]]] = {
        name: list(entries)
        for name, entries in (initial_functions or {}).items()
    }

    for event in source_events:
        if event.index in skipped_event_indexes or _event_is_missing_source(event):
            continue
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
            name: entries
            for name, entries in sorted(functions.items())
        },
    }
    load_source_supplement_from_payload(payload, entrypoint_directory)
    return GeneratedSourceSupplement(payload)


def _event_is_missing_source(event: _SupplementSourceEvent):
    return event.to_node.startswith("missing-source:") or (event.status != 0 and not Path(event.resolved_path).is_file())


def _condition_helper_signatures_from_events(source_events, entrypoint_directory: Path):
    functions: dict[str, list[dict[str, list[str]]]] = {}
    skipped_edges = set()
    grouped_edges = {}
    for event in source_events:
        grouped_edges.setdefault(_event_group_key(event), []).append(event)

    for group in grouped_edges.values():
        function_name = _enclosing_function_name(
            group[0].call_site_file,
            group[0].call_site_line,
        )
        if function_name is None:
            continue
        signatures = _condition_helper_signatures_for_group(group, entrypoint_directory)
        if signatures is None:
            continue
        for signature in signatures:
            entry = {"arguments": list(signature.arguments)}
            if signature.source_index:
                entry["source_index"] = signature.source_index
            functions.setdefault(function_name, [])
            if entry not in functions[function_name]:
                functions[function_name].append(entry)
        skipped_edges.update(event.index for event in group)

    return functions, frozenset(skipped_edges)


def _event_group_key(event: _SupplementSourceEvent):
    return event.call_site_file, event.call_site_line, event.call_site_command


def _condition_helper_signatures_for_group(edges, entrypoint_directory: Path):
    sequences = _source_condition_atom_sequences_for_edge(edges[0])
    if len(sequences) != 1:
        return None

    mapped_groups = _source_condition_edge_groups(edges, sequences[0])
    if mapped_groups is None:
        return None

    signatures = []
    for mapped in mapped_groups:
        signature = _condition_helper_signature_from_edges(mapped, entrypoint_directory)
        if signature is None:
            return None
        signatures.append(signature)
    return tuple(signatures)


def _condition_helper_signature_from_edges(mapped, entrypoint_directory: Path):
    signature = {}
    source_index = None
    for edge, atom in mapped:
        words = _source_expression_words(atom.source_expression)
        if not words:
            return None
        source_position = _positional_word_index(words[0])
        if source_position is None:
            return None
        signature[source_position] = _observed_function_argument(edge, entrypoint_directory)
        if source_index is None and not _event_is_missing_source(edge):
            source_index = source_position - 1

        for argument_index, word in enumerate(words[1:]):
            position = _positional_word_index(word)
            if position is None or argument_index >= len(edge.arguments):
                return None
            signature[position] = edge.arguments[argument_index]
        if len(words) - 1 != len(edge.arguments):
            return None

    if source_index is None or not signature:
        return None
    signature_length = max(signature)
    if any(index not in signature for index in range(1, signature_length + 1)):
        return None
    return _HelperSignature(
        tuple(signature[index] for index in range(1, signature_length + 1)),
        source_index=source_index,
    )


def _source_condition_edge_groups(edges, atoms):
    groups = []
    edge_index = 0
    while edge_index < len(edges):
        mapped, consumed = _source_condition_edge_prefix(edges[edge_index:], atoms)
        if mapped is None or consumed <= 0:
            return None
        groups.append(mapped)
        edge_index += consumed
    return tuple(groups)


def _source_condition_edge_prefix(edges, atoms):
    mapped = []
    edge_index = 0
    status = "true"
    for atom in atoms:
        if atom.separator == "&&" and status == "false":
            continue
        if atom.separator == "||" and status == "true":
            continue

        if atom.source_command is not None:
            if edge_index >= len(edges):
                return None, 0
            edge = edges[edge_index]
            mapped.append((edge, atom))
            edge_index += 1
            status = "true" if edge.status == 0 else "false"
            if atom.negated:
                status = condition_status_not(status)
            continue

        status = literal_command_condition_status(atom.text)
        if status is None:
            return None, 0
        if atom.negated:
            status = condition_status_not(status)

    if not mapped:
        return None, 0
    return tuple(mapped), edge_index


def _source_condition_atom_sequences_for_edge(edge):
    condition = _control_source_condition(edge.call_site_command)
    if condition is not None:
        atoms = _source_condition_atoms_from_text(condition)
        return (atoms,) if atoms else ()
    return _source_condition_atom_sequences_on_line(
        edge.call_site_file,
        edge.call_site_line,
    )


def _source_condition_atoms_from_text(condition: str):
    try:
        return source_logical_condition_atoms_from_text(condition)
    except UnsupportedSourceError:
        return ()


def _control_source_condition(command: str):
    stripped = command.strip()
    match = re.fullmatch(r'(?:if|elif|while|until)\s+(.+?)(?:\s*;\s*(?:then|do).*)?$', stripped, re.S)
    if match is None:
        return None
    return match.group(1).strip()


@lru_cache(maxsize=512)
def _source_condition_atom_sequences_on_line(path: str, line: int):
    candidate = Path(path)
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        ir = LineParserFrontend().parse(candidate, content)
    except Exception:
        return ()

    sequences = []

    def collect(nodes):
        for node in nodes:
            if getattr(node, "condition", None) and node.location.line == line:
                atoms = _source_condition_atoms_from_text(node.condition)
                if atoms:
                    sequences.append(atoms)
            body = getattr(node, "body", None)
            if body:
                collect(body)
            for branch in getattr(node, "branches", ()):
                condition_location = getattr(branch, "condition_location", None)
                if branch.condition and condition_location is not None and condition_location.line == line:
                    atoms = _source_condition_atoms_from_text(branch.condition)
                    if atoms:
                        sequences.append(atoms)
                collect(getattr(branch, "body", ()))
            for arm in getattr(node, "arms", ()):
                collect(getattr(arm, "body", ()))

    collect(ir.nodes)
    return tuple(sequences)


def _source_expression_words(source_expression: str):
    try:
        return tuple(clean_shell_word(word) for word in parse_shell_words_preserving_quotes(source_expression))
    except Exception:
        return ()


def _observed_function_argument(edge, entrypoint_directory: Path):
    if not _event_is_missing_source(edge):
        return _review_path(edge.resolved_path, entrypoint_directory)
    source_site = _source_invocation_from_command(edge.xtrace_command)
    if source_site is not None:
        return source_site[0]
    return _review_path(edge.resolved_path, entrypoint_directory)


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


def _ensure_successful_observation_run(observation: RuntimeSourceObservation):
    if observation.run.target_status != 0:
        raise RuntimeSupplementGenerationError(
            "runtime source observation target exited with status "
            f"{observation.run.target_status}; refusing source supplement generation",
            code="runtime.supplement.nonzero_trace",
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


def _ensure_observation_source_presence_current(observation: RuntimeSourceObservation):
    fingerprint_paths = {fingerprint.path for fingerprint in observation.files}
    for event in observation.sources:
        resolved_path = str(Path(event.resolved_path).resolve(strict=False))
        if resolved_path not in fingerprint_paths and Path(resolved_path).is_file():
            raise RuntimeSupplementGenerationError(
                f"runtime source observation is stale for {resolved_path}: source_presence mismatch",
                code="runtime.supplement.stale_observation",
            )


def _source_invocation_from_command(command: str):
    invocation = source_command_invocation(
        command,
        normalize_trace_wrappers=True,
        stop_at_shell_control=True,
    )
    if invocation is None:
        return None
    return invocation.source_path, invocation.arguments


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
            cleaned = clean_shell_word(word)
            if cleaned in SOURCE_COMMAND_NAMES:
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
