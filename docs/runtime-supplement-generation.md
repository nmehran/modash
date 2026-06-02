# Runtime Supplement Generation And Replay

## Status

Implemented `v0.4.0` iteration. This builds on
[Runtime Source Discovery Foundation](runtime-source-discovery-foundation.md).

The goal is to convert reviewed runtime observations into deterministic source
supplement candidates, then prove those candidates can be replayed through the
existing executable compiler path. Generated supplements remain declarative
data and must still be reviewed before compile.

## Iteration Shape

This work should happen on a development branch as separate tranche commits.
Each tranche should leave the repository green and independently reviewable.

Commit checkpoints:

1. Formalize this iteration.
2. Add observation-to-supplement candidate generation.
3. Add CLI/API supplement artifact plumbing.
4. Add deterministic replay tests and real-world smoke coverage.
5. Add review-pass cleanup if correctness, diagnostics, or performance issues
   surface.

## Product Contract

Runtime supplement generation reads observation JSON. It does not execute the
target script. It must not broaden one observed run into wildcard rules or
shell code.

Required boundaries:

- no automatic compile from an observation
- generated supplement values are exact strings
- generated supplements use existing source supplement schema `version: 1`
- generated function entries are finite allowed helper argument vectors
- generated variables must be derived from exact observed source path prefixes
- invalid or stale observations fail before writing supplement output
- normal compile and trace behavior remain unchanged unless their commands are
  invoked explicitly

## Tranche 1: Observation To Supplement Candidate

Add a narrow generator that produces the smallest supplement candidate it can
justify from schema `1` observations.

Scope:

- Load and validate observation JSON.
- Generate `variables` entries from exact source call expressions containing a
  single environment-like variable prefix, such as
  `source "$MAKEPKG_LIBRARY/util/util.sh"`.
- Generate `functions` entries for helper source events when the source site is
  inside an identifiable function definition, such as makepkg's
  `source_safe() { if ! source "$@"; then ...; fi; }`.
- Prefer paths relative to the entrypoint directory when they remain inside the
  observed project tree; otherwise use absolute paths.
- Validate the generated supplement by round-tripping through the existing
  source supplement loader.

Acceptance:

- Direct variable inference generates a valid source supplement.
- Helper inference identifies `source_safe` from a sourced function definition
  and emits the observed source path plus source arguments.
- Duplicate observations deduplicate deterministically.
- Conflicting variable candidates fail closed with a stable diagnostic.
- Unsupported observations produce an empty but valid supplement rather than
  inventing behavior.

Commit checkpoint: generator, tests, and docs.

## Tranche 2: CLI/API Supplement Artifacts

Expose supplement generation as an explicit workflow step.

CLI:

```sh
modash supplement ENTRY --from-observation observation.json --output source-supplement.json
```

API:

```python
supplement = generate_source_supplement(entrypoint, observation)
write_generated_supplement(supplement, output_path)
```

Scope:

- Add a `supplement` command.
- Add explicit `--from-observation` and `--output` arguments.
- Print the generated supplement path on success.
- Keep trace and compile commands unchanged.
- Fail before writing output for missing files, invalid JSON, wrong schema, and
  generator diagnostics.

Acceptance:

- CLI writes valid supplement schema `1` JSON.
- CLI rejects missing observation and bad output combinations clearly.
- Existing compile CLI still uses the current positional form.
- Existing `trace` command still writes observations unchanged.

Commit checkpoint: CLI/API/artifacts and tests.

## Tranche 3: Replay And Real-World Smoke

Prove the two-pass workflow on synthetic and real-world-shaped cases.

Scope:

- Synthetic observe -> generate supplement -> executable compile -> run parity
  tests.
- Real-world pacman/makepkg smoke: trace the `source_safe` fixture, generate a
  supplement candidate, compile with it, and retain artifacts for review.
- Write normalized real-world result JSON.
- Document generated observation and supplement artifacts as reviewed data.

Acceptance:

- Synthetic replay cases pass without manual supplement fixtures.
- Real-world smoke records a generated supplement artifact and an executable
  compile success.
- Generated supplement artifacts contain no shell code, wildcards, or command
  substitution.
- Full default tests pass.
- Opt-in real-world trace/supplement smoke passes when cached corpus artifacts
  are present.

Commit checkpoint: replay tests, real-world smoke, docs, and artifact review.

## Deferred

Deferred to later iterations:

- automatic supplement generation during trace
- automatic compile from an unreviewed observation
- supplement staleness proofs beyond entrypoint/path/schema validation
- branch coverage diagnostics
- child Bash process trace merge
- broader dynamic dispatch inference

## Definition Of Done

The iteration is complete when:

- observation-to-supplement generation is implemented and tested
- `modash supplement` can write reviewed candidate JSON
- generated supplements replay through existing executable compile
- pacman/makepkg real-world smoke passes with retained observation and
  supplement artifacts
- documentation clearly states that generated supplements are candidates, not
  static proof of all branches
