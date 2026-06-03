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
            report_path = project.path("runtime-graph.json.report.txt")
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
            report = report_path.read_text()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("modash: runtime source graph:", result.stderr)
        self.assertIn("modash: runtime graph review report:", result.stderr)
        self.assertEqual(graph["version"], 2)
        self.assertEqual(graph["observation_version"], 8)
        self.assertEqual(graph["environment"]["policy"], "inherit")
        self.assertIn("shell", graph["run"])
        self.assertEqual(graph["edges"][0]["resolved_path"], str(dependency.resolve(strict=False)))
        self.assertEqual(graph["edges"][0]["xtrace"]["command"], "source ./dep.sh arg")
        self.assertIn("trusted: yes", report)
        self.assertIn("source ./dep.sh arg", report)

    def test_graph_cli_accepts_option_wrapped_source_forms(self):
        cases = {
            "command delimiter source": "command -- source ./dep.sh\n",
            "command path source": "command -p source ./dep.sh\n",
            "builtin delimiter source": "builtin -- source ./dep.sh\n",
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                entrypoint = project.write("main.sh", main_content)
                project.write("dep.sh", "printf 'dep\\n'\n")
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
            self.assertEqual(len(graph["edges"]), 1)
            self.assertEqual(graph["edges"][0]["call_site"]["command"], main_content.strip())

    def test_graph_cli_rejects_eval_source_edge_as_unreplayable(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "eval 'source ./dep.sh'\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
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
            graph_exists = graph_path.exists()

        self.assertEqual(result.returncode, 1)
        self.assertIn("replayable source command", result.stderr)
        self.assertFalse(graph_exists)

    def test_graph_cli_writes_explicit_report_path(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint)
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
            report_path = project.path("reports/graph-review.txt")
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
                    "--report",
                    str(report_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            explicit_report_exists = report_path.is_file()
            default_report_exists = project.path("runtime-graph.json.report.txt").exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(explicit_report_exists)
        self.assertFalse(default_report_exists)

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
        self.assertEqual(report["observation_version"], 8)
        self.assertEqual(report["environment"]["policy"], "overlay")
        self.assertEqual(report["environment"]["recorded_keys"], ["LIB_DIR"])
        self.assertIn("timeout_seconds", report["run"])
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

    def test_compile_observed_cli_compiles_with_in_memory_graph_supplement(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint, env={"LIB_DIR": str(project.path("lib"))})
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
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
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "compile-observed",
                    str(entrypoint),
                    str(output_path),
                    "--from-graph",
                    str(graph_path),
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
            )

        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertEqual(compile_result.stdout, "")
        self.assertIn("modash: compiled from trusted runtime graph:", compile_result.stderr)
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertEqual(run_result.stdout, "dep\n")

    def test_compile_observed_cli_uses_graph_resolved_cwd_relative_dot_source(self):
        with ScriptProject() as project:
            entrypoint = project.write("test/main.sh", '. ./dep.sh\nprintf "main\\n"\n')
            project.write("dep.sh", 'printf "dep\\n"\n')
            observation = trace_sources(entrypoint, cwd=project.root)
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
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
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "compile-observed",
                    str(entrypoint),
                    str(output_path),
                    "--from-graph",
                    str(graph_path),
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
            )

        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertEqual(run_result.stdout, "dep\nmain\n")

    def test_compile_observed_cli_replays_repeated_dynamic_call_site_edges_in_order(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'msg() {',
                    '    printf "msg:%s\\n" "$*"',
                    '}',
                    'run_hooks() {',
                    '    local hook fn=$1 desc=$2',
                    '    shift 2',
                    '    for hook in "$@"; do',
                    '        [ -r "$HOOK_ROOT/$hook" ] || continue',
                    '        unset "$fn"',
                    '        . "$HOOK_ROOT/$hook"',
                    '        type "$fn" >/dev/null || continue',
                    '        msg ":: running $desc [$hook]"',
                    '        "$fn"',
                    '    done',
                    '}',
                    'run_hooks run_hook hook alpha.sh beta.sh missing.sh',
                    "",
                ]),
            )
            project.write("hooks/alpha.sh", 'run_hook() { printf "alpha\\n"; }\n')
            project.write("hooks/beta.sh", 'run_hook() { printf "beta\\n"; }\n')
            observation = trace_sources(entrypoint, env={"HOOK_ROOT": str(project.path("hooks"))})
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
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
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "compile-observed",
                    str(entrypoint),
                    str(output_path),
                    "--from-graph",
                    str(graph_path),
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
                env={**os.environ, "HOOK_ROOT": str(project.path("hooks"))},
            )
            graph = json.loads(graph_path.read_text())

        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
        self.assertEqual([Path(edge["resolved_path"]).name for edge in graph["edges"]], ["alpha.sh", "beta.sh"])
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertEqual(
            run_result.stdout,
            "msg::: running hook [alpha.sh]\n"
            "alpha\n"
            "msg::: running hook [beta.sh]\n"
            "beta\n",
        )

    def test_compile_observed_cli_rejects_stale_graph_before_output(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            dependency = project.write("lib/dep.sh", "echo dep\n")
            observation = trace_sources(entrypoint, env={"LIB_DIR": str(project.path("lib"))})
            observation_path = project.path("observation.json")
            graph_path = project.path("runtime-graph.json")
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
            dependency.write_text("echo changed\n", encoding="utf-8")
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "compile-observed",
                    str(entrypoint),
                    str(output_path),
                    "--from-graph",
                    str(graph_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(graph_result.returncode, 0, graph_result.stderr)
        self.assertEqual(compile_result.returncode, 1)
        self.assertIn("stale", compile_result.stderr)
        self.assertFalse(output_path.exists())

    def test_observe_compile_cli_writes_review_artifacts_and_compiles(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh" exact\necho main\n')
            project.write("lib/dep.sh", 'echo dep:$1\n')
            graph_path = project.path("artifacts/runtime-graph.json")
            observation_path = project.path("artifacts/observation.json")
            report_path = project.path("artifacts/runtime-graph.txt")
            output_path = project.path("dist/compiled.sh")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "observe-compile",
                    str(entrypoint),
                    str(output_path),
                    "--env",
                    f"LIB_DIR={project.path('lib')}",
                    "--reviewed-graph-out",
                    str(graph_path),
                    "--observation-out",
                    str(observation_path),
                    "--report",
                    str(report_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            graph = json.loads(graph_path.read_text())
            observation = json.loads(observation_path.read_text())
            report = report_path.read_text()
            run_result = subprocess.run(
                ["bash", str(output_path)],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dep:exact\nmain\n")
        self.assertEqual(graph["observation_version"], 8)
        self.assertEqual(graph["environment"]["recorded_keys"], ["LIB_DIR"])
        self.assertEqual(observation["version"], 8)
        self.assertEqual(observation["run"]["target_status"], 0)
        self.assertIn("trusted: yes", report)
        self.assertIn("modash: compiled from newly observed trusted runtime graph:", result.stderr)
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        self.assertEqual(run_result.stdout, "dep:exact\nmain\n")

    def test_observe_compile_cli_stops_after_nonzero_trace(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "source ./dep.sh",
                    "exit 7",
                    "",
                ]),
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            graph_path = project.path("artifacts/runtime-graph.json")
            observation_path = project.path("artifacts/observation.json")
            report_path = project.path("artifacts/runtime-graph.txt")
            output_path = project.path("dist/compiled.sh")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "observe-compile",
                    str(entrypoint),
                    str(output_path),
                    "--reviewed-graph-out",
                    str(graph_path),
                    "--observation-out",
                    str(observation_path),
                    "--report",
                    str(report_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            observation_exists = observation_path.is_file()
            graph_exists = graph_path.exists()
            report_exists = report_path.exists()
            output_exists = output_path.exists()

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to compile", result.stderr)
        self.assertEqual(result.stdout, "dep\n")
        self.assertTrue(observation_exists)
        self.assertFalse(graph_exists)
        self.assertFalse(report_exists)
        self.assertFalse(output_exists)

    def test_graph_cli_rejects_nonzero_trace_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\nexit 7\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            observation_path = project.path("artifacts/observation.json")
            graph_path = project.path("artifacts/runtime-graph.json")
            output_path = project.path("dist/compiled.sh")

            trace_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(observation_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
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
            compile_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "compile-observed",
                    str(entrypoint),
                    str(output_path),
                    "--from-graph",
                    str(graph_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            graph_exists = graph_path.exists()
            observation_exists = observation_path.is_file()
            output_exists = output_path.exists()

        self.assertEqual(trace_result.returncode, 7)
        self.assertEqual(trace_result.stdout, "dep\n")
        self.assertTrue(observation_exists)
        self.assertEqual(graph_result.returncode, 1)
        self.assertIn("status 7", graph_result.stderr)
        self.assertFalse(graph_exists)
        self.assertEqual(compile_result.returncode, 1)
        self.assertIn("does not exist", compile_result.stderr)
        self.assertFalse(output_exists)

    def test_supplement_cli_rejects_nonzero_trace_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\nexit 7\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            observation_path = project.path("artifacts/observation.json")
            supplement_path = project.path("artifacts/source-supplement.json")
            report_path = project.path("artifacts/source-supplement.report.json")

            trace_result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "trace",
                    str(entrypoint),
                    "--output",
                    str(observation_path),
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
            supplement_exists = supplement_path.exists()
            report_exists = report_path.exists()

        self.assertEqual(trace_result.returncode, 7)
        self.assertEqual(trace_result.stdout, "dep\n")
        self.assertEqual(supplement_result.returncode, 1)
        self.assertIn("status 7", supplement_result.stderr)
        self.assertFalse(supplement_exists)
        self.assertFalse(report_exists)

    def test_observe_compile_cli_stops_after_alias_perturbed_trace(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'source() { printf "custom-source:%s\\n" "$1"; }',
                    "source ./dep.sh",
                    "printf 'after\\n'",
                    "",
                ]),
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            graph_path = project.path("artifacts/runtime-graph.json")
            observation_path = project.path("artifacts/observation.json")
            report_path = project.path("artifacts/runtime-graph.txt")
            output_path = project.path("dist/compiled.sh")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    "observe-compile",
                    str(entrypoint),
                    str(output_path),
                    "--reviewed-graph-out",
                    str(graph_path),
                    "--observation-out",
                    str(observation_path),
                    "--report",
                    str(report_path),
                ],
                cwd=str(project.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            observation_exists = observation_path.is_file()
            graph_exists = graph_path.exists()
            report_exists = report_path.exists()
            output_exists = output_path.exists()

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to compile", result.stderr)
        self.assertTrue(observation_exists)
        self.assertFalse(graph_exists)
        self.assertFalse(report_exists)
        self.assertFalse(output_exists)

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
