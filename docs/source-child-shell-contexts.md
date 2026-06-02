# Source-Bearing Child-Shell Contexts

## Status

Implemented on the `iteration/source-child-shell-contexts` development branch.

This iteration stays static. It does not run Bash, collect xtrace output,
discover runtime source paths, execute `bash -c` payloads for discovery, or
broaden arbitrary shell validation. It resolves exact source dependencies that
execute inside child-shell-like boundaries when Bash-equivalent lowering is
provable.

## Scope Taken

After explicit source-argument frame restoration, the main remaining
source-resolution gaps are:

- source-bearing child-shell contexts: subshells, pipelines, command
  substitutions, process substitutions, and `bash -c`
- `extglob`, full `GLOBIGNORE` edge behavior, and remaining locale-dependent
  case pattern semantics
- recursive or runtime-dynamic source-bearing dispatch beyond finite modeled
  helpers
- xtrace/runtime source discovery and supplement generation

This iteration takes the child-shell family because it is common in real shell
projects and can be advanced without runtime tracing if the compiler preserves
the child-shell boundary instead of pretending those effects are parent-visible.

## Semantics Contract

- A source site inside a proven child-shell context is still a dependency and
  may be resolved and lowered inside that execution boundary.
- Child-shell source effects do not update parent evaluator state. Variables,
  functions, aliases, cwd, shell options, traps, positional parameters, and
  top-level `return` behavior remain contained by the child shell.
- Source output and status inside the child context must match Bash for the
  supported forms.
- Executable mode must not leave live unresolved `source` / `.` commands for
  supported child-shell sites.
- Unsupported child-shell source forms fail closed before executable output is
  written or overwritten.
- Context mode may annotate child-shell dependencies, but it must not imply
  parent-source effects.

## Non-Goals

- Do not add xtrace, runtime discovery, sandbox execution, or observed-source
  supplement generation.
- Do not model arbitrary child-shell grammar.
- Do not treat child-shell source effects as parent-visible.
- Do not support recursive or runtime-dynamic source-bearing dispatch.
- Do not expand `extglob`, full `GLOBIGNORE`, or locale-dependent pattern
  semantics in this iteration.
- Do not preserve live unresolved source commands in executable output.

## Tranche 1: Explicit Subshell Groups

Goal: lower exact source sites inside explicit subshell command groups while
preserving Bash child-shell containment.

Target examples:

```bash
( source ./dep.sh; printf '%s\n' "$VALUE" )
( . ./dep.sh && run_child_only )
```

Acceptance:

- Exact literal, variable-resolved, supported glob, and supported argument
  source sites inside `( ... )` compile in executable mode.
- Original and compiled output/status match for supported examples.
- Parent-visible variables, functions, aliases, cwd, shell options, traps, and
  positional parameters remain unchanged after the subshell finishes.
- Top-level `return` inside a sourced child-shell dependency stops only the
  sourced body and preserves source status inside the subshell.
- Nested supported sources inside the subshell are lowered in the same
  child-shell boundary.
- Executable artifacts contain no live source commands for accepted forms.

Reject:

- Ambiguous or unresolved source paths.
- Child-shell bodies whose source behavior depends on unsupported grammar.
- Any lowering that would require child-shell effects to mutate modeled parent
  state.

Implementation notes:

- Prefer marking evaluator state with an explicit child-shell boundary and
  discarding child-local mutations after the boundary.
- Reuse existing source wrappers where possible, but render them inside the
  subshell body so same-shell behavior is preserved for commands that follow
  the source inside the child shell.
- Keep diagnostics explicit when a source site is rejected because it is inside
  an unsupported child-shell construct.

## Tranche 2: Pipeline Segments And Command Substitutions

Goal: handle common child-shell-capable forms that have useful static behavior
without broad stream or runtime evaluation.

Target examples:

```bash
source ./dep.sh | cat
value="$(source ./dep.sh; printf '%s' "$VALUE")"
```

Acceptance:

- Exact source sites in a pipeline segment compile only when Bash guarantees
  child containment for that segment and no later parent resolution depends on
  those child-local effects.
- Pipeline segment output/status behavior matches Bash for supported examples.
- Command substitutions compile only for narrow exact source-bearing command
  lists whose output and status are preserved by the existing renderer shape.
- Parent state after the pipeline or substitution is unchanged.
- Unsupported pipeline and substitution forms produce stable diagnostics.

Reject:

- Source paths derived from pipeline output or generated command-substitution
  streams.
- Command substitutions that need arbitrary runtime execution to discover
  source paths.
- Any form where child-local mutations are later used as parent source
  resolver input.
- Pipeline final segments whose semantics depend on `shopt -s lastpipe`, job
  control state, or other shell execution options not modeled exactly.

Implementation notes:

- It is acceptable to split command substitutions out of this tranche during
  implementation if renderer support becomes disproportionate.
- Pipeline segment handling should be segment-local. Do not convert pipeline
  sources into parent-source effects merely because they are statically exact.
- Treat `lastpipe` as a correctness boundary. If the compiler cannot prove the
  segment runs in a child process, it should reject rather than assume ordinary
  pipeline subshell behavior.

## Tranche 3: Process Substitution And `bash -c` Boundaries

Goal: classify and, where safely possible, lower remaining child-shell-like
source boundaries without pretending to solve runtime-dynamic dispatch.

Target examples:

```bash
bash -c 'source ./dep.sh; printf "%s\n" "$VALUE"'
diff <(source ./left.sh; emit_left) <(source ./right.sh; emit_right)
```

Acceptance:

- Context mode classifies source-bearing process substitutions and `bash -c`
  payloads consistently as child-shell dependencies.
- Executable mode lowers exact process-substitution sources and exact static
  `bash -c` payloads when argv, assignment-prefixed environment, cwd, output,
  and status semantics are preserved.
- Unsupported `bash -c` and process-substitution forms fail with stable,
  specific diagnostics instead of falling through to generic unresolved source
  text.
- Real-world fixtures and runtime parity probes cover at least one accepted
  child-shell form and one deliberate rejection per boundary family.

Reject:

- Runtime-built `bash -c` payload strings.
- Parent-expanded double-quoted `bash -c` payloads that contain `$`.
- `bash -c` payloads with extra argv entries until `$0` / positional binding is
  modeled.
- `bash -c` payloads with multiple source commands or dynamic source
  expressions.
- Dynamic function dispatch inside a child shell.
- Process substitutions that require unresolved producer/consumer stream
  modeling.
- Payloads whose argument binding, environment, or cwd semantics are not exact.

Implementation notes:

- `bash -c` lowering is intentionally narrower than general Bash invocation
  modeling. Static single-source payloads with no extra argv are supported.
  Dynamic payload construction, `$0` / positional argument binding, and
  multiple source commands remain fail-closed.
- Single-quoted payloads may contain child-Bash variable references after the
  exact source site. Double-quoted payloads containing `$` are rejected because
  the parent shell would expand them before `bash -c` runs.

## Real-World And Runtime Promotion

Acceptance:

- Add focused synthetic compile/runtime tests for every accepted and rejected
  child-shell boundary above.
- Add controlled real-world fixtures before promoting broader corpus
  expectations.
- Runtime parity probes compare original Bash execution and compiled
  executable output for safe exact subshell and pipeline examples.
- Generated executable artifacts scan clean for live unresolved source
  commands.
- Context artifacts annotate child-shell dependency boundaries clearly enough
  for manual/LLM review.

## Done Definition

- `python -m unittest -v` passes.
- `MODASH_REALWORLD=1` pinned corpus tests pass.
- `MODASH_REALWORLD=1 MODASH_REALWORLD_RUNTIME=1` runtime parity probes pass
  for the promoted child-shell fixtures.
- executable artifact scan reports zero live unresolved source commands for
  accepted child-shell cases.
- generated real-world artifacts receive the manual/LLM spot-check pass.
- `git diff --check` passes.

## Deferred After This Iteration

- `extglob`, full `GLOBIGNORE`, collating symbols, equivalence classes, and
  broader locale-dependent pattern behavior
- recursive or runtime-dynamic source-bearing function dispatch
- xtrace/runtime source discovery and supplement generation
