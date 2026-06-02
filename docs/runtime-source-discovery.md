# Runtime Source Discovery

Runtime source discovery is the explicit observe -> review -> supplement path
for source dependencies that cannot be resolved statically.

Normal `modash` compile never executes the target script. Runtime discovery does
execute it, so it is a separate command and produces review artifacts rather
than silently changing compiler behavior.

## Current Contract

- Runtime tracing records concrete source behavior for one execution.
- One observation is not proof of all branches.
- Generated supplements are declarative exact JSON, not shell code.
- `modash supplement` rejects stale observations before deriving compiler
  input.
- Executable compile remains deterministic and fail-closed.
- Xtrace is currently a gap-detection sidecar, not a trusted source graph.

## User Flow

```sh
modash trace ./entry.sh --output observation.json --timeout 30 -- ./args
modash supplement ./entry.sh --from-observation observation.json --output source-supplement.json
modash ./entry.sh merged.sh --mode executable --source-supplement source-supplement.json
```

The separation matters:

1. `trace` runs the original program and records what happened.
2. `supplement` turns reviewed observations into explicit compiler input.
3. normal executable compile consumes only deterministic supplement data.

## Observation Schema

Current observations use schema `3`. They record:

- entrypoint, cwd, argv, Bash version, trace implementation version
- environment policy and recorded overlay keys
- traced Bash processes and parent linkage
- source events in execution order
- source call-site provenance
- resolved source paths, source arguments, and source status
- file fingerprints for the entrypoint, successful source files, and
  file-backed call sites

Schema validation rejects observations that omit required fingerprint roles.
Fingerprint validation compares path, size, mtime, and SHA-256 before supplement
generation.

Small example:

```json
{
  "version": 3,
  "entrypoint": "/abs/project/entry.sh",
  "cwd": "/abs/project",
  "argv": ["--flag"],
  "bash": {
    "version": "GNU bash, version 5.2.21"
  },
  "trace": {
    "version": "runtime-wrapper-v3"
  },
  "environment": {
    "policy": "overlay",
    "recorded_keys": ["MAKEPKG_LIBRARY"]
  },
  "processes": [
    {
      "index": 0,
      "pid": 12345,
      "parent_index": null,
      "parent_pid": 12300,
      "entrypoint": "/abs/project/entry.sh",
      "cwd": "/abs/project",
      "argv": ["--flag"],
      "command": "/abs/project/entry.sh"
    }
  ],
  "sources": [
    {
      "index": 0,
      "process_index": 0,
      "call_site": {
        "file": "/abs/project/entry.sh",
        "line": 12,
        "command": "source \"$lib\""
      },
      "resolved_path": "/abs/project/lib/util.sh",
      "arguments": [],
      "status": 0
    }
  ],
  "files": [
    {
      "path": "/abs/project/entry.sh",
      "size": 120,
      "mtime_ns": 1710000000000000000,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "roles": ["entrypoint", "call-site"]
    },
    {
      "path": "/abs/project/lib/util.sh",
      "size": 80,
      "mtime_ns": 1710000000000000001,
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "roles": ["source"]
    }
  ]
}
```

## Review Report

`modash supplement` also writes an observation review report. The report is a
review aid, not compiler truth. It can show:

- observed file-backed source sites
- unobserved source-capable sites in fingerprinted files
- child `bash -c` source provenance as process command text
- summary counts for warnings and observed source events

Same-line multi-source coverage is still line-level. If every static source site
on a line produced an observed event, the report treats the line as covered.
Partial same-line execution remains review-only ambiguous until source event
provenance records columns or stronger command identity.

## Safety Model

- Tracing runs the target program and must be requested explicitly.
- Trace execution has a timeout.
- Trace stdout/stderr are forwarded, while trace data is written to artifacts.
- Trace artifacts are data; `modash` does not eval, source, or execute
  trace-derived content.
- Generated supplements should be reviewed before use.
- Automatic compile from an unreviewed observation is out of scope.

## Implemented Runtime Coverage

- direct `source` and dot-source observations
- `builtin source`, `builtin .`, `command source`, and `command .`
- source arguments and failed source status
- cwd-sensitive source resolution
- makepkg-style helper calls such as `source_safe "$@"`
- child Bash process propagation and parent/child process provenance
- xtrace sidecar detection for source-like commands that bypass wrappers
- schema `3` file fingerprints and stale observation rejection
- review reports for unobserved source-capable file-backed sites
- real-world replay probes against pinned pacman fixtures

## Remaining Runtime Roadmap

The next major boundary is trusted xtrace graph construction. Before that,
useful smaller steps are:

- richer source event identity, including source columns or command ordinals
- persisted sanitized xtrace provenance for review
- clearer environment/run metadata for reproducibility
- supplement generation for a broader but still finite set of runtime-dynamic
  source helpers

Do not frame runtime discovery as solving arbitrary Bash semantics. The compiler
still needs a deterministic, reviewable source graph before it can merge scripts
safely.
