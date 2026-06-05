from __future__ import annotations

import os
from pathlib import Path

from methods.runtime_evaluator.compiler_model import (
    ENTRYPOINT_LOGICAL_PATH,
    RUNTIME_COMPILER_VERSION,
    RuntimeObservedCompileError,
    _EmbeddedFile,
)
from methods.runtime_evaluator.compiler_plan import _build_compile_plan
from methods.runtime_evaluator.compiler_prelude import _render_replay_prelude, _target_logical_paths
from methods.runtime_evaluator.compiler_rewrite import _rewrite_file_units, _rewrite_process_payloads
from methods.runtime_evaluator.compiler_safety import (
    validate_graph_source_file_safety,
    validate_runtime_compile_safety,
)
from methods.runtime_evaluator.graph import ensure_graph_fingerprints_current, validate_observed_source_graph


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
    validate_graph_source_file_safety(graph)

    plan = _build_compile_plan(entrypoint_path, graph)
    validate_runtime_compile_safety(plan)
    _rewrite_process_payloads(plan)
    _rewrite_file_units(plan)
    main_plan = plan.process_plans[0]
    embedded_files = tuple(
        _EmbeddedFile(unit.logical_path, unit.transformed or unit.content)
        for logical_path, unit in sorted(plan.file_units.items())
        if logical_path != ENTRYPOINT_LOGICAL_PATH
    )
    prelude = _render_replay_prelude(
        embedded_files,
        main_plan.assignments,
        _target_logical_paths(plan.file_units),
        environment_values=graph["environment"].get("values", {}),
        child_processes=tuple(sorted(plan.process_payloads)),
    )
    entrypoint_unit = plan.file_units[ENTRYPOINT_LOGICAL_PATH]
    return (
        prelude
        + "\n{\n"
        + (entrypoint_unit.transformed or entrypoint_unit.content).rstrip("\n")
        + "\n}\n"
        + "__modash_script_status=$?\n"
        + "__modash_verify_trap_active=1\n"
        + "__modash_verify_replay_consumed \"$__modash_script_status\"\n"
    )


def supports_runtime_graph(graph_payload: dict) -> bool:
    validate_observed_source_graph(graph_payload)
    return True


__all__ = [
    "RUNTIME_COMPILER_VERSION",
    "RuntimeObservedCompileError",
    "compile_runtime_graph",
    "render_runtime_graph_script",
    "supports_runtime_graph",
]
