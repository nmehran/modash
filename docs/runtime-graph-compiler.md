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

Static `modash` compile remains deterministic and trace-free. Runtime graph
compilation is used only by the explicit `compile-observed` and
`observe-compile` commands after a graph has been produced by `trace`/`graph`.

## Intentional limits

One trace still represents one execution path. A trusted graph does not prove
that untraced branches are safe. Re-running the generated executable down an
unobserved source-bearing branch fails closed instead of falling back to a live
`source` operation.

Edges whose call sites cannot be mapped precisely, stale file fingerprints,
unsupported hidden source operations, and nonzero traced targets are rejected
before executable output is promoted.
