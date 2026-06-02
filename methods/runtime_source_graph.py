from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeFileFingerprint,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch,
    load_observation,
    validate_observation,
)

GRAPH_VERSION = 1
GRAPH_TOP_LEVEL_KEYS = frozenset({
    "version",
    "entrypoint",
    "observation_version",
    "summary",
    "nodes",
    "edges",
    "files",
})


class RuntimeSourceGraphError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.graph.invalid"):
        super().__init__(message, code=code)


def build_observed_source_graph(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_graph_entrypoint(entrypoint_path, observation)
    if validate_fingerprints:
        _ensure_fingerprints_current(observation)
    _ensure_trusted_xtrace_links(observation)

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
        to_node = _edge_to_node(event)
        _add_node(nodes, from_node)
        _add_node(nodes, to_node)
        edges.append({
            "index": event.index,
            "process_index": event.process_index,
            "from": from_node["id"],
            "to": to_node["id"],
            "resolved_path": event.resolved_path,
            "status": event.status,
            "arguments": list(event.arguments),
            "call_site": event.call_site.to_dict(),
            "xtrace": xtrace.to_dict(),
        })

    node_list = [nodes[key] for key in sorted(nodes)]
    return {
        "version": GRAPH_VERSION,
        "entrypoint": observation.entrypoint,
        "observation_version": observation.version,
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
    _absolute_path(data["entrypoint"], "entrypoint")
    _nonnegative_int(data["observation_version"], "observation_version")
    if not isinstance(data["summary"], dict):
        raise RuntimeSourceGraphError("summary must be an object")
    nodes = _object_list(data["nodes"], "nodes")
    edges = _object_list(data["edges"], "edges")
    files = tuple(RuntimeFileFingerprint.from_dict(item) for item in _object_list(data["files"], "files"))
    node_ids = set()
    for node in nodes:
        node_id = _nonempty_string(node.get("id"), "nodes[].id")
        node_ids.add(node_id)
        _nonempty_string(node.get("kind"), "nodes[].kind")
    for expected_index, edge in enumerate(edges):
        if _nonnegative_int(edge.get("index"), "edges[].index") != expected_index:
            raise RuntimeSourceGraphError("edges must be indexed contiguously from 0")
        if edge.get("from") not in node_ids:
            raise RuntimeSourceGraphError("edges[].from must reference an existing node")
        if edge.get("to") not in node_ids:
            raise RuntimeSourceGraphError("edges[].to must reference an existing node")
        _absolute_path(edge.get("resolved_path"), "edges[].resolved_path")
        _nonnegative_int(edge.get("process_index"), "edges[].process_index")
        _nonnegative_int(edge.get("status"), "edges[].status")
        _string_list(edge.get("arguments"), "edges[].arguments")
        _call_site(edge.get("call_site"))
        _xtrace(edge.get("xtrace"))
    if not files:
        raise RuntimeSourceGraphError("files must contain at least one file fingerprint")
    return data


def ensure_graph_fingerprints_current(graph):
    graph = validate_observed_source_graph(graph)
    for fingerprint_data in graph["files"]:
        fingerprint = RuntimeFileFingerprint.from_dict(fingerprint_data)
        mismatch = current_fingerprint_mismatch(fingerprint)
        if mismatch is not None:
            raise RuntimeSourceGraphError(
                f"runtime source graph is stale for {fingerprint.path}: {mismatch} mismatch",
                code="runtime.graph.stale_observation",
            )
    return graph


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
        mismatch = current_fingerprint_mismatch(fingerprint)
        if mismatch is not None:
            raise RuntimeSourceGraphError(
                f"runtime source observation is stale for {fingerprint.path}: {mismatch} mismatch",
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


def _edge_to_node(event):
    if event.status == 0:
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
    for item in value:
        _exact_string(item, f"{label}[]")


def _call_site(value):
    _require_keys(value, frozenset({"file", "line", "command"}), "edges[].call_site")
    _absolute_path(value["file"], "edges[].call_site.file")
    _positive_int(value["line"], "edges[].call_site.line")
    _nonempty_string(value["command"], "edges[].call_site.command")


def _xtrace(value):
    _require_keys(
        value,
        frozenset({"index", "process_index", "file", "line", "function", "cwd", "command"}),
        "edges[].xtrace",
    )
    _nonnegative_int(value["index"], "edges[].xtrace.index")
    _nonnegative_int(value["process_index"], "edges[].xtrace.process_index")
    _nonempty_string(value["file"], "edges[].xtrace.file")
    _positive_int(value["line"], "edges[].xtrace.line")
    _exact_string(value["function"], "edges[].xtrace.function")
    _absolute_path(value["cwd"], "edges[].xtrace.cwd")
    _nonempty_string(value["command"], "edges[].xtrace.command")


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
