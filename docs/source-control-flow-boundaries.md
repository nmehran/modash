# Source-Relevant Control Flow Boundaries

## Status

Implemented on the `iteration/source-control-flow-boundaries` development
branch. It builds on the completed source-argument semantics work and stays
static: no xtrace, runtime source discovery, or shell execution during
compilation.

## Summary

The compiler now handles the main static source-argument and wrapped-source
positionals cases. The next practical gap is control flow that is either not
source-relevant or source-bearing in a narrow, lowerable way.

Executable mode is now less eager to reject ordinary Bash logic while still
failing closed when dependency resolution or generated output would become
unsafe.

## What Remains

Current high-value remaining gaps after this iteration:

- Runtime-guarded static lowering for exact source sites inside unknown `if`
  predicates and runtime `case` subjects is implemented in
  [Runtime-Guarded Static Source Lowering](runtime-guarded-source-lowering.md).
- Exact source atoms in top-level `if` / `elif` logical lists are covered by
  [Compound Source Condition Lowering](compound-source-condition-lowering.md).
  Pipelines, command substitutions, and source conditions outside the modeled
  direct-condition, logical-list, and guarded-branch subsets remain
  unsupported.
- Source-free unsupported loops are passed through with conservative state
  merging, but broad loop/runtime semantics remain intentionally narrow.
- Broader source guards beyond exact file/glob tests and exact shell-option
  predicates remain narrow.
- `extglob`, full `GLOBIGNORE` edge behavior, broader case pattern semantics,
  recursive/runtime-dynamic function dispatch, branch-dependent returns, and
  runtime source discovery remain later work.

## Non-Goals

- Do not run Bash or collect xtrace output.
- Do not infer dependencies from runtime-observed behavior.
- Do not silently preserve live `source` or `.` commands in executable output.
- Do not model arbitrary command predicates as a side effect of this iteration.
- Do not broaden recursive or runtime-dynamic function dispatch.

## Tranche 1: Source-Free Control Flow Pass-Through

Executable mode does not fail just because an unsupported `if`, `case`, or
loop predicate exists. It should fail only when unsupported control flow affects
source resolution or source-relevant lowering.

Acceptance:

- An unsupported condition with no reachable source sites compiles and renders
  unchanged.
- If a source-free unsupported branch mutates variables, cwd, shell options,
  arrays, or functions that a later source site needs exactly, the later source
  site still fails closed with a source-relevant diagnostic.
- Branch bodies with no source sites are not evaluated for lint-like validity.
- The pinned bash-completion `completions/cd` executable case is promoted from
  unsupported to success if the generated artifact contains no live source.

Implementation notes:

- Add a source-relevance scan over IR subtrees.
- For unsupported source-free control flow, keep original text in output and
  conservatively mark source-relevant state as ambiguous where needed.
- Preserve current strict behavior when the unsupported construct contains or
  guards a source site.

## Tranche 2: Exact Source Conditions

Simple source commands used directly as `if` conditions are supported:

```bash
if source ./dep.sh; then
  echo loaded
fi

if ! source ./dep.sh; then
  echo failed
fi
```

Acceptance:

- `if source FILE [args...]` and `if ! source FILE [args...]` lower in
  executable mode when the source path and source arguments are exact.
- Lowering preserves Bash status, parent-visible sourced state, `return` from
  sourced files, and subsequent branch execution.
- Context mode annotates the source relationship without claiming runtime
  parity.
- The generated executable contains no live source command for the lowered
  condition.
- Compound conditions, pipelines, command substitutions, multiple source
  conditions, and mixed arbitrary predicates remain fail-closed unless a later
  tranche explicitly models them.

Implementation notes:

- Reuse existing source-site rendering and sourced-file return wrappers.
- Treat the lowered source condition as a status-producing same-shell block.
- Keep exact diagnostics for unsupported source expressions and arguments.

## Tranche 3: Practical Guard Predicate Expansion

This iteration adds a small predicate subset that directly gates source sites and is common in
shell-heavy projects.

Candidate predicates:

- `[ -f ./plugins/*.sh ]`, `[ -r FILE ]`, and `test -f FILE` when the file or
  glob result is exact.
- `shopt -q OPTION` for known shell options when the option state is exact,
  including Bash's non-interactive default-enabled shopt options.
- Existing safe `grep -q` style predicates should remain covered and should not
  be broadened to arbitrary command execution.

Acceptance:

- Guarded source branches with exact file/glob predicates lower correctly.
- Unknown or multi-result predicate state remains fail-closed when it gates a
  source site.
- Source-free guard predicates can pass through under Tranche 1.

## Real-World And Review

Each tranche should have synthetic Bash parity tests first, then real-world
promotion where applicable.

Required checks:

- `python -m unittest -v`
- `MODASH_REALWORLD=1` pinned corpus
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes
- generated executable artifact scan for live `source` commands
- manual/LLM artifact spot-check for promoted real-world outputs

Expected real-world movement:

- bash-completion `completions/cd` executable is promoted from unsupported to
  success because the previously blocking `shopt -q cdable_vars` condition is
  source-free from modash's dependency perspective.
- Add one controlled fixture for direct `if ! source ./dep.sh; then` before
  distilling this branch.
- Keep top-level `bash_completion` timeouts as performance triage unless a
  small profiling pass identifies an obvious non-invasive fix.

## Later Work

Runtime source discovery with xtrace remains a later 0.5-class feature. This
iteration should make the static compiler more precise before introducing any
runtime-observed supplement generation.
