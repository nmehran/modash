# Compound Source Condition Lowering

## Status

Implemented on the `iteration/compound-source-condition-lowering` development
branch. It builds on exact direct source conditions, source-relevant
control-flow boundaries, and runtime-guarded static source lowering.

This iteration remains static. It does not run Bash, use xtrace, infer runtime
dependencies, or broaden arbitrary shell validation.

## Summary

The compiler supports direct source conditions:

```bash
if source ./dep.sh; then
  echo loaded
fi
```

It also preserves unknown runtime guards and lowers exact branch-local source
sites. This iteration adds source commands used as atoms inside compound
condition lists:

```bash
if source ./dep.sh && [[ "$FEATURE" == enabled ]]; then
  echo enabled
fi
```

When the source path and source arguments are exact, modash lowers that source
atom in place, preserves Bash short-circuit behavior, and models source-visible
state conservatively. It fails only when dependency paths, source arguments,
source execution ordering, or later source-relevant state cannot be proven safe.

## Implemented Behavior

This iteration covers the high-value static subset:

- `if source ./dep.sh && true; then` lowers the exact source atom and preserves
  the remaining guard.
- `if runtime_probe && source ./dep.sh; then` lowers the source atom under the
  original runtime guard and marks source effects conditional.
- `if source ./dep.sh || source ./fallback.sh; then` lowers multiple exact
  source atoms in Bash order and preserves short-circuit behavior.
- `if ! source ./dep.sh || source ./fallback.sh; then` preserves negated
  source-command status.
- Reachable `elif` source conditions are evaluated in order; unreachable
  source-bearing `elif` conditions are replaced with no-op condition atoms so
  executable output contains no live source commands.
- Pipelines, subshells, command substitutions, process substitutions, and
  source atoms buried inside unsupported grammar remain later work.

## Non-Goals

- Do not execute predicates or use runtime tracing.
- Do not support source commands in pipelines or subshells.
- Do not support command substitution or process substitution source atoms.
- Do not model arbitrary Bash grammar as a side effect of this iteration.
- Do not leave live unresolved `source` or `.` commands in executable output.
- Do not broaden recursive or runtime-dynamic source-bearing function dispatch.

## Tranche 1: First-Atom Source Conditions

Support exact source commands as the first command atom in `if` / `elif`
logical condition lists.

Acceptance:

- `if source ./dep.sh && true; then ... fi` lowers `./dep.sh`, preserves the
  original `&& true` guard, and matches Bash output/status/state.
- `if source ./dep.sh || source ./fallback.sh; then ... fi` lowers both exact
  source atoms and preserves short-circuit behavior.
- `if ! source ./dep.sh || source ./fallback.sh; then ... fi` keeps existing
  negated direct-source behavior and preserves logical-list semantics.
- Source arguments on exact source atoms are passed through.
- Unresolved source paths or unsupported source arguments fail before output.
- Generated executable output contains no live source command for lowered
  atoms.

Implementation notes:

- Reuse the existing direct-source condition lowering and source-site renderer
  so source atoms become same-shell status-producing blocks.
- Add a small condition-list tokenizer/parser only for top-level `!`, `&&`,
  and `||` command atoms. Keep pipelines and nested constructs rejected.
- Preserve source-site columns so line replacement remains precise.

## Tranche 2: Conditional Source Atom State

Support exact source atoms that are not guaranteed to execute because earlier
runtime condition atoms may short-circuit them.

Acceptance:

- `if runtime_probe && source ./dep.sh; then ... fi` lowers `./dep.sh` under
  the original runtime guard and marks source effects conditional.
- `if runtime_probe || source ./fallback.sh; then ... fi` lowers fallback
  sources only as runtime-possible outcomes.
- Later source sites depending on variables, arrays, cwd, shell options,
  functions, or positional state that diverged across possible condition paths
  fail with `unsupported.source.branch-state`.
- Multiple exact source atoms execute in Bash order for every modeled possible
  condition path.
- Branch-dependent source or function returns remain fail-closed unless all
  possible outcomes have equivalent status/type semantics.

Implementation notes:

- Treat condition-list evaluation as source-relevant control flow, not as a
  predicate truth solver.
- Use the existing possible-outcome merge machinery where possible.
- The renderer should preserve the original condition list and replace only
  exact source atoms.

## Tranche 3: Real-World And Runtime Promotion

Add focused real-world fixtures and runtime parity probes so this behavior does
not remain synthetic-only.

Acceptance:

- Add one pinned-corpus fixture for `source ./dep.sh && runtime_probe`.
- Add one pinned-corpus fixture for `runtime_probe || source ./fallback.sh`.
- Runtime parity probes cover source executed, source skipped, fallback
  executed, and fallback skipped.
- Artifact scan confirms successful executable outputs contain no live source
  commands.
- Generated artifacts are manually/LLM reviewed for condition readability,
  source block placement, and short-circuit preservation.

## Review Checklist

Required checks before merging implementation work:

- `python -m unittest -v`
- `MODASH_REALWORLD=1` pinned corpus
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes
- executable artifact scan for live `source` commands
- spot-check generated compound-condition executable artifacts

## Later Work

Pipelines, subshells, command substitutions, process substitutions, and broad
Bash condition grammar remain separate iterations.

Explicit source-argument frame restoration around later nested source sites is
covered by
[Explicit Source Argument Frame Restoration](source-argument-frame-restoration.md).

Case pattern/fallthrough semantics are covered by
[Case Source Semantics Expansion](case-source-semantics.md). `extglob`, full
`GLOBIGNORE`, recursive/runtime-dynamic source-bearing dispatch, and runtime
source discovery with xtrace remain deferred.
