from __future__ import annotations

from methods.runtime_evaluator.observation_model import RuntimeSourceObservationError

GRAPH_VERSION = 3
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
    "source_entry_status",
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
