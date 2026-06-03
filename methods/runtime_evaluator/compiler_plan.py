from __future__ import annotations

import base64
import re
from pathlib import Path

from methods.runtime_evaluator.compiler_model import (
    ENTRYPOINT_LOGICAL_PATH,
    PROCESS_LOGICAL_PREFIX,
    RuntimeObservedCompileError,
    _CompilePlan,
    _ProcessPlan,
    _ReplayEdge,
    _RewriteUnit,
    _SourceCandidate,
    _base_id,
    _validate_logical_path,
)
from methods.source_commands import source_command_invocation
from methods.source_conditions import (
    condition_exit_status_not,
    literal_command_condition_exit_status,
    source_logical_condition_atoms_from_text,
)
from methods.source_effects import CaseBlock, CStyleForLoop, ForLoop, FunctionDef, IfBlock, SourceSite, WhileLoop
from methods.source_frontend import LineParserFrontend
from methods.source_resolver import UnsupportedSourceError

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
        atom_status = literal_command_condition_exit_status(atom.text)
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
            static_status = literal_command_condition_exit_status(atom.text)
            if static_status is None:
                return False
            status = condition_exit_status_not(static_status) if atom.negated else static_status
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
            status = condition_exit_status_not(status)
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
