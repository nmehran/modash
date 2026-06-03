import os

from methods.compile_renderer import (
    SET_SHEBANG,
    assert_no_unresolved_source_sites,
    context_from_source_events,
    context_paths_from_source_events,
    find_unquoted_substring,
    line_contains_unresolved_source,
    render_context_files,
    render_executable_script,
    replace_runtime_source_references,
    write_output,
)
from methods.source_evaluator import SourceEvaluator
from methods.source_supplements import load_source_supplement
from methods.sources import validate_path


def compile_sources(
    entry_point: str,
    output_file: str,
    mode: str = "context",
    source_supplement=None,
):
    if mode not in {"context", "executable"}:
        raise ValueError(f"Unsupported compile mode: {mode}")

    if not validate_path(entry_point):
        raise FileNotFoundError(f"Error: Could not resolve the path to the entry point - {entry_point}")

    if not os.path.isfile(entry_point):
        raise OSError(f"Error: entry point must be a file - {entry_point}")

    entry_point = os.path.abspath(entry_point)
    supplement = load_source_supplement(source_supplement, os.path.dirname(entry_point))
    evaluation = SourceEvaluator(
        mode=mode,
        source_supplement=supplement,
    ).evaluate(entry_point)
    context = context_from_source_events(evaluation.events, evaluation.disabled_sources, evaluation.line_replacements)
    if mode == "executable":
        output = render_executable_script(entry_point, context)
    else:
        sources = context_paths_from_source_events(entry_point, evaluation.events)
        output = render_context_files(sources, entry_point, context)
    content = '\n'.join(output)
    write_output(output_file, content)
