# Real-World Internal Test Suite

## Status

Internal suite iteration. The opt-in unittest harness, local installed smoke
fixtures, normalized JSON result output, pinned corpus fetch/cache path, pinned
expected-outcome enforcement, retained output artifacts, supplement fixtures,
expanded safe runtime parity probes, and concise opt-in reports are
implemented. The pinned corpus
includes `bash-completion` and `pacman`/`makepkg` library fixtures.

This suite is intended to live on an internal development branch until the
harness and corpus prove useful enough to merge. It should be opt-in and should
not affect the default unit-test gate.

## Goal

Exercise `modashc` against real shell-heavy Unix projects and distro scripts so
the compiler is exposed to source idioms that synthetic tests may not cover.
Findings from this suite should harden the main synthetic regression suite over
time.

The suite should answer three separate questions:

- Does context-mode compilation classify real source graphs without crashing?
- Does executable-mode compilation either succeed or fail with a structured,
  expected unsupported-source diagnostic?
- For explicitly safe wrappers, does executable output match Bash behavior?

## Non-Goals

- Do not make real-world corpus tests part of the default local verification
  command.
- Do not install distro packages as part of the test run.
- Do not require root privileges.
- Do not execute arbitrary real distro scripts by default.
- Do not treat a real-world unsupported diagnostic as a compiler bug unless the
  diagnostic is missing, unstable, misleading, or covers an idiom we decide to
  support.
- Do not commit third-party source trees into this repository.

## Branch And Layout

The initial work should happen on a dedicated branch such as
`internal/realworld-suite`.

Proposed repository layout:

```text
docs/real-world-test-suite.md
test/realworld/manifest.json
test/realworld/fixtures/
test/test_realworld_projects.py
.realworld/
```

`.realworld/` is a local cache and results directory. It should be ignored by
git once the harness exists. The manifest and harness are versioned; downloaded
or extracted corpus files are not.

## Execution Gate

Real-world tests must be opt-in:

```sh
MODASHC_REALWORLD=1 python -m unittest test.test_realworld_projects -v
```

Without `MODASHC_REALWORLD=1`, the real-world test module should skip all
tests quickly and clearly.

Additional environment switches:

```sh
MODASHC_REALWORLD_TIMEOUT=3
MODASHC_REALWORLD_FETCH=1
MODASHC_REALWORLD_RUNTIME=1
MODASHC_REALWORLD_TRACE=1
MODASHC_REALWORLD_REPORT=1
MODASHC_REALWORLD_UPDATE_SNAPSHOTS=1
```

Timeout control, fetching, runtime parity checks, runtime trace smoke probes,
human-readable report output, and snapshot updates are separate operations so a
normal internal corpus run stays deterministic.

## Test Tiers

### Local Installed Smoke Tests

These tests inspect files already present on the host and skip missing files.
They are useful for broad local exposure, but their outputs should not be used
as strict cross-machine snapshots.

Candidate paths on Debian/Ubuntu-like systems:

```text
/etc/profile
/etc/bash.bashrc
/etc/profile.d/*.sh
/usr/share/bash-completion/bash_completion
/etc/X11/xinit/xinitrc
```

Local smoke tests should report normalized outcomes:

- file present or skipped
- context-mode status
- executable-mode status
- parsed source-site count
- resolved source-event count
- disabled source count
- diagnostic code and source fragment for unsupported executable cases

For local smoke fixtures, `success`, `unsupported`, and `timeout` are
reportable outcomes. Only unexpected harness or compiler exceptions fail the
test. Pinned corpus entries should enforce stricter per-entrypoint expectations
once they exist.

### Pinned Corpus Tests

Pinned corpus tests use fixed upstream or distro source artifacts downloaded
into `.realworld/cache`. Each manifest entry should include enough metadata to
reproduce and verify the artifact:

- project name
- upstream or distro source URL
- version or revision
- checksum
- extraction path
- entrypoints
- expected outcome per mode

Pinned artifact downloads happen only when `MODASHC_REALWORLD_FETCH=1` is set.
If the verified artifact is already cached, the suite can extract and run it
without network access.

Good initial candidates:

- `bash-completion`: common Bash completion framework with dynamic completion
  loading.
- `pacman` / `makepkg`: shell-heavy Arch tooling with a large sourced
  `libmakepkg` tree.
- `dracut`: initramfs tooling with many shell modules and distro integration
  scripts.
- `grub2` `util/grub.d`: bootloader script corpus with distro packaging
  history.

The first pinned project is `bash-completion` 2.16.0 from its upstream release
archive. The second is `pacman` 7.1.0 from the upstream Arch Linux release
archive, using makepkg library entrypoints with a real sourced shell library
graph. Supplement-backed `libmakepkg` library entrypoints are pinned as context
and executable success where retained `source_safe` definitions can be lowered
through a reviewed finite supplement fixture. Harness-owned pacman wrapper
fixtures exercise the real helper and focused Bash semantics; those wrappers
are pinned as success in both context and executable modes and have runtime
parity probes.

Pinned manifest entries enforce expected outcomes for both `context` and
`executable` modes. Supported expected statuses are:

- `success`
- `unsupported`
- `timeout`
- `skip`

Unsupported expectations must include a diagnostic `code` and source `fragment`
substring.

Successful pinned runs write retained output artifacts under:

```text
.realworld/outputs/<project-version>/<mode>/<entrypoint>.<mode>.sh
```

These outputs are for manual inspection and remain ignored by git.

Pinned projects may declare small compile-time environment variables. Values may
include `{root}`, which expands to the extracted corpus root. This is intended
for upstream source releases that need a library path to resolve their own
source graph.

Pinned projects may also declare `source_aliases` suffix copies for generated
script names. The initial use is `pacman`, whose release archive stores
makepkg scripts as `.sh.in` templates while their source statements target
generated `.sh` files.

Pinned projects may declare small `fixture_files` copied from
`test/realworld/fixtures/` into the extracted corpus. Fixture paths are
manifest-reviewed and must stay inside the corpus root. The initial pacman
fixture proves exact `source_safe` helper lowering against the real
`libmakepkg/util/util.sh` implementation without executing arbitrary upstream
entrypoints.

Pinned mode expectations may declare a `source_supplement` fixture. The harness
loads the fixture from `test/realworld/fixtures/`, expands `{root}` to the
extracted corpus root, writes a generated supplement inside the ignored corpus
cache, and passes it to `modashc` for that mode only.

### Runtime Trace Smoke Probes

Runtime trace smoke probes are separately opt-in with
`MODASHC_REALWORLD_TRACE=1`. They execute a small pinned pacman/makepkg-style
fixture through the explicit `modashc trace` workflow, validate that source
events were observed, and write observation artifacts under:

```text
.realworld/outputs/<project-version>/trace/<entrypoint>.trace.json
```

These probes are not supplement generation and do not prove all branch paths.
They exist to spot-check that the runtime observation workflow produces
reviewable data against a real helper implementation.

### Manual Artifact Review

The initial retained `bash-completion` artifacts were manually reviewed after
generation:

- context headers and separators are readable
- context output preserves the inspected source body
- executable output has the expected shebang, separator, and comment-stripped
  shell shape
- no live source statement or unresolved-source marker remains in successful
  executable output

Promoted findings so far:

- `completions/cd` executable mode now succeeds. Its
  `if shopt -q cdable_vars; then` predicate is source-free from modashc's
  dependency perspective, so it is preserved as ordinary runtime Bash instead
  of blocking dependency merging.
- pacman/makepkg artifacts exposed that `if ! source "$@"; then` must be
  handled through exact helper argument binding and retained helper dispatch.
  The fixture-backed `source_safe` wrapper succeeds, and supplement-backed raw
  makepkg library entrypoints now compile successfully in executable mode.
- makepkg include guards exposed top-level source `return` requirements. The
  compiler now lowers sourced files containing top-level `return` through
  generated same-shell helper functions that are cleaned up after the source
  site runs.

### Runtime Parity Probes

Runtime parity is implemented only for manifest entries marked safe and only
when `MODASHC_REALWORLD_RUNTIME=1` is set. These tests run controlled wrapper
scripts in temporary directories, with explicit environment variables, no root
privileges, and a timeout.

Runtime parity compares:

- Bash execution of the original wrapper
- Bash execution of `modashc` executable output
- exit status
- stdout and stderr, normalized only where the manifest declares an accepted
  normalization

The runtime probe set starts with controlled pacman fixtures:

- real `source_safe` helper dispatch
- real `source_safe` helper dispatch with exact source arguments
- direct multi-match source glob arguments
- sourced variable, export, and function availability
- cwd-sensitive nested source behavior
- top-level sourced-file return status and state
- wrapped sourced-file positional mutation
- explicit source-argument frame restoration around later nested source sites
- skipped dynamic source payloads behind known `&&` / `||` status

Most real distro scripts should remain compile/classification fixtures, not
runtime fixtures.

### Supplement Fixtures

Real-world tests may include source supplements, but only as pinned compiler
inputs with the same validation rules as user-provided supplements. The harness
supports the second pass of the two-pass workflow:

- first run fails closed and reports the unresolved source-relevant values or
  call signatures
- the manifest points at a reviewed supplement fixture
- the run ingests that supplement and expects executable-mode success

Supplements are appropriate for true runtime inputs. They should not be used to
paper over parser bugs, missing static resolution for exact shell text, or
unsafe executable output.

## Expected Outcomes

Each pinned entrypoint must declare expected outcomes for both output modes:

- `success`: the mode must compile successfully.
- `unsupported`: the mode must fail with a diagnostic whose
  code and fragment match the manifest.
- `skip`: entrypoint is documented but not currently run.
- `timeout`: entrypoint exceeds the configured exploratory budget.

Context mode should normally be expected to succeed. If context mode cannot
classify a file, that should be treated as a harness or compiler issue unless a
specific exception is documented.

Unexpected Python exceptions are always failures.

## Result Format

The harness should emit normalized JSON under `.realworld/results/` so runs can
be compared without reading test logs.

Each suite result includes a `summary` object with record counts, status counts,
total measured record duration, and pinned expectation counts when applicable.
Each mode record includes `duration_seconds`.
When `MODASHC_REALWORLD_REPORT=1` is set, the harness also prints concise
summary lines and unmatched expectations to stderr while still writing the JSON
result files.

Example:

```json
{
  "project": "bash-completion",
  "entrypoint": "bash_completion",
  "mode": "executable",
  "status": "unsupported",
  "expected_status": "unsupported",
  "matched_expectation": true,
  "duration_seconds": 0.032342,
  "source_sites": 12,
  "resolved_events": 8,
  "disabled_sources": 0,
  "diagnostics": [
    {
      "code": "unsupported.source.loop-word-list",
      "line": 2273,
      "fragment": ". \"$i\""
    }
  ]
}
```

Paths in result files should be normalized relative to the corpus root when
possible. Absolute host paths should be avoided unless they identify a local
installed smoke fixture.

## Manifest Sketch

The exact schema can evolve with the harness, but entries should remain small
and reviewable.

```json
{
  "projects": [
    {
      "name": "pacman",
      "kind": "pinned",
      "version": "7.1.0",
      "source": {
        "url": "https://gitlab.archlinux.org/pacman/pacman/-/releases/v7.1.0/downloads/pacman-7.1.0.tar.xz",
        "archive": "pacman-7.1.0.tar.xz",
        "sha256": "530e50d7edbb2a22581c6d6707d2113240276c1bec4ee39a99488e1243c32171",
        "strip_components": 1
      },
      "environment": {
        "MAKEPKG_LIBRARY": "{root}/scripts/libmakepkg"
      },
      "source_aliases": [
        {
          "from_suffix": ".sh.in",
          "to_suffix": ".sh"
        }
      ],
      "fixture_files": [
        {
          "source": "pacman/source-safe-wrapper.sh",
          "path": ".modashc-fixtures/source-safe-wrapper.sh"
        },
        {
          "source": "pacman/source-safe-target.sh",
          "path": ".modashc-fixtures/source-safe-target.sh"
        },
        {
          "source": "pacman/PKGBUILD",
          "path": ".modashc-fixtures/PKGBUILD"
        }
      ],
      "entrypoints": [
        {
          "path": "scripts/libmakepkg/buildenv.sh",
          "modes": {
            "context": {
              "expected": "success"
            },
            "executable": {
              "expected": "success",
              "source_supplement": "pacman/source-safe-supplement.json"
            }
          }
        },
        {
          "path": ".modashc-fixtures/source-safe-wrapper.sh",
          "modes": {
            "context": {
              "expected": "success"
            },
            "executable": {
              "expected": "success"
            }
          },
          "runtime": {
            "expected": "match",
            "cwd": ".modashc-fixtures"
          }
        }
      ]
    }
  ]
}
```

The manifest also includes smaller `bash-completion` entrypoints that exercise
`success` and `unsupported` expectations, plus `pacman` makepkg library and
runtime fixture entrypoints that prove the harness is not overfit to one
upstream project.

## Safety Rules

- The harness may read corpus files and local installed files.
- The harness may fetch pinned artifacts only when explicitly enabled.
- Fetches must verify checksums before extraction.
- Runtime tests must run in temporary directories.
- Runtime tests must have explicit timeouts.
- Runtime tests must not run with elevated privileges.
- Runtime tests must not write outside their temporary directories except for
  harness-owned `.realworld/results/` output.
- The suite should avoid network access during normal test execution.

## Promotion Workflow

Real-world findings should not remain only in corpus snapshots. When the suite
surfaces a meaningful behavior gap:

1. Minimize the shell pattern into a synthetic fixture.
2. Add or update a focused unit/regression test in the normal `test/` suite.
3. Fix the compiler or document the unsupported behavior.
4. Keep the real-world manifest expectation aligned with the chosen contract.

The corpus suite is a discovery tool. The synthetic suite remains the release
gate for specific behavior.

Runtime source discovery and observed supplement generation should follow
[Runtime Source Discovery North Star](runtime-source-discovery.md). Real-world
tracing belongs behind explicit opt-in workflows and reviewed observation
artifacts, not inside normal corpus compilation.

## Initial Milestones

1. Add the opt-in unittest harness and skip gate.
2. Add local installed smoke fixtures for common Debian/Ubuntu paths.
3. Add `.realworld/` to `.gitignore`.
4. Add one pinned source corpus with checksum verification.
5. Emit normalized JSON results for every entrypoint.
6. Promote the first useful real-world finding into a synthetic regression.

Items 1 through 6 are implemented for the initial local smoke,
`bash-completion`, and `pacman` corpus paths. Pinned expected-outcome
enforcement, retained artifacts for successful pinned runs, supplement-backed
pacman success expectations, direct glob source-argument fixtures,
wrapped-source positional mutation fixtures, multiple safe runtime parity
probes, explicit source-argument frame restoration fixtures, opt-in
human-readable reports, and source-relevant control-flow boundary promotion are
also implemented. Runtime-guarded static source fixtures are implemented for
guarded `if` and `case` source lowering; broader runtime discovery and dynamic
tracing remain deferred to the north-star workflow.
