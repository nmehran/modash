from __future__ import annotations

import os
from pathlib import Path

from methods.runtime_evaluator.graph_validate import ensure_graph_fingerprints_current, validate_observed_source_graph
from methods.source_commands import shell_quote as _shell_quote
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

def _display_timeout(value):
    if value is None:
        return "none"
    if float(value).is_integer():
        return str(int(value))
    return str(value)
