from __future__ import annotations

import os
import shutil
import subprocess
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
        graph_path = project.path("graph/runtime-source-graph.json")
        output = project.path("compiled.sh")
        with self.assertRaises(RuntimeSourceGraphError) as context:
            graph = build_observed_source_graph(entrypoint, trace.observation)
            write_observed_source_graph(graph, graph_path)
            compile_observed_main(str(entrypoint), str(output), graph=str(graph_path))
        self.assertEqual(context.exception.code, code)
        self.assertFalse(output.exists())

    def assert_trace_error(self, project: ScriptProject, entry: str, *, code: str, env=None):
        with self.assertRaises(RuntimeSourceTraceError) as context:
            project.trace(entry, env=env)
        self.assertEqual(context.exception.code, code)

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
            env = {"DEP": str(dep)}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            drifted = project.run(compiled, env={"DEP": "/missing/not-used"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:from-main:A:B\nafter:from-dep:A:B\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed environment drift: DEP", drifted.stdout)

    def test_runtime_compiler_records_source_relevant_inherited_environment(self):
        with ScriptProject() as project:
            one = project.write("one.sh", "printf 'one\\n'\n")
            two = project.write("two.sh", "printf 'two\\n'\n")
            project.write("main.sh", "source \"$DEP\"\nprintf 'done\\n'\n")
            old_dep = os.environ.get("DEP")
            os.environ["DEP"] = str(one)
            try:
                compiled, graph = self.compile_observed(project, "main.sh")
            finally:
                if old_dep is None:
                    os.environ.pop("DEP", None)
                else:
                    os.environ["DEP"] = old_dep
            matched = project.run(compiled, env={"DEP": str(one)})
            drifted = project.run(compiled, env={"DEP": str(two)})

        self.assertEqual(graph["environment"]["policy"], "inherit")
        self.assertEqual(graph["environment"]["values"], {"DEP": str(one)})
        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "one\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed environment drift: DEP", drifted.stdout)

    def test_runtime_compiler_evaluates_original_source_argv_side_effects(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source \"$(printf './dep.sh'; touch side-effect)\"\n"
                "[[ -f side-effect ]] && printf 'side:yes\\n' || printf 'side:no\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.path("side-effect").unlink()
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\nside:yes\n")

    def test_runtime_compiler_preserves_source_argument_expansion_status(self):
        with ScriptProject() as project:
            project.write("main.sh", "false\nsource \"$(printf './dep.sh')\"\n")
            project.write("dep.sh", "printf 'inside:%s\\n' \"$?\"\n")
            compiled, graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(graph["edges"][0]["source_entry_status"], 0)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "inside:0\n")

    def test_runtime_compiler_fails_closed_when_source_expression_selects_new_target(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${USE_TWO:-} ]]; then\n"
                "  DEP=./two.sh\n"
                "else\n"
                "  DEP=./one.sh\n"
                "fi\n"
                "source \"$DEP\"\n"
                "printf 'done\\n'\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            matched = project.run(compiled)
            drifted = project.run(compiled, env={"USE_TWO": "1"})

        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "one\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed source path drift", drifted.stdout)
        self.assertNotIn("two\n", drifted.stdout)

    def test_runtime_compiler_resolves_relative_sources_against_physical_cwd_not_pwd(self):
        with ScriptProject() as project:
            project.mkdir("fake")
            project.write(
                "main.sh",
                f"PWD={project.path('fake')}\n"
                "source ./dep.sh\n"
                "printf 'pwd:%s\\n' \"$PWD\"\n",
            )
            project.write("dep.sh", "printf 'real-dep\\n'\n")
            project.write("fake/dep.sh", "printf 'fake-dep\\n'\n")
            compiled, graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(graph["edges"][0]["resolved_path"], str(project.path("dep.sh")))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, f"real-dep\npwd:{project.path('fake')}\n")

    def test_runtime_compiler_preserves_assignment_prefixed_source_scope(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "VAR=old\n"
                "VAR=foo source ./dep.sh\n"
                "printf 'after:%s\\n' \"$VAR\"\n",
            )
            project.write(
                "dep.sh",
                "printf 'dep:%s status:%s\\n' \"$VAR\" \"$?\"\n"
                "VAR=bar\n",
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:foo status:0\nafter:old\n")

    def test_runtime_compiler_preserves_assignment_prefixed_source_export_visibility(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "VAR=old\n"
                "VAR=foo source ./dep.sh\n"
                "printf 'after:%s\\n' \"$VAR\"\n",
            )
            project.write(
                "dep.sh",
                "printf 'shell:%s\\n' \"$VAR\"\n"
                "bash -c 'printf \"env:%s\\n\" \"$VAR\"'\n",
            )
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_assignment_prefixed_source_array_scope(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "A=(x y)\n"
                "A=foo source ./dep.sh\n"
                "printf 'after:%s len:%s\\n' \"${A[*]}\" \"${#A[@]}\"\n",
            )
            project.write("dep.sh", "printf 'inside:%s len:%s\\n' \"${A[*]}\" \"${#A[@]}\"\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_assignment_prefixed_source_readonly_behavior(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "readonly VAR=old\n"
                "VAR=foo source ./dep.sh\n"
                "printf 'after:%s status:%s\\n' \"$VAR\" \"$?\"\n",
            )
            project.write("dep.sh", "printf 'dep:%s status:%s\\n' \"$VAR\" \"$?\"\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertIn("VAR: readonly variable", actual.stdout)
        self.assertTrue(actual.stdout.endswith("dep:old status:0\nafter:old status:0\n"), actual.stdout)

    def test_runtime_compiler_preserves_assignment_prefixed_sourcepath_lookup(self):
        with ScriptProject() as project:
            project.mkdir("lib")
            project.write(
                "main.sh",
                "PATH=old\n"
                "PATH=\"$PWD/lib\" source dep.sh\n"
                "printf 'after:%s\\n' \"$PATH\"\n",
            )
            project.write("lib/dep.sh", "printf 'dep:%s\\n' \"$PATH\"\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, f"dep:{project.root}/lib\nafter:old\n")

    def test_runtime_compiler_preserves_assignment_command_substitution_source_entry_status(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "false\n"
                "VAR=$(false) source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'inside:%s var:%s\\n' \"$?\" \"$VAR\"\n")
            compiled, graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(graph["edges"][0]["source_entry_status"], 1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "inside:1 var:\n")

    def test_runtime_compiler_fails_closed_on_assignment_source_argument_status_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "VAR=foo source \"$(printf './dep.sh'; [[ -f flag ]])\"\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'inside:%s var:%s\\n' \"$?\" \"$VAR\"\n")
            project.write("flag", "")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.path("flag").unlink()
            result = project.run(compiled)

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed source argument status drift", result.stdout)
        self.assertNotIn("inside:1", result.stdout)

    def test_runtime_compiler_fails_closed_on_assignment_rhs_status_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "VAR=\"$(printf foo; [[ -f flag ]])\" source ./dep.sh\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'inside:%s var:%s\\n' \"$?\" \"$VAR\"\n")
            project.write("flag", "")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.path("flag").unlink()
            result = project.run(compiled)

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed source argument status drift", result.stdout)
        self.assertNotIn("inside:1", result.stdout)

    def test_runtime_compiler_fails_closed_on_assignment_source_redirection_status_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "VAR=foo source ./dep.sh > \"$(printf out.txt; [[ -f flag ]])\"\n"
                "cat out.txt\n",
            )
            project.write("dep.sh", "printf 'inside:%s var:%s\\n' \"$?\" \"$VAR\"\n")
            project.write("flag", "")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.path("flag").unlink()
            project.path("out.txt").unlink()
            result = project.run(compiled)

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed source argument status drift", result.stdout)
        self.assertNotIn("inside:1", result.stdout)

    def test_runtime_compiler_preserves_redirection_target_source_entry_status(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "false\n"
                "source ./dep.sh > \"$(false; echo out.txt)\"\n"
                "cat out.txt\n",
            )
            project.write("dep.sh", "printf 'inside:%s\\n' \"$?\"\n")
            expected = project.run("main.sh")
            compiled, graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(graph["edges"][0]["source_entry_status"], 0)
        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_fails_closed_on_source_output_redirection_drift(self):
        with ScriptProject() as project:
            project.mkdir("outdir")
            project.write(
                "main.sh",
                "source ./dep.sh > outdir/out.txt\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            shutil.rmtree(project.path("outdir"))
            result = project.run(compiled)

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed source redirection drift", result.stdout)
        self.assertNotIn("numeric argument required", result.stdout)

    def test_runtime_compiler_fails_closed_on_source_input_redirection_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh < \"$INPUT\"\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "read -r value\nprintf 'dep:%s\\n' \"$value\"\n")
            project.write("input.txt", "value\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"INPUT": "input.txt"})
            project.path("input.txt").unlink()
            result = project.run(compiled, env={"INPUT": "input.txt"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed source redirection drift", result.stdout)
        self.assertNotIn("numeric argument required", result.stdout)

    def test_runtime_compiler_fails_closed_on_sourcepath_path_drift(self):
        with ScriptProject() as project:
            one = project.mkdir("lib1")
            two = project.mkdir("lib2")
            project.write("main.sh", "source dep.sh\nprintf 'done\\n'\n")
            project.write("lib1/dep.sh", "printf 'one\\n'\n")
            project.write("lib2/dep.sh", "printf 'two\\n'\n")
            base_path = os.environ.get("PATH", "")
            compiled, _graph = self.compile_observed(
                project,
                "main.sh",
                env={"PATH": f"{one}:{base_path}"},
            )
            matched = project.run(compiled, env={"PATH": f"{one}:{base_path}"})
            drifted = project.run(compiled, env={"PATH": f"{two}:{base_path}"})

        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "one\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed environment drift: PATH", drifted.stdout)
        self.assertNotIn("two\n", drifted.stdout)

    def test_runtime_compiler_fails_closed_on_home_tilde_source_drift(self):
        with ScriptProject() as project:
            home1 = project.mkdir("home1")
            home2 = project.mkdir("home2")
            project.write("main.sh", "source ~/dep.sh\nprintf 'done\\n'\n")
            project.write("home1/dep.sh", "printf 'one\\n'\n")
            project.write("home2/dep.sh", "printf 'two\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"HOME": str(home1)})
            matched = project.run(compiled, env={"HOME": str(home1)})
            drifted = project.run(compiled, env={"HOME": str(home2)})

        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "one\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed environment drift: HOME", drifted.stdout)
        self.assertNotIn("two\n", drifted.stdout)

    def test_runtime_compiler_fails_closed_on_unset_default_source_drift(self):
        with ScriptProject() as project:
            project.write("main.sh", "source \"${DEP:-./one.sh}\"\nprintf 'done\\n'\n")
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            old_dep = os.environ.pop("DEP", None)
            try:
                compiled, _graph = self.compile_observed(project, "main.sh")
            finally:
                if old_dep is not None:
                    os.environ["DEP"] = old_dep
            matched = project.run(compiled)
            drifted = project.run(compiled, env={"DEP": "./two.sh"})

        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "one\ndone\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed source path drift", drifted.stdout)
        self.assertNotIn("two\n", drifted.stdout)

    def test_runtime_compiler_restores_cdpath_behavior_after_secure_startup(self):
        with ScriptProject() as project:
            cdpath_root = project.mkdir("cdpath")
            project.mkdir("cdpath/target")
            project.write(
                "main.sh",
                "cd target\n"
                "source dep.sh\n"
                "printf 'pwd:%s\\n' \"$PWD\"\n",
            )
            project.write("cdpath/target/dep.sh", "printf 'depA\\n'\n")
            env = {"CDPATH": str(cdpath_root)}
            expected = project.run("main.sh", env=env)
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            actual = project.run(compiled, env=env)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_restores_cdpath_behavior_in_source_free_child_bash(self):
        with ScriptProject() as project:
            cdpath_root = project.mkdir("cdpath")
            project.mkdir("cdpath/target")
            project.write(
                "main.sh",
                "bash -c 'cd target; printf \"childpwd:%s\\n\" \"$PWD\"'\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            env = {"CDPATH": str(cdpath_root)}
            expected = project.run("main.sh", env=env)
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            actual = project.run(compiled, env=env)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_lineno_in_entrypoint_and_child_bash(self):
        cases = {
            "entrypoint": "printf 'mainline:%s\\n' \"$LINENO\"\nsource ./dep.sh\n",
            "source-free child": "bash -c 'printf \"childline:%s\\n\" \"$LINENO\"'\nsource ./dep.sh\n",
            "source-bearing child": "bash -c 'printf \"childline:%s\\n\" \"$LINENO\"; source ./dep.sh'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                expected = project.run("main.sh")
                compiled, _graph = self.compile_observed(project, "main.sh")
                actual = project.run(compiled)

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_prior_status_before_dynamic_command_guard(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "helper() { printf 'prior:%s\\n' \"$?\"; }\n"
                "cmd=helper\n"
                "false\n"
                "\"$cmd\"\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "prior:1\ndep\n")

    def test_runtime_compiler_preserves_dynamic_command_control_flow_and_pipelines(self):
        cases = {
            "short circuit":
                "cmd=printf\n"
                "false && \"$cmd\" 'SHOULD-NOT\\n'\n"
                "source ./dep.sh\n",
            "pipeline":
                "cmd=printf\n"
                "\"$cmd\" 'pipe\\n' | cat\n"
                "source ./dep.sh\n",
            "array command substitution":
                "cmd=printf\n"
                "arr=( $(\"$cmd\" hi) )\n"
                "printf 'arr:%s\\n' \"${arr[*]}\"\n"
                "source ./dep.sh\n",
            "continued option argument":
                "tool() { printf 'tool:%s:%s\\n' \"$1\" \"$2\"; }\n"
                "VALUE=two\n"
                "tool one \\\n"
                "  -flag=\"$VALUE\"\n"
                "source ./dep.sh\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                expected = project.run("main.sh")
                compiled, _graph = self.compile_observed(project, "main.sh")
                actual = project.run(compiled)

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(actual.stdout, expected.stdout)
            self.assertNotIn("SHOULD-NOT", actual.stdout)

    def test_runtime_compiler_evaluates_dynamic_command_arguments_once(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "cmd=printf\n"
                "printf 0 > count\n"
                "\"$cmd\" 'arg:%s\\n' \"$(n=$(cat count); printf '%s' $((n + 1)) > count; printf value)\"\n"
                "printf 'count:%s\\n' \"$(cat count)\"\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.write("count", "0")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_evaluates_dynamic_command_redirections_once(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "cmd=printf\n"
                "printf 0 > count\n"
                "\"$cmd\" 'body\\n' > \"$(n=$(cat count); printf '%s' $((n + 1)) > count; printf out.txt)\"\n"
                "cat out.txt\n"
                "printf 'count:%s\\n' \"$(cat count)\"\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            project.write("count", "0")
            project.path("out.txt").unlink()
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_dynamic_command_process_substitution_arguments(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh\n"
                "cmd=cat\n"
                "\"$cmd\" <(printf 'psub\\n')\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_preserves_dynamic_command_process_substitution_arguments_in_child_bash(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'source ./dep.sh; cmd=cat; \"$cmd\" <(printf \"psub\\\\n\"); printf \"after:%s\\\\n\" \"$?\"'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

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

    def test_runtime_compiler_rejects_dynamic_command_that_can_become_eval_source(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${RUN:-} ]]; then\n"
                "  CMD=eval\n"
                "else\n"
                "  CMD=echo\n"
                "fi\n"
                "\"$CMD\" 'builtin source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep-dyncmd\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_command",
            )

    def test_runtime_compiler_rejects_inert_function_dynamic_source_dispatch(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./lib.sh\n"
                "if [[ ${RUN:-} ]]; then\n"
                "  X=builtin\n"
                "  CMD=source\n"
                "  evil\n"
                "fi\n"
                "printf 'done\\n'\n",
            )
            project.write(
                "lib.sh",
                "evil() {\n"
                "  \"$X\" \"$CMD\" ./extra.sh\n"
                "}\n",
            )
            project.write("extra.sh", "printf 'extra-live\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_command",
            )

    def test_runtime_compiler_rejects_inert_function_shell_source_callbacks(self):
        cases = {
            "eval literal": ("evil() { \"$CMD\" 'builtin source ./extra.sh'; }\n", "runtime.compile.dynamic_command"),
            "mapfile callback": ("evil() { mapfile -C 'builtin source ./extra.sh' -c 1 arr; }\n", "runtime.compile.dynamic_state"),
            "mapfile attached callback": ("evil() { mapfile -C'builtin source ./extra.sh' -c 1 arr; }\n", "runtime.compile.dynamic_state"),
            "readarray attached callback": ("evil() { readarray -C'builtin source ./extra.sh' -c 1 arr; }\n", "runtime.compile.dynamic_state"),
            "non-bash shell": ("evil() { sh -c '. ./extra.sh'; }\n", "runtime.compile.dynamic_command"),
        }
        for name, (lib_script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./lib.sh\nprintf 'done\\n'\n")
                project.write("lib.sh", lib_script)
                project.write("extra.sh", "printf 'extra-live\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                )

    def test_runtime_compiler_rejects_inert_function_computed_replay_state_mutation(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./lib.sh\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then\n"
                "  source ./two.sh\n"
                "fi\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  mark_consumed 'p0:__entrypoint__:4:0|0'\n"
                "fi\n",
            )
            project.write(
                "lib.sh",
                "mark_consumed() {\n"
                "  p=__mod\n"
                "  q=ash_edge_consumed\n"
                "  target=$p$q[$1]\n"
                "  printf -v \"$target\" 1\n"
                "}\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_state",
            )

    def test_runtime_compiler_rejects_attached_printf_v_replay_state_mutation(self):
        cases = {
            "direct": 'printf -v"$target" 1\n',
            "command": 'command printf -v"$target" 1\n',
        }
        for name, mutation in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    "source ./one.sh\n"
                    "if [[ ${RUN_TWO:-yes} == yes ]]; then\n"
                    "  source ./two.sh\n"
                    "fi\n"
                    "if [[ ${MUTATE:-} ]]; then\n"
                    "  p=__mod\n"
                    "  q='ash_edge_consumed[p0:__entrypoint__:3:0|0]'\n"
                    "  target=$p$q\n"
                    f"  {mutation}"
                    "fi\n",
                )
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                )

    def test_runtime_compiler_guards_inert_function_dynamic_eval_source_dispatch(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./lib.sh\n"
                "if [[ ${RUN:-} ]]; then\n"
                "  X=eval\n"
                "  Y='builtin source ./extra.sh'\n"
                "  evil\n"
                "fi\n"
                "printf 'done\\n'\n",
            )
            project.write(
                "lib.sh",
                "evil() {\n"
                "  \"$X\" \"$Y\"\n"
                "}\n",
            )
            project.write("extra.sh", "printf 'extra-live\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            normal = project.run(compiled)
            hostile = project.run(compiled, env={"RUN": "1"})

        self.assertEqual(normal.returncode, 0, normal.stdout)
        self.assertEqual(normal.stdout, "done\n")
        self.assertEqual(hostile.returncode, 125, hostile.stdout)
        self.assertIn("runtime replay cannot allow live eval", hostile.stdout)
        self.assertNotIn("extra-live", hostile.stdout)

    def test_runtime_compiler_does_not_guard_arithmetic_or_array_assignments_as_dynamic_commands(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./lib.sh\n"
                "collect value\n"
                "source ./base.sh\n",
            )
            project.write(
                "lib.sh",
                "collect() {\n"
                "  local -a values=()\n"
                "  local k=0\n"
                "  values[k++]=$1\n"
                "  if ((${#values[@]} == 1)); then\n"
                "    printf 'values:%s\\n' \"${values[0]}\"\n"
                "  fi\n"
                "}\n",
            )
            project.write("base.sh", "printf 'base\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "values:value\nbase\n")

    def test_runtime_compiler_rejects_literal_eval_array_subscript_source(self):
        cases = {
            "echo": "eval echo \\${arr[$PAYLOAD]}\n",
            "colon": "eval : \\${arr[$PAYLOAD]}\n",
        }
        for name, eval_line in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    "if [[ ${RUN:-} ]]; then\n"
                    "  PAYLOAD='$(builtin source ./dep.sh)'\n"
                    "else\n"
                    "  PAYLOAD=0\n"
                    "fi\n"
                    f"{eval_line}"
                    "[[ -f marker ]] && printf 'MARKER\\n'\n"
                    "printf 'done\\n'\n",
                )
                project.write("dep.sh", "touch marker\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_eval",
                )

    def test_runtime_compiler_replays_empty_sourced_file_without_internal_error(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "printf 'before\\n'\n"
                "source ./empty.sh\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("empty.sh", "")
            compiled, _graph = self.compile_observed(project, "main.sh")
            expected = project.run(project.path("main.sh"))
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stderr)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertEqual(actual.stderr, expected.stderr)

    def test_runtime_compiler_hides_trace_environment_from_external_computed_probe(self):
        cases = {
            "computed getenv": 'os.getenv("BASH"+"_ENV")',
            "getattr environ": 'getattr(os,"en"+"viron").get("BA"+"SH"+"_ENV")',
            "chr environ": 'getattr(os,"".join(map(chr,[101,110,118,105,114,111,110]))).get("".join(map(chr,[66,65,83,72,95,69,78,86])))',
        }
        for name, probe in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    f"if python3 -c 'import os,sys; sys.exit(0 if {probe} else 1)'; then\n"
                    "  source ./dep.sh\n"
                    "fi\n"
                    "printf 'done\\n'\n",
                )
                project.write("dep.sh", "printf 'dep-traceonly\\n'\n")
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("dep-traceonly", result.stdout)
            self.assertTrue(result.stdout.endswith("done\n"), result.stdout)

    def test_runtime_compiler_rejects_find_exec_non_bash_shell_source_payload(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "find . -maxdepth 0 -exec sh -c '. ./dep.sh' \\;\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep-find\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unrewritten_source",
            )

    @unittest.skipUnless(shutil.which("busybox"), "busybox is not installed")
    def test_runtime_compiler_rejects_busybox_shell_source_payload(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "busybox sh -c '. ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep-busybox\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unrewritten_source",
            )

    def test_runtime_compiler_rejects_child_replay_marker_forgery_probe(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'if [[ ${RUN_CHILD:-yes} == yes ]]; then source ./dep.sh; fi; "
                "python3 -c '\"'\"'import os,re;m=os.getenv(\"__mod\"+\"ash_child_replay_marker\");"
                "cmd=open(\"/proc/%s/cmdline\"%os.getppid(),\"rb\").read();"
                "match=re.search(b\"child_replay_token=([0-9a-f]+)\",cmd);"
                "m and match and open(m,\"w\").write(match.group(1).decode()+\"\\\\n\")'\"'\"''\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep-child\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.instrumentation_sensitive",
            )

    def test_runtime_compiler_allows_guarded_background_cleanup_kill(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "sleep 5 & bg=$!\n"
                "source ./dep.sh\n"
                "kill \"$bg\" 2>/dev/null || true\n"
                "wait \"$bg\" 2>/dev/null || true\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")

    def test_runtime_compiler_guarded_kill_aborts_current_shell_target(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh\n"
                "if [[ ${RUN_KILL:-} ]]; then kill \"$$\"; fi\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"RUN_KILL": "1"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertNotIn("done\n", result.stdout)
        self.assertIn("runtime replay cannot allow kill target", result.stdout)

    def test_runtime_compiler_rejects_guard_bypassing_kill_forms(self):
        cases = {
            "builtin": "if [[ ${RUN_KILL:-} ]]; then builtin kill \"$$\"; fi\nsource ./dep.sh\n",
            "command": "if [[ ${RUN_KILL:-} ]]; then command kill \"$$\"; fi\nsource ./dep.sh\n",
            "env": "if [[ ${RUN_KILL:-} ]]; then env kill \"$$\"; fi\nsource ./dep.sh\n",
            "command env": "if [[ ${RUN_KILL:-} ]]; then command env kill \"$$\"; fi\nsource ./dep.sh\n",
            "nice": "if [[ ${RUN_KILL:-} ]]; then nice kill \"$$\"; fi\nsource ./dep.sh\n",
            "timeout": "if [[ ${RUN_KILL:-} ]]; then timeout 5 kill \"$$\"; fi\nsource ./dep.sh\n",
            "setsid": "if [[ ${RUN_KILL:-} ]]; then setsid kill \"$$\"; fi\nsource ./dep.sh\n",
            "nohup": "if [[ ${RUN_KILL:-} ]]; then nohup kill \"$$\"; fi\nsource ./dep.sh\n",
            "absolute": "if [[ ${RUN_KILL:-} ]]; then /bin/kill \"$$\"; fi\nsource ./dep.sh\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_command",
                )

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
            env = {"DEP": str(dep)}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            result = project.run(compiled, env=env)

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
            env = {"DEP": str(dep)}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            result = project.run(compiled, env=env)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"source:{dep}\n", result.stdout)
        self.assertIn(f"zero:{project.path('main.sh')}\n", result.stdout)

    def test_runtime_compiler_preserves_top_level_return_status_from_sourced_file(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'printf "dep-before-return\\n"\nreturn 7\nprintf "never\\n"\n')
            project.write("main.sh", 'source "$DEP" || printf "status:%s\\n" "$?"\n')
            env = {"DEP": str(dep)}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            result = project.run(compiled, env=env)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep-before-return\nstatus:7\n")

    def test_runtime_compiler_preserves_prior_status_at_source_entry(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "false\n"
                "source ./dep.sh\n"
                "printf 'after:%s\\n' \"$?\"\n",
            )
            project.write("dep.sh", "printf 'inside:%s\\n' \"$?\"\n")
            expected = project.run("main.sh")
            trace = project.trace("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(expected.stdout, "inside:1\nafter:0\n")
        self.assertEqual(trace.stdout, expected.stdout)
        self.assertEqual(result.returncode, expected.returncode, result.stdout)
        self.assertEqual(result.stdout, expected.stdout)

    def test_runtime_trace_uses_prior_status_for_source_edge_selection(self):
        with ScriptProject() as project:
            project.write("main.sh", "false\nsource ./dep.sh\n")
            project.write(
                "dep.sh",
                "if (($?)); then source ./err.sh; else source ./ok.sh; fi\n",
            )
            project.write("err.sh", "printf 'err\\n'\n")
            project.write("ok.sh", "printf 'ok\\n'\n")
            compiled, graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual([edge["resolved_path"].rsplit("/", 1)[-1] for edge in graph["edges"]], ["dep.sh", "err.sh"])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "err\n")

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
            env = {"DEP": str(dep)}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            result = project.run(compiled, env=env)

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

    def test_runtime_trace_rejects_multiple_observed_versions_of_same_source_path(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh\n"
                "cat > ./dep.sh <<'EOF'\n"
                "printf 'new\\n'\n"
                "EOF\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'old\\n'\n")

            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh")

        self.assertEqual(context.exception.code, "runtime.trace.source_version_drift")

    def test_runtime_compiler_uses_trusted_prelude_tools_under_hostile_environment(self):
        cases = {
            "path base64": (
                {"PATH": None},
                ("fakebin/base64", "#!/bin/bash\nprintf \"printf 'evil-path\\\\n'\\\\n\"\n"),
            ),
            "exported base64 function": (
                {"BASH_FUNC_base64%%": "() { printf \"printf 'evil-function\\\\n'\\\\n\"; }"},
                None,
            ),
            "exported builtin function": (
                {"BASH_FUNC_builtin%%": "() { return 0; }"},
                None,
            ),
        }
        for name, (env, fake_tool) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./dep.sh\nprintf 'done\\n'\n")
                project.write("dep.sh", "printf 'dep\\n'\n")
                if fake_tool is not None:
                    path, content = fake_tool
                    fake = project.write(path, content, executable=True)
                    env = {**env, "PATH": f"{fake.parent}:{project._merged_env({})['PATH']}"}
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled, env={key: value for key, value in env.items() if value is not None})

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep\ndone\n")

    def test_runtime_compiler_plain_bash_launch_cannot_be_enabled_by_environment(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\nprintf 'done\\n'\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(
                compiled,
                env={
                    "__modash_runtime_graph_requires_privileged_bash": "1",
                    "BASH_FUNC_builtin%%": "() { return 0; }",
                },
            )
            run = subprocess.run(
                ["bash", str(compiled)],
                cwd=str(project.root),
                env={
                    **os.environ,
                    "__modash_runtime_graph_requires_privileged_bash": "1",
                    "BASH_FUNC_builtin%%": "() { return 0; }",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("must be executed directly or with bash -p", run.stdout)
        self.assertNotIn("done\n", run.stdout)

    def test_runtime_compiler_sources_embedded_payload_without_mutable_replay_file(self):
        with ScriptProject() as project:
            tmpdir = project.mkdir("tmp")
            project.write(
                "main.sh",
                "if [[ ${MUTATE:-} ]]; then\n"
                "  printf 'printf evil\\\\n' 2>/dev/null > \"${TMPDIR:-/tmp}\"/modash-runtime.*/dep.sh || true\n"
                "fi\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"MUTATE": "1", "TMPDIR": str(tmpdir)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\n")

    def test_runtime_compiler_replay_file_race_cannot_replace_embedded_payload(self):
        with ScriptProject() as project:
            tmpdir = project.mkdir("tmp")
            project.write(
                "main.sh",
                "if [[ ${MUTATE:-} ]]; then\n"
                "  (\n"
                "    for _ in {1..500}; do\n"
                "      for f in \"${TMPDIR:-/tmp}\"/modash-runtime.*/dep.sh; do\n"
                "        [[ -e $f ]] && builtin printf \"builtin printf 'evil-race\\\\n'\\\\n\" > \"$f\"\n"
                "      done\n"
                "    done\n"
                "  ) &\n"
                "fi\n"
                "source ./dep.sh\n"
                "builtin printf 'done\\n'\n",
            )
            project.write("dep.sh", "builtin printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"MUTATE": "1"})
            result = project.run(compiled, env={"MUTATE": "1", "TMPDIR": str(tmpdir)})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")

    def test_runtime_compiler_refresh_uses_builtin_printf(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${MUTATE:-} ]]; then\n"
                "  printf() { command printf 'Y29tbWFuZCBwcmludGYgJ2V2aWwtcHJpbnRmXG4nCg=='; }\n"
                "fi\n"
                "source ./dep.sh\n"
                "command printf 'done\\n'\n",
            )
            project.write("dep.sh", "command printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"MUTATE": "1"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")

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
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.exit_trap",
                env={"RUN_TWO": "yes"},
            )

    def test_runtime_compiler_rejects_trace_disabled_bash_c_source_payload(self):
        cases = {
            "env unset": (
                "env -u BASH_ENV bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                "runtime.compile.instrumentation_sensitive",
            ),
            "empty assignment": (
                "BASH_ENV= bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                "runtime.compile.instrumentation_sensitive",
            ),
            "bash option": (
                "BASH_ENV= bash --noprofile -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                "runtime.compile.instrumentation_sensitive",
            ),
            "command wrapper": (
                "BASH_ENV= command bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                "runtime.compile.instrumentation_sensitive",
            ),
            "env unset bash option": (
                "env -u BASH_ENV bash --noprofile -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                "runtime.compile.instrumentation_sensitive",
            ),
        }
        for name, (script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                )

    def test_runtime_compiler_rejects_unsupported_bash_c_payloads(self):
        cases = {
            "ansi c quote": ("bash -c $'source ./dep.sh'\nprintf 'done\\n'\n", "runtime.compile.unsupported_child_bash"),
            "dynamic payload": ("printf 'source ./dep.sh\\n' > payload.txt\nbash -c \"$(cat payload.txt)\"\nprintf 'done\\n'\n", "runtime.compile.unsupported_child_bash"),
            "multiline payload": ("bash -c '\nsource ./dep.sh\n'\nprintf 'done\\n'\n", "runtime.compile.unsupported_child_bash"),
        }
        for name, (script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
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

    def test_runtime_compiler_allows_child_payload_quoted_exact_dynamic_external_command(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'tool=\"printf\"; \"$tool\" child'\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "childdep\n")

    def test_runtime_compiler_payload_scanner_ignores_bash_c_heredoc_source_text(self):
        command = "BASH_ENV= bash -c 'cat <<EOF\nsource ./not-a-command.sh\nEOF'"
        rewritten = _rewrite_bash_c_payloads(command, {})

        self.assertIn("bash -p -c", rewritten)
        self.assertIn("cat <<EOF", rewritten)
        self.assertIn("source ./not-a-command.sh", rewritten)

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

    def test_runtime_compiler_allows_inert_sourced_function_dynamic_external_commands(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh\n"
                "type diff_cmd >/dev/null && printf 'diff=ready\\n'\n"
                "list_tool_variants\n",
            )
            project.write(
                "dep.sh",
                "diff_cmd () {\n"
                '  "$merge_tool_path" "$LOCAL" "$REMOTE"\n'
                "}\n"
                "list_tool_variants () {\n"
                "  echo bc\n"
                "}\n"
                "prompt_helper () {\n"
                "  git check-ignore -q .\n"
                "}\n"
                "unused_loader () {\n"
                '  source "$1"\n'
                "}\n",
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "diff=ready\nbc\n")

    def test_runtime_compiler_rejects_inert_sourced_function_replay_critical_dispatch(self):
        cases = {
            "builtin source": ("helper() { builtin source ./dep.sh; }\n", "runtime.compile.dynamic_command"),
            "command source": ("helper() { command source ./dep.sh; }\n", "runtime.compile.dynamic_command"),
            "generated helper": (
                "helper() { __modash_select_source_edge p0:__entrypoint__:1:0; }\n",
                "runtime.compile.reserved_namespace",
            ),
        }
        for name, (dep_script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./dep.sh\nprintf 'done\\n'\n")
                project.write("dep.sh", dep_script)
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                )

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

    def test_runtime_compiler_rejects_nested_unobserved_live_sources(self):
        cases = {
            "command substitution": (
                "if [[ ${RUN:-} ]]; then\n"
                "  x=$(builtin source ./dep.sh)\n"
                "  printf 'x:%s\\n' \"$x\"\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
            "process substitution": (
                "if [[ ${RUN:-} ]]; then\n"
                "  cat <(command source ./dep.sh)\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
            "backticks": (
                "if [[ ${RUN:-} ]]; then\n"
                "  x=`builtin source ./dep.sh`\n"
                "  printf 'x:%s\\n' \"$x\"\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
            "array assignment command substitution": (
                "if [[ ${RUN:-} ]]; then\n"
                "  arr=($(builtin source ./dep.sh))\n"
                "  printf 'arr:%s\\n' \"${arr[*]}\"\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
            "array assignment process substitution": (
                "if [[ ${RUN:-} ]]; then\n"
                "  arr=(<(builtin source ./dep.sh))\n"
                "  printf 'arr:%s\\n' \"${arr[*]}\"\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
            "heredoc command substitution": (
                "if [[ ${RUN:-} ]]; then\n"
                "  cat <<EOF\n"
                "$(builtin source ./dep.sh)\n"
                "EOF\n"
                "fi\n"
                "printf 'done\\n'\n",
                "runtime.compile.unrewritten_source",
            ),
        }
        for name, (script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                )

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
            compiled, _graph = self.compile_observed(project, "main.sh")
            observed = project.run(compiled)
            skipped = project.run(compiled, env={"RUN_CHILD": "no"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(observed.stdout, "dep\ndone\n")
        self.assertEqual(skipped.returncode, 125, skipped.stdout)
        self.assertIn("unconsumed observed child process", skipped.stdout)

    def test_runtime_compiler_does_not_put_child_replay_token_in_child_payload(self):
        with ScriptProject() as project:
            project.write("main.sh", "bash -c 'source ./dep.sh'\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            content = compiled.read_text()

        self.assertNotIn("child_replay_token=", content)
        self.assertIn("child_replay_token_file", content)

    def test_runtime_compiler_child_source_edge_drift_aborts_parent(self):
        cases = {
            "skipped edge": (
                "bash -c '[[ ${RUN_CHILD:-yes} == yes ]] && source ./dep.sh'\n"
                "printf 'done\\n'\n",
                "printf 'dep\\n'\n",
                {},
                {"RUN_CHILD": "no"},
                "observed child process replay failed",
            ),
            "overconsumed edge": (
                "bash -c 'for i in ${COUNT:-1}; do source ./dep.sh; done'\n"
                "printf 'done\\n'\n",
                "printf 'dep\\n'\n",
                {},
                {"COUNT": "1 2"},
                "observed child process replay failed",
            ),
            "source status drift": (
                "bash -c 'source ./dep.sh'\n"
                "printf 'done\\n'\n",
                "printf 'dep\\n'\n[[ ${FAIL:-} ]] && return 7 || return 0\n",
                {},
                {"FAIL": "1"},
                "observed child process replay failed",
            ),
        }
        for name, (script, dep_content, trace_env, run_env, message) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", dep_content)
                compiled, _graph = self.compile_observed(project, "main.sh", env=trace_env)
                result = project.run(compiled, env=run_env)

            self.assertEqual(result.returncode, 125, result.stdout)
            self.assertIn(message, result.stdout)
            self.assertNotEqual(result.stdout, "done\n")

    def test_runtime_compiler_preserves_child_status_125_when_replay_succeeds(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'source ./dep.sh; exit 125'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")

    def test_runtime_compiler_preserves_child_bash_c_implicit_zero(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'zero:%s\\n' \"$0\"\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "zero:bash\ndone\n")

    def test_runtime_compiler_fails_closed_when_observed_child_process_repeats(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "for i in ${COUNT:-1}; do\n"
                "  bash -c 'source ./dep.sh'\n"
                "done\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            observed = project.run(compiled)
            repeated = project.run(compiled, env={"COUNT": "1 2"})

        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(observed.stdout, "dep\n")
        self.assertEqual(repeated.returncode, 125, repeated.stdout)
        self.assertIn("unobserved or over-consumed child process", repeated.stdout)

    def test_runtime_compiler_rejects_repeated_identical_child_bash_payloads(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'source ./dep.sh'\n"
                "bash -c 'source ./dep.sh'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.ambiguous_child_process",
            )

    def test_runtime_compiler_preserves_handled_child_bash_failure_status(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'source ./missing.sh' || printf 'handled:%s\\n' \"$?\"\n"
                "printf 'done\\n'\n",
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("missing.sh: No such file or directory\n", result.stdout)
        self.assertIn("handled:1\n", result.stdout)
        self.assertTrue(result.stdout.endswith("done\n"), result.stdout)

    def test_runtime_compiler_rejects_computed_replay_state_mutation(self):
        cases = {
            "computed unset": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "a=__mod\n"
                "b=ash_edge_keys\n"
                'unset "$a$b"\n'
            ),
            "computed printf": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "a=__mod\n"
                "b=ash_aborting\n"
                'printf -v "$a$b" 1\n'
            ),
            "computed read": (
                "source ./one.sh\n"
                "a=__mod\n"
                "b=ash_edge_keys\n"
                'read "$a$b" < /dev/null || true\n'
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                    env={"RUN_TWO": "yes"},
                )

    def test_runtime_compiler_rejects_nameref_replay_state_mutation(self):
        cases = {
            "nameref assignment": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "a=__mod\n"
                "b=ash_edge_consumed\n"
                'declare -n m="$a$b"\n'
            ),
            "nameref retarget": (
                "source ./one.sh\n"
                "declare -n m\n"
                "a=__mod\n"
                "b=ash_edge_consumed\n"
                'm="$a$b"\n'
            ),
            "positional nameref": (
                "declare -A safevar=()\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "declare -n m=${1:-safevar}\n"
                "m['p0:__entrypoint__:3:0|0']=1\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                    env={"RUN_TWO": "yes"},
                )

    def test_runtime_compiler_rejects_computed_replay_state_mutation_variants(self):
        cases = {
            "dynamic read target": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "p=SAFE\n"
                "q=VAR\n"
                'target=$p$q\n'
                'read "$target" <<< 1\n'
            ),
            "dynamic let target": (
                "source ./one.sh\n"
                "p=__mod\n"
                "q=ash_edge_seen\n"
                'target=$p$q\n'
                'let "$target=1"\n'
            ),
            "dynamic arithmetic target": (
                "source ./one.sh\n"
                "p=__mod\n"
                "q=ash_edge_seen\n"
                'target=$p$q\n'
                '(( $target=1 ))\n'
            ),
            "unsafe indirect expansion": (
                "source ./one.sh\n"
                "p=__mod\n"
                "q=ash_tmp\n"
                't=$p$q\n'
                'printf "x:%s\\n" "${!t}"\n'
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state" if name != "unsafe indirect expansion" else "runtime.compile.instrumentation_sensitive",
                    env={"RUN_TWO": "yes"},
                )

    def test_runtime_compiler_rejects_unknown_dynamic_dispatch_that_can_drift_to_source(self):
        cases = {
            "array dispatch": (
                'if [[ ${RUN:-} ]]; then cmd=(source ./dep.sh); else cmd=(echo skipped); fi\n'
                '"${cmd[@]}"\n'
                "printf 'done\\n'\n",
                {},
            ),
        }
        for name, (script, trace_env) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_command",
                    env=trace_env,
                )

    def test_runtime_compiler_live_source_guard_blocks_fixed_arg_dynamic_dispatch(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                'CMD=${CMD:-echo}\n'
                '$CMD ./dep.sh\n'
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"CMD": "source"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("unobserved live source command", result.stdout)

    def test_runtime_compiler_allows_inert_source_text_to_safe_dynamic_external_command(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "cmd=echo\n"
                "\"$cmd\" 'source ./not-a-command.sh'\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            expected = project.run("main.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            actual = project.run(compiled)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_runtime_guards_fixed_arg_dynamic_exec_and_trap_dispatch(self):
        cases = {
            "exec": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "CMD=${CMD:-echo}\n"
                "$CMD true\n"
            ),
            "trap": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "CMD=${CMD:-echo}\n"
                "$CMD 'printf user-exit\\\\n' EXIT\n"
                "exit 0\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(
                    compiled,
                    env={"RUN_TWO": "no", "CMD": "exec" if name == "exec" else "trap"},
                )

            self.assertEqual(result.returncode, 125, result.stdout)
            self.assertIn(
                "runtime replay cannot allow live exec" if name == "exec" else "runtime replay cannot allow live trap",
                result.stdout,
            )

    def test_runtime_compiler_rejects_dynamic_command_and_builtin_source_targets(self):
        cases = {
            "command": 'command "$CMD" ./dep.sh\nprintf "done\\n"\n',
            "builtin": 'builtin "$CMD" ./dep.sh\nprintf "done\\n"\n',
            "dynamic command source arg": '"$CMD" source ./dep.sh\nprintf "done\\n"\n',
            "dynamic builtin command word": 'x=builtin\n"$x" "$CMD" ./dep.sh\nprintf "done\\n"\n',
            "subshell builtin dynamic source": 'if [[ ${RUN:-} ]]; then ( builtin "$CMD" ./dep.sh ); fi\nprintf "done\\n"\n',
            "command substitution builtin dynamic source": 'if [[ ${RUN:-} ]]; then x=$(builtin "$CMD" ./dep.sh); printf "x:%s\\n" "$x"; fi\nprintf "done\\n"\n',
            "heredoc command dynamic source": "cat <<EOF\n$(command \"$CMD\" ./dep.sh)\nEOF\nprintf 'done\\n'\n",
            "computed generated helper": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "if [[ ${MUTATE:-} ]]; then a=__mod; b=ash_select_source_edge; f=$a$b; \"$f\" 'p0:__entrypoint__:3:0'; fi\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_command",
                    env={"CMD": "echo", "X": "echo", "RUN": "1", "RUN_TWO": "yes", "PAYLOAD": "trace"},
                )

    def test_runtime_compiler_allows_exact_env_dynamic_external_commands_with_drift_guard(self):
        cases = {
            "unknown two stage source path": (
                '"$X" "$CMD" ./dep.sh\nprintf "done\\n"\n',
                "echo ./dep.sh\ndone\n",
                {"X": "echo", "CMD": "echo"},
                {"X": "source", "CMD": "echo"},
                "observed environment drift: X",
            ),
            "pipeline two stage source path": (
                'if [[ ${RUN:-} ]]; then "$X" "$CMD" ./dep.sh | cat; fi\nprintf "done\\n"\n',
                "echo ./dep.sh\ndone\n",
                {"X": "echo", "CMD": "echo", "RUN": "1"},
                {"X": "echo", "CMD": "source", "RUN": "1"},
                "observed environment drift: CMD",
            ),
            "command substitution dynamic source guard": (
                'x=$("$CMD" ./dep.sh)\nprintf "x:%s\\n" "$x"\n',
                "x:./dep.sh\n",
                {"CMD": "echo"},
                {"CMD": "source"},
                "observed environment drift: CMD",
            ),
            "pipeline dynamic source guard": (
                '"$CMD" ./dep.sh | cat\nprintf "done\\n"\n',
                "./dep.sh\ndone\n",
                {"CMD": "echo"},
                {"CMD": "source"},
                "observed environment drift: CMD",
            ),
            "dynamic eval source": (
                '"$CMD" "$PAYLOAD"\nprintf "done\\n"\n',
                "trace\ndone\n",
                {"CMD": "echo", "PAYLOAD": "trace"},
                {"CMD": "eval", "PAYLOAD": "source ./dep.sh"},
                "observed environment drift: CMD",
            ),
        }
        for name, (script, expected_stdout, env, drift_env, drift_message) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                compiled, _graph = self.compile_observed(project, "main.sh", env=env)
                result = project.run(compiled, env=env)
                drifted = project.run(compiled, env=drift_env)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, expected_stdout)
            self.assertEqual(drifted.returncode, 125, drifted.stdout)
            self.assertIn(drift_message, drifted.stdout)

    def test_runtime_compiler_rejects_exact_dynamic_state_mutation_commands(self):
        cases = {
            "read": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "p=__mod\n"
                "q=ash_edge_consumed\n"
                "key='p0:__entrypoint__:2:0|0'\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  target=\"${p}${q}[$key]\"\n"
                "  r=read\n"
                "  \"$r\" \"$target\" <<< 1\n"
                "fi\n"
            ),
            "printf": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "p=__mod\n"
                "q=ash_edge_consumed\n"
                "key='p0:__entrypoint__:2:0|0'\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  target=\"${p}${q}[$key]\"\n"
                "  c=printf\n"
                "  \"$c\" -v \"$target\" 1\n"
                "fi\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                    env={"RUN_TWO": "yes"},
                )

    def test_runtime_compiler_allows_quoted_exact_dynamic_external_command(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "tool='printf'\n"
                "x=$(\"$tool\" 'hello')\n"
                "printf 'x:%s\\n' \"$x\"\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "x:hello\ndep\n")

    def test_runtime_compiler_rejects_replay_guard_removal_and_redefinition(self):
        cases = {
            "unset source": "unset -f source\n$CMD ./dep.sh\n",
            "brace unset source": "unset -f {source,trap}\n$CMD ./dep.sh\n",
            "function source": "function source { printf 'fake\\n'; }\n$CMD ./dep.sh\n",
            "function command": "function command { printf 'fake\\n'; }\ncommand bash -c 'printf child\\n'\n",
            "function exit": "exit() { return 0; }\nsource ./dep.sh\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "DEP=./dep.sh\n" + script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                    env={"CMD": "echo"},
                )

        with self.subTest(name="function enable"), ScriptProject() as project:
            project.write("main.sh", "enable() { return 0; }\nsource ./dep.sh\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_trace_error(
                project,
                "main.sh",
                code="runtime.trace.disabled-source-builtin",
                env={"CMD": "echo"},
            )

    def test_runtime_compiler_rejects_exit_trap_bypass_shape(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "trap 'printf \"user-exit\\n\"' EXIT\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "exit 0\n",
            )

    def test_runtime_compiler_rejects_non_exit_traps_at_compile_time(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "trap 'printf err\\n' ERR\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.exit_trap",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.exit_trap",
                env={"RUN_TWO": "yes"},
            )

    def test_runtime_compiler_rejects_exec_validation_bypass(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "exec true\n",
            )

    def test_runtime_compiler_rejects_enable_validation_bypass(self):
        cases = {
            "disable source": (
                "if [[ ${DISABLE:-} ]]; then enable -n source; fi\n"
                "source ./dep.sh || true\n"
                "if [[ ${DISABLE:-} ]]; then enable source; fi\n"
                "printf 'done\\n'\n"
            ),
            "disable builtin": (
                "if [[ ${DISABLE:-} ]]; then enable -n builtin; fi\n"
                "source ./dep.sh || true\n"
                "if [[ ${DISABLE:-} ]]; then enable builtin; fi\n"
                "printf 'done\\n'\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\nreturn 1\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                )

    def test_runtime_compiler_rejects_cross_file_and_dynamic_exit_traps(self):
        cases = {
            "cross file": (
                "source ./trap.sh\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "exit 0\n",
                "trap 'printf \"user-exit\\n\"' EXIT\n",
            ),
            "dynamic target": (
                "source ./trap.sh\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "exit 0\n",
                "sig=EXIT\n"
                "trap 'printf \"user-exit\\n\"' \"$sig\"\n",
            ),
        }
        for name, (main_script, trap_script) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", main_script)
                project.write("trap.sh", trap_script)
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
                project.write("main.sh", "DEP=./dep.sh\n" + script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.instrumentation_sensitive",
                )

    def test_runtime_compiler_rejects_instrumentation_sensitive_state_probes(self):
        cases = {
            "shopt expand aliases": "if shopt -q expand_aliases; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "shopt pipe": "if shopt -p expand_aliases | grep -q -- '-s'; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "xtrace option": "if [[ -o xtrace ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "shopt xtrace": "if shopt -o -q xtrace; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "set option": "if set -o | grep -q '^xtrace'; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "bash env": "if [[ -v BASH_ENV ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "dynamic variable probe": "v=BASH_ENV\nif [[ -v $v ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "dynamic shopt option": "opt=expand_aliases\nif shopt -q \"$opt\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "declare print": "if declare -p BASH_ENV >/dev/null 2>&1; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "compgen variables": "if compgen -v | grep -qx BASH_ENV; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "indirect expansion": "v=BASH_ENV\nif [[ -n ${!v-} ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
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

    def test_runtime_compiler_allows_source_free_pipefail_change(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "set -o pipefail\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\n")

    def test_runtime_compiler_does_not_reject_instrumentation_text_in_comments_or_literals(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "# mention $- PS4 BASH_XTRACEFD MODASH_TRACE_FILE\n"
                "printf '%s\\n' 'literal $- PS4 MODASH_TRACE_FILE'\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "literal $- PS4 MODASH_TRACE_FILE\ndep\n")

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

    def test_runtime_compiler_validates_symlinked_source_by_file_identity(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./link.sh\n")
            project.write("real.sh", "printf 'real\\n'\n")
            project.write("other.sh", "printf 'other\\n'\n")
            project.path("link.sh").symlink_to("real.sh")
            compiled, _graph = self.compile_observed(project, "main.sh")
            matched = project.run(compiled)
            project.path("link.sh").unlink()
            project.path("link.sh").symlink_to("other.sh")
            drifted = project.run(compiled)

        self.assertEqual(matched.returncode, 0, matched.stdout)
        self.assertEqual(matched.stdout, "real\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed source path drift", drifted.stdout)
        self.assertNotIn("other\n", drifted.stdout)

    def test_runtime_compiler_fails_closed_when_observed_missing_source_appears_later(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./missing.sh || true\n"
                "printf 'done\\n'\n",
            )
            compiled, graph = self.compile_observed(project, "main.sh")
            project.write("missing.sh", "printf 'now-present\\n'\n")
            result = project.run(compiled)

        self.assertTrue(graph["edges"][0]["to"].startswith("missing-source:"))
        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertNotIn("now-present", result.stdout)
        self.assertIn("observed missing source drift", result.stdout)

    def test_runtime_compiler_preserves_source_redirections(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh > out.txt\n"
                "printf 'after\\n'\n"
                "cat out.txt\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "after\ndep\n")

    def test_runtime_compiler_preserves_source_redirection_expansion_order(self):
        cases = {
            "source arg stderr":
                "source \"$(printf './dep.sh'; echo ARGERR >&2)\" 2>err.txt\n"
                "printf 'stdout\\n'\n"
                "printf 'ERRFILE:'\n"
                "cat err.txt\n",
            "assignment stderr":
                "VAR=$(echo ASSIGNERR >&2; printf foo) source ./dep.sh 2>err.txt\n"
                "printf 'after:%s\\n' \"${VAR-}\"\n"
                "printf 'ERRFILE:'\n"
                "cat err.txt\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'depout\\n'; echo DEPERR >&2\n")
                expected = project.run("main.sh")
                compiled, _graph = self.compile_observed(project, "main.sh")
                actual = project.run(compiled)

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_compiler_rejects_source_bearing_untrusted_shell_c_payloads(self):
        cases = {
            "dynamic shell": (
                "\"$B\" -c 'source ./dep.sh'\nprintf 'done\\n'\n",
                {"B": "echo"},
                "runtime.compile.unrewritten_source",
            ),
            "sh": (
                "sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
                {},
                "runtime.compile.unrewritten_source",
            ),
            "dynamic shell dynamic payload": (
                "\"$B\" -c \"$PAYLOAD\"\nprintf 'done\\n'\n",
                {"B": "echo", "PAYLOAD": "hello"},
                "runtime.compile.unrewritten_source",
            ),
            "sh dynamic payload": (
                "sh -c \"$PAYLOAD\"\nprintf 'done\\n'\n",
                {"PAYLOAD": "echo child"},
                "runtime.compile.unrewritten_source",
            ),
            "env split sh": (
                "env -S \"sh -c '. ./dep.sh'\"\nprintf 'done\\n'\n",
                {},
                "runtime.compile.unrewritten_source",
            ),
            "env split string sh": (
                "env --split-string=\"sh -c '. ./dep.sh'\"\nprintf 'done\\n'\n",
                {},
                "runtime.compile.unrewritten_source",
            ),
        }
        for name, (script, env, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                    env=env,
                )

    def test_runtime_compiler_allows_env_split_string_without_source_payload(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "env -S \"printf env-ok\\\\n\"\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "env-ok\ndep\n")

    def test_runtime_compiler_allows_source_free_external_interpreter_heredoc(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "python3 - <<'PY'\n"
                "print('helper')\n"
                "PY\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "helper\ndep\n")

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

    def test_runtime_compiler_rejects_command_and_builtin_eval(self):
        cases = {
            "command": "command eval \"$CMD\"\nprintf 'done\\n'\n",
            "builtin": "builtin eval \"$CMD\"\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_eval",
                    env={"CMD": "printf trace\\n"},
                )

    def test_runtime_compiler_rejects_eval_guard_override_or_removal(self):
        cases = {
            "override": "eval() { builtin eval \"$@\"; }\n",
            "unset": "unset -f eval\n",
        }
        for name, prefix in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    prefix
                    + "i=${I:-01}\n"
                    + "result=\"$(eval echo \\${TEST_CASE_\"$i\"})\"\n"
                    + "printf 'result:%s\\n' \"$result\"\n"
                    + "source ./base.sh\n",
                )
                project.write("base.sh", "printf 'base\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                    env={"TEST_CASE_01": "hello"},
                )

    def test_runtime_compiler_rejects_kill_guard_override_or_removal(self):
        cases = {
            "override": "kill() { builtin kill -KILL \"$$\"; }\n",
            "unset": "unset -f kill\n",
        }
        for name, prefix in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", prefix + "source ./dep.sh\n")
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                )

    def test_runtime_compiler_rejects_mutated_or_comment_only_shopt_restore_eval(self):
        cases = {
            "mutated": (
                "shellopts=$(shopt -p extglob)\n"
                "if [[ ${RUN_SOURCE:-} ]]; then shellopts='source ./dep.sh'; fi\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
            "comment": (
                "# shellopts=$(shopt -p extglob)\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
            "subshell assignment": (
                "( shellopts=$(shopt -p extglob) )\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
            "pipeline assignment": (
                "shellopts=$(shopt -p extglob) | cat\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
            "environment assignment": (
                "shellopts=$(shopt -p extglob) true\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_eval",
                    env={"shellopts": "printf trace\\n"},
                )

    def test_runtime_compiler_rejects_control_flow_or_unrelated_shopt_restore_eval(self):
        cases = {
            "dead branch": (
                "if false; then\n"
                "  shellopts=$(shopt -p extglob)\n"
                "fi\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
            "unrelated function": (
                "restore() {\n"
                "  shellopts=$(shopt -p extglob)\n"
                "}\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n"
            ),
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_eval",
                    env={"shellopts": "printf trace\\n"},
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

    def test_runtime_compiler_rejects_shopt_restore_eval_poisoning(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${FAKE_SHOPT:-} ]]; then\n"
                "  shopt() { printf 'builtin source ./dep.sh\\n'; }\n"
                "fi\n"
                "shellopts=$(shopt -p extglob)\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_state",
            )

    def test_runtime_compiler_fails_closed_on_source_status_drift(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source ./dep.sh || printf 'handled:%s\\n' \"$?\"\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n[[ ${FAIL:-} ]] && return 7 || return 0\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            observed = project.run(compiled)
            drifted = project.run(compiled, env={"FAIL": "1"})

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

    def test_runtime_compiler_rejects_unrecognized_child_bash_wrappers_with_sources(self):
        cases = {
            "time": "time bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "nice": "nice bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "env nice": "env -i PATH=\"$PATH\" nice bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
            "nested env": "env /usr/bin/env bash -c 'source ./dep.sh'\nprintf 'done\\n'\n",
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

    def test_runtime_compiler_rejects_source_bearing_non_bash_shell_wrappers(self):
        cases = {
            "nice": "nice sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
            "timeout": "timeout 5 sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
            "setsid": "setsid sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
            "nohup": "nohup sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
            "stdbuf": "stdbuf -o0 sh -c '. ./dep.sh'\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.unrewritten_source",
                )

    def test_runtime_compiler_allows_inert_non_execution_shell_words(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "echo sh -c '. ./not-a-command.sh'\n"
                "source ./dep.sh\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "sh -c . ./not-a-command.sh\ndep\n")

    def test_runtime_compiler_rejects_unobserved_dynamic_child_bash_payloads(self):
        cases = {
            "dynamic command": "bash -c '$CMD ./dep.sh; printf child-done\\n'\nprintf 'done\\n'\n",
            "command dynamic": "bash -c 'command \"$CMD\" ./dep.sh'\nprintf 'done\\n'\n",
            "builtin dynamic": "bash -c 'builtin \"$CMD\" ./dep.sh'\nprintf 'done\\n'\n",
            "time dynamic": "bash -c 'time \"$CMD\" ./dep.sh'\nprintf 'done\\n'\n",
            "eval dynamic": "bash -c 'eval \"$CMD\"'\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.unobserved_child_source",
                    env={"CMD": "echo"},
                )

    def test_runtime_compiler_rejects_child_bash_startup_file_sources(self):
        cases = {
            "inherited bash env": ("bash -c \"printf 'child\\\\n'\"\nprintf 'done\\n'\n", None),
            "explicit assignment": ("BASH_ENV=./evil.sh bash -c 'printf child\\n'\nprintf 'done\\n'\n", "runtime.compile.instrumentation_sensitive"),
            "env assignment": ("env BASH_ENV=./evil.sh bash -c 'printf child\\n'\nprintf 'done\\n'\n", "runtime.compile.instrumentation_sensitive"),
            "export then child": ("export BASH_ENV=./evil.sh\nbash -c 'printf child\\n'\nprintf 'done\\n'\n", "runtime.compile.instrumentation_sensitive"),
            "script invocation": ("bash ./child.sh\nprintf 'done\\n'\n", "runtime.compile.unsupported_child_bash"),
        }
        for name, (script, compile_error) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("child.sh", "printf child\\n")
                project.write("evil.sh", "source ./dep.sh\n")
                project.write("dep.sh", "printf 'dep\\n'\n")
                if compile_error is not None:
                    self.assert_compile_observed_error(
                        project,
                        "main.sh",
                        code=compile_error,
                    )
                    continue
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled, env={"BASH_ENV": str(project.path("evil.sh"))})

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "child\ndone\n")

    def test_runtime_compiler_rejects_child_payload_trace_state_probes(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'if shopt -q expand_aliases; then echo trace; else echo normal; fi'\n"
                "echo done\n",
            )
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.instrumentation_sensitive",
            )

    def test_runtime_compiler_rejects_reserved_word_live_source_prefixes(self):
        with ScriptProject() as project:
            project.write("main.sh", "if [[ ${RUN:-} ]]; then time builtin source ./dep.sh; fi\nprintf 'done\\n'\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unsupported_source_prefix",
            )

    def test_runtime_compiler_rejects_time_prefixed_observed_source_sites(self):
        with ScriptProject() as project:
            project.write("main.sh", "time source ./dep.sh\nprintf 'done\\n'\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unsupported_source_prefix",
            )

    def test_runtime_compiler_rejects_coproc_live_source_prefix(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${RUN:-} ]]; then\n"
                "  coproc { builtin source ./dep.sh; }\n"
                "  cat <&\"${COPROC[0]}\"\n"
                "fi\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.dynamic_command",
            )

    def test_runtime_compiler_rejects_reserved_word_child_bash_sources(self):
        cases = {
            "time builtin": "bash -c 'time builtin source ./dep.sh'\nprintf 'done\\n'\n",
            "time bang builtin": "bash -c 'time ! builtin source ./dep.sh'\nprintf 'done\\n'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.unsupported_source_prefix",
                )

    def test_runtime_compiler_guards_function_local_positional_nameref_targets(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "mutate_ref() {\n"
                "  local -n ref=$1\n"
                "  ref[$2]=1\n"
                "}\n"
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then\n"
                "  source ./two.sh\n"
                "fi\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  a=__mod\n"
                "  b=ash_edge_consumed\n"
                "  mutate_ref \"$a$b\" 'p0:__entrypoint__:6:0|0'\n"
                "fi\n",
            )
            project.write("one.sh", "printf 'one\\n'\n")
            project.write("two.sh", "printf 'two\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"RUN_TWO": "no", "MUTATE": "1"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("runtime replay state nameref target rejected", result.stdout)

    def test_runtime_compiler_guards_function_positional_read_targets(self):
        cases = {
            "direct": 'read -r "$1" <<< 1',
            "builtin": 'builtin read -r "$1" <<< 1',
            "command": 'command read -r "$1" <<< 1',
        }
        for name, read_command in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    "read_into() {\n"
                    f"  {read_command}\n"
                    "}\n"
                    "source ./one.sh\n"
                    "if [[ ${RUN_TWO:-yes} == yes ]]; then\n"
                    "  source ./two.sh\n"
                    "fi\n"
                    "if [[ ${MUTATE:-} ]]; then\n"
                    "  a=__mod\n"
                    "  b=ash_edge_consumed\n"
                    "  read_into \"$a$b\"\n"
                    "fi\n",
                )
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled, env={"RUN_TWO": "no", "MUTATE": "1"})

            self.assertEqual(result.returncode, 125, result.stdout)
            self.assertIn("runtime replay state read target rejected", result.stdout)

    def test_runtime_compiler_allows_array_glob_arithmetic_predicate(self):
        with ScriptProject() as project:
            project.mkdir("data")
            project.write("data/item", "x\n")
            project.write(
                "main.sh",
                "source ./dep.sh\n"
                "if dir_is_empty ./data; then printf 'empty\\n'; else printf 'not-empty\\n'; fi\n",
            )
            project.write(
                "dep.sh",
                "dir_is_empty() {\n"
                "  files=(\"$1\"/*)\n"
                "  (( ${#files} == 0 ))\n"
                "}\n",
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "not-empty\n")

    def test_runtime_compiler_rejects_exported_bash_functions_during_trace(self):
        with ScriptProject() as project:
            project.write("main.sh", "helper\nsource ./dep.sh\n")
            project.write("dep.sh", "printf 'dep\\n'\n")
            with self.assertRaises(RuntimeSourceTraceError) as context:
                project.trace("main.sh", env={"BASH_FUNC_helper%%": "() { printf 'helper\\n'; }"})

        self.assertEqual(context.exception.code, "runtime.trace.exported_function")

    def test_runtime_compiler_child_bash_replay_bypasses_function_override(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "if [[ ${FAKE:-} ]]; then\n"
                "  bash() { printf 'fake\\n'; return 0; }\n"
                "fi\n"
                "bash -c 'source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"FAKE": "1"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\ndone\n")

    def test_runtime_compiler_fails_closed_on_child_wrapper_function_override(self):
        cases = {
            "command": "function command { printf 'fake-command\\n'; return 0; }\ncommand bash -c 'source ./dep.sh'\n",
            "env": "function env { printf 'fake-env\\n'; return 0; }\nenv bash -c 'source ./dep.sh'\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                )

    def test_runtime_compiler_rejects_unsupported_runtime_reference_forms(self):
        cases = {
            "caller": 'printf "caller:%s\\n" "${BASH_SOURCE[1]}"\n',
            "parameter op": 'printf "dir:%s\\n" "${BASH_SOURCE[0]%/*}"\n',
            "array": 'printf "all:%s\\n" "${BASH_SOURCE[@]}"\n',
            "zero op": 'printf "zero-base:%s\\n" "${0##*/}"\n',
        }
        for name, dep_script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./dep.sh\n")
                project.write("dep.sh", dep_script)
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.runtime_reference",
                )

    def test_runtime_compiler_rejects_source_entry_sensitive_state_references(self):
        cases = {
            "pipestatus": 'printf "pipe:%s\\n" "${PIPESTATUS[*]}"\n',
            "underscore": 'printf "under:%s\\n" "$_"\n',
        }
        for name, dep_script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./dep.sh\n")
                project.write("dep.sh", dep_script)
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.graph.function_context_sensitive",
                )

    def test_runtime_compiler_allows_runtime_reference_text_in_comments_and_single_quotes(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write(
                "dep.sh",
                "# ${BASH_SOURCE[1]}\n"
                "printf '%s\\n' '${BASH_SOURCE[1]}'\n",
            )
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "${BASH_SOURCE[1]}\n")

    def test_runtime_compiler_rejects_runtime_references_inside_multiline_strings(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write(
                "dep.sh",
                'printf "%s\\n" "first\n'
                '${BASH_SOURCE[0]}\n'
                '"\n',
            )
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.runtime_reference_multiline",
            )

    def test_runtime_compiler_rejects_runtime_references_inside_heredoc(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "cat <<EOF\nbs:${BASH_SOURCE[0]}\nEOF\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.runtime_reference_heredoc",
            )

    def test_runtime_compiler_allows_runtime_reference_text_inside_quoted_heredoc(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "cat <<'EOF'\nbs:${BASH_SOURCE[0]}\n$(builtin source ./not-a-command.sh)\nEOF\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "bs:${BASH_SOURCE[0]}\n$(builtin source ./not-a-command.sh)\n")

    def test_runtime_compiler_rejects_runtime_references_on_child_bash_lines(self):
        with ScriptProject() as project:
            project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "printf 'bs:%s\\n' \"${BASH_SOURCE[0]}\"; bash -c 'printf child\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.runtime_reference_child_bash",
            )

    def test_runtime_compiler_preserves_negated_source_atoms_in_compound_conditions(self):
        cases = {
            "and": "if true && ! source ./dep.sh; then printf 'then\\n'; else printf 'else\\n'; fi\n",
            "or": "if false || ! source ./dep.sh; then printf 'then\\n'; else printf 'else\\n'; fi\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\nreturn 7\n")
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep\nthen\n")

    def test_runtime_compiler_guards_mutated_shopt_restore_eval(self):
        cases = {
            "read": (
                "shellopts=$(shopt -p extglob)\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  read shellopts <<< 'builtin source ./dep.sh'\n"
                "fi\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n",
                {"MUTATE": "1"},
            ),
            "printf": (
                "shellopts=$(shopt -p extglob)\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  printf -v shellopts '%s' 'builtin source ./dep.sh'\n"
                "fi\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n",
                {"MUTATE": "1"},
            ),
            "array": (
                "shellopts=$(shopt -p extglob)\n"
                "if [[ ${MUTATE:-} ]]; then\n"
                "  shellopts[0]='builtin source ./dep.sh'\n"
                "fi\n"
                "eval \"$shellopts\"\n"
                "printf 'done\\n'\n",
                {"MUTATE": "1"},
            ),
        }
        for name, (script, env) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                compiled, _graph = self.compile_observed(project, "main.sh")
                result = project.run(compiled, env=env)

            self.assertEqual(result.returncode, 125, result.stdout)
            self.assertIn("unsafe shopt restore eval", result.stdout)
            self.assertNotIn("dep\n", result.stdout)
            self.assertNotIn("done\n", result.stdout)

    def test_runtime_compiler_guards_sourced_file_mutated_shopt_restore_eval(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "source_safe() {\n"
                "  local shellopts=$(shopt -p extglob)\n"
                "  source ./dep.sh\n"
                "  eval \"$shellopts\"\n"
                "}\n"
                "source_safe\n"
                "printf 'done\\n'\n",
            )
            project.write(
                "dep.sh",
                "printf 'dep\\n'\n"
                "if [[ ${MUTATE:-} ]]; then shellopts='builtin source ./evil.sh'; fi\n",
            )
            project.write("evil.sh", "printf 'evil\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"MUTATE": "1"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("unsafe shopt restore eval", result.stdout)
        self.assertIn("dep\n", result.stdout)
        self.assertNotIn("evil\n", result.stdout)
        self.assertNotIn("done\n", result.stdout)

    def test_runtime_compiler_rejects_dynamic_read_array_and_mapfile_targets(self):
        cases = {
            "read array": "read -a \"$target\" <<< value\n",
            "mapfile": "mapfile -t \"$target\" <<< value\n",
            "readarray": "readarray -t \"$target\" <<< value\n",
            "mapfile callback": "if [[ ${RUN:-} ]]; then printf line | mapfile -C 'builtin source ./dep.sh' -c 1 arr; fi\n",
            "readarray callback": "if [[ ${RUN:-} ]]; then printf line | readarray -C 'builtin source ./dep.sh' -c 1 arr; fi\n",
        }
        for name, mutation in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "main.sh",
                    "source ./one.sh\n"
                    "p=__mod\n"
                    "q=ash_base64\n"
                    "target=$p$q\n"
                    f"{mutation}",
                )
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_state",
                )

    def test_runtime_compiler_rejects_forged_child_replay_marker(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'for d in \"${TMPDIR:-/tmp}\"/modash-runtime.*; do : > \"$d/child-process-1.ok\" 2>/dev/null || true; done; [[ ${RUN_CHILD:-yes} == yes ]] && source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled, env={"RUN_CHILD": "no"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("observed child process replay failed", result.stdout)
        self.assertNotEqual(result.stdout, "done\n")

    def test_runtime_compiler_rejects_python_child_replay_marker_forgery(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'python3 -c \"import os; m=next(v for k,v in os.environ.items() if k.endswith(\\\"child_replay_marker\\\")); t=next(v for k,v in os.environ.items() if k.endswith(\\\"child_replay_token\\\")); open(m, \\\"w\\\").write(t+\\\"\\\\n\\\")\"; [[ ${RUN_CHILD:-yes} == yes ]] && source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.instrumentation_sensitive",
                env={"RUN_CHILD": "yes"},
            )

    def test_runtime_compiler_rejects_child_marker_forgery_with_signal_exit(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'python3 ./forge.py; [[ ${FORGE:-} ]] && kill -9 $$; [[ ${RUN_CHILD:-yes} == yes ]] && source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write(
                "forge.py",
                "import os\n"
                "m = next((v for k, v in os.environ.items() if k.endswith('child_replay_marker')), None)\n"
                "t = next((v for k, v in os.environ.items() if k.endswith('child_replay_token')), None)\n"
                "if os.environ.get('FORGE') and m and t:\n"
                "    open(m, 'w').write(t + '\\n')\n",
            )
            project.write("dep.sh", "printf 'dep-child\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"RUN_CHILD": "yes"})
            result = project.run(compiled, env={"RUN_CHILD": "yes", "FORGE": "1"})

        self.assertEqual(result.returncode, 125, result.stdout)
        self.assertIn("runtime replay cannot allow kill target", result.stdout)
        self.assertNotIn("done\n", result.stdout)

    def test_runtime_compiler_rejects_child_replay_marker_proc_environ_probe(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'm=; t=; while IFS= read -r -d \"\" e; do case $e in *child_replay_marker=*) m=${e#*=} ;; *child_replay_token=*) t=${e#*=} ;; esac; done < /proc/$$/environ; [[ $m && $t ]] && printf \"%s\\n\" \"$t\" > \"$m\"; [[ ${RUN_CHILD:-yes} == yes ]] && source ./dep.sh'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.instrumentation_sensitive",
                env={"RUN_CHILD": "yes"},
            )

    def test_runtime_compiler_rejects_top_level_declare_and_typeset_in_sourced_files(self):
        cases = {
            "declare": "declare DEP=from-dep\nprintf 'dep\\n'\n",
            "typeset": "typeset DEP=from-dep\nprintf 'dep\\n'\n",
        }
        for name, dep_script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "source ./dep.sh\nprintf 'DEP:%s\\n' \"$DEP\"\n")
                project.write("dep.sh", dep_script)
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.graph.function_context_sensitive",
                )

    def test_runtime_compiler_preserves_user_env_variable(self):
        with ScriptProject() as project:
            project.write("main.sh", "printf 'env:%s\\n' \"$ENV\"\nsource ./dep.sh\n")
            project.write("dep.sh", "printf 'dep-env:%s\\n' \"$ENV\"\n")
            compiled, _graph = self.compile_observed(project, "main.sh", env={"ENV": "prod"})
            result = project.run(compiled, env={"ENV": "prod"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "env:prod\ndep-env:prod\n")

    def test_runtime_trace_rejects_user_bash_env(self):
        with ScriptProject() as project:
            project.write("main.sh", "printf 'main\\n'\n")
            project.write("evil.sh", "printf 'evil\\n'\n")
            self.assert_trace_error(
                project,
                "main.sh",
                code="runtime.trace.instrumentation_environment",
                env={"BASH_ENV": str(project.path("evil.sh"))},
            )

    def test_runtime_compiler_rejects_instrumentation_environment_introspection_pipelines(self):
        cases = {
            "bashopts": "if [[ $BASHOPTS == *expand_aliases* ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "bash aliases": "if [[ ${BASH_ALIASES[source]+x} ]]; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "type source": "if type source | grep -q alias; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "command V source": "if command -V source | grep -q alias; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "type t source": "if type -t source | grep -q alias; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "declare function source": "if declare -F source >/dev/null; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "compgen function source": "if compgen -A function source | grep -q source; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "compgen alias source": "if compgen -A alias source | grep -q source; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "trace fd": "if { : >&19; } 2>/dev/null; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "dynamic trace fd": "fd=19\nif { : >&$fd; } 2>/dev/null; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "env": "v=BASH_ENV\nif env | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "bin env": "v=BASH_ENV\nif /bin/env | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "set": "v=BASH_ENV\nif set | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "absolute env": "v=BASH_ENV\nif /usr/bin/env | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "absolute printenv": "v=BASH_ENV\nif /usr/bin/printenv | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "proc environ": "v=BASH_ENV\nif tr '\\0' '\\n' < /proc/$$/environ | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "proc cmdline": "v=BASH_ENV\nif tr '\\0' '\\n' < /proc/$$/cmdline | grep -q \"$v\"; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "external fd probe": "if python3 -c 'import os,sys; sys.exit(0 if \"19\" in os.listdir(\"/proc/self/fd\") else 1)'; then DEP=./dep.sh; fi\nsource \"$DEP\"\n",
            "external heredoc env probe": "DEP=./dep.sh\nif python3 - <<'PYENV'\nimport os, sys\nsys.exit(0 if any(k.startswith('BASH_') or k.startswith('MODASH_TRACE_') for k in os.environ) else 1)\nPYENV\nthen :; fi\nsource \"$DEP\"\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", "DEP=./dep.sh\n" + script)
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.instrumentation_sensitive",
                )

    def test_runtime_compiler_rejects_child_payload_instrumentation_introspection(self):
        cases = {
            "env": "bash -c 'v=BASH_ENV; if env | grep -q \"$v\"; then echo trace; else echo normal; fi'\nsource ./dep.sh\n",
            "type source": "bash -c 'if type source | grep -q alias; then echo trace; else echo normal; fi'\nsource ./dep.sh\n",
            "bin env": "bash -c 'v=BASH_ENV; if /bin/env | grep -q \"$v\"; then echo trace; else echo normal; fi'\nsource ./dep.sh\n",
            "absolute printenv": "bash -c 'v=BASH_ENV; if /usr/bin/printenv | grep -q \"$v\"; then echo trace; else echo normal; fi'\nsource ./dep.sh\n",
            "proc environ": "bash -c 'v=BASH_ENV; if tr \"\\\\0\" \"\\\\n\" < /proc/$$/environ | grep -q \"$v\"; then echo trace; else echo normal; fi'\nsource ./dep.sh\n",
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

    def test_runtime_compiler_rejects_child_payload_array_assignment_source_expansion(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "bash -c 'if [[ ${RUN:-} ]]; then arr=($(builtin source ./dep.sh)); printf \"arr:%s\\n\" \"${arr[*]}\"; fi'\n"
                "printf 'done\\n'\n",
            )
            project.write("dep.sh", "printf 'dep\\n'\n")
            self.assert_compile_observed_error(
                project,
                "main.sh",
                code="runtime.compile.unobserved_child_source",
            )

    def test_runtime_compiler_rejects_nested_eval_source_bypass(self):
        cases = {
            "parent": (
                "if [[ ${RUN:-} ]]; then x=$(eval \"$CMD\"); printf 'x:%s\\n' \"$x\"; fi\nsource ./base.sh\n",
                "runtime.compile.dynamic_command",
            ),
            "parent builtin": (
                "if [[ ${RUN:-} ]]; then x=$(builtin eval \"$CMD\"); printf 'x:%s\\n' \"$x\"; fi\nsource ./base.sh\n",
                "runtime.compile.dynamic_command",
            ),
            "child": (
                "bash -c 'if [[ ${RUN:-} ]]; then x=$(eval \"$CMD\"); printf \"x:%s\\n\" \"$x\"; fi'\nsource ./base.sh\n",
                "runtime.compile.unobserved_child_source",
            ),
            "literal parent dynamic tail": (
                "if [[ ${RUN:-} ]]; then eval : \"$CMD\"; fi\nsource ./base.sh\n",
                "runtime.compile.dynamic_eval",
            ),
            "literal child dynamic tail": (
                "bash -c 'if [[ ${RUN:-} ]]; then eval : \"$CMD\"; fi'\nsource ./base.sh\n",
                "runtime.compile.unobserved_child_source",
            ),
            "literal printf mutation": (
                "source ./one.sh\n"
                "if [[ ${RUN_TWO:-yes} == yes ]]; then source ./two.sh; fi\n"
                "if [[ ${MUTATE:-} ]]; then p=__mod; q='ash_edge_consumed[p0:__entrypoint__:3:0|0]'; n=$p$q; eval printf -v \\${n} 1; fi\n",
                "runtime.compile.dynamic_eval",
            ),
            "literal echo prompt transform": (
                "eval echo \\${foo@P}\n"
                "source ./base.sh\n",
                "runtime.compile.dynamic_eval",
            ),
        }
        for name, (script, code) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("base.sh", "printf 'base\\n'\n")
                project.write("one.sh", "printf 'one\\n'\n")
                project.write("two.sh", "printf 'two\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code=code,
                    env={"CMD": "printf trace\\n"},
                )

    def test_runtime_compiler_allows_literal_echo_eval_lookup(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "TEST_CASE_01='hello world'\n"
                "for i in 01; do\n"
                "  result=\"$(eval echo \\${TEST_CASE_\"$i\"})\"\n"
                "  printf 'case:%s\\n' \"$result\"\n"
                "done\n"
                "source ./base.sh\n",
            )
            project.write("base.sh", "printf 'base\\n'\n")
            compiled, _graph = self.compile_observed(project, "main.sh")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout + (result.stderr or ""))
        self.assertEqual(result.stdout, "case:hello world\nbase\n")

    def test_runtime_compiler_guards_literal_echo_eval_lookup_runtime_injection(self):
        with ScriptProject() as project:
            project.write(
                "main.sh",
                "i=${I:-01}\n"
                "result=\"$(eval echo \\${TEST_CASE_\"$i\"})\"\n"
                "printf 'case:%s\\n' \"$result\"\n"
                "source ./base.sh\n",
            )
            project.write("base.sh", "printf 'base\\n'\n")
            project.write("dep.sh", "printf 'dep-eval-inject\\n'\n")
            env = {"TEST_CASE_01": "hello"}
            compiled, _graph = self.compile_observed(project, "main.sh", env=env)
            normal = project.run(compiled, env=env)
            hostile = project.run(
                compiled,
                env={**env, "I": "} ; builtin source ./dep.sh #"},
            )

        self.assertEqual(normal.returncode, 0, normal.stdout)
        self.assertEqual(normal.stdout, "case:hello\nbase\n")
        self.assertEqual(hostile.returncode, 125, hostile.stdout)
        self.assertIn("unsafe literal eval argument", hostile.stdout)
        self.assertNotIn("dep-eval-inject", hostile.stdout)

    def test_runtime_compiler_rejects_coproc_dynamic_execution(self):
        cases = {
            "dynamic command": "if [[ ${RUN:-} ]]; then coproc \"$CMD\" ./dep.sh; cat <&\"${COPROC[0]}\"; fi\nsource ./base.sh\n",
            "eval": "if [[ ${RUN:-} ]]; then coproc eval \"$CMD\"; cat <&\"${COPROC[0]}\"; fi\nsource ./base.sh\n",
        }
        for name, script in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", script)
                project.write("base.sh", "printf 'base\\n'\n")
                project.write("dep.sh", "printf 'dep\\n'\n")
                self.assert_compile_observed_error(
                    project,
                    "main.sh",
                    code="runtime.compile.dynamic_command",
                    env={"CMD": "echo"},
                )


if __name__ == "__main__":
    unittest.main()
