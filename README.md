# modash

`modash` merges Bash script projects into one file.

It resolves `source` dependencies without executing shell code during normal
compile. When a path can only be known by running the script, `modash` can trace
one explicit run and compile from the reviewed result.

## Install

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

## Quick Start

Readable review output:

```sh
modash scripts/main.sh merged-context.sh
```

Runnable output for the supported Bash subset:

```sh
modash scripts/main.sh merged.sh --mode executable
```

If executable mode cannot prove a `source` site is safe to lower, it fails
before writing or overwriting the output file.

## Output Modes

### Context Mode

Context mode is the default. It writes dependency-first sections for the files
`modash` can resolve, preserves original source lines, and annotates resolved
relationships:

```bash
# modash: source ./dep.sh -> dep.sh
source ./dep.sh
```

Use context mode for review, debugging, and collecting complete shell-project
context for another tool. It is not a runtime parity mode.

### Executable Mode

Executable mode inlines sourced files at their source sites so supported parent
shell state remains Bash-equivalent: variables, functions, `set` state, current
directory, source arguments, repeated sources, and function-scoped sources.

The supported static subset includes exact paths, exact variables, safe path
commands and file producers, arrays, finite loops, modeled read loops,
branch-aware `if` and `case` source sites, child-shell source contexts, and
bounded source-bearing function calls.

See [Supported Source Resolution](docs/supported-source-resolution.md) for the
current support matrix and fail-closed boundaries.

## Runtime-Dependent Sources

Most `source` dependencies can be resolved without running the script. When a
project chooses a source path at runtime, use runtime discovery explicitly. It
runs the target once, records the source files that were actually loaded, writes
review artifacts, and then compiles from that reviewed graph.

Use this for patterns like hook dispatchers, plugin loaders, or helper
functions where a normal executable compile correctly fails closed because the
source path depends on runtime state.

Recommended flow:

```sh
modash trace scripts/main.sh --output observation.json -- --flag
modash graph scripts/main.sh --from-observation observation.json --output runtime-graph.json
modash compile-observed scripts/main.sh merged.sh --from-graph runtime-graph.json
```

The graph and its text report are the review points. They show which source
sites ran, which files were loaded, and whether the files still match the trace.
One trace represents one execution path; it is not proof that every branch was
covered.

If you want to keep the generated compiler input as a separate file, write an
explicit supplement and pass it to executable compile:

```sh
modash supplement scripts/main.sh --from-graph runtime-graph.json --output source-supplement.json
modash scripts/main.sh merged.sh --mode executable --source-supplement source-supplement.json
```

For automation, `observe-compile` performs the explicit trace, writes the graph
and report, and compiles from that newly observed graph:

```sh
modash observe-compile scripts/main.sh merged.sh --reviewed-graph-out runtime-graph.json -- --flag
```

Normal compile never traces. Runtime artifacts are data, not shell code, and
generated supplements contain exact values for source resolution rather than
commands to execute. See
[Runtime Source Discovery](docs/runtime-source-discovery.md) for the detailed
artifact formats, trust checks, and safety model.

## Commands

```sh
modash <entrypoint> <output> [--mode context|executable] [--source-supplement FILE]
modash trace <entrypoint> [--cwd DIR] [--env KEY=VALUE] [--output FILE] [--timeout SECONDS] [--] [args...]
modash graph <entrypoint> --from-observation observation.json --output runtime-graph.json [--report graph-review.txt]
modash supplement <entrypoint> (--from-observation observation.json [--report report.json] | --from-graph runtime-graph.json) --output source-supplement.json
modash compile-observed <entrypoint> <output> --from-graph runtime-graph.json
modash observe-compile <entrypoint> <output> --reviewed-graph-out runtime-graph.json [--observation-out observation.json] [--report graph-review.txt] [--cwd DIR] [--env KEY=VALUE] [--timeout SECONDS] [--] [args...]
```

Useful options:

- `--mode executable`: write runnable output for the supported subset.
- `--source-supplement FILE`: provide exact values for runtime-dynamic source
  sites.
- `trace --cwd DIR`: run the target script from a specific directory.
- `trace --env KEY=VALUE`: add an environment overlay for the traced run.
- `trace --timeout SECONDS`: bound target execution. Default: `30`.
- `graph --from-observation FILE`: build a trusted source graph from a trace
  observation.
- `graph --report FILE`: choose the human-readable graph review report path.
- `compile-observed --from-graph FILE`: compile executable output using a
  trusted graph and an in-memory generated supplement.
- `observe-compile --reviewed-graph-out FILE`: explicitly run tracing, write
  review artifacts, and compile executable output from the newly observed graph.
- `supplement --report FILE`: choose where to write the review report.

## Development

Run the local verification suite:

```sh
pytest -q
python -m py_compile $(find methods -name '*.py' -print) $(find test -name '*.py' -print) modash.py
git diff --check
```

Optional packaging checks:

```sh
python -m build --sdist --wheel --outdir dist
python -m twine check dist/*
```

Design notes live in [docs](docs/README.md).

## License

This project is licensed under the Apache 2.0 License. See [LICENSE](LICENSE).
