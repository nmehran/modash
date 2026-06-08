# modash Docs

This directory keeps durable project documentation. Release-era implementation
tranche specs are intentionally not retained here once the behavior is covered
by tests, changelog entries, and user-facing docs.

## Current Docs

- [Supported Source Resolution](supported-source-resolution.md): support matrix
  for executable-mode source lowering, fail-closed behavior, and practical
  remaining static gaps.
- [Source Supplements](source-supplements.md): JSON supplement format and exact
  helper-source workflow for runtime-dynamic values.
- [Runtime Source Discovery](runtime-source-discovery.md): explicit runtime
  observe -> graph -> review -> compile workflow, current observation and
  graph schemas, and safety model.
- [Runtime Graph Compiler](runtime-graph-compiler.md): 0.7 trusted graph-tape
  compiler architecture for `compile-observed` / `observe-compile`, including
  replay groups, bundling, and fail-closed boundaries.
- [Real-World Internal Test Suite](real-world-test-suite.md): opt-in pinned
  corpus, generated artifact, runtime parity, trace, and supplement replay
  gates.

## Historical Scope

- [0.6 Release Scope](roadmap-0.6.md): historical coverage-completion scope
  retained as background for the 0.7 runtime compiler work.

For release history, see [CHANGELOG.md](../CHANGELOG.md). For implementation
details, prefer the code and regression tests over stale design snapshots.
