# Runtime Source Discovery North Star

## Status

North-star design document. This is not implemented in `v0.2.0`.

Runtime source discovery is planned as the next major product arc. Its purpose is
to observe concrete runtime source behavior and turn that observation into
reviewed declarative compiler input. It must not become a linter, a Bash
interpreter, or a claim that one run proves all possible source paths.

## Product Shape

The final product has three cooperating workflows:

1. Static compile.
   Resolve and merge everything that can be proven without executing shell code.
2. Runtime observe.
   Run the original script under an explicit controlled tracing command and
   record which source paths Bash actually executed for that concrete run.
3. Replay with supplement.
   Convert reviewed observations into source supplements, then compile
   deterministically with `--source-supplement`.

The compiler remains deterministic. Runtime tracing produces input for the
compiler; it does not silently change compiler semantics.

## Intended User Flow

```sh
modashc trace ./entry.sh -- ./args
# writes .modashc/observations/<run-id>.json

modashc supplement ./entry.sh --from-observation .modashc/observations/<run-id>.json
# writes source-supplement.json

modashc ./entry.sh merged.sh --mode executable --source-supplement source-supplement.json
```

The names above are directional. Exact CLI spelling can change during the
implementation specs, but the separation of observe, supplement, and compile
should not.

## Core Contract

- Runtime discovery records actual sourced paths for one concrete execution.
- A trace observation is not proof of every possible source path.
- Generated supplements are declarative exact data, not shell code.
- The compiler still fails closed if a supplement is incomplete, stale,
  inconsistent, or attempts to make unsupported behavior look safe.
- Executable mode must continue to either produce accepted Bash-equivalent
  output for the supported source graph or fail before writing output.
- Context mode remains readable-first and must not silently reinterpret
  observations as static facts.

## Observation Data

An observation should record enough context to make the run understandable and
replayable:

- schema version
- entrypoint path
- working directory
- argv
- Bash version
- tracing implementation version
- environment policy and recorded environment keys
- source observations in execution order
- source call site when known
- resolved source path
- source arguments when known
- source status
- timestamp or run id

Example shape:

```json
{
  "version": 1,
  "entrypoint": "/abs/project/entry.sh",
  "cwd": "/abs/project",
  "argv": ["--flag"],
  "bash": {
    "version": "5.2.21"
  },
  "environment": {
    "policy": "allowlist",
    "recorded_keys": ["MAKEPKG_LIBRARY"]
  },
  "sources": [
    {
      "call_site": {
        "file": "/abs/project/entry.sh",
        "line": 12,
        "command": "source \"$lib\""
      },
      "resolved_path": "/abs/project/lib/util.sh",
      "arguments": [],
      "status": 0
    }
  ]
}
```

The schema should stay intentionally narrow until the trace parser has real
corpus coverage.

## Architecture

The implementation should be split into small modules:

- `trace_runner`: executes Bash under explicit user request with controlled
  tracing enabled.
- `trace_parser`: converts trace output into structured observations.
- `observation_schema`: validates observation JSON.
- `supplement_generator`: converts observations into source supplement
  candidates.
- `supplement_validator`: checks that generated supplements are declarative,
  exact, path-safe, and compatible with compiler expectations.
- real-world integration: uses trace/observation/replay only after the core
  workflow is proven synthetically.

The trace parser must treat trace text as data. It must not eval, source, or
execute trace-derived content.

## Safety Model

Tracing runs the target program. That must be explicit.

- No runtime tracing happens during normal compile.
- `trace` should warn or document clearly that the target script executes.
- The user must provide or accept cwd and argv explicitly.
- Environment capture should default to an allowlist model.
- Trace output should be written to a predictable artifact path, not mixed into
  compiler output.
- Observed paths should be normalized and validated before supplement
  generation.
- Auto-compiling directly from an unreviewed trace is out of scope until the
  supplement workflow is mature.

## Supplement Semantics

Runtime observations should feed the existing source supplement direction:

- relative supplement values resolve from the entrypoint directory
- script assignments override supplement variables
- supplement variables override process environment
- function entries define finite allowed source-path argument vectors
- values remain exact strings, not shell code

Observation-to-supplement generation should prefer the smallest supplement that
explains the observed run. It should not generate broad wildcards or inferred
runtime rules from one execution.

## Acceptance Milestones

### v0.3.0: Trace Foundation

Detailed iteration spec:
[Runtime Source Discovery Foundation](runtime-source-discovery-foundation.md).

- Add an explicit trace command or internal API.
- Record observation JSON for direct `source`, dot source, function helpers,
  cwd changes, source arguments, and failed source statuses.
- Add synthetic tests for trace parsing and observation schema validation.
- Keep supplement generation experimental or manual.
- Do not integrate tracing into normal compile.

### v0.4.0: Supplement Generation And Replay

- Generate source supplement candidates from observations.
- Validate generated supplements before compile.
- Add a two-pass workflow: observe, generate/review supplement, compile.
- Add one real-world dynamic-source fixture that succeeds only with the
  generated supplement.

### v0.5.0: Polished Xtrace Compatibility

- Stabilize CLI/API names.
- Harden environment/cwd/argv reproducibility.
- Improve diagnostics for stale or incomplete observations.
- Promote runtime discovery into the real-world suite.
- Document limitations and safety behavior as user-facing contract.

## Non-Goals

Runtime discovery should not claim to:

- prove all branches
- lint shell scripts
- validate project behavior
- replace Bash semantics
- accept arbitrary `eval` as safe static fact
- compile from unreviewed observations as if they were static proof
- make unsupported parser or evaluator behavior disappear

The `v0.3.0` foundation uses an explicit Bash prelude to wrap `source` and
dot-source in the traced shell. That is intentionally narrower than full xtrace
compatibility: `builtin source`, wrapper removal, and child Bash processes are
not treated as complete coverage until later runtime-discovery tranches.

## Relationship To Static Resolution

Runtime discovery does not replace static resolution. It complements it:

- static resolution handles provable source graphs without execution
- runtime observation helps users capture concrete paths that are genuinely
  runtime-dependent
- supplements make those observed paths explicit and deterministic
- compile remains fail-closed when the resulting graph cannot be made exact

This boundary is the north star for the runtime discovery roadmap.
