# Retained Helper Dispatch

## Status

Implemented V1 behavior. This is the static source-resolution iteration that
lands before runtime source discovery. A small opt-in runtime parity probe path
now exists in the internal real-world suite.

The feature extends the existing source supplement contract. It does not add a
new public interface and it does not execute project shell code.

## Summary

Support finite, supplement-backed dispatch for retained source helper
definitions such as makepkg's `source_safe`:

```bash
source_safe() {
  local shellopts=$(shopt -p extglob)
  shopt -u extglob
  if ! source "$@"; then
    error "$(gettext "Failed to source %s")" "$1"
    return 1
  fi
  eval "$shellopts"
}
```

Exact helper call sites are already handled when the compiler sees a call such
as:

```bash
source_safe "$MAKEPKG_LIBRARY/util/message.sh"
```

The remaining gap is a library file that defines `source_safe` and keeps that
function available for later callers. Executable mode cannot leave
`source "$@"` in the generated output, but the compiler also cannot infer every
future runtime argument. This iteration allows users to provide a finite set of
accepted helper argument vectors through the existing supplement file.

## Non-Goals

- Do not run Bash or trace runtime execution.
- Do not make ordinary compilation environment-dependent.
- Do not support arbitrary dynamic function dispatch.
- Do not model recursive source-bearing helpers.
- Do not preserve live unresolved `source` commands in executable output.
- Do not use supplement entries as shell code, glob patterns, or expressions.

## Supplement Contract

No schema change is required. The existing `functions` object gains one more
meaning: for retained source helpers, entries declare the finite runtime
argument vectors the merged output will support.

```json
{
  "version": 1,
  "variables": {
    "MAKEPKG_LIBRARY": "/usr/share/makepkg"
  },
  "functions": {
    "source_safe": [
      {
        "arguments": ["/path/to/PKGBUILD"]
      },
      {
        "arguments": ["/etc/makepkg.conf"]
      }
    ]
  }
}
```

Each `arguments` vector is declarative exact data. The first value is the
source path and relative path values still resolve from the entrypoint
directory, matching the source supplement contract. Additional values are exact
strings passed to the sourced file for retained helpers that use
`source "$@"`.

Function supplement entries do not make arbitrary runtime calls safe. They only
authorize dispatch for the named local helper definition when the helper body is
modeled and the source site can be lowered without preserving a live source
command.

## Accepted V1 Shape

A retained helper can be lowered when all of these are true:

- The helper definition is known in the analyzed source graph.
- The helper body contains a modeled positional source site:
  - `source "$@"`
  - `source "${@}"`
  - `source "$*"`
  - `source "${*}"`
  - `source "$1"`
  - the same forms with `.`
- The source site is inside modeled control flow, including the existing
  makepkg-style `if ! source "$@"; then ...; fi` guard shape.
- A supplement provides at least one allowed argument vector for the retained
  helper.
- Each allowed vector has at least one source path argument.
- Extra arguments are accepted for `source "$@"` / `source "${@}"` retained
  helpers and are passed to the sourced file. `source "$1"` / `source "${1}"`
  retained helpers still require exactly one argument because Bash preserves
  the helper's current positional state rather than treating later helper
  arguments as direct source arguments.
- Each source path and its transitive source graph pass the normal resolver
  contract.
- The renderer can preserve source status, local scope, cwd, shell option, and
  variable effects for the lowered source content.
- The supplemented source graph uses supported top-level sourced-file `return`
  semantics and source-argument positional mutation semantics.

V1 should reject the helper instead of guessing when any of these are false.

## Executable Lowering Semantics

Executable mode lowers the retained source site into finite dispatch over the
allowed supplement vectors. The generated output must not contain a live
runtime `source` or `.` command for the lowered site.

The dispatch must run in the same function scope as the original `source`
command. Lowered sourced-file `return` behavior may use the shared executable
source-return wrapper, but the retained dispatch itself must not move the
source site into an unrelated shell context.

Conceptually, this:

```bash
if ! source "$@"; then
  error "failed"
  return 1
fi
```

becomes a same-scope exact dispatch. This is pseudo-shell; the renderer must
replace each placeholder with normal executable source lowering and preserve
that lowered source status:

```text
if ! {
  if [[ $# -eq 1 && ${1-} == '/path/to/PKGBUILD' ]]; then
    <lowered /path/to/PKGBUILD in current helper scope>
  elif [[ $# -eq 1 && ${1-} == '/etc/makepkg.conf' ]]; then
    <lowered /etc/makepkg.conf in current helper scope>
  else
    false
  fi
}; then
  error "failed"
  return 1
fi
```

The concrete renderer may choose a different shell shape, but it must preserve
these properties:

- argument matching is exact string matching after normal path normalization,
  not glob or pattern matching
- argument matching is count-aware
- unknown argument vectors fail closed
- the original failure path observes a failed source status
- inlined source content executes in the original helper's shell context
- the renderer does not append a command that overwrites the lowered source
  status
- path literal quoting is deterministic and safe for spaces and shell
  metacharacters allowed by supplement validation

When lowered source content contains a top-level sourced-file `return`, the
renderer wraps that sourced body in generated same-shell helper functions,
calls them at the source site, and cleans them up before returning. This makes
`return` legal, stops only the sourced body, and preserves the source command
status for guard shapes such as `if ! source "$@"; then`. The supported
contract is normal sourced-library behavior; source files that intentionally
inspect top-level `FUNCNAME` identity or invalid top-level `local` behavior are
still outside V1. Caller positional mutation through top-level `set --` /
`shift` follows the source-argument semantics contract: supported when the
lowering can preserve Bash behavior exactly, including explicit frame
restoration around later nested source sites.

## Context Mode

Context mode remains readable-first. It may preserve the retained helper body
with an annotation that supplement-backed runtime dispatch is required for
executable lowering. It must not claim that a retained `source "$@"` site is
fully resolved unless the supplement-backed dispatch table has been applied.

## Diagnostics

Executable mode fails before writing output when a retained source helper cannot
be lowered exactly.

Diagnostics should identify:

- the retained helper name
- the source site fragment
- whether a supplement function entry is missing, invalid, or insufficient
- the accepted V1 shape, when relevant
- a `details.supplement_skeleton` object when a supplement can make the case
  exact

Useful stable diagnostic families:

- missing retained helper supplement entry
- retained helper supplement vector has zero source path arguments
- retained `source "$1"` / `source "${1}"` helper receives a multi-argument
  supplement vector
- retained helper source site is outside the accepted positional subset
- retained helper body has unsupported source-relevant behavior
- retained helper dispatch would require unsupported source argument expansion

## Tests

Synthetic tests should land before real-world promotion:

- `source_safe` retained with one supplemented path compiles in executable mode.
- `source_safe` retained with one supplemented path plus exact source arguments
  compiles in executable mode and passes those arguments to the lowered source.
- Generated executable output contains no live `source` or `.` command for the
  retained source site.
- Allowed runtime arguments execute the lowered source content.
- Unknown runtime arguments take the original failure branch and return
  non-zero.
- Missing supplement fails before output and emits a valid skeleton.
- Zero-argument, invalid `source "$1"` multi-argument, unquoted `$@` / `$*`,
  recursive helper, dynamic dispatch, and wrapped-source top-level positional
  mutation cases fail with explicit diagnostics.
- Source files containing top-level `return` are rendered with Bash-equivalent
  source status for supported direct and retained helper source sites.
- Context mode remains readable and does not overstate exactness.

Real-world acceptance:

- Promote pacman/makepkg raw `source_safe` library cases from unsupported to
  success in the internal real-world suite once the synthetic tests are green.
- Add a safe runtime parity probe for the pacman wrapper fixture.

## Iteration Tranches

### Tranche 1: Diagnostic And Supplement Binding

- Detect retained source-bearing helpers that would leave live source commands
  in executable output.
- Bind `functions.<name>[].arguments` entries to retained helper definitions.
- Generate supplement skeletons for missing retained helper entries.
- Reject invalid vectors before output.

### Tranche 2: Same-Scope Dispatch Lowering

- Lower accepted positional source sites into finite same-scope dispatch.
- Reuse normal source resolution for each allowed path.
- Preserve source command status through existing guard shapes.
- Keep generated output deterministic and free of unresolved live sources.

### Tranche 3: Real-World Promotion And Cleanup

- Promote the pacman/makepkg retained `source_safe` case in the internal suite.
  Local verification has covered pacman `libmakepkg/util/util.sh` and
  `libmakepkg/lint_package.sh` with a PKGBUILD-like supplemented path.
- Cover makepkg include-guard `return` lowering in the synthetic suite and the
  pacman corpus.
- Add a first opt-in runtime parity probe for the controlled pacman wrapper.
- Review generated executable and context artifacts manually.
- Tighten diagnostics and docs from the real-world findings.
- Keep aggressive performance work and runtime discovery out of scope.

## Done Definition

- Unit tests cover every accepted and rejected V1 shape.
- Existing full test suite passes.
- Executable failures do not create or overwrite output.
- A pacman/makepkg retained helper corpus probe succeeds with a supplement.
- The controlled pacman wrapper runtime probe matches original Bash behavior
  when `MODASH_REALWORLD_RUNTIME=1` is enabled.
- Product docs link to this spec from the supplement and support matrix docs.
