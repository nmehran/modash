import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_evaluator.graph import (  # noqa: E402
    RuntimeSourceGraphError,
    _is_trace_wrapper_source_command,
    build_observed_source_graph,
    ensure_graph_fingerprints_current,
    load_observed_source_graph,
    validate_observed_source_graph,
    write_observed_source_graph_review,
    write_observed_source_graph,
)
from methods.runtime_evaluator.observations import (  # noqa: E402
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

        self.assertEqual(graph["version"], 2)
        self.assertEqual(graph["observation_version"], 8)
        self.assertEqual(graph["environment"]["policy"], "inherit")
        self.assertIn("shell", graph["run"])
        self.assertEqual(graph["summary"]["processes"], 1)
        self.assertEqual(graph["summary"]["edges"], 1)
        self.assertEqual(graph["summary"]["trusted_xtrace_edges"], 1)
        edge = graph["edges"][0]
        self.assertEqual(edge["from"], f"file:{entrypoint.resolve(strict=False)}")
        self.assertEqual(edge["to"], f"file:{dependency.resolve(strict=False)}")
        self.assertEqual(edge["arguments"], ["one"])
        self.assertTrue(edge["source_identity"].startswith("src:"))
        self.assertEqual(edge["xtrace"]["source_identity"], edge["source_identity"])
        self.assertEqual(edge["xtrace"]["command"], "source ./dep.sh one")

    def test_builds_trusted_graph_from_dot_source_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", ". ./dep.sh\n")
            dependency = project.write("dep.sh", 'printf "dep\\n"\n')
            observation = project.trace("main.sh").observation

            graph = build_observed_source_graph(entrypoint, observation)

        edge = graph["edges"][0]
        self.assertEqual(edge["from"], f"file:{entrypoint.resolve(strict=False)}")
        self.assertEqual(edge["to"], f"file:{dependency.resolve(strict=False)}")
        self.assertEqual(edge["call_site"]["command"], ". ./dep.sh")
        self.assertEqual(edge["xtrace"]["command"], ". ./dep.sh")

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

    def test_builds_file_edge_for_sourced_file_that_returns_nonzero(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./dep.sh\nprintf "status:%s\\n" "$?"\n')
            dependency = project.write("dep.sh", "return 7\n")
            observation = project.trace("main.sh").observation

            graph = build_observed_source_graph(entrypoint, observation)

        edge = graph["edges"][0]
        self.assertEqual(edge["status"], 7)
        self.assertEqual(edge["to"], f"file:{dependency.resolve(strict=False)}")
        self.assertIn(
            str(dependency.resolve(strict=False)),
            {
                file["path"]
                for file in graph["files"]
                if "source" in file["roles"]
            },
        )

    def test_build_rejects_nonzero_target_observation(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\nexit 7\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            observation = project.trace("main.sh").observation

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.nonzero_trace")
        self.assertIn("status 7", str(context.exception))

    def test_rejects_stale_observation_before_building_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            dependency.write_text("echo changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")
        self.assertIn("roles=source", str(context.exception))
        self.assertIn("expected", str(context.exception))
        self.assertIn("current", str(context.exception))

    def test_rejects_stale_entrypoint_with_fingerprint_details(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            observation = project.trace("main.sh").observation

            entrypoint.write_text("echo changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")
        self.assertIn("roles=entrypoint", str(context.exception))
        self.assertIn("expected", str(context.exception))
        self.assertIn("current", str(context.exception))

    def test_rejects_stale_call_site_only_file_with_fingerprint_details(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "bash ./child.sh\n")
            child = project.write("child.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            child.write_text("source ./dep.sh\n# changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")
        self.assertIn("roles=call-site", str(context.exception))
        self.assertIn("expected", str(context.exception))
        self.assertIn("current", str(context.exception))

    def test_rejects_missing_source_file_with_fingerprint_details(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            dependency.unlink()

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")
        self.assertIn("roles=source", str(context.exception))
        self.assertIn("file is missing", str(context.exception))

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
                        source_identity="",
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

    def test_validate_graph_rejects_summary_tampering(self):
        graph = self._direct_source_graph()
        graph["summary"]["edges"] = 99

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("summary.edges", str(context.exception))

    def test_validate_graph_rejects_file_node_role_tampering(self):
        graph = self._direct_source_graph()
        source_path = graph["edges"][0]["resolved_path"]
        for file in graph["files"]:
            if file["path"] == source_path:
                file["roles"] = ["call-site"]
        for node in graph["nodes"]:
            if node["id"] == f"file:{source_path}":
                node["roles"] = ["call-site"]

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("resolved_path must have a file fingerprint with role source", str(context.exception))

    def test_validate_graph_rejects_xtrace_process_tampering(self):
        graph = self._direct_source_graph()
        graph["edges"][0]["xtrace"]["process_index"] = 99

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("xtrace.process_index must match", str(context.exception))

    def test_validate_graph_rejects_xtrace_call_site_tampering(self):
        graph = self._direct_source_graph()
        graph["edges"][0]["xtrace"]["line"] = 99

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("xtrace must match edge call_site", str(context.exception))

    def test_validate_graph_rejects_xtrace_index_tampering(self):
        graph = self._direct_source_graph()
        graph["edges"][0]["xtrace"]["index"] = 1

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("xtrace.index values must be unique and contiguous", str(context.exception))

    def test_validate_graph_rejects_source_identity_tampering(self):
        graph = self._direct_source_graph()
        graph["edges"][0]["xtrace"]["source_identity"] = "src:tampered"

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("source_identity must match", str(context.exception))

    def test_validate_graph_rejects_nonzero_target_status_tampering(self):
        graph = self._direct_source_graph()
        graph["run"]["target_status"] = 7

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertEqual(context.exception.code, "runtime.graph.nonzero_trace")
        self.assertIn("status 7", str(context.exception))

    def test_validate_graph_rejects_non_source_trace_wrapper_tampering(self):
        graph = self._direct_source_graph()
        graph["edges"][0]["xtrace"]["command"] = "__modash_trace_command -- echo not-source"

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("xtrace.command must be a source-like command", str(context.exception))

    def test_trace_wrapper_source_command_parser_matrix(self):
        positive_commands = (
            "__modash_trace_source_alias source source 0 -- ./dep.sh",
            "__modash_trace_source_alias dot . 0 -- ./dep.sh",
            "__modash_trace_builtin -- builtin source ./dep.sh",
            "__modash_trace_builtin -- builtin -- source ./dep.sh",
            "__modash_trace_builtin -- builtin -- . ./dep.sh",
            "__modash_trace_command -- command source ./dep.sh",
            "__modash_trace_command -- command -- source ./dep.sh",
            "__modash_trace_command -- command -p source ./dep.sh",
            "__modash_trace_command -- command -p -- source ./dep.sh",
        )
        negative_commands = (
            "__modash_trace_command -- echo source ./dep.sh",
            "__modash_trace_command -- command echo source ./dep.sh",
            "__modash_trace_command -- command -v source",
            "__modash_trace_command -- command -V source",
            "__modash_trace_command -- command -pv source ./dep.sh",
            "__modash_trace_builtin -- builtin echo source ./dep.sh",
            "__modash_trace_command command source ./dep.sh",
        )

        for command in positive_commands:
            with self.subTest(command=command):
                self.assertTrue(_is_trace_wrapper_source_command(command))

        for command in negative_commands:
            with self.subTest(command=command):
                self.assertFalse(_is_trace_wrapper_source_command(command))

    def test_validate_graph_rejects_duplicate_source_identity_tampering(self):
        graph = self._two_source_graph()
        graph["edges"][1]["source_identity"] = graph["edges"][0]["source_identity"]
        graph["edges"][1]["xtrace"]["source_identity"] = graph["edges"][0]["source_identity"]

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("source_identity values must be unique", str(context.exception))

    def test_validate_graph_rejects_unfingerprinted_function_call_file(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { source "$1"; }',
                    '"$MODASH_TEST_HELPER" ./dep.sh',
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(
                entrypoint,
                project.trace("main.sh", env={"MODASH_TEST_HELPER": "load"}).observation,
            )
            graph["edges"][0]["function_call"]["file"] = str(project.path("unfingerprinted.sh"))

            with self.assertRaises(RuntimeSourceGraphError) as context:
                validate_observed_source_graph(graph)

        self.assertIn("function_call.file must have a file fingerprint", str(context.exception))

    def test_validate_graph_rejects_function_call_outside_recorded_stack(self):
        graph = self._dynamic_helper_graph()
        graph["edges"][0]["function_call"]["function"] = "other"

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("function_call.function must be present", str(context.exception))

    def test_validate_graph_rejects_function_call_command_tampering(self):
        graph = self._dynamic_helper_graph()
        graph["edges"][0]["function_call"]["arguments"] = ["./other.sh"]

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("function_call.command must match", str(context.exception))

    def test_validate_graph_rejects_missing_source_target_tampering(self):
        graph = self._missing_source_graph()
        graph["edges"][0]["status"] = 0

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("missing-source edge target status must match edge status", str(context.exception))

    def test_validate_graph_rejects_unreferenced_process_command_node(self):
        graph = self._direct_source_graph()
        process_node = next(node for node in graph["nodes"] if node["id"] == "process:0")
        graph["nodes"].append({
            "id": "process-command:0",
            "kind": "process-command",
            "process_index": 0,
            "command": process_node["command"],
            "entrypoint": graph["entrypoint"],
            "cwd": process_node["cwd"],
        })
        graph["summary"]["nodes"] += 1

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("process-command nodes must be referenced", str(context.exception))

    def test_validate_graph_rejects_unreferenced_missing_source_node(self):
        graph = self._direct_source_graph()
        missing_path = str(Path(graph["entrypoint"]).with_name("missing.sh"))
        graph["nodes"].append({
            "id": "missing-source:1",
            "kind": "missing-source",
            "path": missing_path,
            "status": 1,
        })
        graph["summary"]["nodes"] += 1

        with self.assertRaises(RuntimeSourceGraphError) as context:
            validate_observed_source_graph(graph)

        self.assertIn("missing-source nodes must be referenced", str(context.exception))

    def test_ensure_graph_current_rejects_missing_source_that_now_exists(self):
        with ScriptProject() as project:
            missing = project.path("missing.sh")
            entrypoint = project.write("main.sh", 'source ./missing.sh\nprintf "after\\n"\n')
            observation = project.trace("main.sh").observation
            graph = build_observed_source_graph(entrypoint, observation)

            missing.write_text("echo now-present\n", encoding="utf-8")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                ensure_graph_fingerprints_current(graph)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")
        self.assertIn("source_presence", str(context.exception))

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

            self.assertEqual(payload["version"], 2)
            self.assertEqual(loaded["version"], 2)
            self.assertTrue(text.endswith("\n"))

    def test_writes_human_readable_graph_review_report(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh arg\n")
            dependency = project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(entrypoint, project.trace("main.sh").observation)
            report_path = project.path("reports/runtime-graph.txt")

            written = write_observed_source_graph_review(graph, report_path)
            text = written.read_text()

        self.assertIn("modash runtime source graph review", text)
        self.assertIn("trusted: yes", text)
        self.assertIn("run: observed_at=", text)
        self.assertIn("environment: policy=inherit", text)
        self.assertIn("source identity", text.replace("_", " "))
        self.assertIn(str(dependency.resolve(strict=False)), text)
        self.assertIn("xtrace:", text)
        self.assertTrue(text.endswith("\n"))

    def test_graph_review_report_includes_observed_helper_call(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { source "$1"; }',
                    '"$MODASH_TEST_HELPER" ./dep.sh',
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(
                entrypoint,
                project.trace("main.sh", env={"MODASH_TEST_HELPER": "load"}).observation,
            )
            report_path = project.path("reports/runtime-graph.txt")

            text = write_observed_source_graph_review(graph, report_path).read_text()

        self.assertIn("helper_call:", text)
        self.assertIn("load ./dep.sh", text)

    def test_load_graph_rejects_missing_file_with_stable_code(self):
        with ScriptProject() as project:
            with self.assertRaises(RuntimeSourceGraphError) as context:
                load_observed_source_graph(project.path("missing-graph.json"))

        self.assertEqual(context.exception.code, "runtime.graph.missing")

    def _direct_source_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(entrypoint, project.trace("main.sh").observation)
        return copy.deepcopy(graph)

    def _missing_source_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./missing.sh\nprintf "after\\n"\n')
            graph = build_observed_source_graph(entrypoint, project.trace("main.sh").observation)
        return copy.deepcopy(graph)

    def _two_source_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./one.sh\nsource ./two.sh\n")
            project.write("one.sh", "echo one\n")
            project.write("two.sh", "echo two\n")
            graph = build_observed_source_graph(entrypoint, project.trace("main.sh").observation)
        return copy.deepcopy(graph)

    def _dynamic_helper_graph(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { source "$1"; }',
                    '"$MODASH_TEST_HELPER" ./dep.sh',
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")
            graph = build_observed_source_graph(
                entrypoint,
                project.trace("main.sh", env={"MODASH_TEST_HELPER": "load"}).observation,
            )
        return copy.deepcopy(graph)


if __name__ == "__main__":
    unittest.main()
