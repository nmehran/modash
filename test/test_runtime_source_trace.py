import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_trace import (  # noqa: E402
    RuntimeSourceTraceError,
    default_observation_path,
    trace_sources,
)
from test.support import ScriptProject  # noqa: E402


class RuntimeSourceTraceTestCase(unittest.TestCase):
    def test_default_observation_path_uses_timestamp_when_run_id_omitted(self):
        path = default_observation_path("scripts/main.sh", output_dir="observations")

        self.assertEqual(path.parent, Path("observations"))
        self.assertRegex(path.name, r"^main\.sh-\d{8}T\d{12}Z\.json$")

    def test_trace_records_direct_source(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./dep.sh\nprintf "main\\n"\n')
            dependency = project.write("dep.sh", 'printf "dep\\n"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\nmain\n")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.call_site.file, str(entrypoint.resolve(strict=False)))
        self.assertEqual(event.call_site.line, 1)
        self.assertEqual(event.call_site.command, "source ./dep.sh")
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.arguments, ())
        self.assertEqual(event.status, 0)
        fingerprints = {Path(file.path).name: file for file in result.observation.files}
        self.assertEqual(result.observation.version, 7)
        self.assertEqual(result.observation.environment.policy, "inherit")
        self.assertEqual(result.observation.run.timeout_seconds, 30.0)
        self.assertTrue(result.observation.run.shell)
        self.assertEqual(len(result.observation.xtrace), 1)
        self.assertEqual(event.xtrace_index, 0)
        self.assertTrue(event.source_identity.startswith("src:"))
        xtrace = result.observation.xtrace[0]
        self.assertEqual(xtrace.source_identity, event.source_identity)
        self.assertEqual(xtrace.process_index, event.process_index)
        self.assertEqual(xtrace.file, str(entrypoint.resolve(strict=False)))
        self.assertEqual(xtrace.line, 1)
        self.assertEqual(xtrace.command, "source ./dep.sh")
        self.assertEqual(fingerprints["main.sh"].roles, ("entrypoint", "call-site"))
        self.assertEqual(fingerprints["dep.sh"].roles, ("source",))
        self.assertTrue(all(len(file.sha256) == 64 for file in result.observation.files))

    def test_trace_preserves_inherited_source_positionals(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source "$DEP"\nprintf "main:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            dependency = project.write(
                "dep.sh",
                'printf "dep:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            env = {**os.environ, "DEP": str(dependency)}
            expected = subprocess.run(
                ["bash", str(entrypoint), "A", "B"],
                cwd=str(project.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            result = project.trace("main.sh", argv=("A", "B"), env={"DEP": str(dependency)})

        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(result.returncode, expected.returncode, result.stderr)
        self.assertEqual(result.stdout, expected.stdout)
        self.assertEqual(result.observation.sources[0].arguments, ())

    def test_trace_fails_closed_when_source_alias_removed_before_inherited_positionals(self):
        with ScriptProject() as project:
            sentinel = project.path("executed")
            project.write(
                "main.sh",
                'unalias source\nsource "$DEP"\nprintf "main\\n"\n',
            )
            dependency = project.write(
                "dep.sh",
                f'touch {str(sentinel)!r}\nprintf "dep:%s\\n" "${{1-unset}}"\n',
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", argv=("A",), env={"DEP": str(dependency)})

        self.assertEqual(context.exception.code, "runtime.trace.nontransparent-source")
        self.assertIn("alias was removed", str(context.exception))
        self.assertFalse(sentinel.exists())

    def test_trace_fails_closed_for_inherited_source_shift(self):
        with ScriptProject() as project:
            sentinel = project.path("executed")
            project.write(
                "main.sh",
                'source "$DEP"\nprintf "main:%s\\n" "${1-unset}"\n',
            )
            dependency = project.write(
                "dep.sh",
                f'touch {str(sentinel)!r}\nshift\nprintf "dep:%s\\n" "${{1-unset}}"\n',
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", argv=("A", "B"), env={"DEP": str(dependency)})

        self.assertEqual(context.exception.code, "runtime.trace.nontransparent-source-positionals")
        self.assertIn("may mutate caller positionals", str(context.exception))
        self.assertFalse(sentinel.exists())

    def test_trace_fails_closed_for_inherited_source_set_positionals(self):
        with ScriptProject() as project:
            sentinel = project.path("executed")
            project.write("main.sh", 'source "$DEP"\nprintf "main:%s\\n" "${1-unset}"\n')
            dependency = project.write(
                "dep.sh",
                f'touch {str(sentinel)!r}\nset -- changed\n',
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", argv=("A",), env={"DEP": str(dependency)})

        self.assertEqual(context.exception.code, "runtime.trace.nontransparent-source-positionals")
        self.assertFalse(sentinel.exists())

    def test_trace_allows_no_arg_source_of_helper_file_with_function_shift(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_alias "$DEP" "arg one" arg-two\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_alias() {",
                    "  local path=$1",
                    "  shift",
                    '  source "$path" "$@"',
                    "}",
                    "",
                ]),
            )
            dependency = project.write("dep.sh", 'printf "dep:%s:%s\\n" "$1" "$2"\n')
            env = {**os.environ, "DEP": str(dependency)}
            expected = subprocess.run(
                ["bash", str(entrypoint)],
                cwd=str(project.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            result = project.trace("main.sh", env={"DEP": str(dependency)})

        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(result.returncode, expected.returncode, result.stderr)
        self.assertEqual(result.stdout, expected.stdout)
        self.assertEqual([event.arguments for event in result.observation.sources], [(), ("arg one", "arg-two")])

    def test_trace_ignores_positional_mutation_words_in_quotes_comments_and_heredocs(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source "$DEP"\nprintf "main:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            dependency = project.write(
                "dep.sh",
                "\n".join([
                    'printf "literal:%s\\n" "shift"',
                    "cat >/dev/null <<'EOF'",
                    "shift",
                    "set -- changed",
                    "EOF",
                    "# shift",
                    ': "set -- changed"',
                    "",
                ]),
            )
            env = {**os.environ, "DEP": str(dependency)}
            expected = subprocess.run(
                ["bash", str(entrypoint), "A", "B"],
                cwd=str(project.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            result = project.trace("main.sh", argv=("A", "B"), env={"DEP": str(dependency)})

        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(result.returncode, expected.returncode, result.stderr)
        self.assertEqual(result.stdout, expected.stdout)

    def test_trace_fails_closed_for_control_flow_top_level_positional_mutation(self):
        with ScriptProject() as project:
            sentinel = project.path("executed")
            project.write("main.sh", 'source "$DEP"\nprintf "main:%s\\n" "${1-unset}"\n')
            dependency = project.write(
                "dep.sh",
                "\n".join([
                    f"touch {str(sentinel)!r}",
                    "if true; then",
                    "  shift",
                    "fi",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", argv=("A", "B"), env={"DEP": str(dependency)})

        self.assertEqual(context.exception.code, "runtime.trace.nontransparent-source-positionals")
        self.assertFalse(sentinel.exists())

    def test_trace_allows_explicit_source_args_to_shift_positionals(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source "$DEP" X Y\nprintf "main:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            dependency = project.write(
                "dep.sh",
                'printf "before:%s:%s\\n" "${1-unset}" "${2-unset}"\n'
                'shift\n'
                'printf "after:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            env = {**os.environ, "DEP": str(dependency)}
            expected = subprocess.run(
                ["bash", str(entrypoint), "A", "B"],
                cwd=str(project.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            result = project.trace("main.sh", argv=("A", "B"), env={"DEP": str(dependency)})

        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(result.returncode, expected.returncode, result.stderr)
        self.assertEqual(result.stdout, expected.stdout)
        self.assertEqual(result.observation.sources[0].arguments, ("X", "Y"))

    def test_trace_fails_closed_for_child_bash_c_inherited_source_shift(self):
        with ScriptProject() as project:
            sentinel = project.path("executed")
            project.write(
                "main.sh",
                'bash -c \'. "$1"; printf "child:%s:%s\\n" "${1-unset}" "${2-unset}"\' child "$DEP" extra\n',
            )
            dependency = project.write(
                "dep.sh",
                f'touch {str(sentinel)!r}\nshift\n',
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", env={"DEP": str(dependency)})

        self.assertEqual(context.exception.code, "runtime.trace.nontransparent-source-positionals")
        self.assertFalse(sentinel.exists())

    def test_trace_resolves_relative_entrypoint_from_process_cwd(self):
        original_cwd = os.getcwd()
        try:
            with ScriptProject() as project:
                entrypoint = project.write("scripts/main.sh", 'source ./dep.sh\n')
                dependency = project.write("scripts/dep.sh", "echo dep\n")
                os.chdir(project.root)

                result = trace_sources("scripts/main.sh")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.observation.entrypoint, str(entrypoint.resolve(strict=False)))
            self.assertEqual(result.observation.cwd, str(entrypoint.parent.resolve(strict=False)))
            self.assertEqual(result.observation.sources[0].resolved_path, str(dependency.resolve(strict=False)))
        finally:
            os.chdir(original_cwd)

    def test_trace_resolves_relative_entrypoint_from_explicit_cwd(self):
        with ScriptProject() as project:
            workdir = project.mkdir("work")
            entrypoint = project.write("work/main.sh", 'source ./dep.sh\n')
            dependency = project.write("work/dep.sh", "echo dep\n")

            result = trace_sources("main.sh", cwd=workdir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.observation.entrypoint, str(entrypoint.resolve(strict=False)))
        self.assertEqual(result.observation.cwd, str(workdir.resolve(strict=False)))
        self.assertEqual(result.observation.sources[0].resolved_path, str(dependency.resolve(strict=False)))

    def test_trace_records_dot_source_arguments(self):
        with ScriptProject() as project:
            project.write("main.sh", '. ./dep.sh "one arg" two\n')
            dependency = project.write("dep.sh", 'printf "dep:%s:%s\\n" "$1" "$2"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep:one arg:two\n")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.call_site.command, '. ./dep.sh "one arg" two')
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.arguments, ("one arg", "two"))
        self.assertEqual(event.status, 0)
        self.assertEqual(
            result.observation.xtrace[event.xtrace_index].source_identity,
            event.source_identity,
        )

    def test_trace_records_builtin_and_command_source_invocations(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "builtin source ./dep.sh builtin-source",
                    "builtin . ./dep.sh builtin-dot",
                    "command source ./dep.sh command-source",
                    "command . ./dep.sh command-dot",
                    "",
                ]),
            )
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "dep:builtin-source\n"
            "dep:builtin-dot\n"
            "dep:command-source\n"
            "dep:command-dot\n",
        )
        self.assertEqual(len(result.observation.sources), 4)
        self.assertEqual(
            [event.call_site.command for event in result.observation.sources],
            [
                "builtin source ./dep.sh builtin-source",
                "builtin . ./dep.sh builtin-dot",
                "command source ./dep.sh command-source",
                "command . ./dep.sh command-dot",
            ],
        )
        self.assertEqual(
            [event.call_site.line for event in result.observation.sources],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [event.resolved_path for event in result.observation.sources],
            [str(dependency.resolve(strict=False))] * 4,
        )
        self.assertEqual(
            [event.arguments for event in result.observation.sources],
            [
                ("builtin-source",),
                ("builtin-dot",),
                ("command-source",),
                ("command-dot",),
            ],
        )
        self.assertTrue(all(event.status == 0 for event in result.observation.sources))
        entrypoint_path = str(entrypoint.resolve(strict=False))
        self.assertTrue(
            all(event.call_site.file == entrypoint_path for event in result.observation.sources)
        )
        self.assertEqual(
            [event.source_identity for event in result.observation.sources],
            [command.source_identity for command in result.observation.xtrace],
        )

    def test_trace_links_same_line_source_events_by_identity(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./a.sh; source ./b.sh\n")
            first = project.write("a.sh", "echo a\n")
            second = project.write("b.sh", "echo b\n")

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [event.resolved_path for event in result.observation.sources],
            [
                str(first.resolve(strict=False)),
                str(second.resolve(strict=False)),
            ],
        )
        self.assertEqual(
            [event.source_identity for event in result.observation.sources],
            [command.source_identity for command in result.observation.xtrace],
        )
        self.assertNotEqual(
            result.observation.sources[0].source_identity,
            result.observation.sources[1].source_identity,
        )

    def test_trace_preserves_non_source_builtin_and_command_invocations(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "\n".join([
                    "builtin echo builtin-ok",
                    "command printf 'command:%s\\n' ok",
                    "command -v source >/dev/null",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "builtin-ok\ncommand:ok\n")
        self.assertEqual(result.observation.sources, ())

    def test_trace_records_failed_source_status_without_mixing_trace_output(self):
        with ScriptProject() as project:
            missing = project.path("missing.sh")
            project.write("main.sh", 'source ./missing.sh\nprintf "after\\n"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "after\n")
        self.assertIn("missing.sh", result.stderr)
        self.assertNotIn("MODASH_SOURCE_EVENT", result.stderr)
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.resolved_path, str(missing.resolve(strict=False)))
        self.assertGreater(event.status, 0)

    def test_trace_fingerprints_sourced_file_that_returns_nonzero(self):
        with ScriptProject() as project:
            dependency = project.write("dep.sh", "return 7\n")
            project.write("main.sh", 'source ./dep.sh\nprintf "after:%s\\n" "$?"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "after:7\n")
        event = result.observation.sources[0]
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.status, 7)
        fingerprints = {fingerprint.path: fingerprint for fingerprint in result.observation.files}
        self.assertIn(str(dependency.resolve(strict=False)), fingerprints)
        self.assertIn("source", fingerprints[str(dependency.resolve(strict=False))].roles)

    def test_trace_rejects_missed_source_after_wrapper_removal(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "\n".join([
                    "unalias source",
                    "unset -f source",
                    "source ./dep.sh",
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.incomplete")
        self.assertIn("missed source-like command", str(context.exception))
        self.assertIn("source ./dep.sh", str(context.exception))

    def test_trace_rejects_missed_builtin_source_after_alias_removal(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "\n".join([
                    "unalias builtin",
                    "builtin source ./dep.sh",
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.incomplete")
        self.assertIn("builtin source ./dep.sh", str(context.exception))

    def test_trace_resolves_source_from_runtime_cwd(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'cd subdir\nsource ./dep.sh\n')
            dependency = project.write("subdir/dep.sh", 'printf "subdep\\n"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        event = result.observation.sources[0]
        self.assertEqual(event.call_site.file, str(entrypoint.resolve(strict=False)))
        self.assertEqual(event.call_site.line, 2)
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))

    def test_trace_records_simple_helper_source_call(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "source_safe() {",
                    '  if ! source "$@"; then',
                    "    return 1",
                    "  fi",
                    "}",
                    "source_safe ./dep.sh helper",
                    "",
                ]),
            )
            dependency = project.write("dep.sh", 'printf "helper:%s\\n" "$1"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "helper:helper\n")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.call_site.file, str(entrypoint.resolve(strict=False)))
        self.assertEqual(event.call_site.line, 2)
        self.assertEqual(event.call_site.command, 'if ! source "$@"; then')
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.arguments, ("helper",))
        self.assertEqual(event.status, 0)

    def test_trace_records_dynamic_parent_helper_call_for_nested_source(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'inner() { source "$2" "$1"; }',
                    'wrap() { inner "$2" "$1"; }',
                    'main() { "$MODASH_TEST_HELPER" ./dep.sh mode; }',
                    "main",
                    "",
                ]),
            )
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')

            result = project.trace("main.sh", env={"MODASH_TEST_HELPER": "wrap"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep:mode\n")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.function_stack[:2], ("inner", "wrap"))
        self.assertIsNotNone(event.function_call)
        self.assertEqual(event.function_call.function, "wrap")
        self.assertEqual(event.function_call.file, str(entrypoint.resolve(strict=False)))
        self.assertEqual(event.function_call.line, 3)
        self.assertEqual(event.function_call.command, "wrap ./dep.sh mode")
        self.assertEqual(event.function_call.arguments, ("./dep.sh", "mode"))

    def test_trace_preserves_nested_source_execution_order(self):
        with ScriptProject() as project:
            first = project.write("first.sh", 'source ./second.sh\nprintf "first\\n"\n')
            second = project.write("second.sh", 'printf "second\\n"\n')
            project.write("main.sh", 'source ./first.sh\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "second\nfirst\n")
        self.assertEqual(
            [event.resolved_path for event in result.observation.sources],
            [str(first.resolve(strict=False)), str(second.resolve(strict=False))],
        )

    def test_trace_uses_sourced_function_definition_file_after_source_returns(self):
        with ScriptProject() as project:
            library = project.write(
                "lib.sh",
                "\n".join([
                    "source_safe() {",
                    '  source "$@"',
                    "}",
                    "",
                ]),
            )
            dependency = project.write("dep.sh", 'printf "dep\\n"\n')
            project.write("main.sh", 'source ./lib.sh\nsource_safe ./dep.sh\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [event.resolved_path for event in result.observation.sources],
            [str(library.resolve(strict=False)), str(dependency.resolve(strict=False))],
        )
        helper_event = result.observation.sources[1]
        self.assertEqual(helper_event.call_site.file, str(library.resolve(strict=False)))
        self.assertEqual(helper_event.call_site.line, 2)
        self.assertEqual(helper_event.call_site.command, 'source "$@"')

    def test_trace_uses_sourced_function_definition_file_during_parent_source(self):
        with ScriptProject() as project:
            library = project.write("lib.sh", 'load() { source "$1"; }\n')
            runner = project.write(
                "runner.sh",
                'source ./lib.sh\n"$MODASH_TEST_HELPER" ./dep.sh\n',
            )
            dependency = project.write("dep.sh", 'printf "dep\\n"\n')
            project.write("main.sh", "source ./runner.sh\n")

            result = project.trace("main.sh", env={"MODASH_TEST_HELPER": "load"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(
            [Path(event.call_site.file).name for event in result.observation.sources],
            ["main.sh", "runner.sh", "lib.sh"],
        )
        helper_event = result.observation.sources[2]
        self.assertEqual(helper_event.call_site.file, str(library.resolve(strict=False)))
        self.assertEqual(helper_event.call_site.line, 1)
        self.assertEqual(helper_event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(helper_event.function_call.file, str(runner.resolve(strict=False)))
        self.assertEqual(helper_event.function_call.line, 2)
        self.assertEqual(helper_event.function_call.function, "load")

    def test_trace_distinguishes_same_relative_sourced_function_files(self):
        with ScriptProject() as project:
            first_library = project.write("dir1/lib.sh", 'load_one() { source "$1"; }\n')
            second_library = project.write("dir2/lib.sh", 'load_two() { source "$1"; }\n')
            project.write("one.sh", 'printf "one\\n"\n')
            project.write("two.sh", 'printf "two\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load_one ./one.sh",
                    "load_two ./two.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "one\ntwo\n")
        self.assertEqual(
            [Path(event.call_site.file).name for event in result.observation.sources],
            ["main.sh", "main.sh", "lib.sh", "lib.sh"],
        )
        self.assertEqual(result.observation.sources[2].call_site.file, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[3].call_site.file, str(second_library.resolve(strict=False)))

    def test_trace_updates_identical_redefined_function_provenance(self):
        with ScriptProject() as project:
            first_library = project.write("dir1/lib.sh", 'load() { source "$1"; }\n')
            second_library = project.write("dir2/lib.sh", 'load() { source "$1"; }\n')
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(second_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[2].call_site.file, str(second_library.resolve(strict=False)))

    def test_trace_ignores_heredoc_function_text_for_provenance(self):
        with ScriptProject() as project:
            first_library = project.write("dir1/lib.sh", 'load() { source "$1"; }\n')
            second_library = project.write(
                "dir2/lib.sh",
                "\n".join([
                    "cat >/dev/null <<'EOF'",
                    'load() { source "$1"; }',
                    "EOF",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(second_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[2].call_site.file, str(first_library.resolve(strict=False)))

    def test_trace_ignores_unreached_branch_function_text_for_provenance(self):
        with ScriptProject() as project:
            first_library = project.write("dir1/lib.sh", 'load() { source "$1"; }\n')
            second_library = project.write(
                "dir2/lib.sh",
                "\n".join([
                    "if false; then",
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(second_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[2].call_site.file, str(first_library.resolve(strict=False)))

    def test_trace_updates_reached_branch_function_provenance(self):
        with ScriptProject() as project:
            first_library = project.write("dir1/lib.sh", 'load() { source "$1"; }\n')
            second_library = project.write(
                "dir2/lib.sh",
                "\n".join([
                    "if true; then",
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(second_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[2].call_site.file, str(second_library.resolve(strict=False)))

    def test_trace_updates_same_line_branch_function_provenance(self):
        with ScriptProject() as project:
            first_library = project.write(
                "dir1/lib.sh",
                "\n".join([
                    "if true; then",
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            second_library = project.write(
                "dir2/lib.sh",
                "\n".join([
                    "if true; then",
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(first_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(second_library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[2].call_site.file, str(second_library.resolve(strict=False)))

    def test_trace_fails_closed_on_ambiguous_branch_function_provenance(self):
        with ScriptProject() as project:
            project.write(
                "dir1/lib.sh",
                "\n".join([
                    "if true; then",
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            project.write(
                "dir2/lib.sh",
                "\n".join([
                    'if [[ ${MODASH_TEST_REDEFINE:-} == yes ]]; then',
                    '  load() { source "$1"; }',
                    "fi",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", env={"MODASH_TEST_REDEFINE": "yes"})

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_loop_function_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    "\n".join([
                        "for value in one; do",
                        '  load() { source "$1"; }',
                        "done",
                        "",
                    ]),
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_case_arm_function_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    "\n".join([
                        "case yes in",
                        '  yes) load() { source "$1"; } ;;',
                        "esac",
                        "",
                    ]),
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_nested_function_body_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    "\n".join([
                        'wrap() { load() { source "$1"; }; }',
                        "wrap",
                        "",
                    ]),
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_eval_function_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    'eval \'load() { source "$1"; }\'\n',
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_dynamic_eval_function_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    'eval "$MODASH_TEST_FUNCTION_DEF"\n',
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace(
                    "main.sh",
                    env={"MODASH_TEST_FUNCTION_DEF": 'load() { source "$1"; }'},
                )

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_fails_closed_on_trap_function_provenance(self):
        with ScriptProject() as project:
            for directory in ("dir1", "dir2"):
                project.write(
                    f"{directory}/lib.sh",
                    "\n".join([
                        'trap \'load() { source "$1"; }\' DEBUG',
                        ":",
                        "trap - DEBUG",
                        "",
                    ]),
                )
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "cd dir1",
                    "source ./lib.sh",
                    "cd ../dir2",
                    "source ./lib.sh",
                    "cd ..",
                    "load ./dep.sh",
                    "",
                ]),
            )

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.ambiguous-function-provenance")
        self.assertIn("load", str(context.exception))

    def test_trace_tracks_function_provenance_when_extdebug_is_enabled(self):
        with ScriptProject() as project:
            library = project.write("lib.sh", 'load() { source "$1"; }\n')
            dependency = project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    "shopt -s extdebug",
                    "source ./lib.sh",
                    "load ./dep.sh",
                    "",
                ]),
            )

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(result.observation.sources[0].resolved_path, str(library.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(result.observation.sources[1].call_site.file, str(library.resolve(strict=False)))

    def test_trace_records_child_bash_script_sources(self):
        with ScriptProject() as project:
            parent = project.write("main.sh", 'bash ./child.sh\n')
            child = project.write("child.sh", 'source ./dep.sh child-arg\n')
            dependency = project.write("dep.sh", 'printf "child:%s\\n" "$1"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "child:child-arg\n")
        self.assertEqual(len(result.observation.processes), 2)
        parent_process, child_process = result.observation.processes
        self.assertEqual(parent_process.entrypoint, str(parent.resolve(strict=False)))
        self.assertEqual(child_process.entrypoint, str(child.resolve(strict=False)))
        self.assertEqual(child_process.parent_index, parent_process.index)
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.process_index, child_process.index)
        self.assertEqual(event.call_site.file, str(child.resolve(strict=False)))
        self.assertEqual(event.call_site.line, 1)
        self.assertEqual(event.call_site.command, "source ./dep.sh child-arg")
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.arguments, ("child-arg",))
        self.assertEqual(event.status, 0)

    def test_trace_records_child_bash_c_sources(self):
        with ScriptProject() as project:
            dependency = project.write("dep.sh", 'printf "child-c\\n"\n')
            project.write("main.sh", "bash -c 'source ./dep.sh'\n")

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "child-c\n")
        self.assertEqual(len(result.observation.processes), 2)
        self.assertEqual(result.observation.processes[1].parent_index, 0)
        self.assertEqual(result.observation.processes[1].command, "source ./dep.sh")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.process_index, 1)
        self.assertEqual(event.call_site.command, "source ./dep.sh")
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.status, 0)
        call_site_files = [
            Path(file.path).name
            for file in result.observation.files
            if "call-site" in file.roles
        ]
        self.assertNotIn("bash", call_site_files)
        self.assertEqual(
            {Path(file.path).name for file in result.observation.files},
            {"main.sh", "dep.sh"},
        )

    def test_trace_records_child_bash_c_positional_source(self):
        with ScriptProject() as project:
            dependency = project.write("dep.sh", 'printf "child-positional\\n"\n')
            project.write("main.sh", 'bash -c \'. "$1"\' bash ./dep.sh\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "child-positional\n")
        self.assertEqual(len(result.observation.processes), 2)
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.process_index, 1)
        self.assertEqual(event.call_site.command, '. "$1"')
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))
        self.assertEqual(event.status, 0)
        xtrace = result.observation.xtrace[event.xtrace_index]
        self.assertEqual(xtrace.source_identity, event.source_identity)
        self.assertEqual(xtrace.command, ". ./dep.sh")

    def test_trace_records_command_bash_child_sources(self):
        with ScriptProject() as project:
            child = project.write("child.sh", 'source ./dep.sh\n')
            dependency = project.write("dep.sh", 'printf "command-child\\n"\n')
            project.write("main.sh", 'command bash ./child.sh\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "command-child\n")
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.call_site.file, str(child.resolve(strict=False)))
        self.assertEqual(event.call_site.command, "source ./dep.sh")
        self.assertEqual(event.resolved_path, str(dependency.resolve(strict=False)))

    def test_trace_rejects_missing_entrypoint(self):
        with ScriptProject() as project:
            with self.assertRaises(RuntimeSourceTraceError) as context:
                trace_sources(project.path("missing.sh"))

        self.assertEqual(context.exception.code, "runtime.trace.entrypoint_missing")

    def test_trace_rejects_missing_cwd_with_stable_code(self):
        with ScriptProject() as project:
            project.write("main.sh", "echo main\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                trace_sources("main.sh", cwd=project.path("missing-cwd"))

        self.assertEqual(context.exception.code, "runtime.trace.cwd_missing")

    def test_trace_rejects_unavailable_bash_with_stable_code(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                trace_sources(entrypoint, bash=project.path("missing-bash"))

        self.assertEqual(context.exception.code, "runtime.trace.bash_unavailable")

    def test_trace_timeout_fails_with_stable_code(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "while :; do :; done\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                trace_sources(entrypoint, timeout=0.05)

        self.assertEqual(context.exception.code, "runtime.trace.timeout")

    def test_trace_rejects_invalid_timeout_with_stable_code(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")

            for timeout in (0, True, float("nan")):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(RuntimeSourceTraceError) as context:
                        trace_sources(entrypoint, timeout=timeout)
                    self.assertEqual(context.exception.code, "runtime.trace.invalid_timeout")


if __name__ == "__main__":
    unittest.main()
