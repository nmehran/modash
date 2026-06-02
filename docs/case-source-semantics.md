# Case Source Semantics Expansion

## Status

Implemented on the `iteration/case-source-semantics` development branch.

This iteration stays static. It does not run Bash, collect xtrace output, infer
runtime source paths, or broaden arbitrary shell validation. The goal is to
complete the next useful slice of source-bearing `case` behavior after exact
subjects, runtime-guarded subjects, and compound `if` source conditions.

## Why This Is Next

At the start of this iteration, the main remaining static gaps were:

- broader source-bearing `case` pattern semantics
- source-bearing pipelines, subshells, command substitutions, and process
  substitutions
- `extglob` and full `GLOBIGNORE` edge behavior
- recursive or runtime-dynamic source-bearing dispatch
- xtrace/runtime source discovery

`case` expansion is the best next iteration because it is common in shell-heavy
projects, already has an IR/evaluator path, and can still preserve the
executable-mode contract: lower exact dependency sources or fail before output.
Pipelines, subshells, and xtrace have a larger runtime semantics surface and
should remain separate.

## Summary

Current `case` support covers known scalar subjects, runtime-dynamic subjects
with exact source-bearing arms, default arms, alternate patterns, quoted
literals, and ordinary glob patterns in the modeled subset.

This iteration expands that subset to handle practical Bash `case` pattern
syntax and fallthrough behavior while keeping source effects exact:

```bash
case "$mode" in
  prod | "stage env" | qa\?)
    source ./enabled.sh
    ;;
  [[:digit:]]*)
    source ./numbered.sh
    ;;
esac
```

For accepted forms, executable output must contain no live unresolved `source`
or `.` commands. For unsupported forms, executable mode must fail closed before
writing output.

## Non-Goals

- Do not implement `extglob` case patterns yet.
- Do not implement full Bash `GLOBIGNORE` behavior.
- Do not model source commands in pipelines, subshells, command substitutions,
  or process substitutions.
- Do not add runtime tracing or supplement generation from observed execution.
- Do not support arbitrary dynamic pattern expansion.
- Do not preserve live source commands in executable output.

## Tranche 1: Practical Case Pattern Normalization

Implemented more exact pattern syntax without changing case control-flow shape.

Supported forms:

- mixed quoted and unquoted literal segments in one pattern
- backslash-escaped literal characters
- bracket expressions that can be translated exactly
- POSIX character classes in bracket expressions where Python translation can
  preserve Bash semantics for the modeled locale assumptions
- exact scalar variable-expanded patterns when the variable value is known and
  contains no unsupported shell pattern syntax

Acceptance:

- Exact known subjects select the same arm Bash would select for the supported
  pattern subset.
- Unknown runtime subjects preserve the original `case` and lower exact source
  sites in all runtime-possible arms.
- Pattern normalization is deterministic and does not depend on process cwd.
- Unsupported pattern features fail with stable `unsupported.source.case-*`
  diagnostics before output.
- Existing literal, quoted literal, glob, alternate, and default pattern tests
  remain green.

Implementation notes:

- Put pattern normalization behind a small helper used by both static matching
  and validation.
- Treat variable-expanded patterns as exact only after resolving through the
  evaluator state, source supplement variables, or process environment in the
  existing precedence order.
- Keep pattern validation separate from branch body evaluation so source-free
  invalid patterns do not turn into linting work unless they affect
  source-relevant lowering.

## Tranche 2: Case Fallthrough Terminators

Implemented Bash `case` fallthrough terminators for source-bearing arms:

- `;&` executes the next arm body without testing its pattern.
- `;;&` continues pattern testing at the next arm.

Acceptance:

- Known-subject cases with `;&` execute the matched arm and subsequent
  fallthrough bodies in Bash order.
- Known-subject cases with `;;&` continue testing later arms in Bash order.
- Runtime-dynamic subjects preserve the original `case` shape and lower exact
  source sites only when generated output can preserve the same fallthrough
  behavior.
- Source effects from possible fallthrough paths merge conservatively.
- Later source sites fail closed if they depend on branch-divergent variables,
  arrays, cwd, shell options, functions, positionals, or return flow.
- Branch-dependent source or function returns remain unsupported unless all
  possible outcomes have equivalent status/type semantics.

Implementation notes:

- Reuse existing possible-outcome merging rather than introducing a separate
  case evaluator.
- Represent arm reachability as ordered execution paths, not as a flat
  one-arm-selected result.
- Keep `;;` behavior unchanged.

## Tranche 3: Real-World And Runtime Promotion

Added focused real-world fixtures and parity probes for the newly supported
case forms.

Acceptance:

- Synthetic tests cover known subjects, runtime subjects, mixed quoting,
  escapes, bracket/POSIX classes, exact variable-expanded patterns, `;&`, and
  `;;&`.
- Pinned real-world fixtures include at least one practical source-bearing case
  with expanded pattern syntax.
- Runtime parity probes cover selected arm, skipped arm, fallthrough execution,
  and runtime-subject preservation.
- Generated executable artifact scan reports no live unresolved source
  commands.
- Generated artifacts are manually/LLM reviewed for readable case structure,
  correct source block placement, and no unexpected removed lines.

## Review Checklist

Required before merging implementation work:

- `python -m unittest -v`
- `MODASH_REALWORLD=1` pinned corpus
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes
- executable artifact scan for live unresolved source commands
- manual/LLM spot-check of promoted real-world executable and context outputs

## Later Work

After this iteration, the major remaining static/runtime gaps are:

- source-bearing pipelines and subshells, including child-shell versus
  parent-source semantics
- source commands in command substitutions and process substitutions
- `extglob` patterns and full `GLOBIGNORE` edge behavior
- recursive or runtime-dynamic source-bearing dispatch
- xtrace/runtime source discovery and supplement generation
