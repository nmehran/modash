from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path

from methods.runtime_evaluator import schema as runtime_schema
from methods.runtime_evaluator.graph_model import (
    FILE_NODE_KEYS,
    FUNCTION_CALL_KEYS,
    GRAPH_EDGE_KEYS,
    GRAPH_SUMMARY_KEYS,
    GRAPH_TOP_LEVEL_KEYS,
    GRAPH_VERSION,
    MISSING_SOURCE_NODE_KEYS,
    PROCESS_COMMAND_NODE_KEYS,
    PROCESS_NODE_KEYS,
    RuntimeSourceGraphError,
)
from methods.runtime_evaluator.observations import (
    EnvironmentInfo,
    FILE_FINGERPRINT_ROLES,
    OBSERVATION_VERSION,
    RuntimeFileFingerprint,
    RuntimeRunInfo,
    RuntimeSourceObservation,
    current_fingerprint_mismatch_details,
    format_fingerprint_mismatch,
    validate_observation,
)
from methods.compile_renderer import find_unquoted_substring
from methods.shell.line import get_commands
from methods.source_commands import is_source_like_command_text, is_trace_wrapper_source_command, source_command_invocation
from methods.source_resolver import parse_shell_words_preserving_quotes, strip_shell_word_quotes
from methods.source_words import ASSIGNMENT_WORD_PATTERN
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
    RuntimeRunInfo.from_dict(data["run"])
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
        _nonnegative_int(edge.get("source_entry_status"), "edges[].source_entry_status")
        status = _nonnegative_int(edge.get("status"), "edges[].status")
        failure_kind = _source_failure_kind(edge.get("failure_kind"))
        _string_list(edge.get("arguments"), "edges[].arguments")
        call_site = _call_site(edge.get("call_site"))
        function_stack = _string_list(edge.get("function_stack"), "edges[].function_stack")
        function_call = _function_call(edge.get("function_call"))
        _validate_function_call(function_call, function_stack, file_roles)
        xtrace = _xtrace(edge.get("xtrace"))
        xtrace_indexes.append(xtrace["index"])
        _validate_edge_from_node(edge, node_ids[edge["from"]], process_index, call_site, file_roles)
        _validate_edge_to_node(edge, node_ids[edge["to"]], resolved_path, status, failure_kind, file_roles)
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
    _ensure_source_failure_edges_still_current(graph)
    return graph

def _schema_error(message: str):
    return RuntimeSourceGraphError(message)


_require_keys = partial(runtime_schema.require_keys, error_factory=_schema_error)
_object_list = partial(runtime_schema.object_list, error_factory=_schema_error)
_string_list = partial(runtime_schema.string_list, error_factory=_schema_error)
_absolute_path = partial(runtime_schema.absolute_path, error_factory=_schema_error)
_exact_string = partial(runtime_schema.exact_string, error_factory=_schema_error)
_nonempty_string = partial(runtime_schema.nonempty_string, error_factory=_schema_error)
_positive_int = partial(runtime_schema.positive_int, error_factory=_schema_error)
_nonnegative_int = partial(runtime_schema.nonnegative_int, error_factory=_schema_error)
_integer = partial(runtime_schema.integer, error_factory=_schema_error)

SOURCE_FAILURE_KINDS = frozenset({"file", "missing", "directory", "unreadable", "no-argument", "invalid-option"})

def _source_failure_kind(value):
    failure_kind = _exact_string(value, "edges[].failure_kind")
    if failure_kind not in SOURCE_FAILURE_KINDS:
        raise RuntimeSourceGraphError(f"edges[].failure_kind contains unsupported value: {failure_kind}")
    return failure_kind

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

def _validate_edge_to_node(edge, to_node, resolved_path, status, failure_kind, file_roles):
    kind = to_node["kind"]
    if kind == "file":
        if failure_kind != "file":
            raise RuntimeSourceGraphError("file edge target requires failure_kind=file")
        if to_node["path"] != resolved_path:
            raise RuntimeSourceGraphError("file edge target must match resolved_path")
        _require_fingerprint_role(file_roles, resolved_path, "source", "edges[].resolved_path")
        return
    if kind == "missing-source":
        if failure_kind == "file":
            raise RuntimeSourceGraphError("missing-source edge target cannot use failure_kind=file")
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
    if not _is_replayable_source_call_site(call_site["command"], xtrace["command"]):
        raise RuntimeSourceGraphError("edges[].call_site.command must be a replayable source command")
    if not _is_trusted_xtrace_source_command(xtrace["command"]):
        raise RuntimeSourceGraphError("edges[].xtrace.command must be a source-like command")
    if (
        _is_assignment_prefixed_builtin_source(command=call_site["command"])
        or _is_assignment_prefixed_builtin_source(command=xtrace["command"])
    ):
        raise RuntimeSourceGraphError(
            "runtime source graph cannot trust assignment-prefixed builtin source; "
            "the trace wrapper cannot preserve caller mutation semantics",
            code="runtime.graph.nontransparent_builtin_source",
        )

def _ensure_source_failure_edges_still_current(graph):
    for edge in graph["edges"]:
        failure_kind = edge["failure_kind"]
        if failure_kind == "file":
            continue
        path = Path(edge["resolved_path"])
        if failure_kind == "no-argument":
            continue
        if failure_kind == "invalid-option":
            continue
        if failure_kind == "missing":
            if path.exists() or path.is_symlink():
                raise RuntimeSourceGraphError(
                    "runtime source graph is stale for "
                    f"{edge['resolved_path']}: source_presence mismatch; "
                    "expected absent missing-source edge target; current path exists",
                    code="runtime.graph.stale_observation",
                )
            continue
        if failure_kind == "directory":
            if not path.is_dir():
                raise RuntimeSourceGraphError(
                    "runtime source graph is stale for "
                    f"{edge['resolved_path']}: source_failure_kind mismatch; "
                    "expected directory source failure",
                    code="runtime.graph.stale_observation",
                )
            continue
        if failure_kind == "unreadable":
            if not (path.exists() or path.is_symlink()) or os.access(path, os.R_OK):
                raise RuntimeSourceGraphError(
                    "runtime source graph is stale for "
                    f"{edge['resolved_path']}: source_failure_kind mismatch; "
                    "expected unreadable source failure",
                    code="runtime.graph.stale_observation",
                )
            continue
        raise RuntimeSourceGraphError(f"edges[].failure_kind contains unsupported value: {failure_kind}")

def _ensure_source_presence_matches_fingerprints(observation: RuntimeSourceObservation):
    source_paths = _source_fingerprint_paths(observation.files)
    for event in observation.sources:
        resolved_path = str(Path(event.resolved_path).resolve(strict=False))
        invocation = _event_xtrace_invocation(observation, event)
        if invocation is not None and (invocation.invalid_option is not None or invocation.source_path == ""):
            continue
        if (
            resolved_path not in source_paths
            and Path(resolved_path).is_file()
            and os.access(resolved_path, os.R_OK)
        ):
            raise RuntimeSourceGraphError(
                f"runtime source observation is stale for {resolved_path}: source_presence mismatch",
                code="runtime.graph.stale_observation",
            )


def _event_xtrace_invocation(observation: RuntimeSourceObservation, event):
    if event.xtrace_index is None:
        return None
    try:
        command = observation.xtrace[event.xtrace_index].command
    except IndexError:
        return None
    return source_command_invocation(command, normalize_trace_wrappers=True)


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
    return is_source_like_command_text(command.strip())

def _is_replayable_source_call_site(command: str, xtrace_command: str):
    stripped = command.strip()
    if source_command_invocation(stripped) is not None:
        return True
    if find_unquoted_substring(stripped, xtrace_command.strip()) >= 0:
        return True
    return any(source_command_invocation(part.strip()) is not None for part in get_commands(stripped))

def _is_trusted_xtrace_source_command(command: str):
    return _is_source_like_command(command) or _is_trace_wrapper_source_command(command)

def _is_trace_wrapper_source_command(command: str):
    return is_trace_wrapper_source_command(command)


def _is_assignment_prefixed_builtin_source(command: str):
    for segment in get_commands(command.strip()):
        invocation = source_command_invocation(segment.strip(), stop_at_shell_control=True)
        if invocation is None:
            continue
        words = invocation.words
        builtin_index = None
        for index in range(invocation.source_index):
            if words[index] == "builtin":
                builtin_index = index
                break
        if builtin_index is None:
            continue
        if any(ASSIGNMENT_WORD_PATTERN.match(word) for word in words[:builtin_index]):
            return True
    return False

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

def _parse_int(value: str, label: str):
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeSourceGraphError(f"{label} must be an integer") from exc
