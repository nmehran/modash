from __future__ import annotations

import unittest

from methods.runtime_evaluator.graph import build_observed_source_graph, write_observed_source_graph
from test.support import ScriptProject
from modash import compile_observed_main


class RuntimeGraphCompilerTestCase(unittest.TestCase):
    def compile_observed(self, project: ScriptProject, entry: str, *, env=None):
        entrypoint = project.path(entry)
        trace = project.trace(entry, env=env)
        self.assertEqual(trace.returncode, 0, trace.stderr)
        graph = build_observed_source_graph(entrypoint, trace.observation)
        graph_path = project.path("graph/runtime-source-graph.json")
        output = project.path("compiled.sh")
        write_observed_source_graph(graph, graph_path)
        compile_observed_main(str(entrypoint), str(output), graph=str(graph_path))
        return output, graph

    def test_runtime_compiler_preserves_no_argument_source_positionals_and_locals(self):
        with ScriptProject() as project:
            dep = project.write(
                "dep.sh",
                'printf "dep:%s:%s:%s\\n" "$marker" "${1-unset}" "${2-unset}"\n'
                'marker=from-dep\n',
            )
            project.write(
                "main.sh",
                'f() {\n'
                '  local marker=from-main\n'
                '  source "$DEP"\n'
                '  printf "after:%s:%s:%s\\n" "$marker" "${1-unset}" "${2-unset}"\n'
                '}\n'
                'f A B\n',
            )
            compiled, _graph = self.compile_observed(project, "main.sh", env={"DEP": str(dep)})
            result = project.run(compiled, env={"DEP": "/missing/not-used"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:from-main:A:B\nafter:from-dep:A:B\n")

    def test_runtime_compiler_fails_closed_when_unobserved_source_branch_runs_later(self):
        with ScriptProject() as project:
            project.write("extra.sh", 'printf "extra\\n"\n')
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                'if [[ ${RUN_EXTRA:-} ]]; then\n'
                '  source ./extra.sh\n'
                'fi\n'
                'source ./dep.sh\n',
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            normal = project.run(compiled)
            diverged = project.run(compiled, env={"RUN_EXTRA": "1"})

        self.assertEqual(normal.returncode, 0, normal.stdout)
        self.assertEqual(normal.stdout, "dep\n")
        self.assertEqual(diverged.returncode, 125, diverged.stdout)
        self.assertIn("unobserved or over-consumed source edge", diverged.stdout)

    def test_runtime_compiler_replays_runtime_selected_helper_without_static_helper_model(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'printf "dep:%s\\n" "$VALUE"\n')
            project.write(
                "main.sh",
                'load_dep() {\n'
                '  local VALUE=helper\n'
                '  source "$1"\n'
                '}\n'
                'helper=load_dep\n'
                '"$helper" "$DEP"\n',
            )
            compiled, _graph = self.compile_observed(project, "main.sh", env={"DEP": str(dep)})
            result = project.run(compiled, env={"DEP": "/runtime/value/ignored"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:helper\n")

    def test_runtime_compiler_keeps_same_relative_identical_helpers_distinct(self):
        with ScriptProject() as project:
            project.mkdir("a")
            project.mkdir("b")
            project.write("a/common.sh", 'helper() { printf "a\\n"; }\n')
            project.write("b/common.sh", 'helper() { printf "b\\n"; }\n')
            project.write(
                "main.sh",
                'cd "$ROOT/a"\n'
                'source ./common.sh\n'
                'helper\n'
                'cd "$ROOT/b"\n'
                'source ./common.sh\n'
                'helper\n',
            )
            compiled, _graph = self.compile_observed(project, "main.sh", env={"ROOT": str(project.root)})
            result = project.run(compiled, env={"ROOT": str(project.root)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "a\nb\n")

    def test_runtime_compiler_rewrites_bash_source_references_to_original_paths(self):
        with ScriptProject() as project:
            dep = project.write(
                "dep.sh",
                'printf "source:%s\\n" "${BASH_SOURCE[0]}"\n'
                'printf "zero:%s\\n" "$0"\n',
            )
            project.write("main.sh", 'source "$DEP"\n')
            compiled, _graph = self.compile_observed(project, "main.sh", env={"DEP": str(dep)})
            result = project.run(compiled, env={"DEP": "/not/used"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"source:{dep}\n", result.stdout)
        self.assertIn(f"zero:{project.path('main.sh')}\n", result.stdout)

    def test_runtime_compiler_preserves_top_level_return_status_from_sourced_file(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'printf "dep-before-return\\n"\nreturn 7\nprintf "never\\n"\n')
            project.write("main.sh", 'source "$DEP" || printf "status:%s\\n" "$?"\n')
            compiled, _graph = self.compile_observed(project, "main.sh", env={"DEP": str(dep)})
            result = project.run(compiled, env={"DEP": "/not/used"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep-before-return\nstatus:7\n")

    def test_runtime_compiler_does_not_reject_multiline_non_source_substitutions(self):
        with ScriptProject() as project:
            dep = project.write(
                "dep.sh",
                'value=$(printf "%s\\n" \\\n'
                '  "not a source command" | \\\n'
                '  sed -e "s/source/value/")\n'
                'printf "value:%s\\n" "$value"\n',
            )
            project.write("main.sh", 'source "$DEP"\n')
            compiled, _graph = self.compile_observed(project, "main.sh", env={"DEP": str(dep)})
            result = project.run(compiled, env={"DEP": "/not/used"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "value:not a value command\n")


if __name__ == "__main__":
    unittest.main()
