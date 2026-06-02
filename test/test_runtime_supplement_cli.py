import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_trace import trace_sources, write_trace_observation  # noqa: E402
from test.support import ScriptProject  # noqa: E402


class RuntimeSupplementCliTestCase(unittest.TestCase):
    def test_graph_cli_writes_trusted_runtime_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh arg\n")
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')
            observation = trace_sources(entrypoint)
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
            write_trace_observation(observation, observation_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "graph",
                    str(entrypoint),
                    "--from-observation",
                    str(observation_path),
                    "--output",
                    str(graph_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            graph = json.loads(graph_path.read_text())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("modash: runtime source graph:", result.stderr)
        self.assertEqual(graph["version"], 1)
        self.assertEqual(graph["observation_version"], 4)
        self.assertEqual(graph["edges"][0]["resolved_path"], str(dependency.resolve(strict=False)))
        self.assertEqual(graph["edges"][0]["xtrace"]["command"], "source ./dep.sh arg")

    def test_supplement_cli_generates_candidate_from_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint, env={"LIB_DIR": str(project.path("lib"))})
            observation_path = project.path("observation.json")
            supplement_path = project.path("source-supplement.json")
            report_path = project.path("source-supplement.json.report.json")
            write_trace_observation(observation, observation_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "supplement",
                    str(entrypoint),
                    "--from-observation",
                    str(observation_path),
                    "--output",
                    str(supplement_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            payload = json.loads(supplement_path.read_text())
            report = json.loads(report_path.read_text())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("modash: source supplement:", result.stderr)
        self.assertIn("modash: observation review report:", result.stderr)
        self.assertEqual(payload, {
            "version": 1,
            "variables": {
                "LIB_DIR": "lib",
            },
            "functions": {},
        })
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["observation_version"], 4)
        self.assertEqual(report["summary"]["warnings"], 0)

    def test_supplement_cli_generates_candidate_from_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint, env={"LIB_DIR": str(project.path("lib"))})
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
            supplement_path = project.path("source-supplement.json")
            output_path = project.path("compiled.sh")
            write_trace_observation(observation, observation_path)

            graph_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "graph",
                    str(entrypoint),
                    "--from-observation",
                    str(observation_path),
                    "--output",
                    str(graph_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            supplement_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "supplement",
                    str(entrypoint),
                    "--from-graph",
                    str(graph_path),
                    "--output",
                    str(supplement_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    str(entrypoint),
                    str(output_path),
                    "--mode",
                    "executable",
                    "--source-supplement",
                    str(supplement_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            run_result = subprocess.run(
                ["bash", str(output_path)],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "LIB_DIR": str(project.path("lib"))},
            )
            payload = json.loads(supplement_path.read_text())

        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)
        self.assertEqual(supplement_result.returncode, 0, supplement_result.stderr)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertEqual(run_result.stdout, "dep\n")
        self.assertEqual(payload, {
            "version": 1,
            "variables": {
                "LIB_DIR": "lib",
            },
            "functions": {},
        })

    def test_supplement_cli_writes_explicit_report_path(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint)
            observation_path = project.path("observation.json")
            supplement_path = project.path("source-supplement.json")
            report_path = project.path("reports/review.json")
            write_trace_observation(observation, observation_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "supplement",
                    str(entrypoint),
                    "--from-observation",
                    str(observation_path),
                    "--output",
                    str(supplement_path),
                    "--report",
                    str(report_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            explicit_report_exists = report_path.is_file()
            default_report_exists = project.path("source-supplement.json.report.json").exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(explicit_report_exists)
        self.assertFalse(default_report_exists)

    def test_supplement_cli_rejects_missing_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            output = project.path("source-supplement.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "supplement",
                    str(entrypoint),
                    "--from-observation",
                    str(project.path("missing-observation.json")),
                    "--output",
                    str(output),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime source observation file does not exist", result.stderr)
        self.assertFalse(output.exists())

    def test_supplement_cli_requires_output_argument(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            observation_path = project.path("observation.json")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "supplement",
                    str(entrypoint),
                    "--from-observation",
                    str(observation_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("the following arguments are required: --output", result.stderr)


if __name__ == "__main__":
    unittest.main()
