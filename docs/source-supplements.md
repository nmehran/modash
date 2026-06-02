# Source Supplements

## Status

V1 design and implementation contract. Source supplements are product inputs for
source-relevant values the compiler cannot infer from shell text alone. They are
not test hacks and they are not shell scripts.

This feature exists for common helper patterns such as:

```bash
source_safe() {
  if ! source "$@"; then
    return 1
  fi
}

source_safe ./PKGBUILD
source_safe "$MAKEPKG_LIBRARY/util/message.sh"
```

The compiler should support the exact call-site subset directly. Supplements
cover the remaining runtime-dynamic values only when the user supplies explicit
declarative data.

## Public Interface

CLI:

```sh
modash <entrypoint> <output> --mode executable --source-supplement source-supplement.json
```

API:

```python
compile_sources(entry_point, output_file, mode="executable", source_supplement="source-supplement.json")
get_sources(entrypoint, mode="executable", source_supplement="source-supplement.json")
```

The supplement format is JSON only. V1 uses `version: 1`:

```json
{
  "version": 1,
  "variables": {
    "MAKEPKG_LIBRARY": "/usr/share/makepkg"
  },
  "functions": {
    "source_safe": [
      {
        "arguments": ["./PKGBUILD"]
      }
    ]
  }
}
```

Relative supplement path values resolve from the entrypoint directory. Script
assignments override supplement variables. Supplement variables override the
process environment for source resolution.

## Exact Helper Sources

Quoted all-positionals source expressions are supported inside modeled local
function calls when the first argument is an exact source path. Additional
arguments are passed to the sourced file through the direct source-argument
model:

- `source "$@"`
- `source "${@}"`
- `source "$*"`
- `source "${*}"`
- the same forms with `.`

The function call must already be source-aware and bounded: known local
definition, exact dispatch, exact arguments, no recursion, and modeled body
effects. The source path then passes through the normal resolver contract.

V1 intentionally rejects:

- zero-argument `source "$@"`
- multi-argument `source "$*"` because quoted `$*` is one joined word, not a
  source path plus argument vector
- unquoted `$@` / `$*`
- dynamic function dispatch
- recursive source-bearing helpers
- branch-dependent helper signatures
- helper calls whose unresolved arguments are not supplied by a supplement

Source commands in helper guards such as `if ! source "$@"; then` are lowered at
the condition source site for the quoted all-positionals helper subset. Direct
exact source-bearing conditions such as `if source ./dep.sh; then` are handled
by the executable source-condition subset. Executable output must still contain
no live unresolved source command.

The V1 helper subset also models the common makepkg `source_safe` shopt restore
shape:

```bash
local shellopts=$(shopt -p extglob)
shopt -u extglob
if ! source "$@"; then
  ...
fi
eval "$shellopts"
```

The saved `shopt -p` payload must be exact and must not contain a source
command. Hidden or unresolved source-bearing `eval` payloads remain fail-closed.

## Supplement Validation

Supplements are declarative exact data:

- top-level keys are limited to `version`, `variables`, and `functions`
- `version` must be `1`
- variable and function names must be shell identifiers
- variable values and function arguments must be strings
- values must not contain shell expansion, command substitution, backticks, or
  newlines
- every supplied path value must exist after resolving relative to the
  entrypoint directory

Function entries define finite allowed argument vectors for named source
helpers. By default, the first argument is treated as the source path and
subsequent arguments are exact strings passed to the sourced file. Advanced
generated supplements may set `source_index` to point at a non-leading source
path argument, for helper shapes such as `source "$2" "$1"` or exact `case`
dispatch that selects a non-first positional source path. The value is zero
based and defaults to `0`; it must identify an existing path in the argument
vector.

They do not make arbitrary dynamic dispatch safe.

Retained source helpers that remain callable in generated executable output use
same-scope dispatch lowering for the supported V1 subset. Unknown helper
arguments still fail closed unless the supplement provides a finite allowed
argument vector.

## Two-Pass Workflow

First pass:

- executable mode fails closed before writing output
- the diagnostic identifies the unresolved source-relevant value or helper
  argument
- the diagnostic includes `details.supplement_skeleton`
- the CLI prints the skeleton JSON to stderr

Second pass:

- the user supplies the JSON supplement
- the compiler validates the schema and paths before evaluation
- supplement variables seed the evaluator state
- supplement function signatures may supply unresolved helper call arguments
- executable mode proceeds only if every source site is exact

Example skeleton:

```json
{
  "version": 1,
  "variables": {
    "MAKEPKG_LIBRARY": "<path>"
  },
  "functions": {
    "source_safe": [
      {
        "arguments": ["<source-path>"]
      }
    ]
  }
}
```

Context mode remains readable-first. It may preserve unresolved source text, but
it must not claim exact resolution unless the normal resolver or supplement
contract proves it.
