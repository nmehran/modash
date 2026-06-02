# modash

`modash` merges Bash script projects into a single output file. It has two
first-class output modes:

- **Context mode**: the default readable output for human and LLM review.
- **Executable mode**: a runnable output that preserves supported Bash
  `source` execution semantics.

The compiler resolves dependencies without executing shell code. Runtime tracing
is a separate explicit command that executes the target script and writes a
source observation artifact for review.

## Output Modes

### Context Mode

Context mode is the default:

```sh
modash scripts/main.sh merged-context.sh
```

It renders one section per discovered file, dependency-first with the entrypoint
last. File bodies are deduplicated, original source lines are preserved, and
resolved relationships are annotated directly above the source site:

```bash
# modash: source ./dep.sh -> dep.sh
source ./dep.sh
```

Context mode is readable-first. It is intended for review, debugging, and
feeding complete shell-project context to another tool. It is not a runtime
parity mode.

### Executable Mode

Executable mode must be requested explicitly:

```sh
modash scripts/main.sh merged-runnable.sh --mode executable
```

It inlines sourced files at their source sites so parent variables, `set` state,
current directory state, duplicate source execution, and function-scoped sources
match supported Bash behavior. If executable mode cannot prove a source site is
safe to lower, compilation fails before writing or overwriting the output file.

## Supported Source Resolution

`modash` supports static source paths, exact variables, safe path command
substitutions, safe file/command producers, arrays, finite loops, modeled read
loops, branch-aware `if` and `case` source sites, and bounded source-bearing
function calls.

For the full current support matrix, examples, fail-closed behavior, and
practical remaining source-resolution gaps, see
[Supported Source Resolution](docs/supported-source-resolution.md).

For runtime-dynamic values that cannot be inferred statically, executable mode
can ingest validated JSON source supplements instead of guessing. The internal
real-world suite pins `bash-completion` and `pacman` corpora, generated
artifacts, and runtime parity probes so supported source-resolution behavior is
checked against real shell projects as well as synthetic regressions.

## Usage

```sh
modash <entrypoint> <output> [--mode context|executable] [--source-supplement FILE]
modash trace <entrypoint> [--cwd DIR] [--env KEY=VALUE] [--output FILE] [--timeout SECONDS] [--] [args...]
modash supplement <entrypoint> --from-observation observation.json --output source-supplement.json
```

Arguments:

- `<entrypoint>`: the Bash script that starts the source graph.
- `<output>`: the file to write.
- `--mode`: `context` by default, or `executable` for runtime parity over the
  supported subset.
- `--source-supplement`: optional JSON file with exact source-relevant values
  for runtime-dynamic source sites.

Trace command:

- `trace`: executes the target script under controlled source tracing.
- `--cwd`: optional working directory for the target script.
- `--env`: environment overlay for the target script. May be repeated.
- `--output`: exact observation JSON path. By default observations are written
  under `.modash/observations/`.
- `--timeout`: maximum seconds to let the traced script run. Default: `30`.
- `[args...]`: script arguments after `--`.

Trace forwards the target script stdout and stderr, writes the observation JSON,
and reports the observation path on stderr. Trace observations are data for
review and later supplement generation; they are not used automatically during
compile. If the traced script exceeds the timeout, trace exits non-zero and does
not write an observation.

Supplement command:

- `supplement`: reads a trace observation and writes a source supplement
  candidate.
- `--from-observation`: observation JSON produced by `trace`.
- `--output`: source supplement JSON to review and pass to executable compile.

Generated supplements are candidates from one observed run. Review them before
using them with `--source-supplement`.

Examples:

```sh
modash test/sample_dir/script_main.sh sample-context.sh
modash test/sample_dir/script_main.sh sample-runnable.sh --mode executable
modash trace test/sample_dir/script_main.sh --output observation.json
modash supplement test/sample_dir/script_main.sh --from-observation observation.json --output source-supplement.json
```

## Architecture

- `modash.py`: CLI module and source-tree entrypoint.
- `methods/compile.py`: context and executable renderers.
- `methods/runtime_source_trace.py`: explicit runtime source trace runner and
  trace parser.
- `methods/runtime_source_observations.py`: runtime source observation schema
  and JSON validation helpers.
- `methods/runtime_source_supplements.py`: observation-to-source-supplement
  candidate generator.
- `methods/source_frontend.py`: parser frontend that emits source-effect IR.
- `methods/source_evaluator.py`: abstract evaluator for cwd, variables, arrays,
  shell options, source events, and structured unsupported diagnostics.
- `methods/source_resolver.py`: source command detection, heredoc guards, safe
  dynamic source resolvers, and unsupported-source classification.
- `methods/sources.py`: path-resolution helpers and the `get_sources()`
  compatibility wrapper over source-effect evaluation.
- `methods/functions.py`: function-call extraction utility.
- `test/support.py`: real temporary shell-project harness used by regression
  tests.

The scripts under `setup/` are optional operational helpers for running commands
through a restricted `modash` user. They are not part of dependency discovery
or compilation.

## Development

Run the full local verification suite:

```sh
python -m unittest discover -s ./ -p 'test_*.py' -v
python -m py_compile modash.py methods/*.py methods/regex/*.py test/*.py
bash -n setup/modash_shell.sh setup/run_modash_shell.sh
shellcheck setup/modash_shell.sh setup/run_modash_shell.sh
python -m build --sdist --wheel --outdir dist
python -m twine check dist/*
git diff --check
```

Design notes live in [docs](docs/README.md).

## Installation

```sh
python -m pip install modash
```

For source-tree development:

```sh
git clone https://github.com/nmehran/modash.git
cd modash
python -m pip install -e .
```

No external runtime dependencies are required.

## License

This project is licensed under the Apache 2.0 License. See [LICENSE](LICENSE).
