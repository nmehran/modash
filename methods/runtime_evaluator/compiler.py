from __future__ import annotations

import base64
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from methods.compile import SET_SHEBANG, find_unquoted_substring, replace_runtime_source_references
from methods.runtime_evaluator.graph import (
    RuntimeSourceGraphError,
    ensure_graph_fingerprints_current,
    validate_observed_source_graph,
)
from methods.shell.line import get_commands
from methods.source_commands import contains_source_command, source_command_invocation
from methods.source_conditions import source_logical_condition_atoms_from_text
from methods.source_effects import CaseBlock, CStyleForLoop, ForLoop, FunctionDef, IfBlock, SourceSite, WhileLoop
from methods.source_frontend import LineParserFrontend
from methods.source_resolver import (
    ASSIGNMENT_WORD_PATTERN,
    UnsupportedSourceError,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
)

RUNTIME_COMPILER_VERSION = 2
ENTRYPOINT_LOGICAL_PATH = "__entrypoint__"
PROCESS_LOGICAL_PREFIX = "__process_command__"
REPLAY_FAILURE_STATUS = 125


class RuntimeObservedCompileError(RuntimeSourceGraphError):
    def __init__(self, message: str, code: str = "runtime.compile.invalid_graph"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class _ReplayEdge:
    index: int
    process_index: int
    from_node: str
    to_node: str
    site_file: str
    site_line: int
    site_command: str
    resolved_path: str
    status: int
    arguments: tuple[str, ...]
    xtrace_command: str

    @property
    def is_file(self) -> bool:
        return self.to_node.startswith("file:")

    @property
    def is_missing(self) -> bool:
        return self.to_node.startswith("missing-source:")

    @property
    def source_value(self) -> str:
        invocation = source_command_invocation(_first_source_segment(self.xtrace_command) or "")
        if invocation is None:
            return self.resolved_path
        try:
            words = parse_shell_words_preserving_quotes(invocation.source_expression)
        except UnsupportedSourceError:
            return self.resolved_path
        if not words:
            return ""
        return strip_shell_word_quotes(words[0])


@dataclass(frozen=True)
class _SourceCandidate:
    logical_path: str
    physical_path: str | None
    line: int
    text: str
    separator: str
    ordinal: int
    base_id: str
    process_index: int = 0
    status_before: int | None = None
    repeatable: bool = False


@dataclass
class _RewriteUnit:
    logical_path: str
    physical_path: str | None
    content: str
    process_index: int = 0
    candidates: tuple[_SourceCandidate, ...] = ()
    transformed: str | None = None


@dataclass(frozen=True)
class _EmbeddedFile:
    logical_path: str
    content: str


@dataclass
class _ProcessPlan:
    process_index: int
    edges: tuple[_ReplayEdge, ...]
    units: dict[str, _RewriteUnit]
    candidates: tuple[_SourceCandidate, ...]
    assignments: dict[str, list[_ReplayEdge]] = field(default_factory=dict)


@dataclass
class _CompilePlan:
    entrypoint: Path
    graph: dict
    file_units: dict[str, _RewriteUnit]
    process_plans: dict[int, _ProcessPlan]
    process_payloads: dict[int, tuple[str, str]]


def compile_runtime_graph(entrypoint: str | os.PathLike, output: str | os.PathLike, graph_payload: dict) -> Path:
    target = Path(output)
    script = render_runtime_graph_script(entrypoint, graph_payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)
    return target


def render_runtime_graph_script(entrypoint: str | os.PathLike, graph_payload: dict) -> str:
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    graph = ensure_graph_fingerprints_current(validate_observed_source_graph(graph_payload))
    if Path(graph["entrypoint"]).resolve(strict=False) != entrypoint_path:
        raise RuntimeObservedCompileError(
            f"runtime graph entrypoint does not match requested entrypoint: {graph['entrypoint']}",
            code="runtime.compile.entrypoint_mismatch",
        )

    plan = _build_compile_plan(entrypoint_path, graph)
    _rewrite_process_payloads(plan)
    _rewrite_file_units(plan)
    main_plan = plan.process_plans[0]
    embedded_files = tuple(
        _EmbeddedFile(unit.logical_path, unit.transformed or unit.content)
        for logical_path, unit in sorted(plan.file_units.items())
        if logical_path != ENTRYPOINT_LOGICAL_PATH
    )
    prelude = _render_replay_prelude(embedded_files, main_plan.assignments, _target_logical_paths(plan.file_units))
    entrypoint_unit = plan.file_units[ENTRYPOINT_LOGICAL_PATH]
    return prelude + "\n" + (entrypoint_unit.transformed or entrypoint_unit.content).rstrip("\n") + "\n"


def supports_runtime_graph(graph_payload: dict) -> bool:
    validate_observed_source_graph(graph_payload)
    return True


def _build_compile_plan(entrypoint: Path, graph: dict) -> _CompilePlan:
    edges = tuple(_coerce_edge(edge) for edge in graph["edges"])
    file_units = _file_units(entrypoint, graph, edges)
    process_plans = _process_plans(graph, edges, file_units)
    return _CompilePlan(entrypoint, graph, file_units, process_plans, {})


def _coerce_edge(edge: dict) -> _ReplayEdge:
    return _ReplayEdge(
        index=edge["index"],
        process_index=edge["process_index"],
        from_node=edge["from"],
        to_node=edge["to"],
        site_file=edge["call_site"]["file"],
        site_line=edge["call_site"]["line"],
        site_command=edge["call_site"]["command"],
        resolved_path=edge["resolved_path"],
        status=edge["status"],
        arguments=tuple(edge["arguments"]),
        xtrace_command=edge["xtrace"]["command"],
    )


def _file_units(entrypoint: Path, graph: dict, edges: tuple[_ReplayEdge, ...]) -> dict[str, _RewriteUnit]:
    included_paths = {str(entrypoint)}
    for fingerprint in graph["files"]:
        roles = set(fingerprint["roles"])
        if roles & {"entrypoint", "source"}:
            included_paths.add(fingerprint["path"])
    for edge in edges:
        if edge.from_node.startswith("file:"):
            included_paths.add(edge.site_file)
        if edge.is_file:
            included_paths.add(edge.resolved_path)

    units: dict[str, _RewriteUnit] = {}
    for path_text in sorted(included_paths):
        path = Path(path_text).resolve(strict=False)
        logical_path = ENTRYPOINT_LOGICAL_PATH if path == entrypoint else _logical_embedded_path(entrypoint.parent, path)
        if logical_path in units:
            continue
        content = path.read_text(encoding="utf-8")
        unit = _RewriteUnit(logical_path, str(path), content, process_index=0)
        unit.candidates = _source_candidates(unit)
        units[logical_path] = unit
    return units


def _logical_embedded_path(root: Path, path: Path) -> str:
    path = path.resolve(strict=False)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = "abs/" + base64.urlsafe_b64encode(str(path).encode("utf-8")).decode("ascii").rstrip("=")
    _validate_logical_path(relative)
    return relative


def _process_plans(graph: dict, edges: tuple[_ReplayEdge, ...], file_units: dict[str, _RewriteUnit]) -> dict[int, _ProcessPlan]:
    edges_by_process: dict[int, list[_ReplayEdge]] = {}
    for edge in edges:
        edges_by_process.setdefault(edge.process_index, []).append(edge)
    edges_by_process.setdefault(0, [])

    process_nodes = {node["process_index"]: node for node in graph["nodes"] if node["kind"] == "process"}
    plans: dict[int, _ProcessPlan] = {}
    for process_index, process_edges in sorted(edges_by_process.items()):
        if process_index == 0:
            units = file_units
        else:
            process = process_nodes.get(process_index)
            if process is None:
                raise RuntimeObservedCompileError(f"missing process node for process {process_index}")
            units = _clone_file_units_for_process(file_units, process_index)
            logical_path = _process_logical_path(process_index)
            unit = _RewriteUnit(logical_path, None, process["command"], process_index=process_index)
            unit.candidates = _source_candidates(unit)
            units[logical_path] = unit
        candidates = tuple(candidate for unit in units.values() for candidate in unit.candidates)
        plan = _ProcessPlan(process_index, tuple(process_edges), units, candidates)
        plan.assignments = _assign_edges_to_candidates(process_index, plan.edges, plan.candidates)
        plans[process_index] = plan
    return plans


def _clone_file_units_for_process(file_units: dict[str, _RewriteUnit], process_index: int) -> dict[str, _RewriteUnit]:
    clones: dict[str, _RewriteUnit] = {}
    for logical_path, unit in file_units.items():
        clone = _RewriteUnit(logical_path, unit.physical_path, unit.content, process_index=process_index)
        clone.candidates = tuple(
            _SourceCandidate(
                logical_path=candidate.logical_path,
                physical_path=candidate.physical_path,
                line=candidate.line,
                text=candidate.text,
                separator=candidate.separator,
                ordinal=candidate.ordinal,
                base_id=_base_id(process_index, candidate.logical_path, candidate.line, candidate.ordinal),
                process_index=process_index,
                status_before=candidate.status_before,
                repeatable=candidate.repeatable,
            )
            for candidate in unit.candidates
        )
        clones[logical_path] = clone
    return clones


def _process_logical_path(process_index: int) -> str:
    return f"{PROCESS_LOGICAL_PREFIX}_{process_index}"


def _source_candidates(unit: _RewriteUnit) -> tuple[_SourceCandidate, ...]:
    try:
        ir = LineParserFrontend().parse(unit.physical_path or unit.logical_path, unit.content)
    except Exception as exc:
        raise RuntimeObservedCompileError(
            f"could not parse source candidates in {unit.physical_path or unit.logical_path}: {exc}",
            code="runtime.compile.parse_failed",
        ) from exc

    raw_sites: list[tuple[int, str, str, int | None, bool]] = []

    def add_site(line: int, text: str, separator: str = "", status_before: int | None = None, *, repeatable: bool = False) -> None:
        stripped = text.strip()
        if not stripped:
            return
        probe = stripped.lstrip(";&| ")
        if source_command_invocation(stripped) is None and source_command_invocation(probe) is None:
            return
        raw_sites.append((line, stripped, separator, status_before, repeatable))

    def add_condition_sites(line: int, condition: str, *, repeatable: bool) -> None:
        for text, separator, status_before in _condition_source_sites(condition):
            add_site(line, text, separator, status_before, repeatable=repeatable)

    def collect(nodes, *, repeatable: bool = False) -> None:
        for node in nodes:
            if isinstance(node, SourceSite):
                add_site(node.location.line, node.text, node.separator, repeatable=repeatable)
            elif isinstance(node, FunctionDef):
                collect(node.body, repeatable=True)
            elif isinstance(node, (ForLoop, CStyleForLoop)):
                collect(node.body, repeatable=True)
            elif isinstance(node, WhileLoop):
                if node.condition:
                    add_condition_sites(node.location.line, node.condition, repeatable=True)
                collect(node.body, repeatable=True)
            elif isinstance(node, IfBlock):
                for branch in node.branches:
                    condition_location = getattr(branch, "condition_location", None)
                    if branch.condition and condition_location is not None:
                        add_condition_sites(condition_location.line, branch.condition, repeatable=repeatable)
                    collect(branch.body, repeatable=repeatable)
            elif isinstance(node, CaseBlock):
                for arm in node.arms:
                    collect(arm.body, repeatable=repeatable)

    collect(ir.nodes)
    candidates: list[_SourceCandidate] = []
    ordinals_by_line: dict[int, int] = {}
    for line, text, separator, status_before, repeatable in raw_sites:
        ordinal = ordinals_by_line.get(line, 0)
        ordinals_by_line[line] = ordinal + 1
        base_id = _base_id(unit.process_index, unit.logical_path, line, ordinal)
        candidates.append(_SourceCandidate(
            logical_path=unit.logical_path,
            physical_path=unit.physical_path,
            line=line,
            text=text,
            separator=separator,
            ordinal=ordinal,
            base_id=base_id,
            process_index=unit.process_index,
            status_before=status_before,
            repeatable=repeatable,
        ))
    return tuple(candidates)

def _condition_source_sites(condition: str) -> tuple[tuple[str, str, int | None], ...]:
    try:
        atoms = source_logical_condition_atoms_from_text(condition)
    except Exception:
        try:
            ir = LineParserFrontend().parse("<condition>", condition + "\n")
        except Exception:
            return ()
        return tuple((site.text, site.separator, None) for site in ir.source_sites)

    sites = []
    status: int | None = None
    for atom in atoms:
        if atom.source_command is not None:
            text = f"{atom.separator} {atom.source_command} {atom.source_expression}".strip()
            sites.append((text, atom.separator, status))
            status = None
            continue
        atom_status = _static_command_status(atom.text)
        if atom_status is None:
            status = None
            continue
        if status is None or atom.separator in {"", ";"}:
            status = atom_status
        elif atom.separator == "&&":
            status = atom_status if status == 0 else status
        elif atom.separator == "||":
            status = status if status == 0 else atom_status
        if atom.negated and status is not None:
            status = 0 if status else 1
    return tuple(sites)


def _static_command_status(text: str) -> int | None:
    stripped = text.strip()
    if stripped in {":", "true"}:
        return 0
    if stripped == "false":
        return 1
    return None


def _assign_edges_to_candidates(process_index: int, edges: tuple[_ReplayEdge, ...], candidates: tuple[_SourceCandidate, ...]) -> dict[str, list[_ReplayEdge]]:
    assignments: dict[str, list[_ReplayEdge]] = {candidate.base_id: [] for candidate in candidates}
    candidates_by_line: dict[tuple[str, int], list[_SourceCandidate]] = {}
    for candidate in candidates:
        candidates_by_line.setdefault((candidate.physical_path or candidate.logical_path, candidate.line), []).append(candidate)

    file_edges = [edge for edge in edges if edge.from_node.startswith("file:")]
    edges_by_line: dict[tuple[str, int], list[_ReplayEdge]] = {}
    for edge in file_edges:
        edges_by_line.setdefault((edge.site_file, edge.site_line), []).append(edge)
    for key, line_edges in sorted(edges_by_line.items(), key=lambda item: min(edge.index for edge in item[1])):
        line_candidates = candidates_by_line.get(key)
        if not line_candidates:
            raise RuntimeObservedCompileError(
                f"observed source edge cannot be mapped to a source site: {key[0]}:{key[1]}",
                code="runtime.compile.unmapped_edge",
            )
        _assign_line_edges(assignments, line_candidates, sorted(line_edges, key=lambda edge: edge.index))

    process_edges = [edge for edge in edges if edge.from_node.startswith("process-command:")]
    if process_edges:
        logical_path = _process_logical_path(process_index)
        process_candidates = [candidate for candidate in candidates if candidate.logical_path == logical_path]
        process_edges_by_line: dict[int, list[_ReplayEdge]] = {}
        for edge in process_edges:
            process_edges_by_line.setdefault(edge.site_line, []).append(edge)
        for line, line_edges in sorted(process_edges_by_line.items(), key=lambda item: min(edge.index for edge in item[1])):
            line_candidates = [candidate for candidate in process_candidates if candidate.line == line]
            if not line_candidates:
                raise RuntimeObservedCompileError(
                    f"observed child-process source edge cannot be mapped to payload line {line}",
                    code="runtime.compile.unmapped_process_edge",
                )
            _assign_line_edges(assignments, line_candidates, sorted(line_edges, key=lambda edge: edge.index))
    _reject_unconsumed_static_edge_assignments(assignments, candidates)
    return assignments


def _reject_unconsumed_static_edge_assignments(assignments: dict[str, list[_ReplayEdge]], candidates: tuple[_SourceCandidate, ...]) -> None:
    for candidate in candidates:
        edges = assignments.get(candidate.base_id, [])
        if len(edges) > 1 and not candidate.repeatable:
            raise UnsupportedSourceError(
                "trusted runtime graph contains an unconsumed source edge",
                code="unsupported.source.graph-unconsumed",
            )


def _assign_line_edges(assignments: dict[str, list[_ReplayEdge]], candidates: list[_SourceCandidate], edges: list[_ReplayEdge]) -> None:
    if _assign_line_edges_from_condition(assignments, candidates, edges):
        return
    edge_index = 0
    while edge_index < len(edges):
        prior_status: int | None = None
        consumed = 0
        for candidate in candidates:
            if candidate.status_before is not None:
                prior_status = candidate.status_before
            if candidate.separator == "&&" and prior_status not in (None, 0):
                continue
            if candidate.separator == "||" and prior_status == 0:
                continue
            if edge_index >= len(edges):
                break
            edge = edges[edge_index]
            assignments.setdefault(candidate.base_id, []).append(edge)
            edge_index += 1
            consumed += 1
            prior_status = edge.status
        if consumed == 0:
            raise RuntimeObservedCompileError(
                f"could not assign observed source edges for {candidates[0].physical_path or candidates[0].logical_path}:{candidates[0].line}",
                code="runtime.compile.unmapped_edge",
            )


def _assign_line_edges_from_condition(assignments: dict[str, list[_ReplayEdge]], candidates: list[_SourceCandidate], edges: list[_ReplayEdge]) -> bool:
    if not candidates or not edges:
        return False
    condition = _condition_text_for_candidates(candidates)
    if condition is None:
        return False
    try:
        atoms = source_logical_condition_atoms_from_text(condition)
    except UnsupportedSourceError:
        return False
    by_text: dict[str, list[_SourceCandidate]] = {}
    for candidate in candidates:
        by_text.setdefault(candidate.text.lstrip(";&| "), []).append(candidate)

    mapped: list[tuple[_ReplayEdge, _SourceCandidate]] = []
    edge_index = 0
    status = 0
    for atom in atoms:
        if atom.separator == "&&" and status != 0:
            continue
        if atom.separator == "||" and status == 0:
            continue
        if atom.source_command is None:
            static_status = _static_command_status(atom.text)
            if static_status is None:
                return False
            status = _negate_condition_status(static_status) if atom.negated else static_status
            continue
        if edge_index >= len(edges):
            return False
        source_text = f"{atom.source_command} {atom.source_expression}"
        matches = by_text.get(source_text, [])
        if len(matches) != 1:
            return False
        edge = edges[edge_index]
        mapped.append((edge, matches[0]))
        edge_index += 1
        status = 0 if edge.status == 0 else 1
        if atom.negated:
            status = _negate_condition_status(status)
    if edge_index != len(edges):
        return False
    for edge, candidate in mapped:
        assignments.setdefault(candidate.base_id, []).append(edge)
    return True


def _condition_text_for_candidates(candidates: list[_SourceCandidate]) -> str | None:
    first = candidates[0]
    if first.physical_path is None:
        return None
    try:
        line = Path(first.physical_path).read_text(encoding="utf-8").splitlines()[first.line - 1]
    except (OSError, IndexError):
        return None
    # The frontend already knows the condition for most multi-line blocks; this
    # one-line fallback only supports compact `if ...; then` and loop heads.
    match = re.search(r"(?:^|[;{]\s*)if\s+(.+?)(?:;\s*then|\s+then)(?:[;}]|\s|$)", line, re.S)
    if match is not None:
        return match.group(1).strip()
    match = re.search(r"(?:^|[;{]\s*)(?:while|until)\s+(.+?)(?:;\s*do|\s+do)(?:[;}]|\s|$)", line, re.S)
    if match is not None:
        return match.group(1).strip()
    return None


def _negate_condition_status(status: int) -> int:
    return 0 if status else 1


def _rewrite_process_payloads(plan: _CompilePlan) -> None:
    for process_index, process_plan in plan.process_plans.items():
        if process_index == 0:
            continue
        unit = process_plan.units[_process_logical_path(process_index)]
        unit.transformed = _rewrite_content(
            unit,
            process_plan.assignments,
            plan.entrypoint,
            {},
            rewrite_runtime_references=False,
        )
        embedded_files = []
        for logical_path, file_unit in sorted(process_plan.units.items()):
            if logical_path in {ENTRYPOINT_LOGICAL_PATH, _process_logical_path(process_index)}:
                continue
            transformed = _rewrite_content(
                file_unit,
                process_plan.assignments,
                plan.entrypoint,
                {},
                rewrite_runtime_references=False,
            )
            embedded_files.append(_EmbeddedFile(file_unit.logical_path, transformed))
        payload = (
            _render_replay_prelude(tuple(embedded_files), process_plan.assignments, _target_logical_paths(plan.file_units)).rstrip("\n")
            + "\n"
            + unit.transformed.rstrip("\n")
            + "\n"
        )
        plan.process_payloads[process_index] = (unit.content, payload)


def _rewrite_file_units(plan: _CompilePlan) -> None:
    _ensure_unique_process_payloads(plan.process_payloads)
    main_plan = plan.process_plans[0]
    replacement_counts = {process_index: 0 for process_index in plan.process_payloads}
    for unit in plan.file_units.values():
        unit.transformed = _rewrite_content(
            unit,
            main_plan.assignments,
            plan.entrypoint,
            plan.process_payloads,
            process_replacement_counts=replacement_counts,
        )
    for process_index, count in replacement_counts.items():
        if count != 1:
            raise RuntimeObservedCompileError(
                f"observed child bash -c process {process_index} matched {count} parent command sites; expected exactly 1",
                code="runtime.compile.child_process_mapping_failed",
            )


def _rewrite_content(
    unit: _RewriteUnit,
    assignments: dict[str, list[_ReplayEdge]],
    entrypoint: Path,
    process_payloads: dict[int, tuple[str, str]],
    process_replacement_counts: dict[int, int] | None = None,
    rewrite_runtime_references: bool = True,
) -> str:
    replacements_by_line = _candidate_replacements_by_line(unit)
    lines = unit.content.splitlines()
    output: list[str] = []
    for line_index, original_line in enumerate(lines, start=1):
        line = original_line
        replacements = replacements_by_line.get(line_index, [])
        if replacements:
            line = _apply_replacements(line, replacements)
        if rewrite_runtime_references and unit.physical_path is not None and not _line_has_bash_c_payload(line):
            line = replace_runtime_source_references(line, unit.physical_path, str(entrypoint))
        if process_payloads:
            line = _rewrite_bash_c_payloads(line, process_payloads, process_replacement_counts)
        _ensure_no_unrewritten_source(line, unit, line_index)
        output.append(line)
    rendered = "\n".join(output)
    if unit.content.endswith("\n"):
        rendered += "\n"
    return rendered


def _candidate_replacements_by_line(unit: _RewriteUnit) -> dict[int, list[tuple[int, int, str]]]:
    replacements_by_line: dict[int, list[tuple[int, int, str]]] = {}
    lines = unit.content.splitlines()
    search_start_by_line: dict[int, int] = {}
    for candidate in unit.candidates:
        if candidate.line < 1 or candidate.line > len(lines):
            raise RuntimeObservedCompileError(
                f"source candidate line out of range in {unit.physical_path or unit.logical_path}: {candidate.line}",
                code="runtime.compile.mapping_failed",
            )
        line = lines[candidate.line - 1]
        search_start = search_start_by_line.get(candidate.line, 0)
        needle = candidate.text
        start = find_unquoted_substring(line, needle, search_start)
        if start < 0:
            needle = candidate.text.lstrip(";&| ")
            start = find_unquoted_substring(line, needle, search_start)
        if start < 0:
            raise RuntimeObservedCompileError(
                f"could not locate source site {candidate.text!r} in {unit.physical_path or unit.logical_path}:{candidate.line}",
                code="runtime.compile.mapping_failed",
            )
        end = start + len(needle)
        search_start_by_line[candidate.line] = end
        replacement = _render_replay_group(candidate.base_id)
        if candidate.separator and candidate.text.lstrip().startswith(candidate.separator):
            replacement = f"{candidate.separator} {replacement}"
        replacements_by_line.setdefault(candidate.line, []).append((start, end, replacement))
    return replacements_by_line


def _render_replay_group(base_id: str) -> str:
    return (
        f"{{ __modash_select_source_edge {shlex.quote(base_id)}; "
        "__modash_replay_select_status=$?; "
        "if (( __modash_replay_select_status != 0 )); then "
        "( exit \"$__modash_replay_select_status\" ); "
        "elif [[ $__modash_replay_kind == file ]]; then "
        "builtin source \"$__modash_replay_file\" \"${__modash_replay_args[@]}\"; "
        "else "
        "printf '%s: line %s: %s\\n' \"$__modash_replay_diag_file\" \"$__modash_replay_diag_line\" \"$__modash_replay_diag_message\" >&2; "
        "( exit \"$__modash_replay_status\" ); "
        "fi; }"
    )


def _apply_replacements(line: str, replacements: list[tuple[int, int, str]]) -> str:
    rendered = line
    occupied: list[tuple[int, int]] = []
    for start, end, replacement in sorted(replacements, reverse=True):
        if any(start < right and end > left for left, right in occupied):
            raise RuntimeObservedCompileError(
                f"overlapping source site rewrites in line: {line.strip()}",
                code="runtime.compile.mapping_failed",
            )
        rendered = rendered[:start] + replacement + rendered[end:]
        occupied.append((start, end))
    return rendered


def _rewrite_bash_c_payloads(
    line: str,
    process_payloads: dict[int, tuple[str, str]],
    process_replacement_counts: dict[int, int] | None = None,
    rewrite_runtime_references: bool = True,
) -> str:
    rewritten = line
    search_start = 0
    for command in get_commands(line):
        start = find_unquoted_substring(rewritten, command, search_start)
        if start < 0:
            continue
        end = start + len(command)
        payload = _bash_c_payload(command)
        if payload is None:
            search_start = end
            continue
        replacement = None
        replacement_process = None
        for process_index, (original_payload, transformed_payload) in process_payloads.items():
            if payload == original_payload:
                if replacement is not None:
                    raise RuntimeObservedCompileError(
                        f"ambiguous observed child bash -c payload mapping for: {payload}",
                        code="runtime.compile.child_process_mapping_failed",
                    )
                replacement = _rewrite_bash_c_command(command, transformed_payload)
                replacement_process = process_index
        if replacement is None:
            search_start = end
            continue
        rewritten = rewritten[:start] + replacement + rewritten[end:]
        if process_replacement_counts is not None and replacement_process is not None:
            process_replacement_counts[replacement_process] = process_replacement_counts.get(replacement_process, 0) + 1
        search_start = start + len(replacement)
    return rewritten


def _rewrite_bash_c_command(command: str, payload: str) -> str:
    words = parse_shell_words_preserving_quotes(command.strip())
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    if index + 2 >= len(words):
        raise RuntimeObservedCompileError(f"unsupported bash -c command: {command}")
    command_name = strip_shell_word_quotes(words[index])
    rewritten_words = [*words[:index], command_name, "-c", shlex.quote(payload), *words[index + 3:]]
    return " ".join(rewritten_words)


def _line_has_bash_c_payload(line: str) -> bool:
    return any(_bash_c_payload(command) is not None for command in get_commands(line))


def _render_replay_prelude(
    embedded_files: tuple[_EmbeddedFile, ...],
    assignments: dict[str, list[_ReplayEdge]],
    target_logical_paths: dict[str, str],
) -> str:
    file_setup = []
    for file in embedded_files:
        _validate_logical_path(file.logical_path)
        encoded = base64.b64encode(file.content.encode("utf-8")).decode("ascii")
        file_setup.append("__modash_write_embedded_file " f"{shlex.quote(file.logical_path)} {shlex.quote(encoded)}")

    map_setup: list[str] = []
    edge_keys: list[str] = []
    for base_id, edges in sorted(assignments.items()):
        for occurrence, edge in enumerate(edges):
            key = f"{base_id}|{occurrence}"
            edge_keys.append(key)
            map_setup.extend(_edge_map_lines(base_id, occurrence, edge, target_logical_paths))

    return f'''{SET_SHEBANG}
# Generated by modash 0.7 trusted runtime graph compiler.

__modash_tmp=$(mktemp -d "${{TMPDIR:-/tmp}}/modash-runtime.XXXXXX") || exit {REPLAY_FAILURE_STATUS}
__modash_aborting=0

__modash_abort() {{
  __modash_aborting=1
  printf 'modash runtime replay error: %s\\n' "$1" >&2
  exit {REPLAY_FAILURE_STATUS}
}}

__modash_write_embedded_file() {{
  local logical_path=$1 encoded=$2 output_path
  output_path="$__modash_tmp/$logical_path"
  mkdir -p "$(dirname -- "$output_path")" || __modash_abort "could not create embedded file directory: $logical_path"
  printf '%s' "$encoded" | base64 -d > "$output_path" || __modash_abort "could not unpack embedded file: $logical_path"
}}

{chr(10).join(file_setup)}

declare -a __modash_edge_keys=({" ".join(shlex.quote(key) for key in edge_keys)})
declare -A __modash_edge_target=()
declare -A __modash_edge_kind=()
declare -A __modash_edge_status=()
declare -A __modash_edge_argc=()
declare -A __modash_edge_arg=()
declare -A __modash_edge_diag_file=()
declare -A __modash_edge_diag_line=()
declare -A __modash_edge_diag_message=()
declare -A __modash_edge_seen=()
declare -A __modash_edge_consumed=()
__modash_replay_file=
__modash_replay_kind=
__modash_replay_status=0
__modash_replay_args=()
__modash_replay_diag_file=
__modash_replay_diag_line=
__modash_replay_diag_message=

{chr(10).join(map_setup)}

__modash_select_source_edge() {{
  local base=$1 seen key argc index arg_key
  __modash_replay_file=
  __modash_replay_kind=
  __modash_replay_status=0
  __modash_replay_args=()
  __modash_replay_diag_file=
  __modash_replay_diag_line=
  __modash_replay_diag_message=
  seen=${{__modash_edge_seen[$base]:-0}}
  key="${{base}}|${{seen}}"
  if [[ -z ${{__modash_edge_kind[$key]+set}} ]]; then
    __modash_abort "unobserved or over-consumed source edge: $key"
  fi
  __modash_edge_seen[$base]=$((seen + 1))
  __modash_edge_consumed[$key]=1
  __modash_replay_kind=${{__modash_edge_kind[$key]}}
  __modash_replay_status=${{__modash_edge_status[$key]:-0}}
  __modash_replay_file="$__modash_tmp/${{__modash_edge_target[$key]}}"
  __modash_replay_diag_file=${{__modash_edge_diag_file[$key]:-}}
  __modash_replay_diag_line=${{__modash_edge_diag_line[$key]:-}}
  __modash_replay_diag_message=${{__modash_edge_diag_message[$key]:-}}
  argc=${{__modash_edge_argc[$key]:-0}}
  for ((index = 0; index < argc; index++)); do
    arg_key="${{key}}"$'\x1f'"${{index}}"
    __modash_replay_args+=("${{__modash_edge_arg[$arg_key]}}")
  done
}}

__modash_verify_replay_consumed() {{
  local status=$? key
  if ((__modash_aborting)); then
    rm -rf -- "$__modash_tmp"
    exit "$status"
  fi
  for key in "${{__modash_edge_keys[@]}}"; do
    if [[ -z ${{__modash_edge_consumed[$key]+set}} ]]; then
      printf 'modash runtime replay error: unconsumed observed source edge: %s\\n' "$key" >&2
      rm -rf -- "$__modash_tmp"
      exit {REPLAY_FAILURE_STATUS}
    fi
  done
  rm -rf -- "$__modash_tmp"
  exit "$status"
}}

trap '__modash_verify_replay_consumed' EXIT
'''


def _edge_map_lines(base_id: str, occurrence: int, edge: _ReplayEdge, target_logical_paths: dict[str, str]) -> list[str]:
    key = f"{base_id}|{occurrence}"
    lines = [
        f"__modash_edge_kind[{shlex.quote(key)}]={shlex.quote('file' if edge.is_file else 'missing')}",
        f"__modash_edge_status[{shlex.quote(key)}]={edge.status}",
        f"__modash_edge_argc[{shlex.quote(key)}]={len(edge.arguments)}",
    ]
    if edge.is_file:
        try:
            target = target_logical_paths[edge.resolved_path]
        except KeyError as exc:
            raise RuntimeObservedCompileError(
                f"observed source target is not bundled: {edge.resolved_path}",
                code="runtime.compile.unbundled_target",
            ) from exc
        lines.append(f"__modash_edge_target[{shlex.quote(key)}]={shlex.quote(target)}")
    else:
        lines.extend([
            f"__modash_edge_target[{shlex.quote(key)}]=''",
            f"__modash_edge_diag_file[{shlex.quote(key)}]={shlex.quote(edge.site_file)}",
            f"__modash_edge_diag_line[{shlex.quote(key)}]={edge.site_line}",
            f"__modash_edge_diag_message[{shlex.quote(key)}]={shlex.quote(_missing_source_message(edge))}",
        ])
    for index, argument in enumerate(edge.arguments):
        arg_key = f"{key}\x1f{index}"
        lines.append(f"__modash_edge_arg[{shlex.quote(arg_key)}]={shlex.quote(argument)}")
    return lines


def _target_logical_paths(file_units: dict[str, _RewriteUnit]) -> dict[str, str]:
    return {
        str(Path(unit.physical_path).resolve(strict=False)): logical_path
        for logical_path, unit in file_units.items()
        if unit.physical_path is not None and logical_path != ENTRYPOINT_LOGICAL_PATH
    }


def _ensure_unique_process_payloads(process_payloads: dict[int, tuple[str, str]]) -> None:
    payload_owners: dict[str, int] = {}
    for process_index, (payload, _transformed) in process_payloads.items():
        owner = payload_owners.get(payload)
        if owner is not None:
            raise RuntimeObservedCompileError(
                f"repeated identical bash -c payload is not yet replayable without a parent process occurrence key: processes {owner} and {process_index}",
                code="runtime.compile.ambiguous_child_process",
            )
        payload_owners[payload] = process_index


def _missing_source_message(edge: _ReplayEdge) -> str:
    value = edge.source_value
    if edge.status == 2 and not value:
        command_name = _missing_source_command_name(edge)
        return f"{command_name}: filename argument required"
    return f"{value}: No such file or directory"


def _missing_source_command_name(edge: _ReplayEdge) -> str:
    invocation = source_command_invocation(_first_source_segment(edge.xtrace_command) or "")
    return invocation.command_name if invocation is not None else "source"


def _ensure_no_unrewritten_source(line: str, unit: _RewriteUnit, line_index: int) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    for command in get_commands(line):
        if "__modash_select_source_edge" in command:
            continue
        try:
            words = parse_shell_words_preserving_quotes(command.strip())
        except UnsupportedSourceError:
            words = []
        name = strip_shell_word_quotes(words[0]) if words else ""
        if (
            contains_source_command(command)
            or (name == "eval" and ("source" in command or re.search(r"(^|[\s;&|({])\.\s+", command)))
        ):
            raise RuntimeObservedCompileError(
                f"unsupported unrewritten source command in {unit.physical_path or unit.logical_path}:{line_index}: {command.strip()}",
                code="runtime.compile.unrewritten_source",
            )


def _first_source_segment(command: str) -> str | None:
    for segment in get_commands(command):
        if source_command_invocation(segment) is not None:
            return segment
    return command if source_command_invocation(command) is not None else None


def _base_id(process_index: int, logical_path: str, line: int, ordinal: int) -> str:
    return f"p{process_index}:{logical_path}:{line}:{ordinal}"


def _validate_logical_path(path: str) -> None:
    pure = PurePosixPath(path)
    if path.startswith("/") or ".." in pure.parts or not path or path.endswith("/"):
        raise RuntimeObservedCompileError(
            f"unsafe embedded logical path: {path!r}",
            code="runtime.compile.unsafe_path",
        )


def _bash_c_payload(command: str) -> str | None:
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    if index + 2 >= len(words):
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"} or words[index + 1] != "-c":
        return None
    return strip_shell_word_quotes(words[index + 2])


__all__ = [
    "RUNTIME_COMPILER_VERSION",
    "RuntimeObservedCompileError",
    "compile_runtime_graph",
    "render_runtime_graph_script",
    "supports_runtime_graph",
]
