# Runtime Source Discovery

Runtime source discovery is the explicit observe -> graph -> review -> compile
path for source dependencies that cannot be resolved statically.

Normal `modash` compile never executes the target script. Runtime discovery does
execute it, so it is a separate command and produces review artifacts rather
than silently changing compiler behavior.

## Current Contract

- Runtime tracing records concrete source behavior for one execution.
- One observation is not proof of all branches.
- Generated supplements are declarative exact JSON, not shell code, and remain
  available for static executable compile compatibility.
- `modash graph` rejects stale or untrusted observations before writing a graph.
- `modash supplement` rejects stale observations or graphs before deriving
  compiler input.
- `modash compile-observed` uses the 0.7 trusted runtime graph compiler, which
  rewrites observed source call sites into scope-preserving replay groups and
  fails closed on graph-tape drift.
- Normal executable compile remains deterministic, trace-free, and fail-closed.
- Xtrace source provenance is persisted and reconciled with wrapper-observed
  source events by observed invocation identity before a graph is trusted.

## User Flow

```sh
modash trace ./entry.sh --output observation.json --timeout 30 -- ./args
modash graph ./entry.sh --from-observation observation.json --output runtime-graph.json
modash compile-observed ./entry.sh merged.sh --from-graph runtime-graph.json
```

The supplement command is still available when you explicitly want declarative
source-resolution input for normal executable compile:

```sh
modash supplement ./entry.sh --from-graph runtime-graph.json --output source-supplement.json
modash ./entry.sh merged.sh --mode executable --source-supplement source-supplement.json
```

For automation, `observe-compile` performs an explicit one-shot trace, writes
the observation, trusted graph, and graph review report, then compiles from that
newly observed graph:

```sh
modash observe-compile ./entry.sh merged.sh --reviewed-graph-out runtime-graph.json -- ./args
```

The separation matters:

1. `trace` runs the original program and records what happened.
2. `graph` validates observed source events against sanitized xtrace provenance
   and replayable file call sites, then writes a trusted graph artifact plus a
   compact text review report.
3. `compile-observed` compiles from an already reviewed graph by rewriting
   observed source call sites into graph-backed replay groups; it does not run
   tracing.
4. `supplement` optionally turns reviewed observations or graphs into explicit
   source-resolution input for normal executable compile.
5. normal executable compile consumes only deterministic static/supplement data.
6. `observe-compile` is the explicit automation path for tracing and compiling
   in one command; it still writes graph and report artifacts for review, and
   normal compile remains trace-free.

## Observation Schema

Current observations use schema `8`. They record:

- entrypoint, cwd, argv, Bash version, trace implementation version
- run metadata: observed timestamp, modash version, platform, Python version,
  shell command, target status, and timeout
- environment policy and recorded overlay keys
- traced Bash processes and parent linkage
- source events in execution order
- source identities that link wrapper-observed events to xtrace provenance
- linked xtrace source provenance for every trusted trace source event
- source call-site provenance
- source function stacks and file-backed observed helper-call provenance for
  review and compatibility with graph validation
- resolved source paths, source arguments, and source status
- trace-time file fingerprints for the entrypoint, file-backed source files,
  and file-backed call sites, including sourced files that return non-zero
  status

Schema validation rejects observations that omit required fingerprint roles.
Fingerprint validation compares path, size, mtime, and SHA-256 before supplement
generation. Trace rejects a sourced file that changes while it is being sourced;
graph construction rejects a source file that changes after it was observed.

Small example:

```json
{
  "version": 8,
  "entrypoint": "/abs/project/entry.sh",
  "cwd": "/abs/project",
  "argv": ["--flag"],
  "bash": {
    "version": "GNU bash, version 5.2.21"
  },
  "trace": {
    "version": "runtime-wrapper-v10"
  },
  "environment": {
    "policy": "overlay",
    "recorded_keys": ["MAKEPKG_LIBRARY"]
  },
  "run": {
    "observed_at_utc": "2026-06-02T12:00:00Z",
    "modash_version": "0.7.0",
    "platform": "Linux-6.x-x86_64-with-glibc2.x",
    "python_version": "3.14.0",
    "shell": "/usr/bin/bash",
    "target_status": 0,
    "timeout_seconds": 30.0
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
      "source_identity": "src:aaaaaaaaaaaaaaaaaaaaaaaa",
      "process_index": 0,
      "xtrace_index": 0,
      "call_site": {
        "file": "/abs/project/entry.sh",
        "line": 12,
        "command": "source \"$lib\""
      },
      "function_stack": [],
      "function_call": null,
      "resolved_path": "/abs/project/lib/util.sh",
      "arguments": [],
      "status": 0
    }
  ],
  "xtrace": [
    {
      "index": 0,
      "source_identity": "src:aaaaaaaaaaaaaaaaaaaaaaaa",
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

`modash graph` consumes a schema `8` observation and writes a graph artifact.
The graph is still data, not executable shell code. It contains:

- process nodes
- file nodes with fingerprint roles
- process-command nodes for child `bash -c` style source sites
- source edges with call-site text, resolved paths, source arguments, status,
  source identity, helper-call provenance when needed, and linked xtrace
  provenance
- the file fingerprints needed for stale graph rejection

Graph construction fails closed when source events lack xtrace provenance, when
the observation is stale, or when graph references are malformed. Stale-file
diagnostics name the fingerprint role and show expected/current fields so the
reviewer can see whether an entrypoint, call-site file, or source file changed.
Graph loading also validates process, node, edge, fingerprint, source identity,
and xtrace invariants so a hand-edited graph cannot weaken the observed trust
relationship without being rejected. A graph is a trusted representation of one
observed execution, not proof that every branch was exercised.

`modash graph` writes a text report beside the graph by default:

```sh
modash graph ./entry.sh --from-observation observation.json --output runtime-graph.json
# report: runtime-graph.json.report.txt
```

The report summarizes why the graph is trusted, lists the source edges with
their identities and xtrace commands, and records the file fingerprints that
must remain current for replay.

## Review Report

`modash supplement` also writes an observation review report. The report is a
review aid, not compiler truth. It can show:

- observed file-backed source sites
- unobserved source-capable sites in fingerprinted files
- child `bash -c` source provenance as process command text
- xtrace source command counts and linked source-event counts
- source identities for observed events
- summary counts for warnings and observed source events

Same-line multi-source coverage is precise when the observed xtrace command can
be matched back to a static source site on the same line. Ambiguous same-line
runtime-dynamic sites remain review warnings instead of compiler truth.

## Safety Model

- Tracing runs the target program and must be requested explicitly.
- Trace execution has a timeout.
- Trace stdout/stderr are forwarded, while trace data is written to artifacts.
- Trace artifacts are data; `modash` does not eval, source, or execute
  trace-derived content.
- Generated graphs and supplements should be reviewed before use.
- `observe-compile` is explicit and artifact-writing; normal compile never
  traces or auto-supplements silently.
- Nonzero target exit status is recorded in the observation/graph and is not
  itself a trust failure. `observe-compile` may return that same nonzero status
  after writing graph, report, observation, and compiled output.
- Trace instrumentation failures still stop promotion before graph or executable
  output is written.
- Runtime graph compile is limited to observed source edges whose call site can
  be mapped exactly in an entrypoint, sourced file, or observed child `bash -c`
  payload. Runtime-observed source effects hidden behind dynamic or source-capable
  `eval` remain trace data, but they are not promoted into trusted graph
  compilation. Source-free `eval "$shellopts"` restoration from `shopt -p` is
  allowed for real-world shell-library helpers.
- Runtime graph compile also rejects source redirections, dynamic or multiline
  child `bash -c` payloads, reserved `__modash_` names, and scripts that inspect
  trace-instrumentation-sensitive shell state.
- Runtime graph construction rejects sourced files with top-level
  function-context-sensitive Bash such as `local`, `caller`, `FUNCNAME`, or
  `BASH_LINENO`, because the trace wrapper necessarily observes source calls
  through a function boundary.
- No-argument `source file` tracing preserves inherited positional parameters
  through the trace alias. If that alias is removed before the source call, or
  if the sourced file may mutate caller positionals at top level with `shift`
  or `set --`, tracing fails closed instead of recording a nontransparent
  observation.

## Implemented Runtime Coverage

- direct `source` and dot-source observations
- `builtin source`, `builtin .`, `command source`, and `command .`
- inherited positional parameters, source arguments, and failed source status
- cwd-sensitive source resolution
- makepkg-style helper calls such as `source_safe "$@"`
- finite helper-local source path aliases such as
  `local path=$1; shift; source "$path" "$@"`
- finite helper signatures where the observed source path is not the first
  helper argument, including direct `$2`, helper-local aliases, and exact
  `case` arms
- graph-backed runtime compilation that does not need to statically model
  recursive helper dispatch, runtime-selected helper names, helper-local aliases,
  or finite loops once the trusted graph maps the observed source sites
- repeated guarded runtime-selected source sites from a trusted graph, such as
  a mkinitcpio-style `. "$root/$hook"` hook loop observed for a finite hook list
- child Bash process propagation and parent/child process provenance
- persisted xtrace provenance linked to wrapper-observed source events
- observed helper-call provenance for trusted runtime graph replay
- schema `8` source identities, file fingerprints, and stale observation
  rejection
- trusted runtime source graph construction
- strict graph invariant validation for reviewed graph replay
- compact human-readable graph review reports
- source supplement generation from trusted graphs for static compile
  compatibility
- explicit runtime observed compile from a trusted graph
- explicit one-shot observe -> graph/report -> compile workflow
- review reports for unobserved source-capable file-backed sites
- real-world replay probes against pinned pacman and mkinitcpio fixtures,
  including mkinitcpio runtime hook-dispatch graph replay

## Runtime Compiler

The 0.7 compiler architecture is documented in
[Runtime Graph Compiler](runtime-graph-compiler.md). The 0.6 release-scope
history remains in [modash 0.6 Release Scope](roadmap-0.6.md).
