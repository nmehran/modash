# Source Pattern Semantics Completion

## Status

Implemented on the `iteration/source-pattern-semantics` development branch.

This iteration stays static. It does not run Bash, collect xtrace output,
discover runtime source paths, or broaden arbitrary shell validation. The goal
is to close the remaining deterministic pattern surface that affects source
resolution: pathname expansion, `GLOBIGNORE`, `extglob`, and modeled
case/predicate pattern matching.

## Starting Gaps

After source-argument, control-flow, retained-helper, and child-shell work, the
largest static gaps are:

- deterministic pattern semantics: `extglob`, full `GLOBIGNORE`, and exact
  glob-option interactions
- remaining `case` and `[[ ... ]]` pattern edge semantics
- source guard predicates whose behavior depends on glob or pattern context
- recursive/runtime-dynamic function dispatch
- runtime source discovery and supplement generation

This iteration takes the deterministic pattern family. Recursive dispatch and
xtrace/runtime discovery remain separate later work because they require
runtime input or broader execution modeling.

## Implemented Scope

- Condition evaluation now distinguishes pathname expansion, single-bracket
  argv semantics, double-bracket literal file tests, and pattern matching.
- Exact `extglob` pathname expansion is supported for direct source globs,
  loop word globs, command word-list path operands, and exact source guards.
- Exact `GLOBIGNORE` filtering supports colon-separated pattern lists,
  extglob-aware ignore patterns, empty/null values, and dotfile side effects
  for accepted source-producing patterns.
- Modeled `case` arms and `[[ string == pattern ]]` predicates support the
  deterministic pattern subset, including extglob where Bash permits it.
- A pacman real-world fixture exercises extglob loops, GLOBIGNORE filtering,
  extglob case arms, double-bracket pattern matching, and source guards.

## Semantics Contract

- Executable mode must preserve Bash behavior for every supported pattern
  context.
- Pathname expansion and pattern matching are different contexts and must not
  share behavior accidentally.
- Exact shell option state controls pathname expansion. Branch-dependent
  `shopt`, `set -f`, or `GLOBIGNORE` state remains fail-closed.
- Locale-dependent behavior is supported only for an explicitly modeled
  deterministic subset. Anything requiring host locale collation or equivalence
  behavior remains fail-closed.
- Context mode may annotate unresolved pattern-required sites, but executable
  mode must not leave live source commands for accepted forms.

## Non-Goals

- Do not add xtrace, runtime discovery, sandbox execution, or observed-source
  supplement generation.
- Do not implement recursive or runtime-dynamic source-bearing function
  dispatch.
- Do not add broad word splitting for source arguments.
- Do not replace the line frontend with a full Bash parser in this iteration.
- Do not guess locale-specific collation or equivalence behavior.

## Tranche 1: Pattern Context Boundaries

Goal: make the evaluator and tests distinguish Bash pattern contexts before
adding broader pattern features.

Target contexts:

```bash
source ./plugins/*.sh              # pathname expansion
for dep in ./plugins/*.sh; do ...  # pathname expansion
[ -f ./plugins/*.sh ]              # command argv after pathname expansion
[[ -f ./plugins/*.sh ]]            # no pathname expansion
[[ "$ENV" == prod* ]]              # pattern matching
case "$ENV" in prod*) ... ;; esac  # pattern matching
```

Acceptance:

- Direct source globs and loop globs continue to use pathname-expansion
  semantics.
- Single-bracket file tests with exact glob operands model Bash argv behavior:
  no match, one match, and multi-match cases are distinct.
- Double-bracket file tests do not perform pathname expansion and treat the
  operand as the literal tested path.
- `[[ "$value" == pattern ]]` and `case` arms use pattern matching semantics,
  not filesystem expansion semantics.
- Existing supported ordinary glob, branch, case, and child-shell tests remain
  green.

Reject:

- Branch-dependent glob option state before a source-relevant pattern.
- Ambiguous current directory before a relative pathname expansion.
- Pattern contexts that require unsupported parser recovery.

Implementation notes:

- Prefer a small internal context enum over boolean flags such as "is glob".
- Keep filesystem expansion helpers separate from string pattern match helpers.
- Add focused tests that prove `[[ -f *.sh ]]` does not accidentally use the
  pathname-expansion path.

## Tranche 2: `extglob` And Full `GLOBIGNORE`

Goal: support deterministic pathname expansion in source-relevant sites when
glob-affecting state is exact.

Target examples:

```bash
shopt -s extglob
source ./plugins/@(core|extra).sh
for dep in ./plugins/!(skip).sh; do source "$dep"; done

GLOBIGNORE='*/skip.sh:*.bak'
source ./plugins/*.sh
```

Acceptance:

- Exact `shopt -s extglob` / `shopt -u extglob` state controls pathname
  expansion for direct source globs and loop word globs.
- Supported extglob operators include `?(...)`, `*(...)`, `+(...)`, `@(...)`,
  and `!(...)` for deterministic filesystem matching.
- Quoted extglob metacharacters remain literal where Bash treats them as
  literal.
- Exact `GLOBIGNORE` filtering matches Bash for colon-separated pattern lists,
  empty/null values, dotfile side effects, and the implicit exclusion of `.`
  and `..`.
- `nullglob`, `failglob`, `dotglob`, `globstar`, `nocaseglob`, `set -f`, and
  `GLOBIGNORE` interactions are tested together for accepted and rejected
  source sites.
- All-ignored direct source globs are handled by
  [Missing Source Runtime Error Lowering](missing-source-runtime-lowering.md);
  direct brace-only expansion, exact `nullglob` source-word shifting, and
  direct `failglob` expansion failures are handled by
  [Source Expansion Failure Semantics](source-expansion-failure-semantics.md).

Reject:

- Runtime-dynamic `GLOBIGNORE` values.
- Branch-dependent shell option or `GLOBIGNORE` state before a source-relevant
  glob.
- Locale-dependent filesystem ordering or matching outside the modeled
  deterministic subset.

Implementation notes:

- Keep traversal order aligned with current Bash/GNU behavior used by the real
  corpus tests.
- Use structured pattern parsing rather than expanding extglob with ad hoc
  regular expression replacements.
- If a narrow extglob engine becomes too large, land the pathname-expansion
  context split first and keep extglob fail-closed.

## Tranche 3: Case, Predicate, And Real-World Promotion

Goal: apply the shared pattern semantics to modeled `case` arms and practical
source guard predicates, then prove the behavior with runtime parity fixtures.

Target examples:

```bash
case "$ENV" in
  @(prod|stage)) source ./prod.sh ;;
  !(prod|stage)) source ./default.sh ;;
esac

if [[ "$ENV" == @(prod|stage) ]]; then
  source ./prod.sh
fi

if [ -f ./plugins/@(core|extra).sh ]; then
  source ./plugins/@(core|extra).sh
fi
```

Acceptance:

- Modeled `case` arm patterns support the same deterministic pattern subset as
  `[[ string == pattern ]]` where Bash semantics align.
- Runtime-guarded `case` lowering preserves exact source sites for all
  runtime-possible arms when every arm pattern is supported.
- Practical source guard predicates preserve Bash distinction between
  single-bracket argv/pathname expansion and double-bracket literal file tests.
- Unsupported collating symbols, equivalence classes, and broader
  locale-dependent matching fail with explicit diagnostics.
- Add synthetic runtime parity tests plus a controlled real-world fixture that
  exercises extglob, `GLOBIGNORE`, and a source guard.
- Generated executable artifacts contain no live unresolved source commands for
  accepted pattern cases.

Reject:

- Pattern arms whose behavior depends on unmodeled locale collation or
  equivalence classes.
- Branch-dependent pattern variables that later feed source resolution.
- Guard predicates whose command semantics are not modeled exactly.

## Done Definition

- `python -m unittest -v` passes.
- `MODASH_REALWORLD=1` pinned corpus tests pass.
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes pass
  for promoted pattern fixtures.
- Executable artifact scan reports zero live unresolved source commands for
  accepted pattern cases.
- Generated real-world artifacts receive the manual/LLM spot-check pass.
- `git diff --check` passes.

## Deferred After This Iteration

- recursive or runtime-dynamic source-bearing function dispatch
- xtrace/runtime source discovery and supplement generation
- full parser replacement for unsupported shell grammar
- broad source argument word splitting
