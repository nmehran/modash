import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test.support import ScriptProject  # noqa: E402
from modash import cli_main  # noqa: E402


class RuntimeSourceTraceCliTestCase(unittest.TestCase):
    def test_cli_main_honors_explicit_compile_argv(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            output = project.path("merged.sh")
            original_argv = sys.argv
            sys.argv = ["modash", "--not-the-explicit-argv"]
            try:
                result = cli_main([str(entrypoint), str(output)])
            finally:
                sys.argv = original_argv
            content = output.read_text()

        self.assertEqual(result, 0)
        self.assertIn("echo main", content)

    def test_top_level_help_lists_runtime_commands(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "modash.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runtime commands:", result.stdout)
        self.assertIn("trace", result.stdout)
        self.assertIn("graph", result.stdout)
        self.assertIn("supplement", result.stdout)
        self.assertIn("compile-observed", result.stdout)
        self.assertIn("observe-compile", result.stdout)

    def test_trace_cli_writes_explicit_observation_file_and_forwards_output(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./dep.sh "$1"\nprintf "main:%s:%s\\n" "$1" "$TRACE_VALUE"\n',
            )
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')
            output = project.observation_path("observations/trace.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(output),
                    "--env",
                    "TRACE_VALUE=ok",
                    "--",
                    "arg1",
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output_exists = output.is_file()
            data = json.loads(output.read_text())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep:arg1\nmain:arg1:ok\n")
        self.assertIn("modash: trace observation:", result.stderr)
        self.assertTrue(output_exists)
        self.assertEqual(data["entrypoint"], str(entrypoint.resolve(strict=False)))
        self.assertEqual(data["argv"], ["arg1"])
        self.assertEqual(data["environment"]["recorded_keys"], ["TRACE_VALUE"])
        self.assertEqual(data["sources"][0]["resolved_path"], str(dependency.resolve(strict=False)))
        self.assertEqual(data["sources"][0]["arguments"], ["arg1"])

    def test_trace_cli_writes_default_artifact_under_modash_directory(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")

            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "modash.py"), "trace", str(entrypoint)],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            artifacts = sorted((project.root / ".modash" / "observations").glob("*.json"))
            artifact_paths = [str(path) for path in artifacts]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(len(artifacts), 1)
        self.assertIn(artifact_paths[0], result.stderr)

    def test_trace_cli_writes_generated_artifact_under_output_dir(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            output_dir = project.path("trace-output")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            artifacts = sorted(output_dir.glob("*.json"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(len(artifacts), 1)

    def test_trace_cli_resolves_relative_entrypoint_from_process_cwd(self):
        with ScriptProject() as project:
            entrypoint = project.write("scripts/main.sh", "source ./dep.sh\n")
            dependency = project.write("scripts/dep.sh", "echo dep\n")
            output = project.path("trace.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    "scripts/main.sh",
                    "--output",
                    str(output),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            data = json.loads(output.read_text())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertEqual(data["entrypoint"], str(entrypoint.resolve(strict=False)))
        self.assertEqual(data["cwd"], str(entrypoint.parent.resolve(strict=False)))
        self.assertEqual(data["sources"][0]["resolved_path"], str(dependency.resolve(strict=False)))

    def test_trace_cli_rejects_bad_environment_overlay(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--env",
                    "BAD",
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid --env value", result.stderr)

    def test_trace_cli_rejects_output_and_output_dir_together(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(project.path("trace.json")),
                    "--output-dir",
                    str(project.path("trace-output")),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_trace_cli_rejects_bad_timeout(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--timeout",
                    "0",
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be a positive number", result.stderr)

    def test_trace_cli_timeout_fails_before_writing_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "while :; do :; done\n")
            output = project.path("trace.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(output),
                    "--timeout",
                    "0.05",
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime source trace timed out", result.stderr)
        self.assertFalse(output.exists())

    def test_trace_cli_incomplete_trace_fails_before_writing_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "unalias source",
                    "unset -f source",
                    "source ./dep.sh",
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")
            output = project.path("trace.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(output),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("missed source-like command", result.stderr)
        self.assertFalse(output.exists())

    def test_existing_compile_cli_still_uses_original_positional_form(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            output = project.path("merged.sh")

            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "modash.py"), str(entrypoint), str(output)],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output_exists = output.is_file()
            output_content = output.read_text() if output_exists else ""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output_exists)
        self.assertIn("dep.sh", output_content)


if __name__ == "__main__":
    unittest.main()
