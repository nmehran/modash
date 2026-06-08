import os

from methods.compile_context import (
    context_from_source_events,
    context_paths_from_source_events,
    render_context_files,
    write_output,
)
from methods.compile_renderer import (
    SET_SHEBANG,
    assert_no_unresolved_source_sites,
    find_unquoted_substring,
    line_contains_unresolved_source,
    render_executable_script,
    replace_runtime_source_references,
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

    entry_point_value = os.fspath(entry_point)
    if not validate_path(entry_point_value):
        raise FileNotFoundError(f"Error: Could not resolve the path to the entry point - {entry_point_value}")

    if not os.path.isfile(entry_point_value):
        raise OSError(f"Error: entry point must be a file - {entry_point_value}")

    entry_point = os.path.abspath(entry_point_value)
    supplement = load_source_supplement(source_supplement, os.path.dirname(entry_point))
    evaluation = SourceEvaluator(
        mode=mode,
        source_supplement=supplement,
    ).evaluate(entry_point, entrypoint_source_value=entry_point_value)
    context = context_from_source_events(evaluation.events, evaluation.disabled_sources, evaluation.line_replacements)
    if mode == "executable":
        output = render_executable_script(entry_point, context, entrypoint_source_value=entry_point_value)
    else:
        sources = context_paths_from_source_events(entry_point, evaluation.events)
        output = render_context_files(sources, entry_point, context)
    content = '\n'.join(output)
    write_output(output_file, content)
    if mode == "executable":
        current_mode = os.stat(output_file).st_mode
        executable_bits = 0
        for read_bit, execute_bit in ((0o400, 0o100), (0o040, 0o010), (0o004, 0o001)):
            if current_mode & read_bit:
                executable_bits |= execute_bit
        os.chmod(output_file, current_mode | executable_bits)
