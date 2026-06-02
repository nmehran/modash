# Changelog

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

### Validation

- Full unit suite: `453` tests, `8` skipped.
- Opt-in real-world suite with runtime trace, supplement replay, runtime
  parity, trusted graph replay, and observe-compile gates: `11` tests,
  covering `50` pinned compile records, `20` runtime parity records, `4`
  trace records, `4` supplement replay records, `4` trusted graph replay
  records, and `5` observe-compile records.
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
