# Runtime Source Discovery Foundation

## Status

Planned `v0.3.0` iteration. This is the first implementation batch for
[Runtime Source Discovery North Star](runtime-source-discovery.md).

The goal is to produce reviewed runtime source observations. This iteration
does not generate source supplements, does not compile from traces, and does
not run during normal static compile.

## Iteration Shape

This work should happen on a development branch as separate tranche commits.
Each tranche should leave the repository green and should be independently
reviewable.

Commit checkpoints:

1. Formalize this iteration.
2. Add observation schema and fixture harness.
3. Add controlled trace runner and parser for the first source events.
4. Add CLI/API artifact plumbing.
5. Add real-world smoke coverage, documentation cleanup, and final artifact
   review.

Review-pass commits are allowed after any tranche when correctness,
cleanliness, or performance issues surface.

## Product Contract

Runtime source discovery executes the target script only when the user asks for
that explicitly. It records what Bash actually did for that one run and writes
structured observation JSON as an artifact.

The observation is data, not proof. It should be useful input for later
supplement generation, but the compiler must continue to fail closed unless it
can prove or has been given exact declarative source inputs.

Required boundaries:

- no tracing during normal compile
- no automatic compile from an observation
- no `eval`, `source`, or command execution from trace text
- no broad inference from one run
- no supplement generation in this iteration except possible manual examples
- no claim that an observation covers unexecuted branches

## Tranche 1: Observation Schema And Fixture Harness

Add the durable data model before adding tracing complexity.

Scope:

- Define observation schema version `1`.
- Add validation for required top-level fields.
- Add validation for source event records.
- Add JSON read/write helpers that preserve stable output ordering.
- Add a synthetic fixture harness for temporary Bash projects.
- Add tests for valid and invalid observation documents.

Observation fields for this tranche:

- `version`
- `entrypoint`
- `cwd`
- `argv`
- `bash.version`
- `trace.version`
- `environment.policy`
- `environment.recorded_keys`
- `sources`

Source event fields:

- `index`
- `call_site.file`
- `call_site.line`
- `call_site.command`
- `resolved_path`
- `arguments`
- `status`

Acceptance:

- Invalid JSON and wrong schema versions fail with stable diagnostics.
- Missing required fields fail with stable diagnostics.
- Source arguments must be exact strings.
- Paths are serialized as normalized strings.
- Schema helpers are covered without requiring target script execution.

Commit checkpoint: schema, fixtures, and tests.

## Tranche 2: Controlled Trace Runner And Parser

Add the smallest useful tracing path.

Scope:

- Add an explicit trace runner that executes Bash under user request.
- Use a generated Bash prelude that wraps `source` and aliases dot-source so
  the runner can record expanded source argv and return status directly.
- Capture trace output separately from the target program output.
- Parse source events into observation records.
- Record direct `source`, dot source, source arguments, failed source status,
  cwd at execution time, and source calls made inside simple helper functions.
- Treat trace text as untrusted data.

Acceptance:

- `source ./dep.sh` records `dep.sh` with status `0`.
- `. ./dep.sh arg` records `dep.sh` with `["arg"]`.
- failed `source ./missing.sh` records the failed path and non-zero status.
- `cd subdir; source ./dep.sh` records the resolved path from the runtime cwd.
- `source_safe ./dep.sh` style helper calls are observable for the concrete run.
- Target stdout/stderr are not mixed into the observation JSON.
- Parser failures produce diagnostics instead of partial trusted observations.

Commit checkpoint: runner, parser, and synthetic tests.

Current foundation limitations:

- `builtin source` and `builtin .` bypass the wrapper.
- Scripts that unset or replace the tracing wrapper can hide later source
  events.
- Child Bash processes are not traced by default; the prelude unsets
  `BASH_ENV` after installing current-shell wrappers.
- Dynamically redefined functions that reuse the same relative source token from
  different directories can still make call-site attribution ambiguous.
- This is still observation, not supplement generation or deterministic
  compile replay.

## Tranche 3: CLI/API And Artifact Plumbing

Expose tracing without changing compile behavior.

CLI:

```sh
modashc trace ENTRY [--cwd DIR] [--env KEY=VALUE ...] [--output FILE | --output-dir DIR] [--] [ARGS...]
```

API:

```python
result = trace_sources(entrypoint, *, argv=None, cwd=None, env=None, bash="bash")
write_trace_observation(result, output_path)
```

Scope:

- Add a trace command or equivalent CLI subcommand.
- Add a Python API entry point.
- Write observations under `.modashc/observations/<run-id>.json` by default.
- Allow an explicit output path or output directory.
- Print the observation path on success.
- Keep compile CLI/API behavior unchanged.
- Ensure generated observation artifacts are ignored by git.

Acceptance:

- CLI writes valid schema `1` observation JSON.
- API returns the same structured data the CLI writes.
- Missing entrypoint and non-executable trace failures are explicit.
- Normal `modashc` compile paths do not import or run tracing unless requested.
- Observation file names are deterministic enough for tests, or the tests can
  pass an explicit output path.

Commit checkpoint: CLI/API/artifacts and tests.

## Tranche 4: Real-World Smoke And Review Gate

Use the foundation against a small real-world-shaped target without pretending
that runtime discovery is complete.

Scope:

- Add an opt-in real-world smoke case for a source helper or makepkg-style
  wrapper.
- Add a report printer or concise failure summary only if it materially helps
  review.
- Update docs with the actual CLI/API names selected during implementation.
- Spot-check generated observation JSON by LLM/manual review.
- Confirm no live source compilation behavior regressed.

Acceptance:

- Synthetic trace suite is green.
- Existing compile and real-world suite tests remain green.
- One pacman/makepkg-style `source_safe` smoke observation is reviewed and
  understandable.
- Generated observation JSON contains no shell code that would be executed by
  later stages.
- The branch has clear remaining-work notes for supplement generation.

Commit checkpoint: smoke coverage, docs, and review-pass cleanup.

## Safety And Performance Notes

Tracing is intentionally slower and more invasive than static compile because
it executes the target program. The first implementation should optimize for
correctness and isolation, then clean up avoidable overhead.

Practical performance goals:

- avoid reparsing whole trace files repeatedly
- stream or line-parse trace output where simple
- keep observation validation linear in event count
- keep trace modules out of normal compile imports if that creates startup
  overhead

Safety goals:

- make execution explicit in CLI help and docs
- default environment recording to allowlisted keys
- normalize paths before writing observations
- reject malformed events instead of guessing
- never turn trace strings into executable shell code

## Deferred To Later Iterations

Deferred to `v0.4.0` supplement generation and replay:

- generate source supplement candidates from observations
- validate generated supplements against compiler expectations
- compile with generated supplements after human review
- detect stale observations during replay

Deferred to `v0.5.0` polished xtrace compatibility:

- stable final CLI names if the v0.3 names need adjustment
- richer reproducibility metadata
- broader real-world corpus promotion
- diagnostics for incomplete branch coverage
- stronger environment and cwd reproducibility workflows

Still out of scope:

- proving all possible source paths
- sandboxing arbitrary scripts as a security boundary
- broad dynamic dispatch proof
- command-substitution or `eval` as trusted static facts
- replacing the static evaluator

## Definition Of Done

The iteration is complete when:

- observation schema `1` is implemented and tested
- explicit tracing records direct, dot, failed, cwd-sensitive, argument-bearing,
  and simple helper source events
- CLI/API can write observation artifacts on demand
- normal compile behavior is unchanged
- generated artifacts have had a final LLM/manual spot-check
- documentation clearly states that observation is not compilation and not
  proof of all branches
