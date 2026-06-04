from __future__ import annotations

import unittest

from methods.runtime_evaluator.graph import build_observed_source_graph, write_observed_source_graph
from methods.runtime_evaluator.graph_model import RuntimeSourceGraphError
from methods.runtime_evaluator.errors import RuntimeSourceTraceError
from methods.runtime_evaluator.compiler_rewrite import _rewrite_bash_c_payloads
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

    def assert_compile_observed_error(self, project: ScriptProject, entry: str, *, code: str, env=None):
        entrypoint = project.path(entry)
        trace = project.trace(entry, env=env)
        self.assertEqual(trace.returncode, 0, trace.stderr)
        graph = build_observed_source_graph(entrypoint, trace.observation)
        graph_path = project.path("graph/runtime-source-graph.json")
        output = project.path("compiled.sh")
        write_observed_source_graph(graph, graph_path)
        with self.assertRaises(RuntimeSourceGraphError) as context:
            compile_observed_main(str(entrypoint), str(output), graph=str(graph_path))
        self.assertEqual(context.exception.code, code)
        self.assertFalse(output.exists())

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
        self.assertEqual(diverged.stdout.count("unobserved or over-consumed source edge"), 1)

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

    def test_runtime_compiler_rejects_source_file_mutated_during_trace(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write(
                "dep.sh",
                "printf 'old\\n'\n"
                "cat > ./dep.sh <<'EOF'\n"
                "printf 'new\\n'\n"
                "EOF\n",
            )
            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.mutated-source")

    def test_runtime_compiler_rejects_source_file_mutated_after_source_returns(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "source ./dep.sh\n"
                "cat > ./dep.sh <<'EOF'\n"
                "printf 'new\\n'\n"
                "EOF\n",
            )
            project.write("dep.sh", "printf 'old\\n'\n")
            trace = project.trace("main.sh")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, trace.observation)

        self.assertEqual(context.exception.code, "runtime.graph.stale_observation")

    def test_runtime_compiler_exit_trap_cannot_bypass_unconsumed_edge_validation(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "trap 'printf \"user-exit\\n\"' EXIT\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then\n"
                "  source ./two.sh\n"
                "fi\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"RUN_TWO": "yes"})
            observed = project.run(compiled, env={"RUN_TWO": "yes"})
            diverged = project.run(compiled, env={"RUN_TWO": "no"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertIn("user-exit\n", observed.stdout)
        self.assertEqual(diverged.returncode, 125, diverged.stdout)
        self.assertIn("unconsumed observed source edge", diverged.stdout)

    def test_runtime_compiler_rejects_trace_disabled_bash_c_source_payload(self):
        cases = {
            "env unset": "env -u BASH_ENV bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "empty assignment": "BASH_ENV= bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "bash option": "BASH_ENV= bash --noprofile -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "command wrapper": "BASH_ENV= command bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "env unset bash option": "env -u BASH_ENV bash --noprofile -c 'source ./dep.sh'\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.unobserved_child_source",
                )

    def test_runtime_compiler_rejects_unsupported_bash_c_payloads(self):
        cases = {
            "ansi c quote": "BASH_ENV= bash -c $'source ./dep.sh'\nprintf 'done\\n'\n",
            "dynamic payload": "printf 'source ./dep.sh\\n' > payload.txt\nBASH_ENV= bash -c \"$(cat payload.txt)\"\nprintf 'done\\n'\n",
            "multiline payload": "BASH_ENV= bash -c '\nsource ./dep.sh\n'\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.unsupported_child_bash",
                )

    def test_runtime_compiler_rewrites_env_wrapped_traced_bash_c_source_payload(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "env bash -c 'source ./dep.sh child; printf \"after:%s\\n\" \"$1\"' zero one\n",
            )
            project.write("dep.sh", "printf 'dep:%s:%s\\n' \"$0\" \"$1\"\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:zero:child\nafter:one\n")

    def test_runtime_compiler_payload_scanner_ignores_bash_c_heredoc_source_text(self):
        command = "BASH_ENV= bash -c 'cat <<EOF\nsource ./not-a-command.sh\nEOF'"

        self.assertEqual(_rewrite_bash_c_payloads(command, {}), command)

    def test_runtime_compiler_rejects_multiline_bash_c_string_even_when_source_text_is_quoted(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "BASH_ENV= bash -c 'cat <<\"EOF\"\n"
                "source ./not-a-command.sh\n"
                "EOF'\n"
                "printf 'done\\n'\n",
            )
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unsupported_child_bash",
            )

    def test_runtime_compiler_rejects_function_context_sensitive_sourced_file(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write(
                "dep.sh",
                "if local x=1 2>/dev/null; then\n"
                "  source ./inside.sh\n"
                "else\n"
                "  source ./top.sh\n"
                "fi\n",
            )
            project.write("inside.sh", "printf 'inside\\n'\n")
            project.write("top.sh", "printf 'top\\n'\n")
            trace = project.trace("main.sh")

            with self.assertRaises(RuntimeSourceGraphError) as context:
                build_observed_source_graph(entrypoint, trace.observation)

        self.assertEqual(context.exception.code, "runtime.graph.function_context_sensitive")

    def test_runtime_compiler_replays_top_level_oneline_compound_source_sites(self):
        cases = {
            "if then": "if true; then source ./dep.sh; fi\nprintf 'main:%s\\n' \"$VALUE\"\n",
            "and": "true && source ./dep.sh\nprintf 'main:%s\\n' \"$VALUE\"\n",
            "or": "false || source ./dep.sh\nprintf 'main:%s\\n' \"$VALUE\"\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "VALUE=loaded\nprintf 'dep\\n'\n")
                compiled, graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled)

            self.assertEqual([edge["xtrace"]["command"] for edge in graph["edges"]], ["source ./dep.sh"])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep\nmain:loaded\n")

    def test_runtime_compiler_ignores_heredoc_source_text_when_scanning_for_live_sources(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "cat <<'EOF'\n"
                "source ./not-a-command.sh\n"
                "EOF\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "source ./not-a-command.sh\ndep\n")

    def test_runtime_compiler_fails_closed_when_observed_child_process_is_skipped(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${RUN_CHILD:-yes} == yes ]]; then\n"
                "  bash -c 'source ./dep.sh'\n"
                "fi\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"RUN_CHILD": "yes"})
            observed = project.run(compiled, env={"RUN_CHILD": "yes"})
            skipped = project.run(compiled, env={"RUN_CHILD": "no"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(observed.stdout, "dep\ndone\n")
        self.assertEqual(skipped.returncode, 125, skipped.stdout)
        self.assertIn("unconsumed observed child process", skipped.stdout)

    def test_runtime_compiler_fails_closed_when_observed_child_process_repeats(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "for i in ${COUNT:-1}; do\n"
                "  bash -c 'source ./dep.sh'\n"
                "done\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"COUNT": "1"})
            observed = project.run(compiled, env={"COUNT": "1"})
            repeated = project.run(compiled, env={"COUNT": "1 2"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(observed.stdout, "dep\n")
        self.assertEqual(repeated.returncode, 125, repeated.stdout)
        self.assertIn("unobserved or over-consumed child process", repeated.stdout)

    def test_runtime_compiler_rejects_exit_trap_bypass_shape(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "trap 'printf \"user-exit\\n\"' EXIT\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "exit 0\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.exit_trap",
                env={"RUN_TWO": "yes"},
            )

    def test_runtime_compiler_rejects_reserved_namespace_collision(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "__modash_edge_keys=()\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.reserved_namespace",
                env={"RUN_TWO": "yes"},
            )

    def test_runtime_compiler_rejects_trace_instrumentation_sensitive_scripts(self):
        cases = {
            "alias expansion": "alias setdep='DEP=./dep.sh'\nsetdep\nsource \"$DEP\"\n",
            "xtrace flag": "if [[ $- == *x* ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.instrumentation_sensitive",
                )

    def test_runtime_compiler_honors_sourcepath_disabled(self):
        with ScriptProject() as project:
            project.mkdir("lib")
            project.write(
                "main.sh",
                "shopt -u sourcepath\n"
                "PATH=\"$PWD/lib:$PATH\"\n"
                "source dep.sh\n"
                "printf 'status:%s\\n' \"$?\"\n",
            )
            project.write("lib/dep.sh", "printf 'dep\\n'\n")
            compiled, graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertTrue(graph["edges"][0]["to"].startswith("missing-source:"))
        self.assertEqual(graph["edges"][0]["status"], 1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("dep\n", result.stdout)
        self.assertIn("dep.sh: No such file or directory\n", result.stdout)
        self.assertTrue(result.stdout.endswith("status:1\n"), result.stdout)

    def test_runtime_compiler_rejects_source_redirections(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh > out.txt\n"
                "printf 'after\\n'\n"
                "cat out.txt\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.source_redirection",
            )

    def test_runtime_compiler_preserves_top_level_negated_source_status(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "! source ./dep.sh\n"
                "printf 'status:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\nreturn 7\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\nstatus:0\n")

    def test_runtime_compiler_rejects_dynamic_eval(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "eval \"$CMD\"\n"
                "printf 'done\\n'\n",
            )
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_eval",
                env={"CMD": "printf trace\\n"},
            )

    def test_runtime_compiler_allows_shopt_restore_eval(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "\n".join([
                    "source_safe() {",
                    "  local shellopts=$(shopt -p extglob)",
                    "  shopt -u extglob",
                    '  source "$1"',
                    '  eval "$shellopts"',
                    "}",
                    "shopt -s extglob",
                    "source_safe ./dep.sh",
                    'if shopt -q extglob; then printf "extglob:on\\n"; else printf "extglob:off\\n"; fi',
                    "",
                ]),
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\nextglob:on\n")

    def test_runtime_compiler_fails_closed_on_source_status_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${DISABLE:-} ]]; then enable -n source; fi\n"
                "source ./dep.sh || printf 'handled:%s\\n' \"$?\"\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            observed = project.run(compiled)
            drifted = project.run(compiled, env={"DISABLE": "1"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(observed.stdout, "dep\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed source status drift", drifted.stdout)

    def test_runtime_compiler_rewrites_bash_source_in_child_process_sourced_files(self):
        with ScriptProject() as project:
            dep = project.write(
                "dep.sh",
                "printf 'bs:%s\\n' \"${BASH_SOURCE[0]}\"\n"
                "printf 'zero:%s\\n' \"$0\"\n",
            )
            project.write("main.sh", "bash -c 'source ./dep.sh' child0\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"bs:{dep}\n", result.stdout)
        self.assertIn("zero:child0\n", result.stdout)

    def test_runtime_compiler_rejects_runtime_references_inside_heredoc(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "cat <<EOF\nbs:${BASH_SOURCE[0]}\nEOF\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.runtime_reference_heredoc",
            )

    def test_runtime_compiler_rejects_runtime_references_on_child_bash_lines(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "printf 'bs:%s\\n' \"${BASH_SOURCE[0]}\"; bash -c 'printf child\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.runtime_reference_child_bash",
            )


if __name__ == "__main__":
    unittest.main()
