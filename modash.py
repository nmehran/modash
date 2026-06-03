import argparse
import json
import math
import re
import sys
from pathlib import Path
from methods.compile import compile_sources
from methods.runtime_source_trace import (
    DEFAULT_TRACE_TIMEOUT_SECONDS,
    RuntimeSourceTraceError,
    default_observation_path,
    trace_sources,
    write_trace_observation,
)
from methods.runtime_source_observations import RuntimeSourceObservationError, load_observation
from methods.runtime_observation_reports import (
    RuntimeObservationReportError,
    build_observation_report,
    write_observation_report,
)
from methods.runtime_source_graph import (
    RuntimeSourceGraphError,
    build_observed_source_graph,
    load_observed_source_graph,
    write_observed_source_graph,
    write_observed_source_graph_review,
)
from methods.runtime_source_supplements import (
    RuntimeSupplementGenerationError,
    generate_source_supplement,
    generate_source_supplement_from_graph,
    load_source_supplement_from_payload,
    write_generated_supplement,
)
from methods.shell_line import get_commands
from methods.source_effects import SourceSite
from methods.source_evaluator import SourceEvaluator, SourceOverride
from methods.source_frontend import LineParserFrontend
from methods.source_resolver import (
    UnsupportedSourceError,
    parse_shell_words_preserving_quotes,
    source_command_invocation,
    strip_shell_word_quotes,
)


TOP_LEVEL_HELP_EPILOG = """\
runtime commands:
  trace             run the target and write a runtime source observation
  graph             validate an observation into a trusted runtime graph
  supplement        generate a source supplement from an observation or graph
  compile-observed  compile executable output from a trusted runtime graph
  observe-compile   explicitly trace, write review artifacts, and compile
"""
ASSIGNMENT_WORD_PATTERN = re.compile(r'^[a-zA-Z_]\w*(?:\+)?=.*$')


def main(entry_point, output_file, mode="context", source_supplement=None):
    compile_sources(entry_point, output_file, mode=mode, source_supplement=source_supplement)


def trace_main(
    entrypoint,
    *,
    script_args=None,
    cwd=None,
    env=None,
    output=None,
    output_dir=None,
    timeout=DEFAULT_TRACE_TIMEOUT_SECONDS,
):
    result = trace_sources(entrypoint, argv=script_args or (), cwd=cwd, env=env, timeout=timeout)
    output_path = output or default_observation_path(entrypoint, output_dir=output_dir)
    observation_path = write_trace_observation(result, output_path)

    _forward_trace_output(result)
    print(f"modash: trace observation: {observation_path.resolve(strict=False)}", file=sys.stderr)
    return result.returncode


def supplement_main(entrypoint, *, observation, output, report=None):
    observation_payload = load_observation(observation)
    supplement = generate_source_supplement(entrypoint, observation_payload)
    report_payload = build_observation_report(
        entrypoint,
        observation_payload,
        validate_fingerprints=False,
    )
    supplement_path = write_generated_supplement(supplement, output)
    report_path = write_observation_report(report_payload, report or f"{supplement_path}.report.json")
    print(f"modash: source supplement: {supplement_path.resolve(strict=False)}", file=sys.stderr)
    print(f"modash: observation review report: {report_path.resolve(strict=False)}", file=sys.stderr)


def graph_main(entrypoint, *, observation, output, report=None):
    observation_payload = load_observation(observation)
    graph = build_observed_source_graph(entrypoint, observation_payload)
    graph_path = write_observed_source_graph(graph, output)
    report_path = write_observed_source_graph_review(graph, report or f"{graph_path}.report.txt")
    print(f"modash: runtime source graph: {graph_path.resolve(strict=False)}", file=sys.stderr)
    print(f"modash: runtime graph review report: {report_path.resolve(strict=False)}", file=sys.stderr)


def supplement_from_graph_main(entrypoint, *, graph, output):
    graph_payload = load_observed_source_graph(graph)
    supplement = generate_source_supplement_from_graph(entrypoint, graph_payload)
    supplement_path = write_generated_supplement(supplement, output)
    print(f"modash: source supplement: {supplement_path.resolve(strict=False)}", file=sys.stderr)


def compile_observed_main(entrypoint, output, *, graph):
    graph_payload = load_observed_source_graph(graph)
    _compile_from_graph_payload(entrypoint, output, graph_payload)
    print(f"modash: compiled from trusted runtime graph: {output}", file=sys.stderr)


def observe_compile_main(
    entrypoint,
    output,
    *,
    graph_output,
    report=None,
    observation_output=None,
    script_args=None,
    cwd=None,
    env=None,
    timeout=DEFAULT_TRACE_TIMEOUT_SECONDS,
):
    result = trace_sources(entrypoint, argv=script_args or (), cwd=cwd, env=env, timeout=timeout)
    _forward_trace_output(result)
    graph = build_observed_source_graph(entrypoint, result.observation)
    graph_path = write_observed_source_graph(graph, graph_output)
    report_path = write_observed_source_graph_review(graph, report or f"{graph_path}.report.txt")
    observation_path = write_trace_observation(
        result,
        observation_output or f"{graph_path}.observation.json",
    )
    _compile_from_graph_payload(entrypoint, output, graph)

    print(f"modash: trace observation: {observation_path.resolve(strict=False)}", file=sys.stderr)
    print(f"modash: runtime source graph: {graph_path.resolve(strict=False)}", file=sys.stderr)
    print(f"modash: runtime graph review report: {report_path.resolve(strict=False)}", file=sys.stderr)
    print(f"modash: compiled from newly observed trusted runtime graph: {output}", file=sys.stderr)
    return result.returncode


def _forward_trace_output(result):
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def _compile_from_graph_payload(entrypoint, output, graph_payload):
    generated_supplement = generate_source_supplement_from_graph(entrypoint, graph_payload)
    source_supplement = load_source_supplement_from_payload(
        generated_supplement.to_dict(),
        _entrypoint_directory(entrypoint),
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    compile_sources(
        entrypoint,
        output,
        mode="executable",
        source_supplement=source_supplement,
        source_overrides=_source_overrides_from_graph_payload(graph_payload),
    )


def _source_overrides_from_graph_payload(graph_payload):
    direct_overrides = [
        SourceOverride(
            path=edge["call_site"]["file"],
            line=edge["call_site"]["line"],
            command=_source_override_command(edge),
            resolved_path=edge["resolved_path"],
            arguments=tuple(edge["arguments"]),
        )
        for edge in graph_payload["edges"]
        if edge["from"].startswith("file:") and edge["to"].startswith("file:")
    ]
    return tuple([
        *direct_overrides,
        *_child_process_command_overrides_from_graph_payload(graph_payload),
    ])


def _source_override_command(edge):
    condition_sites = _source_condition_sites(edge["call_site"]["command"])
    if len(condition_sites) == 1:
        return condition_sites[0]
    if len(condition_sites) > 1:
        xtrace_site = _first_direct_source_site(edge.get("xtrace", {}).get("command", ""))
        if xtrace_site in condition_sites:
            return xtrace_site

    parsed_sites = _source_sites_on_line(edge["call_site"]["file"], edge["call_site"]["line"])
    if len(parsed_sites) == 1:
        return parsed_sites[0]
    source_site = _first_direct_source_site(edge["call_site"]["command"])
    return source_site or edge["call_site"]["command"]


def _source_condition_sites(command: str):
    condition = _control_source_condition(command)
    if condition is None:
        return ()
    try:
        atoms = SourceEvaluator._source_logical_condition_atoms_from_text(condition)
    except UnsupportedSourceError:
        return ()
    return tuple(
        f"{atom.source_command} {atom.source_expression}"
        for atom in atoms
        if atom.source_command is not None
    )


def _control_source_condition(command: str):
    stripped = command.strip()
    match = re.fullmatch(r'(?:if|elif|while|until)\s+(.+?)(?:\s*;\s*(?:then|do).*)?$', stripped, re.S)
    if match is None:
        return None
    return match.group(1).strip()


def _source_sites_on_line(path: str, line: int):
    candidate = Path(path)
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError:
        return ()

    try:
        ir = LineParserFrontend().parse(candidate, content)
    except Exception:
        return ()

    sites = []

    def collect(nodes):
        for node in nodes:
            if isinstance(node, SourceSite) and node.location.line == line:
                sites.append(node.text)
            body = getattr(node, "body", None)
            if body:
                collect(body)
            for branch in getattr(node, "branches", ()):
                collect(getattr(branch, "body", ()))
            for arm in getattr(node, "arms", ()):
                collect(getattr(arm, "body", ()))

    collect(ir.nodes)
    return tuple(sites)


def _child_process_command_overrides_from_graph_payload(graph_payload):
    process_commands = {
        node["id"]: node
        for node in graph_payload["nodes"]
        if node["kind"] == "process-command"
    }
    candidate_files = _graph_parent_command_candidate_files(graph_payload)
    overrides = []
    for edge in graph_payload["edges"]:
        if not edge["from"].startswith("process-command:") or not edge["to"].startswith("file:"):
            continue
        process_command = process_commands.get(edge["from"])
        if process_command is None:
            continue
        source_site = _first_direct_source_site(process_command["command"])
        if source_site is None:
            continue
        for path, line in _matching_bash_c_parent_sites(candidate_files, process_command["command"]):
            overrides.append(SourceOverride(
                path=path,
                line=line,
                command=source_site,
                resolved_path=edge["resolved_path"],
                arguments=tuple(edge["arguments"]),
            ))
    return tuple(overrides)


def _graph_parent_command_candidate_files(graph_payload):
    return tuple(
        fingerprint["path"]
        for fingerprint in graph_payload["files"]
        if "entrypoint" in fingerprint["roles"] or "call-site" in fingerprint["roles"]
    )


def _first_direct_source_site(command: str):
    for segment in get_commands(command):
        invocation = source_command_invocation(segment)
        if invocation is not None:
            return invocation.source_site
    return None


def _matching_bash_c_parent_sites(paths, payload: str):
    matches = []
    for path in paths:
        candidate = Path(path)
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            for command in get_commands(line):
                if _bash_c_payload(command) == payload:
                    matches.append((str(candidate), index))
    return tuple(matches)


def _bash_c_payload(command: str):
    try:
        words = parse_shell_words_preserving_quotes(command.strip())
    except UnsupportedSourceError:
        return None
    index = 0
    while index < len(words) and ASSIGNMENT_WORD_PATTERN.match(words[index]):
        index += 1
    if index + 2 >= len(words):
        return None
    command_name = strip_shell_word_quotes(words[index])
    if command_name not in {"bash", "/bin/bash", "/usr/bin/bash"} or words[index + 1] != "-c":
        return None
    return strip_shell_word_quotes(words[index + 2])


def _entrypoint_directory(entrypoint):
    return str(Path(entrypoint).resolve(strict=False).parent)


def parse_env_overlay(values):
    environment = {}
    for value in values or ():
        if "=" not in value or value.startswith("="):
            raise ValueError(f"invalid --env value: {value!r}; expected KEY=VALUE")
        key, env_value = value.split("=", 1)
        if not key:
            raise ValueError(f"invalid --env value: {value!r}; expected KEY=VALUE")
        environment[key] = env_value
    return environment or None


def parse_positive_seconds(value):
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return timeout


def split_trace_args(argv):
    if "--" not in argv:
        return argv, []
    separator = argv.index("--")
    return argv[:separator], argv[separator + 1:]


def trace_cli(argv):
    trace_argv, script_args = split_trace_args(argv)
    parser = argparse.ArgumentParser(description='Run a Bash script and write runtime source observations.')
    parser.add_argument('entrypoint', type=str, help='The Bash script to execute under source tracing.')
    parser.add_argument('--cwd', help='Working directory for the traced script.')
    parser.add_argument(
        '--env',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='Environment overlay for the traced script. May be provided multiple times.',
    )
    parser.add_argument('--output', help='Observation JSON file to write.')
    parser.add_argument('--output-dir', help='Directory for the generated observation JSON file.')
    parser.add_argument(
        '--timeout',
        type=parse_positive_seconds,
        default=DEFAULT_TRACE_TIMEOUT_SECONDS,
        help='Maximum seconds to let the traced script run. Default: %(default)s.',
    )
    args = parser.parse_args(trace_argv)
    if args.output and args.output_dir:
        parser.error('--output and --output-dir are mutually exclusive')

    try:
        environment = parse_env_overlay(args.env)
    except ValueError as exc:
        parser.error(str(exc))

    return trace_main(
        args.entrypoint,
        script_args=script_args,
        cwd=args.cwd,
        env=environment,
        output=args.output,
        output_dir=args.output_dir,
        timeout=args.timeout,
    )


def supplement_cli(argv):
    parser = argparse.ArgumentParser(description='Generate a source supplement candidate from a trace observation.')
    parser.add_argument('entrypoint', type=str, help='The Bash script that was traced.')
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        '--from-observation',
        help='Runtime source observation JSON produced by the trace command.',
    )
    source_group.add_argument(
        '--from-graph',
        help='Trusted runtime source graph JSON produced by the graph command.',
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Source supplement JSON file to write.',
    )
    parser.add_argument(
        '--report',
        help='Observation review report JSON file to write. Defaults to OUTPUT.report.json.',
    )
    args = parser.parse_args(argv)
    if args.from_observation:
        supplement_main(args.entrypoint, observation=args.from_observation, output=args.output, report=args.report)
        return
    if args.report:
        parser.error('--report is only valid with --from-observation')
    supplement_from_graph_main(args.entrypoint, graph=args.from_graph, output=args.output)


def graph_cli(argv):
    parser = argparse.ArgumentParser(description='Build a trusted runtime source graph from a trace observation.')
    parser.add_argument('entrypoint', type=str, help='The Bash script that was traced.')
    parser.add_argument(
        '--from-observation',
        required=True,
        help='Runtime source observation JSON produced by the trace command.',
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Runtime source graph JSON file to write.',
    )
    parser.add_argument(
        '--report',
        help='Human-readable graph review report file to write. Defaults to OUTPUT.report.txt.',
    )
    args = parser.parse_args(argv)
    graph_main(args.entrypoint, observation=args.from_observation, output=args.output, report=args.report)


def compile_observed_cli(argv):
    parser = argparse.ArgumentParser(description='Compile executable output using a trusted runtime source graph.')
    parser.add_argument('entrypoint', type=str, help='The Bash script that was traced.')
    parser.add_argument('output', type=str, help='Executable merged script to write.')
    parser.add_argument(
        '--from-graph',
        required=True,
        help='Trusted runtime source graph JSON produced by the graph command.',
    )
    args = parser.parse_args(argv)
    compile_observed_main(args.entrypoint, args.output, graph=args.from_graph)


def observe_compile_cli(argv):
    trace_argv, script_args = split_trace_args(argv)
    parser = argparse.ArgumentParser(
        description='Run source tracing and compile executable output from the observed trusted graph.',
    )
    parser.add_argument('entrypoint', type=str, help='The Bash script to execute under source tracing.')
    parser.add_argument('output', type=str, help='Executable merged script to write.')
    parser.add_argument('--cwd', help='Working directory for the traced script.')
    parser.add_argument(
        '--env',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='Environment overlay for the traced script. May be provided multiple times.',
    )
    parser.add_argument(
        '--reviewed-graph-out',
        required=True,
        help='Trusted runtime source graph JSON file to write for review.',
    )
    parser.add_argument(
        '--observation-out',
        help='Runtime source observation JSON file to write. Defaults to REVIEWED_GRAPH_OUT.observation.json.',
    )
    parser.add_argument(
        '--report',
        help='Human-readable graph review report file to write. Defaults to REVIEWED_GRAPH_OUT.report.txt.',
    )
    parser.add_argument(
        '--timeout',
        type=parse_positive_seconds,
        default=DEFAULT_TRACE_TIMEOUT_SECONDS,
        help='Maximum seconds to let the traced script run. Default: %(default)s.',
    )
    args = parser.parse_args(trace_argv)

    try:
        environment = parse_env_overlay(args.env)
    except ValueError as exc:
        parser.error(str(exc))

    return observe_compile_main(
        args.entrypoint,
        args.output,
        graph_output=args.reviewed_graph_out,
        report=args.report,
        observation_output=args.observation_out,
        script_args=script_args,
        cwd=args.cwd,
        env=environment,
        timeout=args.timeout,
    )


def cli_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if len(argv) > 0 and argv[0] == "trace":
        try:
            return trace_cli(argv[1:])
        except (RuntimeSourceTraceError, RuntimeSourceObservationError) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            return 1

    if len(argv) > 0 and argv[0] == "supplement":
        try:
            supplement_cli(argv[1:])
        except (
            RuntimeSupplementGenerationError,
            RuntimeObservationReportError,
            RuntimeSourceGraphError,
            RuntimeSourceObservationError,
        ) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            return 1
        return 0

    if len(argv) > 0 and argv[0] == "graph":
        try:
            graph_cli(argv[1:])
        except (RuntimeSourceGraphError, RuntimeSourceObservationError) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            return 1
        return 0

    if len(argv) > 0 and argv[0] == "compile-observed":
        try:
            compile_observed_cli(argv[1:])
        except (
            RuntimeSupplementGenerationError,
            RuntimeSourceGraphError,
            RuntimeSourceObservationError,
            UnsupportedSourceError,
            OSError,
        ) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            return 1
        return 0

    if len(argv) > 0 and argv[0] == "observe-compile":
        try:
            return observe_compile_cli(argv[1:])
        except (
            RuntimeSourceTraceError,
            RuntimeSupplementGenerationError,
            RuntimeSourceGraphError,
            RuntimeSourceObservationError,
            UnsupportedSourceError,
            OSError,
        ) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            return 1

    parser = argparse.ArgumentParser(
        description='Merge Bash scripts into a single script.',
        epilog=TOP_LEVEL_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('entrypoint', type=str, help='The entry-point Bash script that initiates the merging process.')
    parser.add_argument('output', type=str, help='The output file where the merged script will be saved.')
    parser.add_argument(
        '--mode',
        choices=('context', 'executable'),
        default='context',
        help='Output mode. context is readable-first; executable preserves Bash source execution behavior.',
    )
    parser.add_argument(
        '--source-supplement',
        help='JSON file with exact source-relevant values for runtime-dynamic source sites.',
    )
    args = parser.parse_args(argv)
    try:
        main(
            entry_point=args.entrypoint,
            output_file=args.output,
            mode=args.mode,
            source_supplement=args.source_supplement,
        )
    except UnsupportedSourceError as exc:
        print(f"modash: {exc}", file=sys.stderr)
        details = exc.diagnostic.details if exc.diagnostic is not None else exc.details
        skeleton = details.get("supplement_skeleton") if details else None
        if skeleton:
            print("modash: source supplement skeleton:", file=sys.stderr)
            print(json.dumps(skeleton, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli_main())
