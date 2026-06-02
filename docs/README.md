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
- [Runtime Source Discovery](runtime-source-discovery.md): north-star runtime
  observe -> review -> supplement -> deterministic compile workflow, current
  observation schema, safety model, and remaining runtime-discovery roadmap.
- [Real-World Internal Test Suite](real-world-test-suite.md): opt-in pinned
  corpus, generated artifact, runtime parity, trace, and supplement replay
  gates.

For release history, see [CHANGELOG.md](../CHANGELOG.md). For implementation
details, prefer the code and regression tests over stale design snapshots.
