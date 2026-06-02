# Changelog

## Unreleased

### Changed

- Expanded CI coverage to Python 3.10 through 3.14 and added the Python 3.14
  package classifier after the full matrix passed.

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
