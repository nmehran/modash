import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_supplements import (  # noqa: E402
    generate_source_supplement,
    write_generated_supplement,
)
from methods.runtime_source_graph import build_observed_source_graph, write_observed_source_graph  # noqa: E402
from modash import compile_observed_main  # noqa: E402
from test.support import ScriptProject  # noqa: E402


class RuntimeSupplementReplayTestCase(unittest.TestCase):
    def test_variable_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source "$LIB_DIR/dep.sh"\necho "main:$VALUE"\n',
            )
            project.write("lib/dep.sh", 'VALUE=loaded\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:loaded\nmain:loaded\n")

    def test_wrapped_variable_observation_replays_through_executable_compile(self):
        cases = {
            "builtin source": 'builtin source "$LIB_DIR/dep.sh"\necho "main:$VALUE"\n',
            "command dot": 'command . "$LIB_DIR/dep.sh"\necho "main:$VALUE"\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                entrypoint = project.write("main.sh", main_content)
                project.write("lib/dep.sh", 'VALUE=loaded\necho "dep:$VALUE"\n')
                trace = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))})
                supplement = generate_source_supplement(entrypoint, trace.observation)
                supplement_path = project.path("generated/source-supplement.json")
                write_generated_supplement(supplement, supplement_path)

                compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
                result = project.run(compiled)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep:loaded\nmain:loaded\n")
            self.assertEqual(supplement.to_dict()["variables"], {"LIB_DIR": "lib"})

    def test_helper_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_safe "$TARGET" "arg one"\necho "main:$VALUE"\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_safe() {",
                    '  if ! source "$@"; then',
                    "    return 1",
                    "  fi",
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'VALUE="$1"\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:arg one\nmain:arg one\n")

    def test_wrapped_helper_observation_replays_through_executable_compile(self):
        cases = {
            "builtin source": '  builtin source "$@"',
            "command source": '  command source "$@"',
            "command dot": '  command . "$@"',
        }

        for name, helper_source in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                entrypoint = project.write(
                    "main.sh",
                    'source ./helpers.sh\nsource_safe "$TARGET" "arg one"\necho "main:$VALUE"\n',
                )
                project.write(
                    "helpers.sh",
                    "\n".join([
                        "source_safe() {",
                        helper_source,
                        "}",
                        "",
                    ]),
                )
                project.write("dep.sh", 'VALUE="$1"\necho "dep:$VALUE"\n')
                trace = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))})
                supplement = generate_source_supplement(entrypoint, trace.observation)
                supplement_path = project.path("generated/source-supplement.json")
                write_generated_supplement(supplement, supplement_path)

                compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
                result = project.run(compiled)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep:arg one\nmain:arg one\n")
            self.assertEqual(supplement.to_dict()["functions"], {
                "source_safe": [
                    {
                        "arguments": ["dep.sh", "arg one"],
                    },
                ],
            })

    def test_child_bash_c_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "bash -c 'source ./dep.sh; printf \"child:%s\\n\" \"$VALUE\"'\n"
                "printf \"parent:%s\\n\" \"${VALUE-unset}\"\n",
            )
            project.write("dep.sh", 'VALUE=loaded\nprintf "dep:%s\\n" "$VALUE"\n')
            trace = project.trace("main.sh")
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:loaded\nchild:loaded\nparent:unset\n")
        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {},
        })
        self.assertGreaterEqual(len(trace.observation.processes), 2)
        self.assertEqual(trace.observation.sources[0].process_index, 1)

    def test_child_bash_c_positional_graph_replays_through_compile_observed(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "bash -c '. \"$1\"; printf \"child:%s\\n\" \"$VALUE\"' bash \"$DYNAMIC_DEP\"\n"
                "printf \"parent:%s\\n\" \"${VALUE-unset}\"\n",
            )
            dep = project.write("dep.sh", 'VALUE=loaded\nprintf "dep:%s\\n" "$VALUE"\n')
            trace = project.trace("main.sh", env={"DYNAMIC_DEP": str(dep)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"DYNAMIC_DEP": str(dep)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:loaded\nchild:loaded\nparent:unset\n")


if __name__ == "__main__":
    unittest.main()
