# Runtime-Guarded Static Source Lowering

## Status

Implemented on the
`iteration/runtime-guarded-source-lowering` development branch. It builds on
source argument completion and source-relevant control-flow boundaries.

This iteration remains static. It does not use Bash execution, xtrace, runtime
source discovery, or sandboxed tracing.

## Summary

The compiler can lower exact source sites inside modeled control flow and can
pass through source-free unsupported runtime logic. This iteration handles
source-bearing runtime control flow whose source paths are already exact but
whose predicates or subjects are not statically known.

For those cases, modash should preserve the original runtime guard and lower
the exact source sites inside the guarded body. It should not try to validate or
execute the guard. It should fail only when dependency paths, source arguments,
or later source-relevant state cannot be proven safe.

Conceptually, this:

```bash
if some_runtime_probe; then
  source ./feature.sh
fi
```

can become executable output where `some_runtime_probe` remains live Bash and
`./feature.sh` is inlined inside the same branch.

## Implemented Behavior

- Unknown `if` / `elif` predicates no longer block executable output when a
  branch contains otherwise exact source sites.
- Unknown `case` subjects no longer block executable output when every arm
  source path is exact and the original case can be preserved.
- Branch-local source effects are lowered, but later source resolution still
  fails if it depends on branch-divergent variables, arrays, cwd, shell
  options, function definitions, or positional state.
- Runtime loops, recursive/runtime-dynamic dispatch, xtrace discovery,
  `extglob`, and full `GLOBIGNORE` edge behavior remain later work. Compound
  direct source conditions such as `if source ./dep.sh && ...` are covered by
  [Compound Source Condition Lowering](compound-source-condition-lowering.md).

## Non-Goals

- Do not evaluate arbitrary predicates.
- Do not run Bash or collect xtrace output.
- Do not infer dependencies from runtime-observed behavior.
- Do not leave live unresolved `source` or `.` commands in executable output.
- Do not model runtime loops in this iteration.
- Do not broaden recursive or runtime-dynamic function dispatch.
- Do not support compound direct source conditions as a parser side effect of
  this runtime-guarded lowering iteration.

## Tranche 1: Unknown If Predicate Source Lowering

Executable mode should lower exact source sites inside `if` / `elif` / `else`
branches even when the branch predicate itself is unknown or unsupported.

Acceptance:

- Unsupported command predicates such as `if awk 'BEGIN { exit 0 }'; then`
  preserve the original predicate and lower exact branch-local source sites.
- Runtime-variable predicates such as `if [[ -n "$ENABLE_FEATURE" ]]; then`
  lower exact branch-local source sites without requiring compile-time
  environment values.
- The generated executable contains no live source command for lowered branch
  source sites.
- Branch state after an unknown predicate is merged conservatively. Later source
  sites that depend on divergent branch state still fail with
  `unsupported.source.branch-state`.
- Source expressions or source arguments that are unresolved inside the branch
  still fail before output.
- Branch-dependent source or function returns remain fail-closed unless they
  are already modeled exactly.

Implementation notes:

- Reuse the existing context-mode possible-outcome machinery, but keep
  executable-mode fail-closed behavior for unresolved source paths.
- Preserve branch structure in the rendered executable; only replace source
  sites inside the original branch bodies.
- Record conditional or mutually exclusive occurrence context for lowered
  branch source events.

## Tranche 2: Unknown Case Subject Source Lowering

Executable mode should lower exact source sites inside `case` arms when the
case subject is runtime-dynamic but the original case statement can be
preserved.

Acceptance:

- `case "$MODE" in prod) source ./prod.sh ;; dev) source ./dev.sh ;; esac`
  lowers both exact source sites while leaving the runtime case intact.
- Unknown subjects with exact arms merge state conservatively after the case.
- Later source sites depending on branch-divergent case state fail closed.
- Existing exact-subject case behavior remains unchanged.
- Remaining unsupported case pattern families stay fail-closed: `extglob`
  patterns, collating symbols, equivalence classes, and broader
  locale-dependent matching. Practical case pattern normalization and
  fallthrough terminators are covered by
  [Case Source Semantics Expansion](case-source-semantics.md).

Implementation notes:

- Start with the current supported case pattern subset and terminators.
- Do not claim runtime parity for context mode beyond readable provenance.

## Tranche 3: Real-World And Ergonomics Promotion

Add controlled fixtures and real-world coverage so this behavior is not only
synthetic.

Acceptance:

- Add a small pinned-corpus fixture for runtime-guarded `if` source lowering.
- Add a small pinned-corpus fixture for runtime-guarded `case` source lowering.
- Runtime parity probes compare original and compiled behavior for at least one
  enabled and one disabled guarded branch.
- Artifact scan confirms successful executable outputs contain no live source
  commands.
- Generated artifacts are manually/LLM reviewed for readability and guard
  preservation.

## Review Checklist

Required checks before merging implementation work:

- `python -m unittest -v`
- `MODASH_REALWORLD=1` pinned corpus
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes
- executable artifact scan for live `source` commands
- spot-check generated guarded-if and guarded-case executable artifacts

## Later Work

Runtime loops with exact source-invariant bodies should be a separate
iteration. They need explicit repeated-occurrence and post-loop state semantics.

Compound direct source conditions such as `if source ./dep.sh && test ...; then`
are covered by
[Compound Source Condition Lowering](compound-source-condition-lowering.md).

Runtime source discovery with xtrace remains a later 0.5-class feature.
