# modash

`modash` merges Bash script projects into one file.

It resolves `source` dependencies without executing shell code during normal
compile. When a source path is genuinely runtime-dependent, `modash` also
provides an explicit observe -> review -> supplement workflow.

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

## Runtime Observations

Runtime tracing is explicit because it runs the target script.

```sh
modash trace scripts/main.sh --output observation.json -- --flag
modash graph scripts/main.sh --from-observation observation.json --output runtime-graph.json
modash supplement scripts/main.sh --from-graph runtime-graph.json --output source-supplement.json
modash scripts/main.sh merged.sh --mode executable --source-supplement source-supplement.json
```

After reviewing a trusted graph, the compile step can self-supplement without
writing the intermediate supplement file:

```sh
modash compile-observed scripts/main.sh merged.sh --from-graph runtime-graph.json
```

`modash trace` writes an observation JSON artifact. Current observations include
process provenance, resolved source events, linked source identities, sanitized
xtrace source provenance, and schema `5` file fingerprints for stale-observation
detection.

`modash graph` validates a trace observation and writes a trusted runtime source
graph. Graph edges link wrapper-observed source events to sanitized xtrace
provenance and fail closed if that trust link is missing, stale, or inconsistent
with the graph's process, file, edge, and fingerprint invariants.

`modash supplement` writes:

- a schema `1` JSON source supplement candidate
- an observation review report, defaulting to `OUTPUT.report.json`

Generated supplements are exact data, not shell code. Review the graph,
supplement, and report before compiling with `--source-supplement`.
Observation reports can warn about unobserved source-capable sites, but one
traced run is not proof of every branch.

Automatic compile-from-trace remains future work. Runtime discovery still feeds
deterministic compilation only after the trusted graph is made explicit.

## Commands

```sh
modash <entrypoint> <output> [--mode context|executable] [--source-supplement FILE]
modash trace <entrypoint> [--cwd DIR] [--env KEY=VALUE] [--output FILE] [--timeout SECONDS] [--] [args...]
modash graph <entrypoint> --from-observation observation.json --output runtime-graph.json
modash supplement <entrypoint> (--from-observation observation.json [--report report.json] | --from-graph runtime-graph.json) --output source-supplement.json
modash compile-observed <entrypoint> <output> --from-graph runtime-graph.json
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
- `compile-observed --from-graph FILE`: compile executable output using a
  trusted graph and an in-memory generated supplement.
- `supplement --report FILE`: choose the review report path.

## Development

Run the local verification suite:

```sh
python -m unittest
python -m py_compile modash.py methods/*.py methods/regex/*.py test/*.py
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
