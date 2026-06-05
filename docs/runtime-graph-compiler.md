# Runtime Graph Compiler

`compile-observed` in modash 0.7 compiles from a trusted runtime graph by
rewriting observed source operations, not by asking the static evaluator to
simulate runtime Bash control flow.

The compiler treats the graph as an execution tape:

1. validate the graph and file fingerprints;
2. map every trusted source edge to an exact source call site in the original
   entrypoint, sourced file, or observed child `bash -c` payload;
3. replace each mapped source operation with a small replay group;
4. bundle the observed source files into the generated executable; and
5. fail closed if a generated executable reaches an unobserved source site or
   leaves a trusted edge unused.

The original script still decides whether a source site runs. modash replaces
only the operation that would source the file. The generated replay group calls
`builtin source` at the original call site, so Bash preserves normal source
semantics for supported trusted edges: caller locals, no-argument positional
inheritance, explicit source arguments, top-level `return`, nested observed
sources, and child `bash -c` argv.

## Trust boundary

The runtime graph compiler never executes graph payloads as shell code. Graph
values are used as data for validation, source-site mapping, and embedded-file
selection. The generated executable aborts with a modash runtime replay error if
it observes graph drift, including an unobserved source site, an over-consumed
edge, or an unconsumed trusted edge.

Generated replay output starts with `#!/bin/bash -p` and must be executed
directly as an executable, or explicitly with `bash -p`. Launching trusted graph
output with plain `bash output.sh` is rejected before replay setup, because
unprivileged Bash can import hostile exported functions before the generated
script can defend itself. Replay setup uses resolved absolute paths for required
host tools such as `mktemp`, `mkdir`, `base64`, `rm`, `kill`, and the launch
diagnostic `printf`, so hostile runtime `PATH` entries do not change how the
bundle is unpacked, cleaned up, or aborted. Generated replay infrastructure uses
Bash builtins for its own `printf` operations after privileged startup. Replay
clears trace-owned Bash startup state such as `BASH_ENV`, while preserving
ordinary user variables such as `ENV`.

Static `modash` compile remains deterministic and trace-free. Runtime graph
compilation is used only by the explicit `compile-observed` and
`observe-compile` commands after a graph has been produced by `trace`/`graph`.

## Intentional limits

One trace still represents one execution path. A trusted graph does not prove
that untraced branches are safe. Re-running the generated executable down an
unobserved source-bearing branch fails closed instead of falling back to a live
`source` operation.

Edges whose call sites cannot be mapped precisely, stale file fingerprints,
unsupported hidden source operations, repeated observations of the same source
path with different file fingerprints, and trace instrumentation failures are
rejected before executable output is promoted. A target command's own nonzero
exit status is recorded as graph data and should be preserved by generated
output; it is not a graph-trust failure by itself.

The compiler also rejects shapes that can make a trusted graph lie about what
will run: reserved `__modash_` names, trace-instrumentation-sensitive shell
state, aliases, dynamic or source-capable `eval`, source redirections, dynamic
or multiline child `bash -c` payloads, unsupported child `bash -c` wrappers that
hide source operations, `exec`, trap manipulation, computed mutation of
generated replay state, runtime `$0` / `BASH_SOURCE` references inside heredocs
or multiline strings, and runtime `$0` / `BASH_SOURCE` references on parent
lines that also contain child `bash -c` payloads. The main `eval` exception is a
straight-line `eval "$shellopts"` restore immediately backed by a persistent,
source-free `shellopts=$(shopt -p ...)`, `local shellopts=$(shopt -p ...)`, or
`declare` / `typeset` equivalent in the current shell; generated replay
validates the restored text again at runtime before applying it as `shopt`
state. Subshell, pipeline, environment-assignment, branch-local, mutated, or
environment-controlled variants are rejected. Source-free literal lookup forms
such as `eval echo ...` and `eval : ...` are allowed when the evaluated command
word is fixed and later arguments are static or guarded escaped
parameter-reference construction. Generated replay validates the final eval
arguments after normal shell expansion and marks replay failed if the constructed
argument can inject shell syntax. `eval printf ...` is rejected because
`printf -v` can mutate computed replay state variables. Escaped parameter lookup
evals reject array subscripts and parameter operators that can trigger command
substitution or indirect runtime source execution.

Child process replay uses a generated verification marker rather than treating
ordinary child exit status as the replay proof. A legitimate observed child can
exit with status `125` and the parent preserves that status when child replay
verification succeeds. Child replay tokens are read by generated child setup
from short-lived replay files and are not placed in the child command line or
exported environment. Generated selectors abort when called outside generated
replay groups.

Replay-critical function overrides are rejected for `source`, `.`, `exec`,
`trap`, `command`, `builtin`, `shopt`, `env`, `enable`, and `exit`. The
compiler also rejects or guards runtime shapes that can bypass validation indirectly,
including unsafe nameref targets, `unset -f` forms that can remove replay
guards, positional `read` targets that could point at generated replay state,
wrapped `builtin read` / `command read` variants of those positional targets,
`mapfile` / `readarray` callback execution, indirect expansion over
instrumentation variables, absolute or PATH-based environment dumps,
`/proc/*/environ` and `/proc/*/cmdline` probes, trace file descriptor probes,
and dynamic command dispatch that can become `builtin source ...`,
`command source ...`, `eval`, replay-state mutation, or shell `-c` execution.
Source-bearing shell payloads hidden behind direct non-Bash shells, BusyBox
shell applets, common command-tail wrappers such as `nice`, `timeout`, `setsid`,
`nohup`, and `stdbuf`, or exec-style utilities such as `find -exec sh -c` are
also rejected instead of being left live.
Source command recognition covers source-capable `time` and `coproc` prefixes so
they fail closed instead of live-sourcing; observed `time`-prefixed source sites
are rejected until timing output can be replayed precisely.
Direct `kill` calls are guarded at runtime so normal background-helper cleanup
can work, but attempts to target the replay shell or bypass the guard with
`builtin kill`, `command kill`, `env kill`, or absolute kill paths are rejected.

Generated scripts also install source/dot guard functions. Rewritten replay
groups use `builtin source`, but a live unobserved `source` or `.` command that
appears because runtime control flow drifted aborts instead of sourcing a file
outside the trusted graph.

Embedded source payloads are kept in generated readonly maps and streamed into
`builtin source` through process substitution. The generated script no longer
sources mutable temporary replay files, so user code cannot race-rewrite bundled
source bytes between unpack and replay. Empty sourced files are valid embedded
payloads and replay without internal diagnostics.

Runtime-selected helper names are supported when the observed graph can replay a
finite helper call sequence without trusting changed runtime arguments. Dynamic
helper path arguments that depend on explicit trace/observe `--env` overlays are
supported by recording those exact values in the graph and verifying them before
replay. Ambient environment values are not serialized wholesale; source-relevant
ambient values used by observed source call sites are recorded when they
contribute to the selected source path or arguments. Use `--env KEY=VALUE` for
explicit overlays and for complex environment-dependent source selection that
should be reviewed directly.

Sourced library files may define functions that were not executed by the traced
command. Those inert function bodies are not treated as top-level replay actions,
but literal `builtin source`, `command source`, generated replay-token access,
computed replay-state mutation, `eval`, `exec`, `trap`, `enable`, source-capable
dynamic command tails, `mapfile` / `readarray` callbacks, source-bearing shell
payloads, and similar replay-bypass commands remain rejected or guarded. Plain
unobserved `source` / `.` in an inert function body remains guarded by generated
runtime source/dot functions and aborts if it ever runs. Runtime guards are also
inserted before preserved dynamic command sites so benign helper dispatch can
continue while replay-critical runtime values fail closed.

Bundled files rewrite `$0` and `BASH_SOURCE` references to stable original
physical paths. Exact relative-path spelling and symlink spelling are not part
of the 0.7 runtime graph compiler contract.

Graph construction also rejects sourced files with top-level
function-context-sensitive Bash such as `local`, `caller`, `declare`,
`typeset`, `FUNCNAME`, `BASH_LINENO`, `PIPESTATUS`, or `$_`, because the trace
wrapper necessarily observes source calls through a function boundary.
