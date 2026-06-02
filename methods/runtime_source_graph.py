from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_source_observations import (
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    current_fingerprint_mismatch,
    load_observation,
    validate_observation,
)

GRAPH_VERSION = 1


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
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    return target


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
