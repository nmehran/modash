import argparse
import json
import math
import sys
from methods.compile import compile_sources
from methods.runtime_source_trace import (
    DEFAULT_TRACE_TIMEOUT_SECONDS,
    RuntimeSourceTraceError,
    default_observation_path,
    trace_sources,
    write_trace_observation,
)
from methods.runtime_source_observations import RuntimeSourceObservationError
from methods.runtime_source_supplements import (
    RuntimeSupplementGenerationError,
    generate_source_supplement_from_observation_file,
    write_generated_supplement,
)
from methods.source_resolver import UnsupportedSourceError


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

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(f"modash: trace observation: {observation_path.resolve(strict=False)}", file=sys.stderr)
    return result.returncode


def supplement_main(entrypoint, *, observation, output):
    supplement = generate_source_supplement_from_observation_file(entrypoint, observation)
    supplement_path = write_generated_supplement(supplement, output)
    print(f"modash: source supplement: {supplement_path.resolve(strict=False)}", file=sys.stderr)


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
    parser.add_argument(
        '--from-observation',
        required=True,
        help='Runtime source observation JSON produced by the trace command.',
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Source supplement JSON file to write.',
    )
    args = parser.parse_args(argv)
    supplement_main(args.entrypoint, observation=args.from_observation, output=args.output)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == "trace":
        try:
            sys.exit(trace_cli(sys.argv[2:]))
        except (RuntimeSourceTraceError, RuntimeSourceObservationError) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "supplement":
        try:
            supplement_cli(sys.argv[2:])
        except (RuntimeSupplementGenerationError, RuntimeSourceObservationError) as exc:
            print(f"modash: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    parser = argparse.ArgumentParser(description='Merge Bash scripts into a single script.')
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
    args = parser.parse_args()
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
        sys.exit(1)
