# Supported Source Resolution

This document is the user-facing contract for source discovery and executable
lowering. `modash` resolves `source` and `.` dependencies without executing
shell code. Context mode may preserve unresolved source text for readability,
but executable mode only lowers source sites that can be resolved exactly.

## Output Mode Contract

- **Context mode** is readable-first. It renders one deduplicated section per
  resolved file, preserves original source lines, and annotates resolved source
  relationships.
- **Executable mode** is parity-first. It inlines source bodies at the source
  site when the evaluator can preserve supported Bash parent-source semantics.
- Unsupported executable-mode source sites fail before the output file is
  written or overwritten.

## Static Paths

Supported:

```bash
source ./dep.sh
. ./dep.sh
source ../shared/dep.sh
source "./dir with spaces/dep.sh"
source ./dir#tag/dep.sh
source ./config
source "/absolute/path/dep.sh"
```

This includes relative, parent-relative, absolute, non-`.sh`, spaces, and `#`
inside path components.

## Variables And Path Commands

Supported when the values are exact:

```bash
DEP_PATH="./dep.sh"
source "$DEP_PATH"

source "$ENV_PROVIDED_ABSOLUTE_PATH"

THIS_DIR="$(dirname "$BASH_SOURCE")"
source "$THIS_DIR/dep.sh"

source "$(dirname "$BASH_SOURCE")/dep.sh"
source "./plugins/$(basename ./plugins/dep.sh .sh).sh"
source "$(realpath ./dep.sh)"
```

The supported path command substitutions are `dirname`, `basename`, and
`realpath`.

## Safe File And Command Producers

Supported when producer output is exact for the current project tree and
resolves to the required number of paths:

```bash
source "$(cat dep-path.txt)"
source "$(find ./plugins -name dep.sh)"
eval "source ./dep.sh"
```

For loop word lists and read-loop producers, the supported safe commands are
`cat`, `find`, `printf`, `sort`, `head`, `grep -lF`, `grep -lE`, `realpath`,
`dirname`, and `basename`.
Modeled `find` producers preserve Bash/GNU `find` traversal order rather than
sorting matches.

## Arrays

Supported when indexes and values are exact:

```bash
deps=(./a.sh ./b.sh)
source "${deps[0]}"
source "${deps[$i]}"

declare -A by_env=([prod]=./prod.sh [dev]=./dev.sh)
source "${by_env[$ENV]}"
```

The evaluator also models exact array append, indexed assignment,
command-substitution array assignment, and `mapfile` / `readarray -t`
population from exact files.

## Loops

Supported finite loop forms include literal word lists, exact scalar word
lists, exact custom-IFS splitting, exact arrays, ordinary deterministic file
globs, safe command-substitution word lists, bounded C-style loops, bounded
`while` / `until`, and modeled `while read` file enumeration.

Examples:

```bash
for dep in ./a.sh ./b.sh; do
  source "$dep"
done

DEPS="./a.sh ./b.sh"
for dep in $DEPS; do
  source "$dep"
done

for dep in "${deps[@]}"; do
  source "$dep"
done

for dep in ./plugins/*.sh; do
  source "$dep"
done

for ((i=0; i<3; i++)); do
  source "./deps/$i.sh"
done

while read -r dep; do
  source "$dep"
done < deps.txt

find ./plugins -name '*.sh' -print | while read -r dep; do
  source "$dep"
done
```

Loop glob handling is option-aware for `nullglob`, `dotglob`, `globstar`,
`nocaseglob`, `extglob`, practical `GLOBIGNORE` filtering, comma braces, and
simple brace sequences.

## Direct Source Expansion

Supported when direct source command-word expansion resolves to one or more
regular files:

```bash
source ./single-plugin/*.sh
source ./{real,missing}.sh
```

When a direct source glob, extglob, or brace expansion resolves to multiple
command words, Bash treats the first expanded word as the source file and
passes the remaining expanded words as positional arguments to that sourced
file. Executable mode preserves that shape:

```bash
source ./plugins/*.sh extra
```

If `./plugins/*.sh` expands to `./plugins/00-loader.sh
./plugins/10-config.sh`, `00-loader.sh` is sourced with
`./plugins/10-config.sh` and `extra` as its `$1` and `$2`.
Only the first remaining expanded word must be a sourceable regular file;
later expanded words are arguments and may name non-file paths exactly as Bash
would pass them.

When a supported direct source glob produces no usable source file, executable
mode lowers Bash's runtime source failure instead of preserving a live source
command. Ordinary unmatched globs and exact `GLOBIGNORE` all-filtered globs
produce a missing-file failure with status `1`; bare `nullglob` source sites
that lose their filename produce Bash's no-filename source failure with status
`2`.

Exact `nullglob` source-word shifting is supported when a later exact word
becomes the filename:

```bash
shopt -s nullglob
source ./missing/*.sh ./fallback.sh arg
```

Here `./fallback.sh` is sourced with `arg` as `$1`.

Direct `failglob` expansion failures are lowered when the source site is not
behind a runtime-dependent guard:

```bash
shopt -s failglob
source ./missing/*.sh
```

The generated executable prints a Bash-shaped `no match` diagnostic, preserves
status `1`, and prevents later commands on the same physical line from running.
Parsed source conditions, parsed source-bearing `for` loop word lists, and
direct function-body source sites are also lowered when the failure is
deterministic.

## Direct Source Arguments

Supported direct source sites may pass exact positional arguments to the
sourced file:

```bash
source ./library.sh alpha "beta gamma"
```

The sourced file sees those arguments as its temporary `$1`, `$2`, and `$@`
state. The caller's positional parameters are restored after the source site
returns, while normal sourced-file effects such as variables, functions, cwd,
and return status remain parent-visible. Nested source sites inherit the current
sourced-file positional state unless they pass their own exact arguments.

Source arguments must resolve to exact strings. Unresolved variables, command
substitution, unquoted `$@` / `$*`, and unquoted variable expansions that would
require word-splitting support remain fail-closed.

Executable mode models top-level positional mutation in wrapped sourced files
when Bash behavior is exact. Top-level `set -- ...` can update caller
positionals after a source site returns. Top-level `shift` by itself inside an
explicit source-argument frame remains temporary as Bash restores that frame,
while `shift` after a caller-visible `set --` updates the captured positional
mutation. Sourced files without explicit source arguments can also mutate
caller positionals through top-level `set --` or `shift` when a generated
return wrapper is needed.

Executable mode also models explicit source-argument frame restoration around
later nested source sites. A pre-nested top-level `set --` inside a sourced file
entered with explicit arguments does not escape merely because it ran before a
nested source. Nested sources without explicit arguments can dirty the current
frame when they mutate positionals, nested sources with explicit arguments
restore their own frame before returning to the outer source body, and later
top-level `set --` mutations in the outer sourced file can supersede the
barrier.

## Sourced-File Return

Supported sourced files may contain top-level `return` statements. Executable
mode lowers those sourced bodies through generated same-shell helper functions
that are cleaned up after the source site runs. The lowered return stops the
sourced body, preserves source status, and does not exit the caller. This is
intended for normal sourced libraries and include guards; files that depend on
top-level `FUNCNAME` identity or invalid top-level `local` behavior remain
outside the supported contract.

```bash
source ./guarded-library.sh
echo "source status: $?"
```

This covers common include guards:

```bash
[[ -n "$LIBRARY_SH" ]] && return
LIBRARY_SH=1
```

## Child-Shell Source Contexts

Supported exact source sites can be lowered inside child-shell boundaries when
the compiler can preserve the boundary instead of treating child-local effects
as parent-visible:

```bash
( source ./dep.sh; printf '%s\n' "$VALUE" )
source ./dep.sh | sed 's/^/child:/'
value="$(source ./dep.sh; printf '%s\n' "$VALUE")"
cat <(source ./dep.sh; emit_child)
bash -c 'source ./dep.sh; printf "%s\n" "$VALUE"'
CHILD_ENV=exact bash -c 'source ./dep.sh; printf "%s\n" "$CHILD_ENV"'
```

Variables, functions, aliases, cwd, shell options, traps, positional
parameters, and top-level `return` behavior from these source sites stay inside
the child shell. Non-final pipeline segments are modeled as child shells.
Pipeline final segments remain fail-closed because they can depend on
`lastpipe` and job-control state.

`bash -c` support is deliberately narrow: the payload must be a static
single-source command string with no extra argv entries, no parent-expanded
double-quoted `$` references, and no dynamic source expression. Assignment
prefixes are preserved as environment for the child Bash process.

## Branches And Cases

Supported branch-aware source lowering includes `if` / `elif` / `else` with
side-effect-free file, non-empty, empty, exact string, pattern, compound
logical, arithmetic, regex, and safe `grep -q` predicates.
Unknown runtime predicates are preserved when the predicate itself is not a
source-bearing command and branch-local source sites are exact.

```bash
if [[ -f ./optional.sh ]]; then
  source ./optional.sh
fi

if grep -q enabled config; then
  source ./enabled.sh
else
  source ./disabled.sh
fi
```

Exact source atoms are also supported inside top-level `if` / `elif` logical
condition lists. The compiler lowers only the source atom and leaves the
runtime guard shape intact:

```bash
if source ./dep.sh && runtime_probe; then
  echo loaded
fi

if runtime_probe || source ./fallback.sh; then
  echo ready
fi
```

Source effects that depend on a runtime condition are treated as conditional.
Later source sites fail closed if they need variables, arrays, cwd, shell
options, functions, or positional state that diverged across possible
condition paths.

Supported `case` blocks use modeled arm patterns: literal patterns, alternate
patterns, default arms, quoted literals, mixed quoted/unquoted segments,
backslash-escaped literals, ordinary glob patterns, bracket expressions, POSIX
character classes in the modeled C-locale subset, and exact scalar
variable-expanded patterns. Known scalar subjects are statically selected.
Unknown runtime subjects preserve the original `case` and lower exact source
sites in all runtime-possible arms. Source-bearing `case` arms may use `;;`,
`;&`, or `;;&` terminators.

```bash
case "$ENV" in
  prod) source ./prod.sh ;;
  dev) source ./dev.sh ;;
  *) source ./default.sh ;;
esac
```

## Functions

Supported function calls are bounded and local. The function definition must be
known, arguments must be exact, and source-relevant body effects must be
modeled.

Supported source-relevant effects include positional source arguments, exact
assignment prefixes, `local` scalar assignments, cwd changes, exact `return` /
`shift`, exact dynamic dispatch, nested modeled control flow, same-line
post-definition calls, source-equivalent branch-defined functions,
function-call status for chained source sites, and functions defined by sourced
files.

```bash
load_dep() {
  source "$1"
}

load_dep ./dep.sh
```

## Wrapped Source Invocations

Executable mode lowers exact `builtin` and `command` source invocation forms
when their operands are otherwise supported source expressions. This covers
parent-shell source sites and the modeled child-shell contexts.

```bash
builtin source ./dep.sh arg
builtin . ./dep.sh arg
command source ./dep.sh arg
command . ./dep.sh arg
command -p source ./dep.sh arg
command -- source ./dep.sh arg
```

Assignment-prefixed and negated wrapped source commands remain fail-closed until
their broader simple-command prefix semantics are modeled.

## Unsupported In Executable Mode

Unsupported or ambiguous source forms fail closed before output is written.
Common examples:

```bash
source "$DEP"                        # DEP unknown
source ./dep.sh "$UNKNOWN_ARG"       # source argument unknown
source ./dep.sh $ARG_WITH_SPACES     # would require word splitting
source ./literal-missing.sh          # non-glob missing literal file
source "$(cat one two)"              # ambiguous path output
source "$(find . -name '*.sh')"      # ambiguous when multiple files match
source "$(cat dep-path.txt | sort)"  # unapproved source-site pipeline
source `cat dep-path.txt`            # backticks
eval "source ./dep.sh; echo extra"   # unsafe eval payload
bash -c "source $DEP"                # parent-expanded dynamic payload
printf ready | source ./dep.sh       # lastpipe-sensitive final segment
```

Other fail-closed families include quoted globs, `set -f` / `noglob`,
`failglob` after runtime-dependent `&&` / `||` guards, source-relevant local
function calls behind unknown logical guards, branch-dependent or runtime-dynamic
glob state, source commands in unsupported shell grammar, final pipeline
segments whose semantics depend on `lastpipe`, unsupported dynamic `case`
subjects or arm patterns, unsupported process substitution outside modeled
read-loop or child-shell input, unknown runtime-dynamic or recursive function
dispatch, non-equivalent branch-defined functions, branch-dependent function
returns, nested dynamic substitutions, and multi-result command-substitution
output where a single source path is required.

## Practical Remaining Work

The remaining source-resolution surface is narrower than general Bash support:

- Remaining case edge semantics such as collating symbols, equivalence classes,
  and broader locale-dependent pattern behavior. The implemented deterministic
  pattern subset includes practical `extglob` and `GLOBIGNORE` handling.
- Exact positional-count predicates such as `[ "$#" -eq 0 ]` are modeled for
  function-return guards when the current positional frame is exact.
- Recursive or runtime-dynamic source-bearing function dispatch outside a
  finite trusted runtime graph. Exact makepkg-style helper calls using quoted
  `$@` / `$*` are also covered by [Source Supplements](source-supplements.md).
  Retained helper definitions that remain callable after static merging are
  supported only for finite exact or supplement-backed argument vectors.
- Broader runtime-discovered helper shapes should flow through
  [Runtime Source Discovery](runtime-source-discovery.md). In 0.7, observed
  compile uses graph-tape source rewriting so the static evaluator does not
  need bespoke support for every dynamic helper, loop, or dispatcher shape.
  Ambiguous dispatch still needs a reviewable trusted graph before it can be
  merged safely.
