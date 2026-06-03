import copy
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
                'source ./helpers.sh\nsource_safe "$MODASH_TEST_TARGET" "arg one"\necho "main:$VALUE"\n',
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
            trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(project.path("dep.sh"))})
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
                    'source ./helpers.sh\nsource_safe "$MODASH_TEST_TARGET" "arg one"\necho "main:$VALUE"\n',
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
                trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(project.path("dep.sh"))})
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

    def test_second_positional_helper_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_second fast "$MODASH_TEST_TARGET"\necho "main:$VALUE"\n',
            )
            project.write("helpers.sh", 'source_second() { source "$2" "$1"; }\n')
            project.write("dep.sh", 'VALUE="$1"\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(project.path("dep.sh"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:fast\nmain:fast\n")
        self.assertEqual(supplement.to_dict()["functions"], {
            "source_second": [
                {
                    "arguments": ["fast", "dep.sh"],
                    "source_index": 1,
                },
            ],
        })

    def test_local_alias_helper_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_mode fast "$MODASH_TEST_TARGET"\necho "main:$VALUE"\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_mode() {",
                    '  local mode="$1"',
                    '  local path="$2"',
                    '  source "$path" "$mode"',
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'VALUE="$1"\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(project.path("dep.sh"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:fast\nmain:fast\n")
        self.assertEqual(supplement.to_dict()["functions"], {
            "source_mode": [
                {
                    "arguments": ["fast", "dep.sh"],
                    "source_index": 1,
                },
            ],
        })

    def test_case_dispatch_helper_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_case main "$MODASH_TEST_TARGET"\necho "main:$VALUE"\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_case() {",
                    '  case "$1" in',
                    '    main) source "$2" ;;',
                    "  esac",
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'VALUE=loaded\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(project.path("dep.sh"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:loaded\nmain:loaded\n")
        self.assertEqual(supplement.to_dict()["functions"], {
            "source_case": [
                {
                    "arguments": ["main", "dep.sh"],
                    "source_index": 1,
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

    def test_compile_observed_replays_source_condition_helper_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "source_safe() {",
                    '  if ! source "$@"; then',
                    "    return 1",
                    "  fi",
                    "}",
                    'source_safe "$MODASH_TEST_TARGET" "arg one"',
                    'printf "main:%s\\n" "$VALUE"',
                    "",
                ]),
            )
            dep = project.write("dep.sh", 'VALUE="$1"\nprintf "dep:%s\\n" "$VALUE"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_TARGET": str(dep)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(graph["edges"][0]["call_site"]["command"], 'if ! source "$@"; then')
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"MODASH_TEST_TARGET": str(dep)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:arg one\nmain:arg one\n")

    def test_compile_observed_replays_compound_source_condition_edges(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'if source ./a.sh && source ./b.sh; then printf "main:%s:%s\\n" "$A" "$B"; fi\n',
            )
            project.write("a.sh", 'A=alpha\nprintf "a\\n"\n')
            project.write("b.sh", 'B=beta\nprintf "b\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [edge["xtrace"]["command"] for edge in graph["edges"]],
                ["source ./a.sh", "source ./b.sh"],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "a\nb\nmain:alpha:beta\n")

    def test_compile_observed_replays_oneline_function_condition_edges(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { if source "$1" && source "$2"; then printf "loaded:%s:%s\\n" "$A" "$B"; fi; }',
                    "load ./a.sh ./b.sh",
                    "",
                ]),
            )
            project.write("a.sh", 'A=alpha\nprintf "a\\n"\n')
            project.write("b.sh", 'B=beta\nprintf "b\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [edge["xtrace"]["command"] for edge in graph["edges"]],
                ["source ./a.sh", "source ./b.sh"],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "a\nb\nloaded:alpha:beta\n")

    def test_compile_observed_replays_oneline_function_short_circuit_edges(self):
        cases = {
            "or first source succeeds": (
                'load() { if source "$1" || source "$2"; then '
                'printf "loaded:%s:%s\\n" "$A" "${B-unset}"; fi; }\n'
                "load ./a.sh ./b.sh\n",
                "a\nloaded:alpha:unset\n",
            ),
            "false and falls through to second source": (
                'load() { if false && source "$1" || source "$2"; then '
                'printf "loaded:%s:%s\\n" "${A-unset}" "$B"; fi; }\n'
                "load ./a.sh ./b.sh\n",
                "b\nloaded:unset:beta\n",
            ),
        }
        for name, (main_script, expected_stdout) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                entrypoint = project.write("main.sh", main_script)
                project.write("a.sh", 'A=alpha\nprintf "a\\n"\n')
                project.write("b.sh", 'B=beta\nprintf "b\\n"\n')
                trace = project.trace("main.sh")
                graph = build_observed_source_graph(entrypoint, trace.observation)
                graph_path = project.path("graph/runtime-source-graph.json")
                compiled = project.path("compiled.sh")
                write_observed_source_graph(graph, graph_path)

                compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
                result = project.run(compiled)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, expected_stdout)

    def test_compile_observed_replays_missing_source_condition_status(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'if source ./missing.sh || source ./b.sh; then printf "ok:%s\\n" "$B"; fi\n',
            )
            project.write("b.sh", 'B=beta\nprintf "b\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [(edge["to"].split(":", 1)[0], edge["status"]) for edge in graph["edges"]],
                [("missing-source", 1), ("file", 0)],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("./missing.sh: No such file or directory\n", result.stdout)
        self.assertTrue(result.stdout.endswith("b\nok:beta\n"), result.stdout)

    def test_compile_observed_replays_helper_missing_source_fallback(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { if source "$1" || source "$2"; then printf "ok:%s\\n" "$VALUE"; fi; }',
                    "load ./missing.sh ./fallback.sh",
                    "",
                ]),
            )
            project.write("fallback.sh", 'VALUE=fallback\nprintf "fallback\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [(edge["to"].split(":", 1)[0], edge["status"]) for edge in graph["edges"]],
                [("missing-source", 1), ("file", 0)],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("./missing.sh: No such file or directory\n", result.stdout)
        self.assertTrue(result.stdout.endswith("fallback\nok:fallback\n"), result.stdout)

    def test_compile_observed_replays_mixed_repeated_helper_short_circuit_edges(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { if source "$1" || source "$2"; then printf "ok:%s\\n" "$VALUE"; fi; }',
                    "load ./first.sh ./fallback-a.sh",
                    "load ./missing.sh ./fallback-b.sh",
                    "",
                ]),
            )
            project.write("first.sh", 'VALUE=first\nprintf "first\\n"\n')
            project.write("fallback-a.sh", 'VALUE=A\nprintf "fallback:A\\n"\n')
            project.write("fallback-b.sh", 'VALUE=B\nprintf "fallback:B\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [(edge["to"].split(":", 1)[0], edge["status"]) for edge in graph["edges"]],
                [("file", 0), ("missing-source", 1), ("file", 0)],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.startswith("first\nok:first\n"), result.stdout)
        self.assertIn("./missing.sh: No such file or directory\n", result.stdout)
        self.assertTrue(result.stdout.endswith("fallback:B\nok:B\n"), result.stdout)

    def test_compile_observed_replays_while_source_condition_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "i=0",
                    'while source ./dep.sh; do',
                    '  printf "loop:%s\\n" "$VALUE"',
                    "  ((i++))",
                    "  [[ $i -ge 1 ]] && break",
                    "done",
                    "",
                ]),
            )
            project.write("dep.sh", 'VALUE=loaded\nprintf "dep\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(graph["edges"][0]["call_site"]["command"], 'while source ./dep.sh; do')
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\nloop:loaded\n")

    def test_child_bash_c_graph_replay_preserves_child_positionals_and_zero(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                (
                    "bash -c '. \"$1\"; printf \"after:%s:%s:%s\\n\" "
                    "\"$0\" \"${1-unset}\" \"${2-unset}\"' "
                    "child \"$DYNAMIC_DEP\" extra\n"
                ),
            )
            dep = project.write(
                "dep.sh",
                'printf "dep:%s:%s\\n" "${1-unset}" "${2-unset}"\n',
            )
            expected = project.run("main.sh", env={"DYNAMIC_DEP": str(dep)})
            trace = project.trace("main.sh", env={"DYNAMIC_DEP": str(dep)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"DYNAMIC_DEP": str(dep)})

        self.assertEqual(expected.returncode, 0, expected.stdout)
        self.assertEqual(trace.returncode, expected.returncode, trace.stderr)
        self.assertEqual(trace.stdout, expected.stdout)
        self.assertEqual(result.returncode, expected.returncode, result.stdout)
        self.assertEqual(result.stdout, expected.stdout)

    def test_recursive_helper_graph_replays_through_compile_observed(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "load_hooks() {",
                    '  if [ "$#" -eq 0 ]; then',
                    "    return 0",
                    "  fi",
                    '  if [ -r "$ROOT/$1" ]; then',
                    '    source "$ROOT/$1"',
                    "  fi",
                    "  shift",
                    '  load_hooks "$@"',
                    "}",
                    "load_hooks alpha.sh beta.sh missing.sh",
                    "",
                ]),
            )
            project.write("alpha.sh", 'printf "alpha\\n"\n')
            project.write("beta.sh", 'printf "beta\\n"\n')
            trace = project.trace("main.sh", env={"ROOT": str(project.root)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"ROOT": str(project.root)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "alpha\nbeta\n")
        self.assertEqual([Path(edge["resolved_path"]).name for edge in graph["edges"]], ["alpha.sh", "beta.sh"])

    def test_recursive_helper_static_compile_remains_fail_closed_without_graph(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "\n".join([
                    "load_hooks() {",
                    '  if [ "$#" -eq 0 ]; then',
                    "    return 0",
                    "  fi",
                    '  source "$ROOT/$1"',
                    "  shift",
                    '  load_hooks "$@"',
                    "}",
                    "load_hooks alpha.sh beta.sh",
                    "",
                ]),
            )
            project.write("alpha.sh", 'printf "alpha\\n"\n')
            project.write("beta.sh", 'printf "beta\\n"\n')
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "recursive function call") as context:
                project.compile(
                    "main.sh",
                    output=output,
                    mode="executable",
                    env={"ROOT": str(project.root)},
                )

        self.assertEqual(context.exception.diagnostic.code, "unsupported.source.function-recursion")
        self.assertFalse(output.exists())

    def test_compile_observed_rejects_unconsumed_graph_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./dep.sh\nprintf "main\\n"\n')
            project.write("dep.sh", 'printf "dep\\n"\n')
            trace = project.trace("main.sh")
            graph = build_observed_source_graph(entrypoint, trace.observation)

            extra_edge = copy.deepcopy(graph["edges"][0])
            extra_edge["index"] = 1
            extra_edge["source_identity"] = "src:unconsumed0000000000000"
            extra_edge["xtrace"]["index"] = 1
            extra_edge["xtrace"]["source_identity"] = extra_edge["source_identity"]
            graph["edges"].append(extra_edge)
            graph["summary"]["edges"] = 2
            graph["summary"]["trusted_xtrace_edges"] = 2
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            with self.assertRaises(NotImplementedError) as context:
                compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))

        self.assertEqual(context.exception.code, "unsupported.source.graph-unconsumed")
        self.assertFalse(compiled.exists())

    def test_compile_observed_rejects_unconsumed_child_process_graph_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                (
                    "bash -c '. \"$1\"; printf \"child:%s\\n\" \"$VALUE\"' "
                    "child \"$DYNAMIC_DEP\"\n"
                ),
            )
            dep = project.write("dep.sh", 'VALUE=loaded\nprintf "dep:%s\\n" "$VALUE"\n')
            trace = project.trace("main.sh", env={"DYNAMIC_DEP": str(dep)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            self.assertEqual(len(graph["edges"]), 1)
            self.assertTrue(graph["edges"][0]["from"].startswith("process-command:"))

            extra_edge = copy.deepcopy(graph["edges"][0])
            extra_edge["index"] = 1
            extra_edge["source_identity"] = "src:unconsumedchild00000000"
            extra_edge["xtrace"]["index"] = 1
            extra_edge["xtrace"]["source_identity"] = extra_edge["source_identity"]
            graph["edges"].append(extra_edge)
            graph["summary"]["edges"] = 2
            graph["summary"]["trusted_xtrace_edges"] = 2
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            with self.assertRaises(NotImplementedError) as context:
                compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))

        self.assertEqual(context.exception.code, "unsupported.source.graph-unconsumed")
        self.assertFalse(compiled.exists())

    def test_recursive_helper_graph_replay_fails_when_observed_edge_is_missing(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    "load_hooks() {",
                    '  if [ "$#" -eq 0 ]; then',
                    "    return 0",
                    "  fi",
                    '  source "$ROOT/$1"',
                    "  shift",
                    '  load_hooks "$@"',
                    "}",
                    "load_hooks alpha.sh beta.sh",
                    "",
                ]),
            )
            project.write("alpha.sh", 'printf "alpha\\n"\n')
            project.write("beta.sh", 'printf "beta\\n"\n')
            trace = project.trace("main.sh", env={"ROOT": str(project.root)})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph["edges"] = graph["edges"][:1]
            graph["summary"]["edges"] = 1
            graph["summary"]["trusted_xtrace_edges"] = 1
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            with self.assertRaises(NotImplementedError) as context:
                compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))

        self.assertIn(context.exception.code, {
            "unsupported.source.function-argument",
            "unsupported.source.function-recursion",
            "unsupported.source.unresolved",
        })
        self.assertFalse(compiled.exists())

    def test_dynamic_helper_name_graph_replays_through_compile_observed(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load_one() { source "$1"; }',
                    '"$MODASH_TEST_HELPER" "$MODASH_TEST_TARGET_ONE"',
                    '"$MODASH_TEST_HELPER" "$MODASH_TEST_TARGET_TWO"',
                    "",
                ]),
            )
            dependency_one = project.write("one.sh", 'printf "dep:one\\n"\n')
            dependency_two = project.write("two.sh", 'printf "dep:two\\n"\n')
            trace = project.trace(
                "main.sh",
                env={
                    "MODASH_TEST_HELPER": "load_one",
                    "MODASH_TEST_TARGET_ONE": str(dependency_one),
                    "MODASH_TEST_TARGET_TWO": str(dependency_two),
                },
            )
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(
                compiled,
                env={
                    "MODASH_TEST_HELPER": "load_one",
                    "MODASH_TEST_TARGET_ONE": str(dependency_one),
                    "MODASH_TEST_TARGET_TWO": str(dependency_two),
                },
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:one\ndep:two\n")

    def test_dynamic_wrapper_helper_graph_replays_observed_parent_call(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'inner() { source "$2" "$1"; printf "inner:%s:%s\\n" "$1" "$2"; }',
                    'wrap() { inner "$2" "$1"; }',
                    'main() { "$MODASH_TEST_HELPER" ./dep.sh mode; }',
                    "main",
                    "",
                ]),
            )
            project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')
            expected = project.run("main.sh", env={"MODASH_TEST_HELPER": "wrap"})
            trace = project.trace("main.sh", env={"MODASH_TEST_HELPER": "wrap"})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(graph["edges"][0]["function_call"]["function"], "wrap")
            self.assertEqual(graph["edges"][0]["function_call"]["line"], 3)
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"MODASH_TEST_HELPER": "wrap"})

        self.assertEqual(expected.returncode, 0, expected.stdout)
        self.assertEqual(trace.returncode, expected.returncode, trace.stderr)
        self.assertEqual(trace.stdout, expected.stdout)
        self.assertEqual(result.returncode, expected.returncode, result.stdout)
        self.assertEqual(result.stdout, expected.stdout)

    def test_dynamic_helper_name_graph_replays_mixed_short_circuit_edges(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load() { if source "$1" || source "$2"; then printf "ok:%s\\n" "$VALUE"; fi; }',
                    '"$MODASH_TEST_HELPER" ./first.sh ./fallback-a.sh',
                    '"$MODASH_TEST_HELPER" ./missing.sh ./fallback-b.sh',
                    "",
                ]),
            )
            project.write("first.sh", 'VALUE=first\nprintf "first\\n"\n')
            project.write("fallback-a.sh", 'VALUE=A\nprintf "fallback:A\\n"\n')
            project.write("fallback-b.sh", 'VALUE=B\nprintf "fallback:B\\n"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_HELPER": "load"})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"MODASH_TEST_HELPER": "load"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.startswith("first\nok:first\n"), result.stdout)
        self.assertIn("./missing.sh: No such file or directory\n", result.stdout)
        self.assertTrue(result.stdout.endswith("fallback:B\nok:B\n"), result.stdout)

    def test_dynamic_helper_name_graph_uses_next_observed_helper_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load_a() { source "$1"; }',
                    'load_b() { source "$1"; }',
                    "load_b ./b.sh",
                    '"$MODASH_TEST_HELPER" ./a.sh',
                    'printf "done:%s\\n" "$VALUE"',
                    "",
                ]),
            )
            project.write("a.sh", 'VALUE=A\nprintf "a\\n"\n')
            project.write("b.sh", 'VALUE=B\nprintf "b\\n"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_HELPER": "load_a"})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [(edge["xtrace"]["function"], Path(edge["resolved_path"]).name) for edge in graph["edges"]],
                [("load_b", "b.sh"), ("load_a", "a.sh")],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"MODASH_TEST_HELPER": "load_a"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "b\na\ndone:A\n")

    def test_dynamic_helper_name_graph_uses_next_observed_condition_helper_edge(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'load_a() { if source "$1"; then printf "in:a:%s\\n" "$VALUE"; fi; }',
                    'load_b() { if source "$1"; then printf "in:b:%s\\n" "$VALUE"; fi; }',
                    "load_b ./b.sh",
                    '"$MODASH_TEST_HELPER" ./a.sh',
                    'printf "done:%s\\n" "$VALUE"',
                    "",
                ]),
            )
            project.write("a.sh", 'VALUE=A\nprintf "a\\n"\n')
            project.write("b.sh", 'VALUE=B\nprintf "b\\n"\n')
            trace = project.trace("main.sh", env={"MODASH_TEST_HELPER": "load_a"})
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            compiled = project.path("compiled.sh")
            write_observed_source_graph(graph, graph_path)

            self.assertEqual(
                [(edge["xtrace"]["function"], Path(edge["resolved_path"]).name) for edge in graph["edges"]],
                [("load_b", "b.sh"), ("load_a", "a.sh")],
            )
            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env={"MODASH_TEST_HELPER": "load_a"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "b\nin:b:B\na\nin:a:A\ndone:A\n")

    def test_dynamic_helper_name_static_compile_remains_fail_closed_without_graph(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'printf "dep\\n"\n')
            project.write(
                "main.sh",
                "\n".join([
                    'load_one() { source "$1"; }',
                    '"$MODASH_TEST_HELPER" "$MODASH_TEST_TARGET_ONE"',
                    "",
                ]),
            )
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "dynamic function dispatch") as context:
                project.compile(
                    "main.sh",
                    output=output,
                    mode="executable",
                )

        self.assertEqual(context.exception.diagnostic.code, "unsupported.source.function-dispatch")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
