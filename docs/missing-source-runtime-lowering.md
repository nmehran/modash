# Missing Source Runtime Error Lowering

## Status

Implemented on the `iteration/missing-source-runtime-lowering` development
branch.

This iteration stays static. It does not run Bash, collect xtrace output,
discover runtime source paths, or treat missing dependencies as successful
dependencies. The goal is narrower: when Bash would execute a source command
whose source path is deterministically missing because pathname expansion
produced no usable source file, executable mode should lower that runtime error
instead of failing compilation.

## Starting Gap

After pattern semantics completion, deterministic source-producing globs are
well covered when they resolve to one or more regular files. The main remaining
glob-shaped gap is the opposite outcome:

```bash
source ./missing/*.sh

GLOBIGNORE='./plugins/core.sh:./plugins/extra.sh'
source ./plugins/*.sh

for dep in ./optional/*.sh; do
  source "$dep"
done
```

Bash does not treat every empty expansion the same way:

- With ordinary glob state, an unmatched source glob remains a literal word and
  `source` reports a missing file with status `1`.
- When `GLOBIGNORE` filters every matched source path and `nullglob` is not set,
  the original pattern also remains literal and `source` reports a missing file
  with status `1`.
- With `nullglob`, the missing glob word disappears. A bare `source` command
  then reports "filename argument required" with status `2`; a command with
  later words may shift the next word into the filename position.
- With `failglob`, expansion itself fails before `source` runs.

The compiler currently fails closed for these cases because executable output
cannot preserve a live `source` command. This iteration lowers the deterministic
runtime failures that can be represented without executing shell code.

## Semantics Contract

- Executable mode must never leave a live unresolved `source` or `.` command in
  accepted output.
- Missing-source lowering represents a runtime source command failure, not a
  resolved dependency.
- The lowered block must preserve command status for direct execution, `&&`,
  `||`, `if source ...`, `if ! source ...`, and modeled runtime-guarded source
  sites.
- Lowering must preserve parent-shell behavior: no subshell may hide the command
  status from surrounding shell control flow.
- Bash diagnostic text should be stable and Bash-shaped. Tests may normalize
  script-name and line-number prefixes when generated output cannot preserve
  the original physical line number.
- Context mode remains readable-first: it may annotate the missing-source
  outcome, but executable mode owns the no-live-source guarantee.

## Non-Goals

- Do not add xtrace, runtime discovery, sandbox execution, or observed-source
  supplement generation.
- Do not treat arbitrary missing literal source paths as resolved dependencies
  in this iteration unless they arise from an accepted source-producing glob
  outcome.
- `failglob` expansion abort semantics are handled by
  [Source Expansion Failure Semantics](source-expansion-failure-semantics.md).
- Do not broaden source-argument word splitting.
- Exact `nullglob` cases where later command words become the source filename
  are handled by
  [Source Expansion Failure Semantics](source-expansion-failure-semantics.md).
- Do not replace the line frontend with a full Bash parser.

## Implemented Scope

- Ordinary unmatched direct source globs lower to same-shell missing-file
  failures with status `1`.
- Direct source globs whose exact `GLOBIGNORE` removes every matched source file
  lower to the same missing-file failure when `nullglob` is not set.
- Bare direct source globs that disappear under exact `nullglob` lower to
  Bash's no-filename source failure with status `2`.
- Finite `for` loop glob words preserve Bash's distinction between one literal
  missing pattern and zero `nullglob` iterations.
- Lowered failures preserve status for `$?`, `&&`, `||`, `if source ...`, and
  `if ! source ...`, and executable output contains no live source command for
  accepted missing-source sites.

## Tranche 1: Direct Source Missing Runtime Errors

Goal: lower deterministic missing-source outcomes for direct source commands.
Implemented.

Target examples:

```bash
source ./missing/*.sh
source ./missing/*.sh || echo optional

if source ./missing/*.sh; then
  echo loaded
else
  echo skipped
fi

if ! source ./missing/*.sh; then
  echo missing
fi
```

Acceptance:

- Ordinary unmatched direct source globs lower to a Bash-equivalent missing-file
  source failure with status `1`.
- Direct source globs whose matches are completely removed by exact
  `GLOBIGNORE` lower to the same missing-file source failure when `nullglob` is
  not set.
- Bare direct source globs that disappear under exact `nullglob` lower to
  Bash's no-filename source failure with status `2`.
- Lowered status controls following `&&`, `||`, direct `$?` capture, `if
  source`, and `if ! source`.
- Existing successful direct glob cases still source the first match and pass
  remaining expanded words as source arguments.
- Generated executable output contains no live source command for the lowered
  missing-source site.

Reject:

- Runtime-dependent `failglob` guards and failglob failures inside unsupported
  shell grammar.
- Branch-dependent or runtime-dynamic glob state.
- Missing-source sites inside unsupported shell grammar.

Implementation notes:

- Prefer an explicit source-failure event or replacement kind over pretending a
  missing path is a real dependency.
- Keep path display words separate from filesystem paths, because the diagnostic
  should name the shell word Bash would have attempted to source.
- Reuse existing source-site replacement machinery so status-producing blocks
  work inside direct, negated, and compound source condition paths.

## Tranche 2: Loop And Finite Expansion Missing Outcomes

Goal: apply the same missing-source model to finite source-producing loops and
word-list expansion paths. Implemented.

Target examples:

```bash
for dep in ./missing/*.sh; do
  source "$dep"
done

shopt -s nullglob
for dep in ./missing/*.sh; do
  source "$dep"
done
```

Acceptance:

- A non-`nullglob` unmatched loop glob runs the loop once with the literal
  pattern, and a body source of that loop variable lowers to a missing-file
  source failure with status `1`.
- A `GLOBIGNORE` all-filtered loop glob behaves the same as the literal pattern
  case when `nullglob` is not set.
- A `nullglob` empty loop word list runs zero body iterations and preserves the
  Bash loop status for surrounding `&&`, `||`, and `$?` capture.
- Lowering composes with existing modeled loop bodies, source arguments, current
  directory state, and shell-option state.
- Existing successful loop glob, brace, extglob, and `GLOBIGNORE` cases remain
  green.

Reject:

- Runtime-dependent `failglob` loop guards and failglob loop failures inside
  unsupported shell grammar.
- Loop bodies whose source path depends on unsupported mutation between the loop
  variable assignment and the source command.
- Dynamic loop word producers that cannot prove whether the source path is
  missing or present.
- Broad word-splitting cases outside the current exact loop model.

Implementation notes:

- The evaluator should distinguish "zero iterations" from "one literal missing
  word" before source resolution.
- Missing-source events should carry enough state to preserve occurrence model,
  execution model, replacement kind, source expression, and display word.
- Keep source-free loop pass-through behavior unchanged.

## Tranche 3: Promotion, Diagnostics, And Artifact Review

Goal: make the behavior observable, documented, and hard to regress.
Implemented with synthetic runtime parity coverage and support-matrix updates.

Acceptance:

- Add synthetic runtime parity tests for direct missing source globs,
  all-filtered `GLOBIGNORE`, `nullglob` bare source failures, and loop literal
  missing-word behavior.
- Add safety tests for runtime-dependent `failglob`, branch-dependent glob state,
  and unsupported grammar.
- Add or promote a real-world fixture only if it represents the supported
  static contract without requiring runtime discovery.
- Update the support matrix and dynamic-source docs after implementation.
- Generated executable artifacts pass the no-live-source scan.
- Generated artifacts receive the manual/LLM spot-check pass we use for
  source-lowering iterations.

Required checks:

- `python -m unittest -v`
- `MODASH_REALWORLD=1` pinned corpus tests
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes
- `git diff --check`

## Deferred After This Iteration

- Arbitrary missing literal source files unrelated to source-producing glob
  expansion.
- Recursive or runtime-dynamic source-bearing function dispatch.
- Xtrace/runtime source discovery and supplement generation.
- Full parser replacement for unsupported shell grammar.
