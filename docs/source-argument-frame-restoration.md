# Explicit Source Argument Frame Restoration

## Status

Implemented on the `iteration/source-argument-frame-restoration` development
branch.

This iteration stays static. It does not run Bash, collect xtrace output, infer
runtime source paths, or broaden arbitrary shell validation. The goal is to
close the remaining source-argument edge where Bash restores explicit source
argument frames differently around later nested source calls.

## Scope Taken

After the case semantics work, the main remaining source-resolution gaps were:

- explicit source-argument frames that run top-level `set --` before a later
  nested source call
- source-bearing child-shell contexts: subshells, pipelines, command
  substitutions, process substitutions, and `bash -c`; see
  [Source-Bearing Child-Shell Contexts](source-child-shell-contexts.md)
- `extglob`, full `GLOBIGNORE` edge behavior, and remaining locale-dependent
  case pattern semantics
- recursive or runtime-dynamic source-bearing dispatch beyond finite modeled
  helpers
- xtrace/runtime source discovery and supplement generation

This iteration took the first item because it was the narrowest static
correctness gap. The child-shell and xtrace families have larger runtime
surfaces and should remain separate.

## Summary

The compiler already supports exact source arguments:

```bash
set -- outer
source ./dep.sh arg
```

It also synchronizes modeled top-level positional mutation back to the caller
when Bash does:

```bash
# dep.sh
set -- changed one
```

The final modeled edge appears when that sourced file mutates top-level
positionals, then performs a later nested source:

```bash
# main.sh
set -- outer
source ./dep.sh arg
printf '%s\n' "$1"

# dep.sh
set -- changed one
source ./nested.sh "$@"
```

In Bash, any later nested source site inside the outer explicit source frame
changes which top-level positional mutation escapes to the original caller.
Pre-nested `set --` is not enough by itself. A nested no-argument source can
dirty the current frame when it mutates positionals, and a later top-level
`set --` in the outer sourced file can supersede the barrier after the nested
source returns. The compiler now models those exact static cases.

## Non-Goals

- Do not model source commands in pipelines, subshells, command substitutions,
  process substitutions, or `bash -c` in this iteration. That follow-up is
  covered by [Source-Bearing Child-Shell Contexts](source-child-shell-contexts.md).
- Do not add broad word splitting for source arguments.
- Do not support runtime-dynamic or recursive source dispatch.
- Do not add xtrace, sandboxing, runtime-observed supplements, or shell
  execution during compilation.
- Do not preserve live unresolved `source` commands in executable output.

## Tranche 1: Explicit Frame Barrier Semantics

Goal: model the Bash positional-frame effect of nested source calls inside a
sourced file that itself was entered with explicit arguments.

Acceptance:

- `source ./dep.sh arg` where `dep.sh` runs `set -- changed; source ./nested.sh
  "$@"` matches Bash output, status, and caller positionals.
- Pre-nested top-level `set --` mutations are not synchronized to the original
  caller when a later nested source prevents them from escaping.
- Top-level `set --` mutations after that nested source still synchronize to
  the caller.
- Nested sources without explicit arguments inherit the current sourced-file
  positional state and synchronize back only when they mutate it.
- Existing supported cases for top-level `set --`, `shift`, and top-level
  `return` remain unchanged.

Implementation notes:

- Treat explicit source-argument frames as an ordered same-shell positional
  stack, not just a boolean "positionals changed" flag.
- Track explicit source-argument frame dirtiness while evaluating nested source
  sites.
- Renderer instrumentation should discard or supersede only the positional
  capture that Bash would not expose to the original caller.
- Preserve `$?` across instrumentation.

## Tranche 2: Nested Source Status And Return Coverage

Goal: prove the frame model across the source behaviors that already require
generated wrappers.

Acceptance:

- Nested explicit source with no positional mutation preserves the outer
  source's frame behavior.
- Nested explicit source with its own top-level `set --` or `shift` matches
  Bash for the nested source body and the original caller after the outer
  source returns.
- Nested explicit source with top-level `return N` preserves status observed by
  following `status=$?`, `&&`, `||`, `if source ...`, and direct source
  conditions.
- Source-bearing helper calls after a top-level positional mutation are
  supported when the helper source path and arguments are exact and the same
  frame-barrier semantics can be proven.
- Branch-dependent positional mutation or branch-dependent return/status still
  fails closed unless all possible outcomes are equivalent.

Implementation notes:

- Keep helper dispatch bounded. Dynamic helper names, recursive helper calls,
  and supplement-free runtime argument vectors remain unsupported.
- Prefer extending the existing wrapper and line-replacement path over adding a
  second rendering mechanism.
- Diagnostics should keep the existing `unsupported.source.positionals` code
  for still-unsupported frame shapes.

## Tranche 3: Real-World And Runtime Promotion

Goal: make the behavior durable outside synthetic unit cases.

Acceptance:

- Add focused synthetic compile/runtime tests for every accepted and rejected
  frame shape above.
- Add a controlled real-world fixture that exercises the explicit-frame
  barrier in executable and context modes.
- Add a runtime parity probe comparing original Bash execution and compiled
  executable output.
- Generated executable artifacts contain no live unresolved `source` / `.`
  commands.
- Context artifacts annotate the nested source relationship clearly enough for
  manual/LLM review.
- Docs are updated so the supported matrix no longer lists this edge as
  remaining once implementation is green.

## Done Definition

- `python -m unittest -v` passes.
- `MODASH_REALWORLD=1` pinned corpus tests pass.
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes pass.
- executable artifact scan reports zero live unresolved source commands.
- generated real-world artifacts receive the manual/LLM spot-check pass.
- `git diff --check` passes.

## Follow-Up After This Iteration

- Source-bearing child-shell contexts were implemented separately; see
  [Source-Bearing Child-Shell Contexts](source-child-shell-contexts.md).
- `extglob`, full `GLOBIGNORE`, collating symbols, equivalence classes, and
  broader locale-dependent pattern behavior
- recursive or runtime-dynamic source-bearing function dispatch
- xtrace/runtime source discovery and supplement generation
