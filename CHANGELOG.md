# Changelog

## v0.7.0 - 2026-06-03

### Added

- Added the 0.7 trusted runtime graph compiler. `compile-observed` now
  rewrites graph-backed source call sites into scope-preserving replay groups
  and bundles the observed source files, instead of routing replay through the
  static `SourceEvaluator` override path.
- Runtime graph compilation now fails closed on unmapped, over-consumed,
  unconsumed, stale, or unbundled graph edges before or during generated script
  execution.
- Runtime graph compilation preserves Bash's own source mechanics for trusted
  graph edges by executing Bash `source` at the rewritten source site,
  including caller locals, inherited no-argument source positionals, explicit
  source arguments, top-level `return`, nested observed sources, child
  `bash -c` payload arguments, and stable original physical-path `$0` /
  `BASH_SOURCE` references in bundled files.
- Added synthetic coverage for runtime-selected helpers, same-relative-path
  identical helper files, stable physical-path `$0` / `BASH_SOURCE` reference
  rewriting, top-level return propagation, and fail-closed unobserved graph-tape
  drift.
- Added adversarial runtime graph compiler coverage for computed generated-state
  mutation, `exec`, EXIT trap manipulation, trace-instrumentation-sensitive
  probes, command/builtin `eval`, live unobserved source dispatch, child
  `bash -c` wrapper drift, hostile replay setup environments, source file
  version drift during trace, source-entry-sensitive `PIPESTATUS` / `$_`
  references, dynamic validation-bypass command dispatch, child replay failure
  propagation, unsafe shopt-restore poisoning, generated `exit` bypasses,
  exported Bash function rejection, time-prefixed source replay behavior,
  and unsupported runtime `$0` / `BASH_SOURCE` forms.
- Added regressions for plain-`bash` launch rejection, embedded payload stream
  replay, child process replay success markers, `BASH_ENV` child startup
  neutralization, unquoted heredoc command-substitution bypasses, positional
  `read` replay-state guards, and array-glob arithmetic predicates from
  real-world pacman helpers.
- Added hardening regressions for runtime-validated shopt restore evals,
  trace-owned environment rejection, user `ENV` preservation, child replay
  failure propagation and marker-forgery rejection, top-level `declare` /
  `typeset` source-context drift,
  instrumentation-sensitive `env` / `set` probes, `read -a` / `mapfile`
  generated-state mutation, nested dynamic eval, unsupported coprocess
  execution, and source-free literal `eval echo` lookup forms found in real
  Git mergetool sources.
- Added regressions for generated replay `printf` function poisoning, source
  expansions inside array assignments, wrapped positional `builtin read` /
  `command read` replay-state mutation, dynamic later arguments to literal
  `eval`, absolute `printenv` and `/proc/*/environ` instrumentation probes, and
  child replay marker forgeries through `/proc/$$/environ`.
- Added regressions for child replay marker forgery through arbitrary external
  environment readers, `bash -c` implicit `$0` preservation, source-bearing
  non-Bash shell payload rejection, dynamic shell command `-c` rejection, safe
  literal `eval printf -v` replay-state mutation rejection, trace-owned
  `BASHOPTS` / `BASH_ALIASES` / file descriptor probes, and replay-critical
  command-resolution introspection.
- Added hardening regressions for forged generated helper calls, dynamic
  `eval` live-source bypasses, Bash prompt-transformation eval expansion,
  dynamic shell `-c` payloads, `mapfile` / `readarray` callbacks, mutable replay
  file races, dynamic array command dispatch, child replay marker forgery with
  abnormal child termination, legitimate child status `125`, and env-constrained
  dynamic helper path arguments.
- Added regressions for dynamic command dispatch drifting into `eval` source
  execution, inert sourced-library functions with source-capable dynamic tails,
  literal eval array-subscript command substitution, empty sourced files,
  computed external interpreter probes of trace environment, hidden
  source-bearing non-Bash shell payloads, and argv-visible child replay token
  forgery.
- Added review-driven hardening for inert sourced-library bodies, source-free
  literal eval lookup replay, attached `mapfile` / `readarray` callbacks,
  command-tail non-Bash shell wrappers, obfuscated external trace-environment
  probes, and source-relevant inherited environment drift.
- Added guarded direct-`kill` replay support for ordinary background-helper
  cleanup while rejecting guard-bypassing kill forms and current-shell targets.
- Added review-driven replay validation for original source argument expansion,
  source-entry status, selected source path/argument drift, sourcepath `PATH`
  drift, tilde `HOME` drift, unset-default parameter drift, generated dynamic
  guard `$?` preservation, `CDPATH` parity after secure startup, `eval` / `kill`
  guard override rejection, `env -S` source-bearing payload rejection, and
  trace-environment hiding from external interpreter probes.
- Added source-free external interpreter heredoc replay coverage while keeping
  heredoc bodies that inspect trace-owned environment fail-closed.
- Added review-driven regressions for physical-cwd source resolution when `PWD`
  is assigned by user code, assignment-prefixed source scope and sourcepath
  lookup, assignment command-substitution source-entry status, attached
  `printf -vNAME` replay-state mutation, command-tail `kill` bypasses,
  replay-time missing-source drift, source redirection replay, and external
  trace-fd probes.
- Added follow-up replay parity regressions for in-place dynamic-command guards
  in short-circuit, pipeline, and command-substitution contexts; source
  redirection expansion order; symlinked source path validation; source-free
  child Bash `CDPATH` behavior; and inert source-looking text passed to safe
  dynamic external commands.
- Added guarded direct-`exec` replay support so final process replacement can
  run after observed source setup while still validating consumed graph edges
  before the shell is replaced.
- Added review-driven replay support for safe cases that had been rejected too
  broadly: `time`-prefixed source sites, `time`-wrapped child Bash sources,
  ANSI-C quoted child `bash -c` payloads, static `command exec` / `builtin exec`
  final process replacement through the generated validation wrapper, and
  observed child Bash script invocations with runtime path validation.
- Added review-driven replay parity regressions for dynamic command argv and
  redirection expansion exactly-once behavior, full assignment-prefixed source
  export/array/readonly behavior, redirection target source-entry status, and
  simple `LINENO` references in generated entrypoints and child `bash -c`
  payloads.
- Added review-driven regressions for assignment-prefixed source-entry status
  drift from source arguments, assignment RHS, and redirection targets; dynamic
  command process-substitution arguments in entrypoint and child `bash -c`
  payloads; source redirection setup drift; and explicit `set -e`
  source-finalization trace diagnostics.
- Added review-driven regressions for assignment-prefixed nonzero source status
  replay, negated assignment-prefixed source sites, backslash-continued source
  commands, middle-position child `bash -c` source payloads, explicit-argument
  source files that assign caller positionals, source-free dynamic external `-c`
  payloads, child-shell source diagnostics, and process-substitution-backed
  source diagnostics.
- Added review-driven regressions and fixes for `source --` / `. --` option
  terminators, `set -e`-suppressed source contexts, one-line `select` source
  replay, and safe literal associative `printf -v` targets.
- Fixed static executable lowering so source arguments are evaluated by Bash at
  the generated source site instead of being frozen during dependency
  discovery, preserving loop variables, runtime `"$@"`, command substitutions,
  and assignment-prefixed source positional mutation.

### Changed

- `observe-compile` and `compile-observed` now use the new runtime observed
  compiler path by default. Normal static compile remains deterministic,
  trace-free, and backed by the existing static evaluator.
- Removed the obsolete graph-to-`SourceOverride` replay adapter from `modash.py`,
  substantially reducing CLI plumbing and separating runtime graph compilation
  from static source evaluation.
- Documentation now describes runtime observed compilation as graph-tape
  rewriting rather than in-memory supplement replay.
- Runtime graph replay now sources bundled files from embedded payload streams
  instead of mutable temporary replay files.
- Rewritten child `bash -c` replay now preserves a legitimate child status
  `125` when replay verification succeeds.
- Runtime observations now carry exact explicit `--env` overlay values. Generated
  runtime graph output verifies those values before replay, allowing traced
  env-dependent source paths to compile without trusting changed runtime env.
- Runtime graph safety validation now distinguishes observed source/helper
  function bodies from inert sourced-library function bodies, so real-world
  libraries can define uncalled dynamic external-tool helpers without blocking
  the observed replay path.
- Nonzero traced target status is now preserved as trusted observation/graph
  data instead of blocking graph, supplement, or observed executable generation.
  Trace instrumentation failures still fail before promotion.
- Child replay tokens are no longer injected into child `bash -c` command-line
  payloads; generated child setup reads them from short-lived replay files before
  user payload execution.
- Runtime observations now use schema `9` and trusted runtime graphs use schema
  `4`. Graph edges record source-entry status and source failure kind so replay
  can validate the status Bash exposes to sourced files after source argument
  expansion and can faithfully replay missing, directory, unreadable, and
  no-argument source failures.
- Runtime graph replay now expands the original source argument words before
  selecting the trusted edge, validates the selected source path and arguments
  against the graph, and aborts on replay-time drift instead of blindly replaying
  the observed target.
- Runtime tracing now resolves its helper tools before user code runs and uses
  physical current-directory resolution for source path fingerprints. Generated
  replay uses the same physical-cwd source resolution and rechecks missing
  source edges at runtime.
- Runtime graph replay now preserves assignment-prefixed source commands with
  Bash-native temporary export, array replacement, readonly diagnostic, and
  sourcepath behavior. Replay validates assignment-prefixed source-entry status
  against replay-time source argument, assignment RHS, and redirection target
  expansion outcomes. Simple source redirections are scoped to the emulated
  source operation, redirection target command-substitution status is included
  in source-entry status validation, and replay aborts cleanly when runtime
  redirection drift prevents the observed source operation from running.
- Runtime graph replay now wraps dynamic command guards in place without
  double-evaluating command arguments or redirection targets, keeps
  process-substitution arguments live for the actual command invocation, allows
  inert source-looking text for ordinary dynamic external commands, validates
  symlinked file-backed source paths by file identity, rewrites simple `LINENO`
  references to original line constants, and restores ordinary Bash mode for
  source-free rewritten child `bash -c` payloads without shifting child payload
  line numbers.
  External interpreter environment reads are allowed when trace-owned state is
  hidden and the payload does not inspect trace/replay-owned names, `/proc`
  process state, or trace file descriptors.
- Runtime tracing now reports source commands that terminate the shell before
  trace finalization, for example through explicit `exit` or a source body that
  trips `errexit`, with the targeted `runtime.trace.errexit_source_exit`
  diagnostic. `errexit`-suppressed source contexts and nonzero source-entry
  status remain traceable and replayable.
- Runtime graph replay now restores nonzero source-entry and dynamic-command
  prior status without tripping `errexit` before the observed command runs, and
  dynamic `printf -v` guards now allow safe literal associative-array targets
  while still rejecting computed or generated-state targets.
- Runtime tracing now reports source commands in unsupported child-shell
  contexts with `runtime.trace.unsupported_child_shell_source`, and
  process-substitution-backed source inputs with
  `runtime.trace.unsupported_process_substitution_source`.

### Validation

- Full unit suite: `828` tests, `9` skipped.
- Runtime-focused graph/source/replay safety suites passed.
- Opt-in real-world suite with runtime parity, trace, supplement replay,
  trusted graph replay, and observe-compile gates: `17` tests passed. Runtime
  graph replay recorded `10` matches and observe-compile recorded `11` matches,
  with no expected-error records.
- Python bytecode compilation passed for `modash.py`, all `methods` modules, and
  all tests.

## v0.6.0 - 2026-06-03

### Added

- Runtime graph replay now supports finite runtime-selected helper names when a
  trusted graph identifies the observed helper signature and source edge.
- Runtime graph replay now supports finite recursive helper dispatch, including
  mkinitcpio-style hook loops observed through a trusted graph.
- Runtime source supplements can represent source path arguments that are not
  the first helper argument.
- Added real-world coverage for Git dynamic helper dispatch and recursive
  mkinitcpio hook dispatch through trace, trusted graph replay, observe-compile,
  and runtime parity probes.
- Added exact positional-count guard modeling for function-return guards such as
  `[ "$#" -eq 0 ]`.
- Added the 0.6 coverage-completion release scope document.

### Changed

- Trusted runtime graph replay now consumes observed helper aliases more
  efficiently without rebuilding alias maps for each lookup.
- Trusted runtime graph replay now partitions repeated source-condition helper
  edges by observed invocation, preserving mixed short-circuit fallback behavior
  and generated supplement arguments.
- Executable source dispatch now switches on the source path word rather than
  the entire source argument vector, preserving observed helper forms such as
  `source "$ROOT/$name.sh" "$@"`.
- Trusted runtime graph replay now records file-backed helper-call provenance
  for dynamic wrapper helpers and uses it to replay the observed parent helper
  call without guessing through transitive helper bodies.
- Runtime tracing now attributes source calls in sourced one-line helper
  functions to the helper definition file even while the parent file is still
  being sourced, preventing duplicate parent-source graph edges.
- Runtime tracing now tracks sourced function definitions separately from raw
  `BASH_SOURCE` path text, so helper calls remain attributable when multiple
  directories source files with the same relative name or redefine identical
  helper bodies, without treating inert heredoc text as a live definition or
  accepting branch/control/eval/trap/alias-context provenance that cannot be
  trusted.
- Graph-backed dynamic helper dispatch now uses the next unconsumed observed
  source edge to disambiguate between multiple source-capable helpers.
- The real-world harness now distinguishes promoted static expectations from
  graph-backed runtime expectations for hard runtime-dynamic cases.
- README and runtime discovery docs now present the observe-compile workflow as
  the explicit path for runtime-dependent sources while keeping normal compile
  deterministic and trace-free.

### Validation

- Full unit suite: `593` tests, `9` skipped.
- Opt-in real-world suite with runtime trace, supplement replay, runtime
  parity, trusted graph replay, and observe-compile gates: `17` tests passed.
- Opt-in installed-wheel smoke built the local wheel, installed it into a fresh
  virtual environment, ran `modash observe-compile`, and verified original vs
  compiled executable status, stdout, and stderr parity.
- Generated promoted graph and observe-compile executable artifacts were
  spot-checked for source-free executable output.
- PyPI distribution build: sdist and wheel passed `twine check`.

## v0.5.0 - 2026-06-02

### Added

- Runtime observations now use schema `6` and persist source identities linking
  wrapper-observed source events to sanitized xtrace source provenance.
- Runtime observations, graph artifacts, and review reports now carry run
  metadata and recorded environment overlay key names without serializing
  ambient environment values.
- Added `modash graph` to build a trusted runtime source graph from trace
  observations and write a compact human-readable graph review report.
- `modash supplement` can now generate deterministic source supplements from a
  trusted runtime graph.
- Added `modash compile-observed` to compile executable output from a trusted
  graph with an in-memory generated supplement.
- Added explicit `modash observe-compile`, which traces a target run, writes
  observation, graph, and graph review artifacts, then compiles from the newly
  observed graph.
- Trusted graph supplement generation now recognizes finite helper-local
  source path aliases such as `local path=$1; shift; source "$path" "$@"`.
- Added a pinned `mkinitcpio` real-world corpus fixture that promotes runtime
  parity, trace, supplement replay, trusted graph replay, and observe-compile
  against real install-hook source files.
- Added a pinned `mkinitcpio` runtime hook-dispatch fixture that leaves static
  executable mode fail-closed but requires trace, trusted graph replay, and
  observe-compile to resolve guarded repeated runtime-selected real hook files.

### Changed

- Runtime source graphs now validate process, node, edge, file fingerprint, and
  xtrace invariants before graph replay.
- `compile-observed` and `observe-compile` now use trusted file-backed graph
  edges as exact source overrides, which preserves observed CWD-relative source
  resolution during deterministic compile.
- Runtime graph/report/supplement stale diagnostics now show the fingerprinted
  file role plus expected and current fingerprint fields.
- Runtime source graphs now reject duplicate source identities before replay.
- Runtime observations now fingerprint file-backed source files even when the
  source command returns non-zero.
- Runtime tracing now reconciles wrapper and xtrace source events by observed
  invocation identity instead of relying on global sequence order.
- Executable source evaluation now supports exact deterministic bitwise
  arithmetic operators used by shell libraries for constants and bit masks.
- The opt-in real-world harness now promotes trusted graph replay and
  `compile-observed` as first-class pacman fixture probes.
- The opt-in real-world harness now promotes `observe-compile` against pacman
  fixtures and retains observation, graph, report, and compiled artifacts.
- The opt-in real-world harness now drives trace, supplement, graph, and
  observe-compile probes from manifest-declared runtime expectations.
- Runtime graph overrides now replay repeated observed source edges from the
  same source call site in observed order.
- The opt-in real-world harness can require specific observed source suffixes
  for trace, graph, and observe-compile probes.
- Observe-compile real-world probes now compare the compiled artifact against a
  separate untraced original run, including exit status, stdout, and stderr.

### Validation

- Full unit suite: `456` tests, `8` skipped.
- Opt-in real-world suite with runtime trace, supplement replay, runtime
  parity, trusted graph replay, and observe-compile gates: `11` tests,
  covering `52` pinned compile records, `20` runtime parity records, `5`
  trace records, `4` supplement replay records, `5` trusted graph replay
  records, and `6` observe-compile records.
- PyPI distribution build: sdist and wheel passed `twine check`.

## v0.4.5 - 2026-06-02

### Added

- Runtime trace observation now covers `builtin source`, `builtin .`,
  `command source`, and `command .` invocation forms.
- Executable compile now lowers exact `builtin` / `command` source invocation
  forms when their source operands are otherwise supported.
- Runtime tracing now uses an xtrace sidecar to detect source-like commands
  that bypass wrapper observation and fails closed before writing observations
  when a trace is incomplete.
- Runtime trace observations now propagate into child Bash processes and merge
  parent/child source events with process provenance.
- Runtime trace observations now use schema `3` file fingerprints for
  entrypoints, file-backed call sites, and resolved source files.
- `modash supplement` now writes an observation review report beside generated
  source supplements.

### Changed

- Expanded CI coverage to Python 3.10 through 3.14 and added the Python 3.14
  package classifier after the full matrix passed.
- Promoted pacman fixture coverage for `builtin` / `command` source invocation
  forms into context, executable, runtime parity, and trace smoke paths.
- Promoted child Bash trace coverage into synthetic replay and opt-in
  real-world trace/supplement replay probes.
- Runtime supplement generation now rejects stale trace observations before
  deriving compiler input.
- Reworked README and docs into a smaller user-facing set, removing stale
  implementation-tranche specs that are already represented by release history
  and tests.
- Removed detached/dead internal helpers from the 0.4.5 polish branch.

### Validation

- Full unit suite: `407` tests, `6` skipped.
- Opt-in real-world suite with runtime trace, supplement replay, and runtime
  parity gates: `9` tests.
- PyPI distribution build: sdist and wheel passed `twine check`.

## v0.4.1 - 2026-06-02

Python 3.10 compatibility patch release.

### Fixed

- Replaced a Python 3.11-only `datetime.UTC` import in runtime source tracing
  with the Python 3.10-compatible `datetime.timezone.utc` API.

### Validation

- Full unit suite: `386` tests, `6` skipped.
- PyPI distribution build: sdist and wheel passed `twine check`, and the built
  wheel installed a working `modash` console script.

## v0.4.0 - 2026-06-02

Runtime source discovery and supplement replay release.

### Added

- Explicit `modash trace` workflow that executes a target Bash script under
  controlled source tracing and writes reviewed observation JSON.
- Runtime source observation schema with validation for entrypoint, cwd, argv,
  Bash/trace metadata, environment policy, source call sites, resolved paths,
  source arguments, and source status.
- Bounded trace execution with `--timeout`, stable timeout diagnostics, and
  fail-closed behavior before writing observations on timeout.
- `modash supplement` workflow that converts reviewed observations into
  declarative source supplement candidates.
- Observation-to-supplement inference for exact variable-prefix source paths and
  makepkg-style helper source calls such as `source_safe() { source "$@"; }`.
- Synthetic observe -> supplement -> executable compile -> run replay tests.
- Opt-in real-world trace and generated-supplement replay probes for the pinned
  pacman/makepkg `source_safe` fixture.
- PyPI packaging metadata, installed `modash` console script, and trusted
  publishing workflow for GitHub release publishing.

### Changed

- Renamed the project and CLI to `modash` before the first public package
  release.
- Documented the three-step runtime workflow: observe, review/generate
  supplement, then compile deterministically with `--source-supplement`.
- Promoted runtime discovery docs from north-star planning into implemented
  `v0.3.0` trace foundation and `v0.4.0` supplement replay milestones.
- Kept normal compile deterministic: tracing never runs during context or
  executable compile unless the explicit `trace` command is invoked.

### Validation

- Full unit suite: `385` tests, `6` skipped.
- Cached real-world suite: `9` tests, `3` skipped opt-in runtime gates.
- Opt-in runtime trace smoke probe: matched the pinned pacman `source_safe`
  observation.
- Opt-in runtime supplement replay probe: generated a supplement candidate,
  compiled with it, and matched Bash output/status.
- Generated replay artifact scan: no live unresolved `source` sites.
- PyPI distribution build: sdist and wheel passed `twine check`, and the built
  wheel installed a working `modash` console script.

### Notes

- Generated source supplements are candidates from one observed run. They must be
  reviewed before use and are not proof of unexecuted branches.
- Runtime discovery still complements static resolution; it does not replace the
  fail-closed compiler contract.
- Polished xtrace compatibility, child Bash trace merging, broader branch
  coverage diagnostics, and automatic compile from unreviewed observations remain
  future work.

## v0.2.0 - 2026-05-28

Static Bash parity hardening and real-world validation release.

### Added

- Source supplements for exact source-relevant variables and helper call
  signatures via `--source-supplement`.
- Retained helper dispatch for makepkg-style helpers such as `source_safe`.
- Executable lowering for source arguments, positional frame restoration,
  child-shell source contexts, runtime-guarded exact source sites, compound
  source conditions, source-bearing `case` arms, deterministic pattern/glob
  semantics, missing source runtime failures, and deterministic source expansion
  failures.
- Internal real-world suite with pinned `bash-completion` and `pacman` corpora,
  generated artifact outputs, JSON run summaries, and opt-in runtime parity
  probes.

### Changed

- Broadened executable-mode Bash parity for the supported static subset while
  keeping unsupported source forms fail-closed before output is written.
- Improved structured diagnostics for unresolved executable output, supplements,
  retained helpers, source arguments, guard boundaries, and expansion failures.
- Expanded the documented support matrix and implementation specs for source
  resolution behavior.

### Validation

- Full unit suite: `342` tests, `4` skipped.
- Pinned real-world corpus: `42` mode records, with expected top-level
  `bash_completion` timeouts and all other pinned entries successful.
- Runtime parity probes: `16` pinned pacman wrapper probes matched Bash.
- Generated executable artifact scan: no live unresolved `source` sites.

### Notes

- `modash` still resolves dependencies without executing shell code.
- Xtrace/runtime source discovery and supplement generation remain future work.
- Recursive or runtime-dynamic source-bearing dispatch and unsupported shell
  grammar remain fail-closed.

## v0.1.0 - 2026-05-22

Initial source-effect IR compiler baseline.

### Added

- Context output mode as the default readable renderer for human and LLM review.
- Executable output mode that inlines supported `source` sites while preserving
  parent-shell state.
- Source-effect IR frontend, evaluator, source events, and structured
  unsupported-source diagnostics.
- Supported source-resolution matrix covering static paths, variables, path
  commands, safe producers, arrays, loops, read loops, branches, cases, and
  bounded source-bearing functions.
- Fail-closed executable behavior for unsupported or ambiguous source forms.
- Real temporary shell-project test harness and expanded Bash parity regression
  suite.
- Optional setup shell helper containment tests.

### Notes

- `modash` resolves dependencies without executing shell code.
- Context mode is readable-first and not a runtime parity mode.
- Executable mode is parity-first for the documented supported subset.
- Remaining practical source-resolution gaps are tracked in
  `docs/supported-source-resolution.md`.
