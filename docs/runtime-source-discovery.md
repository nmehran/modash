# Runtime Source Discovery

Runtime source discovery is the explicit observe -> graph -> review ->
supplement path for source dependencies that cannot be resolved statically.

Normal `modash` compile never executes the target script. Runtime discovery does
execute it, so it is a separate command and produces review artifacts rather
than silently changing compiler behavior.

## Current Contract

- Runtime tracing records concrete source behavior for one execution.
- One observation is not proof of all branches.
- Generated supplements are declarative exact JSON, not shell code.
- `modash graph` rejects stale or untrusted observations before writing a graph.
- `modash supplement` rejects stale observations or graphs before deriving
  compiler input.
- Executable compile remains deterministic and fail-closed.
- Xtrace source provenance is persisted and reconciled with wrapper-observed
  source events before a graph is trusted.

## User Flow

```sh
modash trace ./entry.sh --output observation.json --timeout 30 -- ./args
modash graph ./entry.sh --from-observation observation.json --output runtime-graph.json
modash supplement ./entry.sh --from-graph runtime-graph.json --output source-supplement.json
modash ./entry.sh merged.sh --mode executable --source-supplement source-supplement.json
```

The separation matters:

1. `trace` runs the original program and records what happened.
2. `graph` validates observed source events against sanitized xtrace provenance
   and writes a trusted graph artifact.
3. `supplement` turns reviewed observations or graphs into explicit compiler
   input.
4. normal executable compile consumes only deterministic supplement data.

## Observation Schema

Current observations use schema `4`. They record:

- entrypoint, cwd, argv, Bash version, trace implementation version
- environment policy and recorded overlay keys
- traced Bash processes and parent linkage
- source events in execution order
- linked xtrace source provenance for every trusted trace source event
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
  "version": 4,
  "entrypoint": "/abs/project/entry.sh",
  "cwd": "/abs/project",
  "argv": ["--flag"],
  "bash": {
    "version": "GNU bash, version 5.2.21"
  },
  "trace": {
    "version": "runtime-wrapper-v4"
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
      "xtrace_index": 0,
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
  "xtrace": [
    {
      "index": 0,
      "process_index": 0,
      "file": "/abs/project/entry.sh",
      "line": 12,
      "function": "",
      "cwd": "/abs/project",
      "command": "source \"$lib\""
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

## Trusted Source Graph

`modash graph` consumes a schema `4` observation and writes a graph artifact.
The graph is still data, not executable shell code. It contains:

- process nodes
- file nodes with fingerprint roles
- process-command nodes for child `bash -c` style source sites
- source edges with call-site text, resolved paths, source arguments, status,
  and linked xtrace provenance
- the file fingerprints needed for stale graph rejection

Graph construction fails closed when source events lack xtrace provenance, when
the observation is stale, or when graph references are malformed. A graph is a
trusted representation of one observed execution, not proof that every branch
was exercised.

## Review Report

`modash supplement` also writes an observation review report. The report is a
review aid, not compiler truth. It can show:

- observed file-backed source sites
- unobserved source-capable sites in fingerprinted files
- child `bash -c` source provenance as process command text
- xtrace source command counts and linked source-event counts
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
- Generated graphs and supplements should be reviewed before use.
- Automatic compile from an unreviewed observation is out of scope.

## Implemented Runtime Coverage

- direct `source` and dot-source observations
- `builtin source`, `builtin .`, `command source`, and `command .`
- source arguments and failed source status
- cwd-sensitive source resolution
- makepkg-style helper calls such as `source_safe "$@"`
- child Bash process propagation and parent/child process provenance
- persisted xtrace provenance linked to wrapper-observed source events
- schema `4` file fingerprints and stale observation rejection
- trusted runtime source graph construction
- source supplement generation from trusted graphs
- review reports for unobserved source-capable file-backed sites
- real-world replay probes against pinned pacman fixtures

## Remaining Runtime Roadmap

Useful remaining steps are:

- richer source event identity, including source columns or command ordinals
- clearer environment/run metadata for reproducibility
- supplement generation for a broader but still finite set of runtime-dynamic
  source helpers
- direct compile from a reviewed trusted graph without an intermediate
  supplement file

Do not frame runtime discovery as solving arbitrary Bash semantics. The compiler
still needs a deterministic, reviewable source graph before it can merge scripts
safely.
