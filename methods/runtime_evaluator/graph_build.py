from __future__ import annotations

import json
import os
from pathlib import Path

from methods.runtime_evaluator.graph_model import GRAPH_VERSION, RuntimeSourceGraphError
from methods.runtime_evaluator.graph_validate import (
    _coerce_observation,
    _ensure_fingerprints_current,
    _ensure_graph_entrypoint,
    _ensure_source_presence_matches_fingerprints,
    _ensure_trusted_xtrace_links,
    _source_fingerprint_paths,
    validate_observed_source_graph,
)
from methods.runtime_evaluator.scanners import function_context_sensitive_top_level_lines
from methods.source_commands import source_command_invocation

def build_observed_source_graph(entrypoint: str | os.PathLike, observation, *, validate_fingerprints=True):
    entrypoint_path = Path(entrypoint).resolve(strict=False)
    observation = _coerce_observation(observation)
    _ensure_graph_entrypoint(entrypoint_path, observation)
    if validate_fingerprints:
        _ensure_fingerprints_current(observation)
        _ensure_source_presence_matches_fingerprints(observation)
    _ensure_no_function_context_sensitive_sources(observation)
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
            "source_value": event.source_value,
            "failure_kind": _source_failure_kind(event, xtrace, source_fingerprint_paths),
            "source_entry_status": event.source_entry_status,
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

def write_observed_source_graph(graph: dict, path: str | os.PathLike):
    graph = validate_observed_source_graph(graph)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    return target

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


def _source_failure_kind(event, xtrace, source_fingerprint_paths):
    resolved_path = str(Path(event.resolved_path).resolve(strict=False))
    invocation = source_command_invocation(xtrace.command, normalize_trace_wrappers=True)
    if invocation is not None and invocation.invalid_option is not None:
        return "invalid-option"
    if invocation is not None and invocation.source_path == "":
        return "no-argument"
    if resolved_path in source_fingerprint_paths:
        return "file"
    if not event.arguments and invocation is None:
        return "no-argument"
    resolved = Path(resolved_path)
    if resolved.is_dir():
        return "directory"
    if (resolved.exists() or resolved.is_symlink()) and not os.access(resolved, os.R_OK):
        return "unreadable"
    return "missing"


def _ensure_no_function_context_sensitive_sources(observation):
    source_paths = {
        fingerprint.path
        for fingerprint in observation.files
        if "source" in fingerprint.roles
    }
    for path in sorted(source_paths):
        lines = function_context_sensitive_top_level_lines(path)
        if not lines:
            continue
        first_line = lines[0]
        raise RuntimeSourceGraphError(
            "runtime source graph cannot trust a sourced file with top-level "
            f"function-context-sensitive Bash at {path}:{first_line}",
            code="runtime.graph.function_context_sensitive",
        )
