# Source Argument Semantics Completion

## Status

Implemented on the source-argument semantics development branch. This branch
should stay separate until the review pass, full verification, and generated
artifact inspection are complete.

## Summary

The compiler supports exact direct source arguments and makepkg-style helper
source arguments. This iteration finished the main Bash source-argument
semantics reachable without runtime tracing:

- direct source globs that expand to more than one file
- wrapped sourced files that mutate caller positional parameters with top-level
  `set --` or `shift`
- real-world and runtime parity probes that prove the behavior in executable
  output

This iteration is still static. It does not add xtrace, runtime source
discovery, or environment-dependent compilation.

## Implemented Scope

Implemented behavior:

- `source ./dep.sh alpha "beta gamma"` compiles in executable mode.
- `source "$@"` and `source "$1"` helper forms compile when their local
  function call arguments are exact or supplement-backed.
- Retained makepkg-style helpers can dispatch over finite supplement argument
  vectors.
- Source bodies containing top-level `return` are lowered through generated
  helper functions.
- Direct source globs that expand to multiple regular files source the first
  match and pass the remaining expanded words as source arguments.
- Wrapped sourced files synchronize modeled top-level positional mutation back
  to the caller when Bash behavior is exact.
- The real-world suite has controlled pacman fixtures for direct glob source
  arguments and wrapped positional mutation.

Explicit source-argument frame restoration around later nested source sites was
finished in
[Explicit Source Argument Frame Restoration](source-argument-frame-restoration.md).

## Non-Goals

- Do not run Bash, enable xtrace, or trace runtime execution.
- Do not support arbitrary dynamic source dispatch.
- Do not add broad shell word-splitting support.
- Do not leave live unresolved `source` commands in executable output.
- Do not preserve unsupported behavior silently; fail closed when exact parity
  cannot be proven.

## Tranche 1: Direct Multi-Match Source Globs

Goal: support Bash-equivalent direct source glob argument semantics.

Acceptance:

- A direct source glob with one match continues to source that file normally.
- A direct source glob with multiple regular-file matches resolves the first
  match as the source path and passes the remaining expanded words as exact
  source arguments.
- Existing modeled glob state remains honored: `nullglob`, `dotglob`,
  `globstar`, `nocaseglob`, `failglob`, and practical `GLOBIGNORE` filtering.
- Unsupported glob state such as `extglob` still fails closed.
- Executable output contains no live source command for the accepted cases.
- Synthetic tests compare Bash output for at least:
  - one-match direct source glob
  - multi-match direct source glob where the sourced file reads `$1`, `$2`,
    and `$#`
  - quoted literal glob characters that must not expand
  - rejected unsupported glob-state cases

Implementation notes:

- Reuse the existing glob expansion path and ordering already used by loop
  source resolution.
- Represent the remaining matches as `source_arguments` on the resolved source
  event.
- Keep context mode readable by showing the resolved first source and preserving
  enough annotation to explain the extra source arguments.

## Tranche 2: Wrapped Positional Mutation Lowering

Goal: support exact Bash behavior when a wrapped sourced file mutates caller
positionals.

Acceptance:

- Sourced files with exact source arguments and top-level `set -- ...` match
  Bash output, status, and caller positional state after the source returns.
- Sourced files with exact source arguments and top-level `shift` match Bash
  output, status, and caller positional state; Bash restores the explicit
  source-argument frame after `shift`.
- Sourced files without explicit source arguments but with top-level `return`
  and top-level positional mutation also match Bash.
- Positional mutation inside function definitions remains function-local and
  must not be treated as caller mutation.
- Branch-local top-level positional mutation is supported only when the
  existing evaluator can model the branch path exactly.
- Chained or compound mutation forms fail closed unless lowering can preserve
  the command status observed by following `&&`, `||`, `if`, or `case`
  constructs.

Implementation notes:

- Generated wrappers need an explicit caller-visible positional-sync channel.
  The sourced body can update a generated array and mutation flag after modeled
  top-level `set --` or successful `shift`; the source-site fragment can then
  apply `set -- "${array[@]}"` in the caller scope after the generated body
  returns.
- The lowering must preserve `$?` after `set --` and `shift` instrumentation.
- Function bodies inside sourced files must be skipped by the top-level scan.
- If status preservation or source-site scope cannot be proven, keep the
  current fail-closed diagnostic.
- Explicit source-argument frame restoration around later nested source sites is
  covered by
  [Explicit Source Argument Frame Restoration](source-argument-frame-restoration.md).

## Tranche 3: Real-World And Runtime Promotion

Goal: prove the new behavior outside synthetic fixtures without broadening the
runtime trust surface.

Acceptance:

- Add at least one controlled real-world fixture for direct multi-match source
  glob arguments.
- Add at least one controlled real-world/runtime fixture for wrapped positional
  mutation.
- Runtime parity compares original Bash execution and compiled executable
  output for status, stdout, stderr, cwd-sensitive behavior, exported state,
  and function availability where relevant.
- The pinned corpus summary remains expectation-matched.
- Executable artifacts scan cleanly for live `source` / `.` commands.
- Context and executable pacman artifacts receive the same LLM/human review
  pass we have been using.

## Done Definition

- Unit tests cover every accepted and rejected shape in this document.
- `python -m unittest -v` passes.
- `MODASH_REALWORLD=1` pinned corpus tests pass.
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes pass.
- `git diff --check` passes.
- Real-world result JSON has only expected outcomes.
- Generated executable artifacts contain no unexpected live source sites.
- Docs are updated so the supported matrix and remaining-work sections match
  the implemented contract.

## Deferred After This Iteration

Dynamic source discovery with xtrace remains a later 0.5-class feature. This
iteration should make that later work easier by reducing the static
source-argument gaps first, but it should not introduce tracing, sandboxing, or
runtime-observed supplement generation.
