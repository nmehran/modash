import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_graph import (  # noqa: E402
    RuntimeSourceGraphError,
    build_observed_source_graph,
    load_observed_source_graph,
    write_observed_source_graph,
)
from methods.runtime_source_observations import (  # noqa: E402
    BashInfo,
    EnvironmentInfo,
    RuntimeProcess,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
)
from test.support import ScriptProject  # noqa: E402


class RuntimeSourceGraphTestCase(unittest.TestCase):
    def test_builds_trusted_graph_from_direct_source_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh one\n")
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')
            observation = project.trace("main.sh").observation

            graph = build_observed_source_graph(entrypoint, observation)

        self.assertEqual(graph["version"], 1)
        self.assertEqual(graph["observation_version"], 4)
        self.assertEqual(graph["summary"]["processes"], 1)
        self.assertEqual(graph["summary"]["edges"], 1)
        self.assertEqual(graph["summary"]["trusted_xtrace_edges"], 1)
        edge = graph["edges"][0]
        self.assertEqual(edge["from"], f"file:{entrypoint.resolve(strict=False)}")
        self.assertEqual(edge["to"], f"file:{dependency.resolve(strict=False)}")
        self.assertEqual(edge["arguments"], ["one"])
        self.assertEqual(edge["xtrace"]["command"], "source ./dep.sh one")

    def test_builds_process_command_node_for_child_bash_c_source(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "bash -c 'source ./dep.sh child'\n")
            dependency = project.write("dep.sh", 'printf "child:%s\\n" "$1"\n')
            observation = project.trace("main.sh").observation

            graph = build_observed_source_graph(entrypoint, observation)

        edge = graph["edges"][0]
        self.assertEqual(edge["from"], "process-command:1")
        self.assertEqual(edge["to"], f"file:{dependency.resolve(strict=False)}")
        process_command_nodes = [
            node for node in graph["nodes"]
            if node["kind"] == "process-command"
        ]
        self.assertEqual(len(process_command_nodes), 1)
        self.assertEqual(process_command_nodes[0]["command"], "source ./dep.sh child")

    def test_rejects_stale_observation_before_building_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            dependency.write_text("echo changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")

    def test_rejects_source_events_without_trusted_xtrace_links(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = RuntimeSourceObservation(
                entrypoint=str(entrypoint),
                cwd=str(project.root),
                argv=(),
                bash=BashInfo(version="test"),
                trace=TraceInfo(version="test"),
                environment=EnvironmentInfo(policy="inherit", recorded_keys=()),
                processes=(
                    RuntimeProcess(
                        index=0,
                        pid=100,
                        parent_pid=50,
                        parent_index=None,
                        entrypoint=str(entrypoint),
                        cwd=str(project.root),
                        argv=(),
                        command=str(entrypoint),
                    ),
                ),
                sources=(
                    RuntimeSourceEvent(
                        index=0,
                        process_index=0,
                        xtrace_index=None,
                        call_site=SourceCallSite(
                            file=str(entrypoint),
                            line=1,
                            command="source ./dep.sh",
                        ),
                        resolved_path=str(dependency),
                    ),
                ),
                xtrace=(),
                files=(
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")),
                    fingerprint_file(dependency, roles=("source",)),
                ),
            )

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.untrusted_observation")

    def test_writes_graph_json_with_trailing_newline(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(entrypoint, project.trace("main.sh").observation)
            path = project.path(".modash/graphs/run.json")

            written = write_observed_source_graph(graph, path)
            text = written.read_text()
            payload = json.loads(text)
            loaded = load_observed_source_graph(path)

            self.assertEqual(payload["version"], 1)
            self.assertEqual(loaded["version"], 1)
            self.assertTrue(text.endswith("\n"))

    def test_load_graph_rejects_missing_file_with_stable_code(self):
        with ScriptProject() as project:
            with self.assertRaises(RuntimeSourceGraphError) as context:
                load_observed_source_graph(project.path("missing-graph.json"))

        self.assertEqual(context.exception.code, "runtime.graph.missing")


if __name__ == "__main__":
    unittest.main()
