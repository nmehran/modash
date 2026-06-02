import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_trace import RuntimeSourceTraceError, trace_sources  # noqa: E402
from test.support import ScriptProject  # noqa: E402


class RuntimeSourceTraceTestCase(unittest.TestCase):
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

    def test_trace_records_failed_source_status_without_mixing_trace_output(self):
        with ScriptProject() as project:
            missing = project.path("missing.sh")
            project.write("main.sh", 'source ./missing.sh\nprintf "after\\n"\n')

            result = project.trace("main.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "after\n")
        self.assertIn("missing.sh", result.stderr)
        self.assertNotIn("MODASHC_SOURCE_EVENT", result.stderr)
        self.assertEqual(len(result.observation.sources), 1)
        event = result.observation.sources[0]
        self.assertEqual(event.resolved_path, str(missing.resolve(strict=False)))
        self.assertGreater(event.status, 0)

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
