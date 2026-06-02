import json
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
    def test_supplement_cli_generates_candidate_from_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint, env={"LIB_DIR": str(project.path("lib"))})
            observation_path = project.path("observation.json")
            supplement_path = project.path("source-supplement.json")
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

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("modash: source supplement:", result.stderr)
        self.assertEqual(payload, {
            "version": 1,
            "variables": {
                "LIB_DIR": "lib",
            },
            "functions": {},
        })

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
