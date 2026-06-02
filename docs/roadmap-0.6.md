# modash 0.6 Release Scope

Status: 0.6.0 release candidate.

0.6 is the coverage-completion milestone after the 0.5 trusted runtime graph
release. The goal is not another diagnostic layer. The goal is to make the
runtime graph path and the static resolver cover the known remaining product
gaps well enough that real shell-heavy projects can be merged with confidence.

## Release Bar

During implementation, 0.6 was not ready while any of these were still merely
documented as future work:

- broader real-world `observe-compile` promotion beyond the controlled
  pacman/mkinitcpio fixtures
- finite runtime-dynamic helper shapes where a trusted graph can keep replay
  deterministic and reviewable
- recursive or runtime-dynamic source dispatch for executions that produce a
  finite trusted graph
- known static source-resolution edge semantics that block deterministic merge
  of real projects

Expected failures are allowed only as temporary tranche checkpoints. They are
not a 0.6 acceptance state for the categories above.

The core 0.6 rule:

> If an execution can produce a trusted, finite runtime source graph, `modash`
> should compile from that graph with parity for that execution. Static mode
> should cover deterministic source-resolution behavior without requiring a
> trace.

## Tranche 1: Diverse Real-World Coverage Matrix

Add corpus coverage by behavior class rather than by convenience. The real-world
suite should prove that `modash` is not overfit to pacman/makepkg or
mkinitcpio-style hook loops.

Each promoted candidate must record:

- project and pinned version
- source-resolution behavior class
- static context result
- static executable result
- trace result
- trusted graph replay result
- `observe-compile` result
- runtime parity result when executable output is produced
- whether any unsupported behavior is a temporary 0.6 blocker

Behavior classes to cover:

- completion forests and shell libraries
- initramfs or boot hook dispatch
- package/build helper libraries
- plugin or module loaders
- distro maintainer or install scripts
- generated config/load-path scripts
- child Bash and command-string source dispatch

Acceptance:

- at least three new behavior classes beyond the current pacman/mkinitcpio
  coverage
- no lazy "failure as success" records for targeted 0.6 behavior
- every unsupported record has a concrete implementation follow-up and remains
  an active 0.6 blocker until resolved

## Tranche 2: Finite Runtime-Dynamic Helper Shapes

Generalize trusted graph supplement generation and graph replay for helper
patterns that are dynamic in shell syntax but finite in an observed graph.

In-scope helper shapes:

- wrapper chains where helper A calls helper B before the source
- helper-local aliases beyond first-argument aliases
- shifted positional frames across helper calls
- helper-local arrays that feed source arguments
- finite loops over helper arguments or helper-local arrays
- exact `case` or finite dispatch tables inside helpers
- retained helper definitions that remain callable after merge and can be
  constrained by graph-backed exact argument vectors

Acceptance:

- synthetic coverage for each supported shape
- graph tampering tests for each shape that depends on graph trust
- real-world promotion for every shape that was motivated by corpus pressure
- generated executable output contains no live dynamic source for observed paths
  and fails closed for unobserved paths

## Tranche 3: Recursive And Runtime-Dynamic Dispatch

Support recursive and runtime-selected source dispatch when the observed graph
is finite, trusted, and replayable. This is not arbitrary eval. It is
deterministic replay from reviewed graph edges.

In-scope dispatch shapes:

- recursive helper calls with finite observed depth
- dynamically selected helper names when xtrace and wrapper provenance identify
  the call site and source edge
- repeated source call sites with multiple observed edges
- loops whose iteration values are runtime-selected but fully observed
- source dispatch through child Bash where provenance remains linked to the
  parent graph

Required safety properties:

- graph replay consumes observed edges in order per precise call-site identity
- missing, duplicate, stale, or mismatched graph edges fail before output
- unobserved runtime paths lower to explicit fail-closed dispatch, not a live
  unresolved source command
- recursion depth is graph-backed and finite; unobserved extra recursion fails
  closed

Acceptance:

- parity tests for recursive helper replay
- parity tests for dynamic helper-name replay
- tamper tests for missing, duplicate, reordered, and stale recursive edges
- at least one real-world fixture whose static mode cannot resolve the dispatch
  but `observe-compile` can

## Tranche 4: Static Edge Semantics Completion

Close static source-resolution gaps that block deterministic real-world merges.
This tranche should be driven by the coverage matrix, but it is explicitly part
of 0.6 rather than a deferred bucket.

In-scope static work:

- locale-sensitive `case` behavior that can be modeled deterministically under
  the active locale or a pinned evaluation locale
- broader supported shell grammar around source-bearing constructs
- remaining guard predicates that decide exact source reachability
- command prefixes and assignment forms that affect source resolution
- shell option interactions that change expansion or source argument selection
- child-shell and command-string boundaries that can be parsed exactly

Acceptance:

- every static gap surfaced by the 0.6 matrix is either implemented or has a
  failing regression test marked as a 0.6 blocker
- deterministic static behavior has Bash parity tests
- unsupported static behavior fails before output with a precise diagnostic
  until implemented
- static support does not weaken executable mode's no-live-unresolved-source
  guarantee

## Tranche 5: Real-World Promotion And Artifact Review

Turn successful probes into pinned expectations and reviewable artifacts.

Acceptance:

- generated context and executable artifacts are spot-checked for each new
  promoted behavior class
- trusted graph reports are readable enough to review without opening raw JSON
- result summaries distinguish static success, runtime graph success,
  observe-compile success, parity success, timeout, and active blocker
- no promoted 0.6 target treats an unresolved source failure as success

## Tranche 6: Maintainability Cleanup

0.6 should not leave the static engine harder to extend. The main target is
`source_evaluator.py`, which has accumulated condition evaluation, expansion,
function execution, child-shell handling, and runtime graph replay behavior.

Cleanup targets:

- split condition and guard evaluation from source expansion
- split helper/function execution from top-level source traversal
- isolate runtime graph override replay from static source discovery
- move child-shell handling behind a focused boundary
- keep diagnostics stable while moving code

Acceptance:

- no behavior-only refactor without full unit and real-world gates
- no broad compatibility shims for old internal APIs
- net readability improvement in the modules touched

## 0.6 Done Criteria

0.6 is done when:

- the real-world suite covers diverse behavior classes, not correlated variants
- `observe-compile` succeeds with parity for runtime-dynamic source dispatch
  cases that produce trusted finite graphs
- finite helper, recursive, and runtime-selected dispatch have synthetic and
  real-world coverage
- deterministic static source-resolution gaps surfaced by the matrix are closed
- generated artifacts have passed manual review
- release docs describe the supported behavior without exposing internal schema
  details in the user-facing README
