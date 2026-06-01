# Dynamic Source Resolution

## Status

Current implementation. The compiler supports static sources,
variable/env-expanded paths, path command substitutions such as `dirname`,
`basename`, and `realpath`, plus Python-only dynamic resolvers for safe `cat`,
safe `find`, and safe `eval source`. The source-effect evaluator also supports
exact finite `for` loops over literal words, known scalar path variables, exact
custom-IFS scalar word lists, exact `${array[@]}` expansions, safe
command-substitution word lists, and deterministic ordinary file globs in
finite loop word lists. Exact indexed, associative, appended,
command-substitution, and file-populated arrays are modeled, as are bounded
`while` / `until`, C-style `for ((...))`, and `while read` file enumeration
from exact files, safe producer pipelines, and safe process substitutions.
Direct source globs are accepted when they resolve to one or more regular files;
multi-match direct globs pass the remaining expanded words as source positional
arguments.
Exact source sites inside explicit subshells, non-final pipeline segments,
command substitutions, process substitutions, and static single-source
`bash -c` payloads are lowered with child-shell semantics when the boundary can
be preserved exactly.
Modeled `if` / `elif` / `else` blocks can lower source sites inside branches
when branch predicates are side-effect-free and branch state is exact enough for
later source resolution. Executable mode neutralizes source sites in statically
unreachable branches instead of resolving or preserving them as live runtime
sources. The current modeled condition subset includes exact file/string tests,
compound logical predicates, arithmetic predicates, regex and pattern matching,
and safe `grep -q` file checks. Exact `case` blocks can lower source sites for
known scalar subjects and modeled arm patterns, with non-matching arm sources
neutralized in executable output. Bounded local function calls can lower sources
when the function definition is known, arguments are exact, and source-relevant
function body effects are modeled. Runtime-dynamic source arguments such as
`source "$@"` inside helper functions are supported for the exact call-site
subset, and user-supplied source supplements handle values that cannot be
inferred statically. Unsupported forms fail closed. The detailed supplement and
helper-source contract lives in
[Source Supplements And Exact Helper Sources](source-supplements.md).

## Goal

Support common source-path idioms found in shell projects without executing
shell code. The resolver should turn known-safe expressions into resolved source
paths, and should produce explicit unsupported diagnostics when an expression is
ambiguous, side-effectful, or outside the supported subset.

The compiler is not a sandbox and should not become one. Dynamic source
resolution belongs in a deterministic Python resolver layer, not in the optional
setup shell helper.

## Non-Goals

- Do not execute shell commands.
- Do not execute `eval`.
- Do not run a restricted shell to discover dependencies.
- Do not infer dependencies from arbitrary Bash semantics.
- Do not silently omit unresolved source statements.
- Do not treat context mode as a license to guess.

## Terms

- **Source site**: A `source` or `.` command in an input file.
- **Source expression**: The argument portion of a source site, such as
  `"./dep.sh"` or `"$(cat dep-path.txt)"`.
- **Resolver**: A Python component that can prove a source expression resolves
  to one or more files.
- **Exact resolution**: A resolver result with deterministic file path output
  and no side effects.
- **Unsupported diagnostic**: A structured failure explaining why the compiler
  refused to resolve a source site.
- **Parent-source semantics**: The sourced file runs in the current shell
  context, matching normal `source ./dep.sh`.
- **Child-shell semantics**: The sourced file runs inside a child process, such
  as `bash -c "source ./dep.sh"`.
- **Source supplement**: A user-authored data file that supplies exact values
  for source-relevant variables, function arguments, or call signatures the
  compiler cannot infer from static shell text alone.

## Resolver Contract

Each resolver must be pure, deterministic, and fail closed. A resolver accepts
the current parser context and a source site, then returns either resolved
source records or an unsupported diagnostic.

Resolved source records should carry enough metadata for both renderers:

```python
ResolvedSource(
    path="/absolute/path/to/dep.sh",
    source_expression='"$(cat dep-path.txt)"',
    source_site='source "$(cat dep-path.txt)"',
    execution_model="parent-source",
    confidence="exact",
)
```

The minimum fields are:

- `path`: absolute resolved path.
- `source_expression`: original source argument.
- `source_site`: original source command or command fragment.
- `execution_model`: `parent-source`, `child-shell`, or `context-only`.
- `confidence`: `exact` for accepted compiler dependencies.

Executable mode should accept only `parent-source` records unless the compiler
implements an explicit equivalent for another execution model. Context mode may
render `context-only` records, but the output must make that limitation clear.

## Safety Rules

- Resolver input is parsed data, not shell text to execute.
- File reads are allowed only for safe producer resolvers whose purpose is
  path-list discovery, such as safe `cat`, `sort`, `head`, and `grep -lF` /
  `grep -lE`.
- Directory walks are allowed only for safe `find` subsets.
- Multiple candidate paths are unsupported in executable mode unless the source
  form has deterministic multi-source semantics. Direct source positional
  arguments are supported when each argument resolves to an exact string.
- Ambiguous current-directory state is unsupported.
- Any command separator, redirection, pipe, process substitution, background
  operator, or unapproved command substitution in a dynamic source expression is
  unsupported.
- Executable mode must not emit unresolved live `source` commands.
- Unsupported cases must fail before output is written.
- Context mode may preserve unresolved source text for readability, but it must
  not claim a resolved dependency unless resolution is exact.
- Supplement-provided values must be explicit and scoped. They may provide
  source path values or function call signatures, but they must not execute
  shell code or weaken the fail-closed rule.

## Resolver Priority

Resolvers should be tried from most explicit to most specialized:

1. Literal path resolver
2. Known variable and environment resolver
3. Path command resolver: `dirname`, `basename`, `realpath`
4. Safe `cat` resolver
5. Safe `find` resolver
6. Direct one-match glob resolver
7. Safe `eval source` resolver
8. Exact child-shell source lowering
9. Exact function-call argument binding
10. Supplement-backed source resolution
11. Unsupported dynamic diagnostic

The resolver registry should make this ordering explicit in code and tests.

## Current Support Matrix

The user-facing current support matrix lives in
[Supported Source Resolution](supported-source-resolution.md). This document
describes resolver contracts, safety rules, implementation details, and future
design constraints.

## Implemented Resolver Subset

### Safe `cat`

Target forms:

```bash
source "$(cat dep-path.txt)"
. "$(cat ./dep-path.txt)"
```

Accept only when:

- `cat` has exactly one operand.
- The operand path is statically resolved.
- The operand is a regular file.
- The file content contains exactly one non-empty line after trimming one final
  newline.
- The resolved line is a valid source path under the current parser context.
- No flags, pipes, redirects, process substitutions, globs, or extra command
  substitutions are present.

Reject examples:

```bash
source "$(cat one two)"
source "$(cat dep-path.txt | head -1)"
source "$(cat < dep-path.txt)"
source "$(cat "$(other)")"
```

### Safe `find`

Target forms:

```bash
source "$(find . -name dep.sh -print -quit)"
source "$(find ./plugins -type f -name init.sh -print -quit)"
```

Accept only when:

- Search roots are statically resolved directories.
- Supported predicates are limited to:
  - `-name <pattern>`
  - `-path <pattern>`
  - `-type f`
  - `-maxdepth <n>`
  - `-mindepth <n>`
  - `-print`
  - `-quit`, only after `-print`
- No `-exec`, `-delete`, `-ok`, `-printf`, shell escapes, pipes, redirects, or
  process substitutions are present.
- The resolver finds exactly one matching regular file.
- Traversal order follows Bash/GNU `find` traversal order for executable parity.

Reject examples:

```bash
source "$(find . -name dep.sh)"
source "$(find . -name dep.sh -quit)"
source "$(find . -exec echo {} \;)"
source "$(find . -name '*.sh' | head -1)"
```

The first rejection is intentional unless the resolver can prove exactly one
result. If multiple files match, executable mode must reject rather than choose
one implicitly.

### Safe `eval source`

Target forms:

```bash
eval "source ./dep.sh"
eval ". \"$DEP_PATH\""
eval "$KNOWN_SOURCE_COMMAND"
```

Accept only when:

- After known variable and environment expansion, the eval payload parses as
  exactly one source command.
- The source expression inside the payload resolves through the normal resolver
  registry.
- No nested `eval`, command separators, redirects, pipes, assignments,
  functions, arithmetic expansion, or unapproved substitutions are present.

Reject examples:

```bash
eval "source ./dep.sh; rm -rf out"
eval "$UNRESOLVED_OR_MULTI_COMMAND_PAYLOAD"
eval "DEP=./dep.sh; source \"$DEP\""
eval "source $(cat dep-path.txt)"
```

The last example should be rejected at the `eval` layer initially. A later
implementation may allow nested resolver dispatch only if diagnostics remain
clear and the parser can prove there is still one source command.

### Deterministic Globs

Target loop forms:

```bash
for dep in ./plugins/*.sh; do
  source "$dep"
done

for dep in "./plugin dir#tag"/*.sh; do
  source "$dep"
done
```

Target direct-source form:

```bash
source ./single-plugin/*.sh
```

Accept loop globs only when:

- The glob metacharacters are unquoted.
- The pattern is an ordinary file glob using `*`, `?`, `[]`, deterministic
  brace expansion, modeled `extglob`, or modeled `globstar` recursion.
- Expansion is cwd-aware and deterministic.
- Every match is a regular file.
- Modeled shell state is limited to `nullglob`, `dotglob`, `globstar`,
  `nocaseglob`, `extglob`, `failglob`, and practical `GLOBIGNORE` filtering.

Accept direct source globs and direct brace-only source expansion when command
word expansion resolves to at least one regular file. For multiple
direct-source expanded words, source the first expanded word and pass the
remaining expanded words as positional arguments to that sourced file. Exact
`nullglob` source-word shifting and deterministic `failglob` expansion failures are
covered by
[Source Expansion Failure Semantics](source-expansion-failure-semantics.md).

Reject examples:

```bash
for dep in "./plugins/*.sh"; do source "$dep"; done
set -f
for dep in ./plugins/*.sh; do source "$dep"; done
GLOBIGNORE=./plugins/a.sh:./plugins/b.sh
for dep in ./plugins/*.sh; do source "$dep"; done
```

Currently rejected glob-affecting state includes `set -f`, branch-dependent or
runtime-dynamic glob options, and unknown-guard `failglob`. Exact `GLOBIGNORE`
all-filtered source-producing globs lower to Bash-equivalent runtime source
failures when `nullglob` is not set.

### Command-Substitution Word Lists

Target loop forms:

```bash
for dep in $(cat deps.txt); do
  source "$dep"
done

for dep in $(find ./plugins -type f -name '*.sh' -print); do
  source "$dep"
done

deps=($(cat deps.txt))
for dep in "${deps[@]}"; do
  source "$dep"
done
```

Accept only when the command substitution is a single safe producer:

- `cat` with exact readable file operands
- safe `find` with the existing predicate subset and Bash/GNU traversal order
- `printf '%s\n' ...` with exact arguments
- `sort` with exact readable file operands and optional `-u`
- `head` with one exact readable file operand and optional `-n N` / `-N`
- `grep -lF` or `grep -lE` with exact readable file operands
- `realpath` over exact existing file operands
- `dirname` / `basename` over exact operands

Unquoted command-substitution output is split with the exact current `IFS`,
including custom scalar `IFS` values. Quoted command substitution is accepted
only when it produces exactly one non-empty line. Pipes, command separators,
nested substitutions, and backticks remain fail-closed.

### Bounded `while` / `until` And Read Loops

Target forms:

```bash
i=0
while (( i < 2 )); do
  source "./deps/$i.sh"
  ((i++))
done

while IFS= read -r dep; do
  source "$dep"
done < deps.txt

while read -r dep || [[ -n "$dep" ]]; do
  source "$dep"
done < deps.txt

find ./plugins -type f -name '*.sh' -print | while read -r dep; do
  source "$dep"
done

while read -r dep; do
  source "$dep"
done < <(find ./plugins -type f -name '*.sh' -print)

for (( i=0; i<2; i++ )); do
  source "./deps/$i.sh"
done
```

The evaluator models exact `while` / `until` conditions, exact arithmetic
assignment and increment/decrement mutations, local `break` / `continue`, and a
bounded iteration limit. It also models `while read` file enumeration, including
the common `IFS= read -r` form for paths containing spaces and the
`read ... || [[ -n "$var" ]]` guard for files without a final newline. Read-loop
input may come from an exact file redirection, safe producer pipeline, or safe
process substitution. Pipeline read loops use an explicit subshell wrapper
unless exact `lastpipe` / `monitor` shell state proves the loop runs in the
parent shell. C-style `for ((...))` loops are modeled when init, condition, and
update clauses are exact arithmetic expressions.

Unknown loop conditions, unsupported C-style arithmetic, unsupported read
options, multi-level loop control, and loops exceeding the modeled iteration
limit fail before executable output is written.

### `bash -c "source ..."`

Target forms:

```bash
bash -c 'source ./dep.sh; printf "%s\n" "$VALUE"'
bash -c ". ./dep.sh"
CHILD_ENV=exact bash -c 'source ./dep.sh; printf "%s\n" "$CHILD_ENV"'
```

This is not equivalent to parent-shell `source`. The sourced file executes in a
child Bash process, so variable and directory side effects do not propagate back
to the parent script.

Current handling:

- Context mode records exact dependencies with `child-shell` semantics.
- Executable mode lowers static single-source payloads with no extra argv
  entries by rewriting only the inner source command inside the `bash -c`
  payload.
- Assignment prefixes before `bash` are preserved as child-process
  environment.
- Single-quoted payloads may keep child-Bash variable references after the
  exact source site.
- Double-quoted payloads containing `$`, dynamic source expressions, multiple
  source commands, and payloads with extra `$0` / positional argv entries fail
  closed.

Executable lowering renders an equivalent child-shell boundary rather than
inlining the file into the parent shell.

### Function Argument Source Sites

Target forms:

```bash
source_safe() {
  if ! source "$@"; then
    return 1
  fi
}

source_safe ./PKGBUILD
source_safe "$MAKEPKG_LIBRARY/util/message.sh"
```

The desired final behavior is to support these helper patterns when call sites
can be proven. `source "$@"`, `source "$1"`, and similar positional source
expressions are not inherently unsafe; they are unsafe only when the compiler
cannot determine the function call arguments.

Accept initially when:

- The function definition is known and source-relevant body effects are modeled.
- Every reachable call site has an exact argument list.
- Positional parameters map to an exact source path plus any exact source
  arguments, or to an explicitly modeled multi-source call shape.
- The resolved source paths pass the normal resolver contract.
- Function return/status behavior remains exact enough for later source
  resolution.

Reject when:

- A reachable call site uses unresolved `$@`, `$*`, branch-dependent variables,
  dynamic dispatch, recursive calls, or incompatible call signatures.
- Different reachable call sites imply non-equivalent source graphs that cannot
  be represented safely.
- The function can be called from outside the analyzed graph without a
  supplement that declares the missing call signature.

The first implementation should stay narrow: direct function calls with literal
or already-resolved arguments, then tests against the makepkg `source_safe`
pattern.

### Source Supplements

Some real projects intentionally defer source paths to runtime inputs. Those
cases are handled through the two-pass JSON supplement workflow documented in
[Source Supplements And Exact Helper Sources](source-supplements.md): first
fail closed with a structured diagnostic and supplement skeleton, then ingest
and validate explicit user-provided values on the second pass.

## Future Pattern Families

These still need separate specs before implementation:

- Broader glob semantics beyond ordinary deterministic file globs.
- Static evaluation of conditional predicates outside the modeled
  side-effect-free subset.
- Remaining case edge semantics such as collating symbols, equivalence
  classes, and broader locale-dependent pattern behavior. See
  [Source Pattern Semantics Completion](source-pattern-semantics.md).
- Complex array/list-based source paths outside exact indexed, associative,
  append, command-substitution, and file-populated arrays.
- Broader user-defined function semantics, including runtime-dynamic dispatch,
  recursive calls, non-equivalent branch-defined functions, branch-dependent
  returns, and source-relevant function calls behind unknown logical guards.
- Unsupported process substitution and generated source streams outside exact
  read-loop producer input or exact child-shell source boundaries.

### Unsupported But Practical

These are intentionally tracked as practical future work, not permanently
unsupported forms:

- Source commands in unsupported shell grammar or pipeline final segments whose
  semantics depend on `lastpipe` / job-control state. Exact source atoms in
  top-level `if` / `elif` logical lists are covered by
  [Compound Source Condition Lowering](compound-source-condition-lowering.md).
- Source-free control flow whose body effects exceed the current conservative
  state merge.
- Static guard evaluation outside exact file/glob tests, exact `shopt -q`, and
  the current safe `grep -q` subset.
- Source arguments that require word splitting, command substitution, or
  unresolved runtime values.
- Command predicates outside the safe `grep -q` file-check subset.
- Runtime-dynamic helper sources such as `source "$@"` when exact call-site
  binding is unavailable and no retained-helper supplement has been provided.
- Regex predicates requiring POSIX classes or unsupported Bash ERE behavior.
- Nested modeled control flow inside branch bodies when the current line
  frontend cannot preserve exact nested locations.
- Branch-divergent cwd, variables, arrays, or shell options followed by later
  source resolution that depends on that divergent state.
- Case subjects or arm patterns outside the exact modeled case subset,
  including collating symbols, equivalence classes, and broader
  locale-dependent matching.

These are not merely more dynamic resolvers. They require control-flow and
multi-result semantics, and they should be designed separately.

Executable mode fails closed when a source command appears inside these
unsupported families. That prevents output from silently preserving runtime
`source` behavior that the compiler has not lowered.

## Test Requirements

Each resolver needs tests for:

- accepted exact source resolution
- rejected ambiguous output
- rejected unsafe syntax
- context-mode rendering
- executable-mode parity or explicit executable-mode rejection
- diagnostics that identify the rejected source site
- unsupported executable-mode failures that do not create or overwrite output

Regression tests should use real temporary shell projects through
`ScriptProject`, not mocked strings alone.

## Implementation Status

The initial resolver layer now covers the current resolver-driven compiler
scope:

- `ResolvedSource` records describe exact dependency resolution.
- `methods.source_resolver` owns source command detection, heredoc guards, safe
  dynamic source resolvers, and unsupported-source classification.
- `methods.source_evaluator` owns traversal, cwd tracking, variable state, and
  source-event production.
- `methods.sources` owns path-resolution helpers and the `get_sources()`
  compatibility wrapper over source-effect evaluation.
- Safe `cat`, safe `find`, and safe `eval source` are implemented.
- Exact finite `for` loop lowering is implemented for literal words, known
  scalar path variables, and exact `${array[@]}` expansion.
- Deterministic file-glob loop lowering is implemented, including `nullglob`,
  `dotglob`, `globstar`, `nocaseglob`, deterministic brace expansion, and
  practical `GLOBIGNORE` filtering. Direct source globs are implemented for
  one-match and multi-match source-argument cases.
- Missing-source runtime-error lowering is implemented for unmatched or
  all-filtered source-producing globs when Bash behavior is deterministic; see
  [Missing Source Runtime Error Lowering](missing-source-runtime-lowering.md).
- Direct brace-only source expansion, exact `nullglob` source-word shifting,
  and deterministic `failglob` expansion failure lowering are implemented; see
  [Source Expansion Failure Semantics](source-expansion-failure-semantics.md).
- Exact custom-IFS scalar and command-substitution loop word splitting is
  implemented.
- Safe producer word lists are implemented for `cat`, `find`, `printf`, `sort`,
  `head`, `grep -lF` / `grep -lE`, `realpath`, `dirname`, and `basename`.
- Branch-aware `if` / `elif` / `else` source lowering is implemented for the
  side-effect-free predicate subset and fail-closed branch-state merge.
- Runtime-guarded `if` / `elif` / `else` source lowering is implemented for
  exact source sites inside unknown predicates that do not themselves contain
  source-bearing commands.
- Exact `case` source lowering is implemented for known scalar subjects,
  mutually exclusive arms, and no-op unreachable source sites.
- Runtime-guarded `case` lowering is implemented for unknown scalar subjects
  with supported patterns and exact arm-local source sites.
- Bounded local function source lowering is implemented for known definitions,
  exact arguments, positional source expressions, parent-state mutations,
  exact assignment prefixes, scalar `local` assignments, exact `return` /
  `shift`, exact dynamic dispatch, same-line post-definition calls, and nested
  modeled control flow. It also supports source-equivalent branch-defined
  functions and exact function-call status for chained source sites.
- Supplement-backed retained helper dispatch is implemented for modeled
  positional source helpers with finite argument vectors. The first argument is
  the source path; extra exact arguments are passed through for `source "$@"`
  helpers.
- Top-level `return` in supported sourced files is lowered with a generated
  same-shell wrapper so include guards and source status are preserved.
- Wrapped sourced files can synchronize modeled top-level positional mutation
  back to the caller when Bash semantics are exact.
- Explicit source-argument frame restoration is modeled around later nested
  source sites when the source paths and arguments remain exact; see
  [Explicit Source Argument Frame Restoration](source-argument-frame-restoration.md).
- Exact source sites inside explicit subshells, non-final pipeline segments,
  command substitutions, process substitutions, and static single-source
  `bash -c` payloads are lowered with child-shell semantics; see
  [Source-Bearing Child-Shell Contexts](source-child-shell-contexts.md).
- Executable mode fails before output when unsupported source forms would leave
  live runtime `source` commands.

Structured diagnostic objects are implemented for unsupported source failures.
Current diagnostics are raised as explicit `UnsupportedSourceError` instances
with stable codes, source locations, rejected fragments, messages, and hints.

Future resolver increments should stay small, tested, and fail-closed. Case
pattern broadening, complex array support, broader supplement-backed source
resolution, recursive functions, non-equivalent branch-defined functions,
branch-dependent function returns, and runtime-dispatch support should not be
added as one-off resolver patches; those belong in the evaluator/IR design.
Runtime source discovery and observed supplement generation are scoped by
[Runtime Source Discovery North Star](runtime-source-discovery.md).
