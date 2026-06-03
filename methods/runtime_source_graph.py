from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_source_observations import (
    EnvironmentInfo,
    FILE_FINGERPRINT_ROLES,
    OBSERVATION_VERSION,
    RuntimeFileFingerprint,
    RuntimeRunInfo,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch_details,
    format_fingerprint_mismatch,
    load_observation,
    validate_observation,
)
from methods.runtime_source_commands import is_trace_wrapper_source_command
from methods.source_resolver import (
    parse_shell_words_preserving_quotes,
    source_command_invocation,
    strip_shell_word_quotes,
)

GRAPH_VERSION = 2
GRAPH_TOP_LEVEL_KEYS = frozenset({
    "version",
    "entrypoint",
    "observation_version",
    "environment",
    "run",
    "summary",
    "nodes",
    "edges",
    "files",
})
GRAPH_SUMMARY_KEYS = frozenset({"processes", "nodes", "edges", "trusted_xtrace_edges"})
GRAPH_EDGE_KEYS = frozenset({
    "index",
    "source_identity",
    "process_index",
    "from",
    "to",
    "resolved_path",
    "status",
    "arguments",
    "call_site",
    "function_stack",
    "function_call",
    "xtrace",
})
FUNCTION_CALL_KEYS = frozenset({"file", "line", "function", "command", "arguments"})
PROCESS_NODE_KEYS = frozenset({
    "id",
    "kind",
    "process_index",
    "pid",
    "parent_index",
    "entrypoint",
    "cwd",
    "argv",
    "command",
})
FILE_NODE_KEYS = frozenset({"id", "kind", "path", "roles"})
PROCESS_COMMAND_NODE_KEYS = frozenset({
    "id",
    "kind",
    "process_index",
    "command",
    "entrypoint",
    "cwd",
})
MISSING_SOURCE_NODE_KEYS = frozenset({"id", "kind", "path", "status"})


class RuntimeSourceGraphError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.graph.invalid"):
        super().__init__(message, code=code)


def build_observed_source_graph(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_graph_entrypoint(entrypoint_path, observation)
    _ensure_successful_observation_run(observation.run)
    if validate_fingerprints:
        _ensure_fingerprints_current(observation)
        _ensure_source_presence_matches_fingerprints(observation)
    _ensure_trusted_xtrace_links(observation)
    source_fingerprint_paths = _source_fingerprint_paths(observation.files)

    nodes = {}
    for process in observation.processes:
        _add_node(nodes, _process_node(process))
    for fingerprint in observation.files:
        _add_node(nodes, _file_node(fingerprint.path, fingerprint.roles))

    edges = []
    for event in observation.sources:
        process = observation.processes[event.process_index]
        xtrace = observation.xtrace[event.xtrace_index]
        from_node = _edge_from_node(event, process)
        to_node = _edge_to_node(event, source_fingerprint_paths)
        _add_node(nodes, from_node)
        _add_node(nodes, to_node)
        edges.append({
            "index": event.index,
            "source_identity": event.source_identity,
            "process_index": event.process_index,
            "from": from_node["id"],
            "to": to_node["id"],
            "resolved_path": event.resolved_path,
            "status": event.status,
            "arguments": list(event.arguments),
            "call_site": event.call_site.to_dict(),
            "function_stack": list(event.function_stack),
            "function_call": event.function_call.to_dict() if event.function_call is not None else None,
            "xtrace": xtrace.to_dict(),
        })

    node_list = [nodes[key] for key in sorted(nodes)]
    return {
        "version": GRAPH_VERSION,
        "entrypoint": observation.entrypoint,
        "observation_version": observation.version,
        "environment": observation.environment.to_dict(),
        "run": observation.run.to_dict(),
        "summary": {
            "processes": len(observation.processes),
            "nodes": len(node_list),
            "edges": len(edges),
            "trusted_xtrace_edges": len(edges),
        },
        "nodes": node_list,
        "edges": edges,
        "files": [fingerprint.to_dict() for fingerprint in observation.files],
    }


def build_observed_source_graph_from_observation_file(
    entrypoint: str | os.PathLike,
    observation_path: str | os.PathLike,
    *,
    validate_fingerprints=True,
):
    return build_observed_source_graph(
        entrypoint,
        load_observation(observation_path),
        validate_fingerprints=validate_fingerprints,
    )


def write_observed_source_graph(graph: dict, path: str | os.PathLike):
    graph = validate_observed_source_graph(graph)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    return target


def build_observed_source_graph_review(graph, *, validate_fingerprints=True):
    graph = ensure_graph_fingerprints_current(graph) if validate_fingerprints else validate_observed_source_graph(graph)
    lines = [
        "modash runtime source graph review",
        f"entrypoint: {graph['entrypoint']}",
        f"observation_schema: {graph['observation_version']}",
        (
            "run: "
            f"observed_at={graph['run']['observed_at_utc']} "
            f"shell={graph['run']['shell']} "
            f"timeout={_display_timeout(graph['run']['timeout_seconds'])} "
            f"python={graph['run']['python_version']} "
            f"modash={graph['run']['modash_version']}"
        ),
        (
            "environment: "
            f"policy={graph['environment']['policy']} "
            f"recorded_keys={','.join(graph['environment']['recorded_keys']) or '-'}"
        ),
        (
            "summary: "
            f"processes={graph['summary']['processes']} "
            f"nodes={graph['summary']['nodes']} "
            f"edges={graph['summary']['edges']} "
            f"fingerprinted_files={len(graph['files'])}"
        ),
        "trusted: yes",
        "trust_checks:",
        "- graph schema and node/edge references are valid",
        "- every edge has linked xtrace provenance with matching source identity",
        "- every file-backed source edge has a source fingerprint",
        "- every file-backed call site has a call-site fingerprint",
        "- graph fingerprints are current",
    ]
    missing_edges = [edge for edge in graph["edges"] if edge["to"].startswith("missing-source:")]
    if missing_edges:
        lines.append("- missing-source edge targets are still absent")

    lines.append("edges:")
    if graph["edges"]:
        for edge in graph["edges"]:
            lines.extend(_review_edge_lines(edge))
    else:
        lines.append("- none")

    lines.append("fingerprints:")
    for fingerprint in graph["files"]:
        lines.append(
            "- "
            f"{fingerprint['path']} "
            f"roles={','.join(fingerprint['roles'])} "
            f"size={fingerprint['size']} "
            f"mtime_ns={fingerprint['mtime_ns']} "
            f"sha256={fingerprint['sha256']}"
        )
    return "\n".join(lines) + "\n"


def write_observed_source_graph_review(graph: dict, path: str | os.PathLike, *, validate_fingerprints=True):
    report = build_observed_source_graph_review(graph, validate_fingerprints=validate_fingerprints)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    return target


def load_observed_source_graph(path: str | os.PathLike):
    graph_path = Path(path)
    if not graph_path.is_file():
        raise RuntimeSourceGraphError(
            f"runtime source graph file does not exist: {graph_path}",
            code="runtime.graph.missing",
        )
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeSourceGraphError(
            f"invalid runtime source graph JSON: {graph_path}: {exc}",
            code="runtime.graph.invalid_json",
        ) from exc
    return validate_observed_source_graph(data)


def validate_observed_source_graph(data):
    if not isinstance(data, dict):
        raise RuntimeSourceGraphError("runtime source graph must be a JSON object")
    _require_keys(data, GRAPH_TOP_LEVEL_KEYS, "runtime source graph")
    if data["version"] != GRAPH_VERSION:
        raise RuntimeSourceGraphError(f"runtime source graph version must be {GRAPH_VERSION}")
    entrypoint = _absolute_path(data["entrypoint"], "entrypoint")
    if _nonnegative_int(data["observation_version"], "observation_version") != OBSERVATION_VERSION:
        raise RuntimeSourceGraphError(f"observation_version must be {OBSERVATION_VERSION}")
    EnvironmentInfo.from_dict(data["environment"])
    run = RuntimeRunInfo.from_dict(data["run"])
    _ensure_successful_graph_run(run)
    summary = _summary(data["summary"])
    nodes = _object_list(data["nodes"], "nodes")
    edges = _object_list(data["edges"], "edges")
    files = tuple(RuntimeFileFingerprint.from_dict(item) for item in _object_list(data["files"], "files"))

    file_roles = _fingerprint_roles_by_path(files)
    if not files:
        raise RuntimeSourceGraphError("files must contain at least one file fingerprint")
    _require_fingerprint_role(file_roles, entrypoint, "entrypoint", "entrypoint")

    node_ids = {}
    process_nodes = {}
    file_nodes = {}
    process_command_nodes = {}
    missing_source_node_ids = set()
    for node in nodes:
        node_id = _nonempty_string(node.get("id"), "nodes[].id")
        if node_id in node_ids:
            raise RuntimeSourceGraphError("nodes[].id values must be unique")
        kind = _nonempty_string(node.get("kind"), "nodes[].kind")
        node_ids[node_id] = node
        if kind == "process":
            process_index = _process_node_schema(node)
            process_nodes[process_index] = node
            continue
        if kind == "file":
            path = _file_node_schema(node, file_roles)
            file_nodes[path] = node
            continue
        if kind == "process-command":
            process_index = _process_command_node_schema(node)
            process_command_nodes[node_id] = node
            continue
        if kind == "missing-source":
            _missing_source_node_schema(node)
            missing_source_node_ids.add(node_id)
            continue
        raise RuntimeSourceGraphError(f"nodes[].kind contains unsupported value: {kind}")

    _validate_process_nodes(process_nodes)
    _validate_process_command_nodes(process_command_nodes, process_nodes)
    if set(file_nodes) != set(file_roles):
        raise RuntimeSourceGraphError("file nodes must exactly match fingerprinted files")
    _validate_summary(summary, nodes, edges, process_nodes)

    xtrace_indexes = []
    source_identities = set()
    referenced_process_command_nodes = set()
    referenced_missing_source_nodes = set()
    for expected_index, edge in enumerate(edges):
        _require_keys(edge, GRAPH_EDGE_KEYS, "edge")
        if _nonnegative_int(edge.get("index"), "edges[].index") != expected_index:
            raise RuntimeSourceGraphError("edges must be indexed contiguously from 0")
        source_identity = _nonempty_string(edge.get("source_identity"), "edges[].source_identity")
        if edge.get("from") not in node_ids:
            raise RuntimeSourceGraphError("edges[].from must reference an existing node")
        if edge.get("to") not in node_ids:
            raise RuntimeSourceGraphError("edges[].to must reference an existing node")
        process_index = _nonnegative_int(edge.get("process_index"), "edges[].process_index")
        if process_index not in process_nodes:
            raise RuntimeSourceGraphError("edges[].process_index must reference an existing process node")
        if source_identity in source_identities:
            raise RuntimeSourceGraphError("edges[].source_identity values must be unique")
        source_identities.add(source_identity)
        resolved_path = _absolute_path(edge.get("resolved_path"), "edges[].resolved_path")
        status = _nonnegative_int(edge.get("status"), "edges[].status")
        _string_list(edge.get("arguments"), "edges[].arguments")
        call_site = _call_site(edge.get("call_site"))
        function_stack = _string_list(edge.get("function_stack"), "edges[].function_stack")
        function_call = _function_call(edge.get("function_call"))
        _validate_function_call(function_call, function_stack, file_roles)
        xtrace = _xtrace(edge.get("xtrace"))
        xtrace_indexes.append(xtrace["index"])
        _validate_edge_from_node(edge, node_ids[edge["from"]], process_index, call_site, file_roles)
        _validate_edge_to_node(edge, node_ids[edge["to"]], resolved_path, status, file_roles)
        _validate_edge_xtrace(edge, xtrace, process_index, call_site, source_identity)
        if node_ids[edge["from"]]["kind"] == "process-command":
            referenced_process_command_nodes.add(edge["from"])
        if node_ids[edge["to"]]["kind"] == "missing-source":
            referenced_missing_source_nodes.add(edge["to"])

    if sorted(xtrace_indexes) != list(range(len(edges))):
        raise RuntimeSourceGraphError("edges[].xtrace.index values must be unique and contiguous from 0")
    if set(process_command_nodes) != referenced_process_command_nodes:
        raise RuntimeSourceGraphError("process-command nodes must be referenced by graph edges")
    if missing_source_node_ids != referenced_missing_source_nodes:
        raise RuntimeSourceGraphError("missing-source nodes must be referenced by graph edges")
    return data


def ensure_graph_fingerprints_current(graph):
    graph = validate_observed_source_graph(graph)
    for fingerprint_data in graph["files"]:
        fingerprint = RuntimeFileFingerprint.from_dict(fingerprint_data)
        mismatch = current_fingerprint_mismatch_details(fingerprint)
        if mismatch is not None:
            raise RuntimeSourceGraphError(
                format_fingerprint_mismatch("runtime source graph", fingerprint, mismatch),
                code="runtime.graph.stale_observation",
            )
    _ensure_missing_source_edges_still_missing(graph)
    return graph


def _review_edge_lines(edge):
    call_site = edge["call_site"]
    function_call = edge["function_call"]
    xtrace = edge["xtrace"]
    arguments = " ".join(_shell_quote(argument) for argument in edge["arguments"]) or "-"
    lines = [
        (
            "- "
            f"edge[{edge['index']}] "
            f"identity={edge['source_identity']} "
            f"status={edge['status']} "
            f"args={arguments}"
        ),
        f"  from: {edge['from']}",
        f"  to: {edge['to']}",
        f"  resolved: {edge['resolved_path']}",
        f"  call_site: {call_site['file']}:{call_site['line']}: {call_site['command']}",
    ]
    if function_call is not None:
        lines.append(
            "  helper_call: "
            f"{function_call['file']}:{function_call['line']}: "
            f"{function_call['command']}"
        )
    lines.append(f"  xtrace: {xtrace['file']}:{xtrace['line']}: {xtrace['command']}")
    return lines


def _shell_quote(value: str):
    if value and all(character.isalnum() or character in "@%_+=:,./-" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _display_timeout(value):
    if value is None:
        return "none"
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _summary(value):
    _require_keys(value, GRAPH_SUMMARY_KEYS, "summary")
    return {
        "processes": _nonnegative_int(value["processes"], "summary.processes"),
        "nodes": _nonnegative_int(value["nodes"], "summary.nodes"),
        "edges": _nonnegative_int(value["edges"], "summary.edges"),
        "trusted_xtrace_edges": _nonnegative_int(
            value["trusted_xtrace_edges"],
            "summary.trusted_xtrace_edges",
        ),
    }


def _fingerprint_roles_by_path(files):
    roles_by_path = {}
    for fingerprint in files:
        if fingerprint.path in roles_by_path:
            raise RuntimeSourceGraphError("files[].path values must be unique")
        roles_by_path[fingerprint.path] = set(fingerprint.roles)
    return roles_by_path


def _source_fingerprint_paths(files):
    return {
        fingerprint.path
        for fingerprint in files
        if "source" in fingerprint.roles
    }


def _require_fingerprint_role(roles_by_path, path: str, role: str, label: str):
    roles = roles_by_path.get(str(Path(path).resolve(strict=False)))
    if role not in (roles or ()):
        raise RuntimeSourceGraphError(f"{label} must have a file fingerprint with role {role}")


def _validate_summary(summary, nodes, edges, process_nodes):
    if summary["processes"] != len(process_nodes):
        raise RuntimeSourceGraphError("summary.processes must match process node count")
    if summary["nodes"] != len(nodes):
        raise RuntimeSourceGraphError("summary.nodes must match node count")
    if summary["edges"] != len(edges):
        raise RuntimeSourceGraphError("summary.edges must match edge count")
    if summary["trusted_xtrace_edges"] != len(edges):
        raise RuntimeSourceGraphError("summary.trusted_xtrace_edges must match edge count")


def _validate_process_nodes(process_nodes):
    if set(process_nodes) != set(range(len(process_nodes))):
        raise RuntimeSourceGraphError("process nodes must be indexed contiguously from 0")
    for process in process_nodes.values():
        parent_index = process["parent_index"]
        if parent_index is not None and parent_index not in process_nodes:
            raise RuntimeSourceGraphError("process node parent_index must reference an existing process node")


def _validate_process_command_nodes(process_command_nodes, process_nodes):
    for node in process_command_nodes.values():
        process_index = node["process_index"]
        process = process_nodes.get(process_index)
        if process is None:
            raise RuntimeSourceGraphError(
                "process-command nodes must reference an existing process node",
            )
        if (
            node["command"] != process["command"]
            or node["entrypoint"] != process["entrypoint"]
            or node["cwd"] != process["cwd"]
        ):
            raise RuntimeSourceGraphError(
                "process-command nodes must match their referenced process node",
            )


def _process_node_schema(node):
    _require_keys(node, PROCESS_NODE_KEYS, "process node")
    process_index = _nonnegative_int(node["process_index"], "nodes[].process_index")
    expected_id = f"process:{process_index}"
    if node["id"] != expected_id:
        raise RuntimeSourceGraphError("process node id must match process_index")
    _positive_int(node["pid"], "nodes[].pid")
    if node["parent_index"] is not None:
        _nonnegative_int(node["parent_index"], "nodes[].parent_index")
    _absolute_path(node["entrypoint"], "nodes[].entrypoint")
    _absolute_path(node["cwd"], "nodes[].cwd")
    _string_list(node["argv"], "nodes[].argv")
    _nonempty_string(node["command"], "nodes[].command")
    return process_index


def _file_node_schema(node, file_roles):
    _require_keys(node, FILE_NODE_KEYS, "file node")
    path = _absolute_path(node["path"], "nodes[].path")
    if node["id"] != f"file:{path}":
        raise RuntimeSourceGraphError("file node id must match path")
    roles = _file_roles(node["roles"], "nodes[].roles")
    expected_roles = file_roles.get(path)
    if expected_roles is None:
        raise RuntimeSourceGraphError("file node path must have a file fingerprint")
    if set(roles) != expected_roles:
        raise RuntimeSourceGraphError("file node roles must match file fingerprint roles")
    return path


def _process_command_node_schema(node):
    _require_keys(node, PROCESS_COMMAND_NODE_KEYS, "process-command node")
    process_index = _nonnegative_int(node["process_index"], "nodes[].process_index")
    if node["id"] != f"process-command:{process_index}":
        raise RuntimeSourceGraphError("process-command node id must match process_index")
    _nonempty_string(node["command"], "nodes[].command")
    _absolute_path(node["entrypoint"], "nodes[].entrypoint")
    _absolute_path(node["cwd"], "nodes[].cwd")
    return process_index


def _missing_source_node_schema(node):
    _require_keys(node, MISSING_SOURCE_NODE_KEYS, "missing-source node")
    _missing_source_node_index(node["id"])
    path = _absolute_path(node["path"], "nodes[].path")
    status = _positive_int(node["status"], "nodes[].status")
    return {"path": path, "status": status}


def _missing_source_node_index(node_id: str):
    prefix = "missing-source:"
    if not node_id.startswith(prefix):
        raise RuntimeSourceGraphError("missing-source node id must include its edge index")
    return _nonnegative_int(_parse_int(node_id[len(prefix):], "missing-source node id"), "nodes[].id")


def _validate_edge_from_node(edge, from_node, process_index, call_site, file_roles):
    kind = from_node["kind"]
    if kind == "file":
        if from_node["path"] != call_site["file"]:
            raise RuntimeSourceGraphError("file-backed edge source must match call_site.file")
        _require_fingerprint_role(file_roles, call_site["file"], "call-site", "edges[].call_site.file")
        return
    if kind == "process-command":
        if from_node["process_index"] != process_index:
            raise RuntimeSourceGraphError("process-command edge source must match edge process_index")
        if from_node["entrypoint"] != call_site["file"]:
            raise RuntimeSourceGraphError("process-command edge source must match call_site.file")
        return
    raise RuntimeSourceGraphError("edges[].from must reference a file or process-command node")


def _validate_edge_to_node(edge, to_node, resolved_path, status, file_roles):
    kind = to_node["kind"]
    if kind == "file":
        if to_node["path"] != resolved_path:
            raise RuntimeSourceGraphError("file edge target must match resolved_path")
        _require_fingerprint_role(file_roles, resolved_path, "source", "edges[].resolved_path")
        return
    if kind == "missing-source":
        if edge["to"] != f"missing-source:{edge['index']}":
            raise RuntimeSourceGraphError("missing-source edge target id must match edge index")
        if to_node["path"] != resolved_path:
            raise RuntimeSourceGraphError("missing-source edge target must match resolved_path")
        if to_node["status"] != status:
            raise RuntimeSourceGraphError("missing-source edge target status must match edge status")
        if status == 0:
            raise RuntimeSourceGraphError("missing-source edge target requires non-zero source status")
        return
    raise RuntimeSourceGraphError("edges[].to must reference a file or missing-source node")


def _validate_edge_xtrace(edge, xtrace, process_index, call_site, source_identity):
    if xtrace["source_identity"] != source_identity:
        raise RuntimeSourceGraphError("edges[].xtrace.source_identity must match edge source_identity")
    if xtrace["process_index"] != process_index:
        raise RuntimeSourceGraphError("edges[].xtrace.process_index must match edge process_index")
    if xtrace["file"] != call_site["file"] or xtrace["line"] != call_site["line"]:
        raise RuntimeSourceGraphError("edges[].xtrace must match edge call_site")
    if not _is_replayable_source_call_site(call_site["command"]):
        raise RuntimeSourceGraphError("edges[].call_site.command must be a replayable source command")
    if not _is_trusted_xtrace_source_command(xtrace["command"]):
        raise RuntimeSourceGraphError("edges[].xtrace.command must be a source-like command")


def _ensure_missing_source_edges_still_missing(graph):
    for edge in graph["edges"]:
        if not edge["to"].startswith("missing-source:"):
            continue
        if Path(edge["resolved_path"]).is_file():
            raise RuntimeSourceGraphError(
                "runtime source graph is stale for "
                f"{edge['resolved_path']}: source_presence mismatch; "
                "expected absent missing-source edge target; current file exists",
                code="runtime.graph.stale_observation",
            )


def _ensure_source_presence_matches_fingerprints(observation: RuntimeSourceObservation):
    source_paths = _source_fingerprint_paths(observation.files)
    for event in observation.sources:
        resolved_path = str(Path(event.resolved_path).resolve(strict=False))
        if resolved_path not in source_paths and Path(resolved_path).is_file():
            raise RuntimeSourceGraphError(
                f"runtime source observation is stale for {resolved_path}: source_presence mismatch",
                code="runtime.graph.stale_observation",
            )


def _file_roles(value, label: str):
    if not isinstance(value, list):
        raise RuntimeSourceGraphError(f"{label} must be a list")
    roles = []
    for item in value:
        role = _nonempty_string(item, f"{label}[]")
        if role not in FILE_FINGERPRINT_ROLES:
            raise RuntimeSourceGraphError(f"{label} contains unsupported role: {role}")
        if role in roles:
            raise RuntimeSourceGraphError(f"{label} values must be unique")
        roles.append(role)
    if not roles:
        raise RuntimeSourceGraphError(f"{label} must contain at least one role")
    if roles != sorted(roles):
        raise RuntimeSourceGraphError(f"{label} must be sorted")
    return tuple(roles)


def _is_source_like_command(command: str):
    return source_command_invocation(command.strip()) is not None


def _is_replayable_source_call_site(command: str):
    return source_command_invocation(command.strip()) is not None


def _is_trusted_xtrace_source_command(command: str):
    return _is_source_like_command(command) or _is_trace_wrapper_source_command(command)


def _is_trace_wrapper_source_command(command: str):
    return is_trace_wrapper_source_command(command)


def _coerce_observation(observation):
    if isinstance(observation, RuntimeSourceObservation):
        return observation
    return validate_observation(observation)


def _ensure_graph_entrypoint(entrypoint_path: Path, observation: RuntimeSourceObservation):
    observed_entrypoint = Path(observation.entrypoint).resolve(strict=False)
    if observed_entrypoint != entrypoint_path:
        raise RuntimeSourceGraphError(
            f"observation entrypoint does not match requested entrypoint: {observed_entrypoint}",
            code="runtime.graph.entrypoint_mismatch",
        )


def _ensure_successful_observation_run(run: RuntimeRunInfo):
    if run.target_status != 0:
        raise RuntimeSourceGraphError(
            f"runtime source observation target exited with status {run.target_status}; refusing trusted graph promotion",
            code="runtime.graph.nonzero_trace",
        )


def _ensure_successful_graph_run(run: RuntimeRunInfo):
    if run.target_status != 0:
        raise RuntimeSourceGraphError(
            f"runtime source graph target exited with status {run.target_status}; refusing trusted graph replay",
            code="runtime.graph.nonzero_trace",
        )


def _ensure_fingerprints_current(observation: RuntimeSourceObservation):
    for fingerprint in observation.files:
        mismatch = current_fingerprint_mismatch_details(fingerprint)
        if mismatch is not None:
            raise RuntimeSourceGraphError(
                format_fingerprint_mismatch("runtime source observation", fingerprint, mismatch),
                code="runtime.graph.stale_observation",
            )


def _ensure_trusted_xtrace_links(observation: RuntimeSourceObservation):
    if observation.sources and not observation.xtrace:
        raise RuntimeSourceGraphError(
            "runtime source graph requires trusted xtrace provenance for every source event",
            code="runtime.graph.untrusted_observation",
        )
    for event in observation.sources:
        if event.xtrace_index is None:
            raise RuntimeSourceGraphError(
                "runtime source graph requires every source event to reference xtrace provenance",
                code="runtime.graph.untrusted_observation",
            )


def _add_node(nodes: dict[str, dict], node: dict):
    existing = nodes.get(node["id"])
    if existing is None:
        nodes[node["id"]] = node
        return
    existing_roles = existing.get("roles")
    new_roles = node.get("roles")
    if existing_roles is not None and new_roles is not None:
        existing["roles"] = sorted(set(existing_roles) | set(new_roles))


def _process_node(process):
    return {
        "id": f"process:{process.index}",
        "kind": "process",
        "process_index": process.index,
        "pid": process.pid,
        "parent_index": process.parent_index,
        "entrypoint": process.entrypoint,
        "cwd": process.cwd,
        "argv": list(process.argv),
        "command": process.command,
    }


def _file_node(path: str, roles=()):
    resolved = str(Path(path).resolve(strict=False))
    return {
        "id": f"file:{resolved}",
        "kind": "file",
        "path": resolved,
        "roles": sorted(set(roles)),
    }


def _process_command_node(process):
    return {
        "id": f"process-command:{process.index}",
        "kind": "process-command",
        "process_index": process.index,
        "command": process.command,
        "entrypoint": process.entrypoint,
        "cwd": process.cwd,
    }


def _missing_source_node(event):
    return {
        "id": f"missing-source:{event.index}",
        "kind": "missing-source",
        "path": event.resolved_path,
        "status": event.status,
    }


def _edge_from_node(event, process):
    if event.call_site.file == process.entrypoint and process.command != process.entrypoint:
        return _process_command_node(process)
    return _file_node(event.call_site.file)


def _edge_to_node(event, source_fingerprint_paths):
    if event.resolved_path in source_fingerprint_paths:
        return _file_node(event.resolved_path)
    return _missing_source_node(event)


def _require_keys(data, expected_keys, label: str):
    if not isinstance(data, dict):
        raise RuntimeSourceGraphError(f"{label} must be an object")
    missing = sorted(expected_keys - set(data))
    if missing:
        raise RuntimeSourceGraphError(f"{label} missing required keys: {', '.join(missing)}")
    unknown = sorted(set(data) - expected_keys)
    if unknown:
        raise RuntimeSourceGraphError(f"{label} has unknown keys: {', '.join(unknown)}")


def _object_list(value, label: str):
    if not isinstance(value, list):
        raise RuntimeSourceGraphError(f"{label} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeSourceGraphError(f"{label}[{index}] must be an object")
    return value


def _string_list(value, label: str):
    if not isinstance(value, list):
        raise RuntimeSourceGraphError(f"{label} must be a list")
    return tuple(_exact_string(item, f"{label}[]") for item in value)


def _call_site(value):
    _require_keys(value, frozenset({"file", "line", "command"}), "edges[].call_site")
    return {
        "file": _absolute_path(value["file"], "edges[].call_site.file"),
        "line": _positive_int(value["line"], "edges[].call_site.line"),
        "command": _nonempty_string(value["command"], "edges[].call_site.command"),
    }


def _function_call(value):
    if value is None:
        return None
    _require_keys(value, FUNCTION_CALL_KEYS, "edges[].function_call")
    return {
        "file": _absolute_path(value["file"], "edges[].function_call.file"),
        "line": _positive_int(value["line"], "edges[].function_call.line"),
        "function": _nonempty_string(value["function"], "edges[].function_call.function"),
        "command": _nonempty_string(value["command"], "edges[].function_call.command"),
        "arguments": _string_list(value["arguments"], "edges[].function_call.arguments"),
    }


def _validate_function_call(function_call, function_stack, file_roles):
    if function_call is None:
        return
    if str(Path(function_call["file"]).resolve(strict=False)) not in file_roles:
        raise RuntimeSourceGraphError(
            "edges[].function_call.file must have a file fingerprint",
        )
    if function_call["function"] not in function_stack:
        raise RuntimeSourceGraphError(
            "edges[].function_call.function must be present in edges[].function_stack",
        )
    parsed = _function_call_command(function_call["command"])
    if parsed is None:
        raise RuntimeSourceGraphError("edges[].function_call.command must be a shell function call")
    function_name, arguments = parsed
    if function_name != function_call["function"] or arguments != function_call["arguments"]:
        raise RuntimeSourceGraphError(
            "edges[].function_call.command must match function and arguments",
        )


def _function_call_command(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command)
    except Exception:
        return None
    if not words:
        return None
    function_name = strip_shell_word_quotes(words[0])
    if not function_name:
        return None
    arguments = tuple(strip_shell_word_quotes(word) for word in words[1:])
    return function_name, arguments


def _xtrace(value):
    _require_keys(
        value,
        frozenset({"index", "source_identity", "process_index", "file", "line", "function", "cwd", "command"}),
        "edges[].xtrace",
    )
    return {
        "index": _nonnegative_int(value["index"], "edges[].xtrace.index"),
        "source_identity": _nonempty_string(value["source_identity"], "edges[].xtrace.source_identity"),
        "process_index": _nonnegative_int(value["process_index"], "edges[].xtrace.process_index"),
        "file": _absolute_path(value["file"], "edges[].xtrace.file"),
        "line": _positive_int(value["line"], "edges[].xtrace.line"),
        "function": _exact_string(value["function"], "edges[].xtrace.function"),
        "cwd": _absolute_path(value["cwd"], "edges[].xtrace.cwd"),
        "command": _nonempty_string(value["command"], "edges[].xtrace.command"),
    }


def _absolute_path(value, label: str):
    value = _nonempty_string(value, label)
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        raise RuntimeSourceGraphError(f"{label} must be an absolute path")
    return str(candidate.resolve(strict=False))


def _exact_string(value, label: str):
    if not isinstance(value, str):
        raise RuntimeSourceGraphError(f"{label} values must be strings")
    if "\0" in value:
        raise RuntimeSourceGraphError(f"{label} values must not contain NUL bytes")
    return value


def _nonempty_string(value, label: str):
    value = _exact_string(value, label)
    if not value:
        raise RuntimeSourceGraphError(f"{label} must not be empty")
    return value


def _positive_int(value, label: str):
    value = _integer(value, label)
    if value < 1:
        raise RuntimeSourceGraphError(f"{label} must be greater than 0")
    return value


def _nonnegative_int(value, label: str):
    value = _integer(value, label)
    if value < 0:
        raise RuntimeSourceGraphError(f"{label} must be greater than or equal to 0")
    return value


def _integer(value, label: str):
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeSourceGraphError(f"{label} must be an integer")
    return value


def _parse_int(value: str, label: str):
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeSourceGraphError(f"{label} must be an integer") from exc
