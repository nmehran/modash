import json
import re
import shlex
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.source_effects import DiagnosticSeverity
from test.support import ScriptProject


class CompileRegressionTestCase(unittest.TestCase):
    @staticmethod
    def normalized_bash_source_errors(output: str):
        return re.sub(r"^.*?: line [0-9]+: ", "<script>: line N: ", output, flags=re.MULTILINE)

    def test_relative_entry_point_compiles_to_runnable_script(self):
        with ScriptProject() as project:
            output_file = project.path("merged_script.sh")

            project.compile("test/sample_dir/script_main.sh", output=output_file, cwd=REPO_ROOT, mode="executable")
            result = project.run(output_file, cwd=REPO_ROOT)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("This is the main script", result.stdout)

    def test_static_dependency_forms_match_bash(self):
        cases = {
            "relative source": ("main.sh", 'source ./dep.sh\necho "main"\n', {"dep.sh": 'echo "dep"\n'}, None),
            "dot source": ("main.sh", '. ./dep.sh\necho "main"\n', {"dep.sh": 'echo "dep"\n'}, None),
            "parent relative source": (
                "app/main.sh",
                'source ../shared/dep.sh\necho "main"\n',
                {"shared/dep.sh": 'echo "dep"\n'},
                "app",
            ),
            "spaces in path": (
                "main.sh",
                'source "./dir with spaces/dep.sh"\necho "main"\n',
                {"dir with spaces/dep.sh": 'echo "dep"\n'},
                None,
            ),
            "hash in path": (
                "main.sh",
                'source ./dir#tag/dep.sh\necho "main"\n',
                {"dir#tag/dep.sh": 'echo "dep"\n'},
                None,
            ),
            "non sh source": ("main.sh", 'source ./config\necho "main"\n', {"config": 'echo "config"\n'}, None),
        }

        for name, (entry_path, entry_content, files, cwd) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                for path, content in files.items():
                    project.write(path, content)
                project.write(entry_path, entry_content)

                project.assert_compiled_matches(self, entry_path, cwd=cwd)

    def test_builtin_and_command_source_invocations_match_bash(self):
        cases = {
            "builtin source": 'builtin source ./dep.sh builtin-source "two words"\n',
            "builtin dot": 'builtin . ./dep.sh builtin-dot\n',
            "builtin delimiter source": 'builtin -- source ./dep.sh builtin-delimiter-source\n',
            "builtin delimiter dot": 'builtin -- . ./dep.sh builtin-delimiter-dot\n',
            "command source": 'command source ./dep.sh command-source\n',
            "command dot": 'command . ./dep.sh command-dot\n',
            "command path source": 'command -p source ./dep.sh command-path\n',
            "command delimiter source": 'command -- source ./dep.sh command-delimiter\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "dep.sh",
                    'VALUE="${1-default}:${2-none}"\nprintf "dep:%s\\n" "$VALUE"\n',
                )
                project.write("main.sh", main_content + 'printf "main:%s\\n" "$VALUE"\n')

                project.assert_compiled_matches(self, "main.sh")
                compiled = project.path("compiled.sh").read_text()
                self.assertNotIn("builtin source ./dep.sh", compiled)
                self.assertNotIn("builtin . ./dep.sh", compiled)
                self.assertNotIn("builtin -- source ./dep.sh", compiled)
                self.assertNotIn("builtin -- . ./dep.sh", compiled)
                self.assertNotIn("command source ./dep.sh", compiled)
                self.assertNotIn("command . ./dep.sh", compiled)
                self.assertNotIn("command -p source ./dep.sh", compiled)
                self.assertNotIn("command -- source ./dep.sh", compiled)

    def test_builtin_and_command_source_child_shell_invocations_match_bash(self):
        cases = {
            "subshell": '( command source ./dep.sh subshell; printf "child:%s\\n" "$VALUE" )\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "pipeline": 'builtin source ./dep.sh pipeline | sed "s/^/pipe:/"\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "command substitution": 'value="$(command . ./dep.sh substitution; printf "child:%s" "$VALUE")"\nprintf "[%s]\\n" "$value"\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "process substitution": 'cat <(builtin . ./dep.sh process; printf "child:%s\\n" "$VALUE")\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'VALUE="${1-default}"\nprintf "dep:%s\\n" "$VALUE"\n')
                project.write("main.sh", main_content)

                project.assert_compiled_matches(self, "main.sh")
                compiled = project.path("compiled.sh").read_text()
                self.assertNotIn("command source ./dep.sh", compiled)
                self.assertNotIn("command . ./dep.sh", compiled)
                self.assertNotIn("builtin source ./dep.sh", compiled)
                self.assertNotIn("builtin . ./dep.sh", compiled)

    def test_subshell_source_lowering_matches_bash(self):
        cases = {
            "source": '( source ./dep.sh; printf "child:%s\\n" "$VALUE" )\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "dot source": '(. ./dep.sh && printf "child:%s\\n" "$VALUE")\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "source arguments": '( source ./dep.sh alpha "beta gamma"; printf "child:%s\\n" "$VALUE" )\n',
            "nested source": '( source ./outer.sh; printf "child:%s\\n" "$VALUE" )\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'VALUE="${1-default}:${2-none}"\nprintf "dep:%s\\n" "$VALUE"\n')
                project.write("outer.sh", 'source ./dep.sh nested\nVALUE="outer:$VALUE"\n')
                project.write("main.sh", main_content)

                project.assert_compiled_matches(self, "main.sh")
                compiled = project.path("compiled.sh").read_text()
                self.assertNotIn("source ./dep.sh", compiled)
                self.assertNotIn(". ./dep.sh", compiled)
                if "parent:" in main_content:
                    self.assertIn("${VALUE-unset}", compiled)

    def test_pipeline_and_command_substitution_source_lowering_matches_bash(self):
        cases = {
            "pipeline": 'source ./dep.sh | cat\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "pipeline args": 'source ./dep.sh alpha "beta gamma" | cat\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "command substitution": 'value="$(source ./dep.sh; printf "child:%s" "$VALUE")"\nprintf "[%s]\\n" "$value"\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "command substitution args": 'value="$(source ./dep.sh alpha; printf "child:%s" "$VALUE")"\nprintf "[%s]\\n" "$value"\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'VALUE="${1-default}:${2-none}"\nprintf "dep:%s\\n" "$VALUE"\n')
                project.write("main.sh", main_content)

                project.assert_compiled_matches(self, "main.sh")
                compiled = project.path("compiled.sh").read_text()
                self.assertNotIn("source ./dep.sh", compiled)
                if "parent:" in main_content:
                    self.assertIn("${VALUE-unset}", compiled)

    def test_long_leading_pipeline_source_arguments_match_bash(self):
        with ScriptProject() as project:
            project.write("child-shell-target.sh", 'VALUE="${1-default}"\nprintf "dep:%s\\n" "$VALUE"\n')
            project.write("unset.sh", 'printf "followup:unset\\n"\n')
            project.write(
                "main.sh",
                "source ./child-shell-target.sh pipeline | sed 's/^/pipe:/'\n"
                'printf "parent:%s\\n" "${VALUE-unset}"\n'
                'source "./${VALUE-unset}.sh"\n',
            )

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("source ./child-shell-target.sh", compiled)

    def test_final_pipeline_segment_source_remains_fail_closed(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", 'printf "x\\n" | source ./dep.sh\n')
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "lastpipe|pipeline") as cm:
                project.compile("main.sh", output=output, mode="executable")

        self.assertIsNotNone(cm.exception.diagnostic)
        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.child-shell")
        self.assertFalse(output.exists())

    def test_process_substitution_and_bash_c_source_lowering_matches_bash(self):
        cases = {
            "process substitution": 'cat <(source ./dep.sh; printf "child:%s\\n" "$VALUE")\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "process substitution args": 'cat <(source ./dep.sh alpha; printf "child:%s\\n" "$VALUE")\n',
            "bash c double quoted": 'bash -c "source ./dep.sh; printf \\"child:double\\\\n\\""\nprintf "parent:%s\\n" "${VALUE-unset}"\n',
            "bash c dot source": 'bash -c ". ./dep.sh; printf \\"child:dot\\\\n\\""\n',
            "bash c single quoted": "bash -c 'source ./dep.sh; printf \"child:%s\\n\" \"$VALUE\"'\nprintf \"parent:%s\\n\" \"${VALUE-unset}\"\n",
            "bash c source args": "bash -c 'source ./dep.sh alpha \"beta gamma\"; printf \"child:%s\\n\" \"$VALUE\"'\n",
            "bash c env prefix": "CHILD_ENV=exact bash -c 'source ./dep.sh; printf \"env:%s\\n\" \"$CHILD_ENV\"'\n",
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'VALUE="${1-default}:${2-none}"\nprintf "dep:%s\\n" "$VALUE"\n')
                project.write("main.sh", main_content)

                project.assert_compiled_matches(self, "main.sh")
                compiled = project.path("compiled.sh").read_text()
                self.assertNotIn("source ./dep.sh", compiled)
                if name == "bash c env prefix":
                    self.assertIn("CHILD_ENV=exact bash -c", compiled)
                if "parent:" in main_content:
                    self.assertIn("${VALUE-unset}", compiled)

    def test_dynamic_bash_c_source_remains_fail_closed(self):
        cases = {
            "dynamic source path": 'DEP=./dep.sh\nbash -c "source $DEP"\n',
            "parent expanded payload": 'bash -c "source ./dep.sh; echo $VALUE"\n',
            "extra argv": "bash -c 'source ./dep.sh' child-name\n",
            "multi source payload": "bash -c 'source ./dep.sh; source ./other.sh'\n",
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("other.sh", 'echo "other"\n')
                project.write("main.sh", main_content)
                output = project.path("compiled.sh")

                with self.assertRaisesRegex(NotImplementedError, "bash -c|dynamic|parent-expanded") as cm:
                    project.compile("main.sh", output=output, mode="executable")

            self.assertIsNotNone(cm.exception.diagnostic)
            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.child-shell")
            self.assertFalse(output.exists())

    def test_absolute_dependency_forms_match_bash(self):
        with ScriptProject() as project:
            absolute_dep = project.write("dep.sh", 'echo "absolute"\n')
            project.write("main.sh", f'source "{absolute_dep}"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            absolute_dep = project.write("dep.sh", 'echo "absolute var"\n')
            project.write("main.sh", f'DEP_PATH="{absolute_dep}"\nsource "$DEP_PATH"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            absolute_dep = project.write("abs dir#tag/dep.sh", 'echo "absolute special"\n')
            project.write("main.sh", f'source "{absolute_dep}"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_context_sensitive_dependency_forms_match_bash(self):
        with ScriptProject() as project:
            project.write("subdir/dep.sh", 'echo "dep"\n')
            project.write("main.sh", 'cd subdir && source ./dep.sh\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("subdir/data.txt", "payload\n")
            project.write("subdir/worker.sh", textwrap.dedent("""\
                cd "$(dirname "$BASH_SOURCE")" || exit 1
                cat ./data.txt
                """))
            project.write("main.sh", 'cd "$(dirname "$BASH_SOURCE")" || exit 1\nsource ./subdir/worker.sh\n')

            project.assert_compiled_matches(self, "main.sh", cwd=Path("/"))

    def test_dynamic_but_statically_resolvable_sources_match_bash(self):
        cases = {
            "variable dirname": 'THIS_DIR="$(dirname "$BASH_SOURCE")"\nsource "$THIS_DIR/dep.sh"\necho "main"\n',
            "inline dirname": 'source "$(dirname "$BASH_SOURCE")/dep.sh"\necho "main"\n',
            "inline dirname bare": 'source "$(dirname dep.sh)/dep.sh"\necho "main"\n',
            "inline basename trailing slash": 'source "./plugins/$(basename ./plugins/dep.sh/)"\necho "main"\n',
            "inline basename suffix": 'source "./plugins/$(basename ./plugins/dep.sh .sh).sh"\necho "main"\n',
            "realpath": 'source "$(realpath ./dep.sh)"\necho "main"\n',
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("plugins/dep.sh", 'echo "plugin dep"\n')
                project.write("main.sh", content)

                project.assert_compiled_matches(self, "main.sh")

    def test_parameter_default_source_library_uses_environment_value(self):
        with ScriptProject() as project:
            project.write("lib/a.sh", 'echo "a"\n')
            project.write("lib/b.sh", 'echo "b"\n')
            project.write("fallback/unused.sh", 'echo "unused"\n')
            project.write("main.sh", textwrap.dedent("""\
                LIB=${LIB:-./fallback}
                for dep in "$LIB"/*.sh; do
                  source "$dep"
                done
                echo "main"
                """))

            project.assert_compiled_matches(
                self,
                "main.sh",
                env={"LIB": str(project.path("lib"))},
            )

    def test_exact_array_index_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("deps/feature.sh", 'echo "feature"\n')
            project.write("main.sh", textwrap.dedent("""\
                deps=(./unused.sh ./deps/feature.sh)
                source "${deps[1]}"
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_non_source_array_words_match_bash(self):
        with ScriptProject() as project:
            project.write("main.sh", textwrap.dedent("""\
                tokens=(source ./dep.sh)
                printf '%s:%s\n' "${tokens[0]}" "${tokens[1]}"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_literal_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "dep:a:$dep"\n')
            project.write("b.sh", 'echo "dep:b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./a.sh ./b.sh; do
                  echo "before:$dep"
                  source "$dep"
                  echo "after:$dep"
                done
                echo "final:$dep"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_array_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "array:a"\n')
            project.write("deps/b.sh", 'echo "array:b"\n')
            project.write("main.sh", textwrap.dedent("""\
                deps=(./deps/a.sh ./deps/b.sh)
                for dep in "${deps[@]}"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_newline_do_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "newline:a"\n')
            project.write("b.sh", 'echo "newline:b"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./a.sh ./b.sh
                do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_scalar_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "scalar:a"\n')
            project.write("deps/b.sh", 'echo "scalar:b"\n')
            project.write("main.sh", textwrap.dedent("""\
                FIRST=./deps/a.sh
                SECOND=./deps/b.sh
                for dep in "$FIRST" "$SECOND"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "scalar wordlist:a:$dep"\n')
            project.write("deps/b.sh", 'echo "scalar wordlist:b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEPS="./deps/a.sh   ./deps/b.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps dir#tag/a dep.sh", 'echo "quoted scalar special:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEP="./deps dir#tag/a dep.sh"
                for dep in "$DEP"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "scalar glob:b:$dep"\n')
            project.write("plugins/a.sh", 'echo "scalar glob:a:$dep"\n')
            project.write("plugins/readme.txt", 'echo "not sourced"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEPS="./plugins/*.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_empty_scalar_for_loop_sources_match_bash_without_live_source(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "should not run"\n')
            project.write("main.sh", textwrap.dedent("""\
                dep=./a.sh
                DEPS=""
                for dep in $DEPS; do source "$dep"; done
                echo "done:$dep"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn('\n  source "$dep"', compiled_content)

    def test_exact_for_loop_repeated_same_line_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "repeat:a"\n')
            project.write("b.sh", 'echo "repeat:b"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./a.sh ./b.sh; do
                  source "$dep"; source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_for_loop_special_paths_match_bash(self):
        with ScriptProject() as project:
            project.write("deps dir#tag/a dep.sh", 'echo "special:a:$dep"\n')
            project.write("deps dir#tag/b dep.sh", 'echo "special:b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in "./deps dir#tag/a dep.sh" "./deps dir#tag/b dep.sh"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_for_loop_absolute_paths_match_bash(self):
        with ScriptProject() as project:
            first = project.write("deps/a.sh", 'echo "absolute:a"\n')
            second = project.write("deps/b.sh", 'echo "absolute:b"\n')
            project.write("main.sh", textwrap.dedent(f"""\
                for dep in "{first}" "{second}"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_for_loop_cwd_sensitive_source_expression_matches_bash(self):
        with ScriptProject() as project:
            project.write("one dir/dep.sh", 'echo "cwd:one:$PWD"\n')
            project.write("two#dir/dep.sh", 'echo "cwd:two:$PWD"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dir in "one dir" two#dir; do
                  cd "$dir"
                  source ./dep.sh
                  cd ..
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_glob_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "plugin:b:$dep"\n')
            project.write("plugins/a.sh", 'echo "plugin:a:$dep"\n')
            project.write("plugins/readme.txt", 'echo "not sourced"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

    def test_glob_for_loop_special_paths_match_bash(self):
        with ScriptProject() as project:
            project.write("plugin {dir}#tag/b dep.sh", 'echo "special:b:$dep"\n')
            project.write("plugin {dir}#tag/a dep.sh", 'echo "special:a:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in "./plugin {dir}#tag"/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

    def test_glob_for_loop_after_cd_matches_bash(self):
        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "cd:b:$PWD:$dep"\n')
            project.write("plugins/a.sh", 'echo "cd:a:$PWD:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                cd plugins
                for dep in ./*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

    def test_glob_option_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("plugins/.hidden.sh", 'echo "dotglob:hidden:$dep"\n')
            project.write("plugins/a.sh", 'echo "dotglob:a:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s dotglob
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "globstar:a:$dep"\n')
            project.write("plugins/nested/b.sh", 'echo "globstar:b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s globstar
                for dep in ./plugins/**/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/one/a.sh", 'echo "no globstar one-level:$dep"\n')
            project.write("plugins/one/deep/b.sh", 'echo "no globstar deep:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/**/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "nocase:a:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s nocaseglob
                for dep in ./plugins/*.SH; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "extglob:a:$dep"\n')
            project.write("plugins/b.sh", 'echo "extglob:b:$dep"\n')
            project.write("plugins/c.sh", 'echo "unexpected:c:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                for dep in ./plugins/@(a|b).sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/pre.sh", 'echo "extglob-zero-or-more:$dep"\n')
            project.write("plugins/pred.sh", 'echo "extglob-one-d:$dep"\n')
            project.write("plugins/predd.sh", 'echo "extglob-two-d:$dep"\n')
            project.write("plugins/b.sh", 'echo "extglob-optional:$dep"\n')
            project.write("plugins/ab.sh", 'echo "extglob-optional-a:$dep"\n')
            project.write("plugins/c.sh", 'echo "extglob-plus:$dep"\n')
            project.write("plugins/cc.sh", 'echo "extglob-plus-two:$dep"\n')
            project.write("plugins/skip.sh", 'echo "unexpected skip:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                for dep in ./plugins/?(a)b.sh ./plugins/+(c).sh ./plugins/pre*(d).sh ./plugins/!(skip).sh; do
                  source "$dep"
                done
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh", env={"LC_ALL": "C"})
            actual = project.run(output, env={"LC_ALL": "C"})
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("unexpected skip", compiled_content)

    def test_brace_and_nullglob_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "brace:a:$dep"\n')
            project.write("plugins/b.sh", 'echo "brace:b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/{a,b}.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("deps/01.sh", 'echo "seq:01:$dep"\n')
            project.write("deps/02.sh", 'echo "seq:02:$dep"\n')
            project.write("deps/03.sh", 'echo "seq:03:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./deps/{01..03}.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("deps/1.sh", 'echo "seq-step:1:$dep"\n')
            project.write("deps/3.sh", 'echo "seq-step:3:$dep"\n')
            project.write("deps/5.sh", 'echo "seq-step:5:$dep"\n')
            project.write("letters/a.sh", 'echo "seq-letter:a:$dep"\n')
            project.write("letters/b.sh", 'echo "seq-letter:b:$dep"\n')
            project.write("letters/c.sh", 'echo "seq-letter:c:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./deps/{1..5..-2}.sh ./letters/{c..a..1}.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("main.sh", textwrap.dedent("""\
                shopt -s nullglob
                for dep in ./missing/*.sh; do
                  source "$dep"
                done
                echo done
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn('\n  source "$dep"', compiled_content)

    def test_globignore_for_loop_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("plugins/.hidden.sh", 'echo "globignore:hidden:$dep"\n')
            project.write("plugins/a.sh", 'echo "globignore:a:$dep"\n')
            project.write("plugins/b.sh", 'echo "globignore:b:$dep"\n')
            project.write("plugins/c.sh", 'echo "globignore:c:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                GLOBIGNORE=./plugins/b.sh:./plugins/c.sh
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "globignore extglob:a:$dep"\n')
            project.write("plugins/b.sh", 'echo "unexpected:b:$dep"\n')
            project.write("plugins/c.sh", 'echo "unexpected:c:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                GLOBIGNORE=./plugins/@(b|c).sh
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

    def test_direct_source_glob_with_single_match_matches_bash(self):
        with ScriptProject() as project:
            project.write("plugins/only.sh", 'echo "single glob"\n')
            project.write("main.sh", 'source ./plugins/*.sh\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "direct extglob:$1:$2"\n')
            project.write("plugins/b.sh", 'echo "unexpected direct extglob"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                source ./plugins/@(a|b).sh explicit
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

        with ScriptProject() as project:
            project.write("plugins/@(a|b)-literal.sh", 'echo "quoted extglob literal"\n')
            project.write("plugins/a-literal.sh", 'echo "unexpected extglob expansion"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                source ./plugins/"@(a|b)"-*.sh
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh", env={"LC_ALL": "C"})
            actual = project.run(output, env={"LC_ALL": "C"})
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("unexpected extglob expansion", compiled_content)

    def test_direct_source_glob_with_quoted_literal_path_chars_matches_bash(self):
        with ScriptProject() as project:
            project.write("plugin {dir}#tag/only dep.sh", 'echo "special single glob"\n')
            project.write("main.sh", 'source "./plugin {dir}#tag"/*.sh\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh", env={"LC_ALL": "C"})

    def test_missing_direct_source_globs_lower_runtime_failures(self):
        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "unexpected:a"\n')
            project.write("plugins/b.sh", 'echo "unexpected:b"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./missing/*.sh
                printf 'direct:%s\\n' "$?"
                source ./missing/*.sh || printf 'optional:%s\\n' "$?"
                if source ./missing/*.sh; then
                  echo wrong-if
                else
                  printf 'if:%s\\n' "$?"
                fi
                if ! source ./missing/*.sh; then
                  printf 'negated:%s\\n' "$?"
                fi
                GLOBIGNORE=./plugins/a.sh:./plugins/b.sh
                source ./plugins/*.sh
                printf 'globignore:%s\\n' "$?"
                shopt -s nullglob
                source ./empty/*.sh
                printf 'nullglob:%s\\n' "$?"
                """))

            output = project.compile("main.sh", mode="executable")
            compiled_content = output.read_text()
            expected = project.run("main.sh")
            actual = project.run(output)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(
            self.normalized_bash_source_errors(actual.stdout),
            self.normalized_bash_source_errors(expected.stdout),
        )
        self.assertNotIn("source ./missing/*.sh", compiled_content)
        self.assertNotIn("source ./plugins/*.sh", compiled_content)
        self.assertNotIn("source ./empty/*.sh", compiled_content)

    def test_missing_loop_source_globs_lower_runtime_failures(self):
        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "unexpected:a"\n')
            project.write("plugins/b.sh", 'echo "unexpected:b"\n')
            project.write("brace/real.sh", 'printf "brace-real:%s\\n" "$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./missing/*.sh; do
                  source "$dep" || printf 'loop:%s\\n' "$?"
                done
                for dep in ./brace/{missing,real}.sh; do
                  source "$dep" || printf 'brace:%s\\n' "$?"
                done
                DEPS="./scalar-missing/*.sh"
                for dep in $DEPS; do
                  source "$dep" || printf 'scalar:%s\\n' "$?"
                done
                GLOBIGNORE=./plugins/a.sh:./plugins/b.sh
                for dep in ./plugins/*.sh; do
                  source "$dep" || printf 'globignore:%s\\n' "$?"
                done
                shopt -s nullglob
                for dep in ./empty/*.sh; do
                  source "$dep"
                done
                printf 'null-loop:%s\\n' "$?"
                """))

            output = project.compile("main.sh", mode="executable")
            compiled_content = output.read_text()
            expected = project.run("main.sh")
            actual = project.run(output)

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(
            self.normalized_bash_source_errors(actual.stdout),
            self.normalized_bash_source_errors(expected.stdout),
        )
        self.assertNotIn('\n  source "$dep"', compiled_content)

    def test_direct_brace_source_expansion_matches_bash(self):
        with ScriptProject() as project:
            project.write("real.sh", 'printf "brace-real:%s:%s\\n" "$1" "$?"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./{real,missing}.sh
                printf 'after:%s\\n' "$?"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("real.sh", 'echo "unexpected real"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./{missing,real}.sh
                printf 'missing-first:%s\\n' "$?"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(
            self.normalized_bash_source_errors(actual.stdout),
            self.normalized_bash_source_errors(expected.stdout),
        )
        self.assertNotIn("unexpected real", compiled_content)

        with ScriptProject() as project:
            project.write("real.sh", 'printf "brace-options:%s:%s\\n" "$1" "$?"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s nullglob failglob
                GLOBIGNORE='*.sh'
                source ./{real,missing}.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_nullglob_source_word_shift_matches_bash(self):
        with ScriptProject() as project:
            project.write("fallback.sh", 'printf "fallback:%s:%s\\n" "$1" "$?"\n')
            project.write("plugins/a.sh", 'printf "plugin-a:%s:%s\\n" "$1" "$2"\n')
            project.write("plugins/b.sh", 'echo "unexpected plugin-b source"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s nullglob
                source ./missing/*.sh ./fallback.sh arg
                printf 'fallback-status:%s\\n' "$?"
                source ./empty/*.sh ./plugins/*.sh explicit
                printf 'plugin-status:%s\\n' "$?"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("source ./missing/*.sh ./fallback.sh", compiled_content)
        self.assertNotIn("source ./empty/*.sh ./plugins/*.sh", compiled_content)
        self.assertNotIn("unexpected plugin-b source", compiled_content)

    def test_expanded_source_arguments_allow_non_files_after_source_file_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'printf "dep:%s:%s:%s\\n" "$1" "$2" "$3"\n')
            project.mkdir("args/dir")
            project.write("args/file", "argument file\n")
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh ./args/*
                source ./{dep.sh,args/dir} brace
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("source ./dep.sh ./args/*", compiled_content)
        self.assertNotIn("source ./{dep.sh,args/dir}", compiled_content)

    def test_failglob_source_expansion_failure_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "unexpected dep source"\n')
            project.write("plugins/a.sh", 'echo "unexpected plugin source"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s failglob
                source ./missing/*.sh
                printf 'direct:%s\\n' "$?"
                echo before; source ./missing/*.sh; echo SHOULD_NOT_RUN
                printf 'same-line:%s\\n' "$?"
                source ./dep.sh ./missing/*.sh
                printf 'argument:%s\\n' "$?"
                GLOBIGNORE=./plugins/a.sh
                source ./plugins/*.sh
                printf 'globignore:%s\\n' "$?"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(
            self.normalized_bash_source_errors(actual.stdout),
            self.normalized_bash_source_errors(expected.stdout),
        )
        self.assertNotIn("SHOULD_NOT_RUN", actual.stdout)
        self.assertNotIn("unexpected dep source", compiled_content)
        self.assertNotIn("unexpected plugin source", compiled_content)

    def test_failglob_source_conditions_match_bash(self):
        cases = {
            "one-line if": textwrap.dedent("""\
                shopt -s failglob
                if source ./missing/*.sh; then echo yes; fi
                printf 'status:%s\\n' "$?"
                """),
            "multiline if else": textwrap.dedent("""\
                shopt -s failglob
                if source ./missing/*.sh
                then
                  echo yes
                else
                  echo no
                fi
                printf 'status:%s\\n' "$?"
                """),
            "errexit": textwrap.dedent("""\
                set -e
                shopt -s failglob
                if source ./missing/*.sh; then echo yes; fi
                echo SHOULD_NOT_RUN
                """),
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", content)

                output = project.compile("main.sh", mode="executable")
                expected = project.run("main.sh")
                actual = project.run(output)
                compiled_content = output.read_text()

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(
                self.normalized_bash_source_errors(actual.stdout),
                self.normalized_bash_source_errors(expected.stdout),
            )
            self.assertNotIn("source ./missing/*.sh", compiled_content)
            self.assertNotIn("echo yes", compiled_content)
            self.assertNotIn("echo no", compiled_content)

    def test_failglob_loop_word_failures_match_bash(self):
        cases = {
            "inline": textwrap.dedent("""\
                shopt -s failglob
                for file in ./missing/*.sh; do source "$file"; done
                printf 'status:%s\\n' "$?"
                """),
            "multiline": textwrap.dedent("""\
                shopt -s failglob
                for file in ./missing/*.sh
                do
                  source "$file"
                  echo SHOULD_NOT_RUN
                done
                printf 'status:%s\\n' "$?"
                """),
            "errexit": textwrap.dedent("""\
                set -e
                shopt -s failglob
                for file in ./missing/*.sh
                do
                  source "$file"
                done
                echo SHOULD_NOT_RUN
                """),
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("main.sh", content)

                output = project.compile("main.sh", mode="executable")
                expected = project.run("main.sh")
                actual = project.run(output)
                compiled_content = output.read_text()

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(
                self.normalized_bash_source_errors(actual.stdout),
                self.normalized_bash_source_errors(expected.stdout),
            )
            self.assertNotIn('source "$file"', compiled_content)
            self.assertNotIn("SHOULD_NOT_RUN", actual.stdout)

    def test_source_free_failglob_loop_does_not_dirty_later_source_state(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'printf "dep:%s\\n" "$DEP"\n')
            project.write("other.sh", 'echo "unexpected other"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s failglob
                DEP=./dep.sh
                for file in ./missing/*.sh
                do
                  DEP=./other.sh
                done
                source "$DEP"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = project.path("compiled.sh").read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(
            self.normalized_bash_source_errors(actual.stdout),
            self.normalized_bash_source_errors(expected.stdout),
        )
        self.assertNotIn("unexpected other", compiled_content)
        self.assertNotIn('source "$DEP"', compiled_content)

    def test_failglob_function_body_failures_match_bash(self):
        cases = {
            "compact": textwrap.dedent("""\
                shopt -s failglob
                load(){ echo before; source ./missing/*.sh; echo SHOULD_NOT_RUN_FUNC; }
                load; echo SHOULD_NOT_RUN_LINE
                printf 'status:%s\\n' "$?"
                """),
            "multiline": textwrap.dedent("""\
                shopt -s failglob
                load(){
                  echo before
                  source ./missing/*.sh
                  echo SHOULD_NOT_RUN_FUNC
                }
                load
                echo next
                printf 'status:%s\\n' "$?"
                """),
            "nested": textwrap.dedent("""\
                shopt -s failglob
                inner(){ echo inner-before; source ./missing/*.sh; echo SHOULD_NOT_RUN_INNER; }
                outer(){ echo outer-before; inner; echo SHOULD_NOT_RUN_OUTER; }
                outer; echo SHOULD_NOT_RUN_LINE
                echo next
                """),
            "errexit": textwrap.dedent("""\
                set -e
                shopt -s failglob
                load(){ echo before; source ./missing/*.sh; echo SHOULD_NOT_RUN_FUNC; }
                load
                echo SHOULD_NOT_RUN_LINE
                """),
            "source-free loop before later source": textwrap.dedent("""\
                shopt -s failglob
                load(){
                  for file in ./missing/*.sh
                  do
                    echo SHOULD_NOT_RUN_LOOP
                  done
                  source ./dep.sh
                  echo SHOULD_NOT_RUN_FUNC
                }
                load; echo SHOULD_NOT_RUN_LINE
                echo next
                """),
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo SHOULD_NOT_RUN_DEP\n')
                project.write("main.sh", content)

                output = project.compile("main.sh", mode="executable")
                expected = project.run("main.sh")
                actual = project.run(output)
                compiled_content = output.read_text()

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(
                self.normalized_bash_source_errors(actual.stdout),
                self.normalized_bash_source_errors(expected.stdout),
            )
            self.assertNotIn("source ./missing/*.sh", compiled_content)
            self.assertNotIn("source ./dep.sh", compiled_content)
            self.assertNotIn("SHOULD_NOT_RUN_DEP", compiled_content)
            self.assertNotIn("SHOULD_NOT_RUN", actual.stdout)
        self.assertNotIn("source ./missing/*.sh", compiled_content)

    def test_if_block_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("optional.sh", 'echo "optional:$LOAD_OPTIONAL"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$LOAD_OPTIONAL" ]]; then
                  source ./optional.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LOAD_OPTIONAL": "1"})
            project.assert_compiled_matches(self, "main.sh", env={"LOAD_OPTIONAL": ""})

    def test_if_else_branch_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod:$MODE"\n')
            project.write("dev.sh", 'echo "dev:$MODE"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ "$MODE" == prod ]]; then
                  source ./prod.sh
                else
                  source ./dev.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"MODE": "prod"})
            project.assert_compiled_matches(self, "main.sh", env={"MODE": "dev"})

    def test_if_elif_branch_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod:$MODE"\n')
            project.write("stage.sh", 'echo "stage:$MODE"\n')
            project.write("dev.sh", 'echo "dev:$MODE"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ "$MODE" == prod ]]; then
                  source ./prod.sh
                elif [[ "$MODE" == stage ]]; then
                  source ./stage.sh
                else
                  source ./dev.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"MODE": "prod"})
            project.assert_compiled_matches(self, "main.sh", env={"MODE": "stage"})
            project.assert_compiled_matches(self, "main.sh", env={"MODE": "dev"})

    def test_if_bracket_and_test_predicates_match_bash(self):
        with ScriptProject() as project:
            project.write("bracket.sh", 'echo "bracket"\n')
            project.write("testdep.sh", 'echo "test predicate"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [ -f ./bracket.sh ]; then
                  source ./bracket.sh
                fi
                if test -f ./testdep.sh; then
                  source ./testdep.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_compound_if_predicates_match_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "compound dep"\n')
            project.write("fallback.sh", 'echo "fallback dep"\n')
            project.write("mismatch.sh", 'echo "should not be inlined"\n')
            project.write("variable-pattern.sh", 'echo "variable pattern dep"\n')
            project.write("extglob-pattern.sh", 'echo "extglob pattern dep"\n')
            project.write("nocasematch.sh", 'echo "nocasematch dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                LOAD_DEP=1
                MODE=prod-eu
                PATTERN=prod*
                if [[ -f ./dep.sh && -n "$LOAD_DEP" ]]; then
                  source ./dep.sh
                fi
                if [[ -f ./missing.sh || "$LOAD_DEP" == 1 ]]; then
                  source ./fallback.sh
                fi
                if [[ "$MODE" != prod* ]]; then
                  source ./mismatch.sh
                fi
                if [[ "$MODE" == $PATTERN ]]; then
                  source ./variable-pattern.sh
                fi
                if [[ "$MODE" == @(prod-eu|stage) ]]; then
                  source ./extglob-pattern.sh
                fi
                shopt -s nocasematch
                if [[ "$MODE" == PROD-EU ]]; then
                  source ./nocasematch.sh
                fi
                echo "main"
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("should not be inlined", compiled_content)

    def test_arithmetic_regex_and_grep_if_predicates_match_bash(self):
        with ScriptProject() as project:
            project.write("arithmetic.sh", 'echo "arithmetic:$COUNT"\n')
            project.write("numeric.sh", 'echo "numeric:$COUNT"\n')
            project.write("regex.sh", 'echo "regex:$MODE"\n')
            project.write("pattern.sh", 'echo "pattern:$MODE"\n')
            project.write("grep.sh", 'echo "grep"\n')
            project.write("grep-regex.sh", 'echo "grep regex"\n')
            project.write("config", "enabled=true\n")
            project.write("main.sh", textwrap.dedent("""\
                COUNT=2
                MODE=prod-eu
                if (( COUNT > 1 )); then
                  source ./arithmetic.sh
                fi
                if [[ "$COUNT" -gt 1 ]]; then
                  source ./numeric.sh
                fi
                if [[ "$MODE" =~ ^prod ]]; then
                  source ./regex.sh
                fi
                if [[ "$MODE" == prod* ]]; then
                  source ./pattern.sh
                fi
                if grep -q enabled config; then
                  source ./grep.sh
                fi
                if grep -Eq '^enabled=true$' config; then
                  source ./grep-regex.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_if_unreachable_sources_match_bash(self):
        cases = {
            "unreachable else source": textwrap.dedent("""\
                MODE=prod
                if [[ "$MODE" == prod ]]; then
                  source ./prod.sh
                else
                  source ./missing.sh
                fi
                echo "main"
                """),
            "unreachable then source": textwrap.dedent("""\
                MODE=dev
                if [[ "$MODE" == prod ]]; then
                  source ./missing.sh
                else
                  source ./dev.sh
                fi
                echo "main"
                """),
            "missing optional file guard": textwrap.dedent("""\
                if [[ -f ./missing-optional.sh ]]; then
                  source ./missing-optional.sh
                fi
                echo "main"
                """),
            "inline unreachable source": textwrap.dedent("""\
                MODE=prod
                if [[ "$MODE" == prod ]]; then source ./prod.sh; else source ./missing.sh; fi
                echo "main"
                """),
            "unreachable command source": textwrap.dedent("""\
                MODE=prod
                if [[ "$MODE" == prod ]]; then
                  echo "prod"
                else
                  eval "source ./missing.sh"
                fi
                echo "main"
                """),
            "unreachable command dot source": textwrap.dedent("""\
                MODE=prod
                if [[ "$MODE" == prod ]]; then
                  echo "prod"
                else
                  eval ". ./missing.sh"
                fi
                echo "main"
                """),
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("prod.sh", 'echo "prod"\n')
                project.write("dev.sh", 'echo "dev"\n')
                project.write("main.sh", content)

                project.assert_compiled_matches(self, "main.sh")

    def test_case_block_sources_match_bash(self):
        cases = {
            "assigned subject": (
                textwrap.dedent("""\
                    ENV=prod
                    case "$ENV" in
                      prod) source ./prod.sh ;;
                      dev) source ./missing-dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "environment subject": (
                textwrap.dedent("""\
                    case "$ENV" in
                      prod) source ./prod.sh ;;
                      dev) source ./dev.sh ;;
                    esac
                    echo "main"
                    """),
                {"ENV": "dev"},
            ),
            "default arm": (
                textwrap.dedent("""\
                    ENV=qa
                    case "$ENV" in
                      prod) source ./missing-prod.sh ;;
                      *) source ./default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "alternate patterns": (
                textwrap.dedent("""\
                    ENV=stage
                    case "$ENV" in
                      prod|stage) source ./prod.sh ;;
                      dev) source ./missing-dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "glob pattern": (
                textwrap.dedent("""\
                    ENV=prod-eu
                    case "$ENV" in
                      prod-*) source ./prod.sh ;;
                      dev) source ./missing-dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "quoted literal pattern": (
                textwrap.dedent("""\
                    ENV='prod-*'
                    case "$ENV" in
                      "prod-*") source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "escaped literal pattern": (
                textwrap.dedent("""\
                    ENV='prod-*'
                    case "$ENV" in
                      prod\\-\\*) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "mixed quoted pattern": (
                textwrap.dedent("""\
                    ENV=prod-eu
                    case "$ENV" in
                      prod"-"*) source ./prod.sh ;;
                      dev) source ./missing-dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "bracket pattern": (
                textwrap.dedent("""\
                    ENV=b
                    case "$ENV" in
                      [abc]) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "posix class pattern": (
                textwrap.dedent("""\
                    ENV=5
                    case "$ENV" in
                      [[:digit:]]) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "quoted variable pattern": (
                textwrap.dedent("""\
                    ENV=prod
                    PATTERN=prod
                    case "$ENV" in
                      "$PATTERN") source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "unquoted variable glob pattern": (
                textwrap.dedent("""\
                    ENV=prod-eu
                    PATTERN='prod-*'
                    case "$ENV" in
                      $PATTERN) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                """),
                None,
            ),
            "extglob pattern": (
                textwrap.dedent("""\
                    shopt -s extglob
                    ENV=prod
                    case "$ENV" in
                      @(prod|stage)) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "negative extglob pattern": (
                textwrap.dedent("""\
                    shopt -s extglob
                    ENV=qa
                    case "$ENV" in
                      !(prod|stage)) source ./default.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "variable extglob pattern": (
                textwrap.dedent("""\
                    shopt -s extglob
                    ENV=stage
                    PATTERN="@(prod|stage)"
                    case "$ENV" in
                      $PATTERN) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "unquoted variable pattern keeps expanded quotes literal": (
                textwrap.dedent("""\
                    PATTERN='"prod"'
                    case '"prod"' in
                      $PATTERN) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "single quoted subject ignores process environment": (
                textwrap.dedent("""\
                    case '$ENV' in
                      '$ENV') source ./prod.sh ;;
                      prod) source ./dev.sh ;;
                    esac
                    echo "main"
                    """),
                {"ENV": "prod"},
            ),
            "mixed single quoted subject ignores process environment": (
                textwrap.dedent("""\
                    case x'$ENV' in
                      'x$ENV') source ./prod.sh ;;
                      xprod) source ./dev.sh ;;
                    esac
                    echo "main"
                    """),
                {"ENV": "prod"},
            ),
            "single quoted command substitution subject is literal": (
                textwrap.dedent("""\
                    case '$(echo prod)' in
                      '$(echo prod)') source ./prod.sh ;;
                      prod) source ./dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "single quoted command substitution pattern is literal": (
                textwrap.dedent("""\
                    ENV='$(echo prod)'
                    case "$ENV" in
                      '$(echo prod)') source ./prod.sh ;;
                      prod) source ./dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "no matching arm": (
                textwrap.dedent("""\
                    ENV=qa
                    case "$ENV" in
                      prod) source ./missing-prod.sh ;;
                      dev) source ./missing-dev.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "inline arms": (
                textwrap.dedent("""\
                    ENV=prod
                    case "$ENV" in prod) source ./prod.sh ;; dev) source ./missing-dev.sh ;; esac
                    echo "main"
                    """),
                None,
            ),
            "quoted subject containing in": (
                textwrap.dedent("""\
                    case "value in prod" in
                      *prod) source ./prod.sh ;;
                      *) source ./missing-default.sh ;;
                    esac
                    echo "main"
                    """),
                None,
            ),
            "unknown source-free case": (
                textwrap.dedent("""\
                    case "$ENV" in
                      prod) echo "prod mode" ;;
                      *) echo "other mode" ;;
                    esac
                    source ./prod.sh
                    echo "main"
                    """),
                None,
            ),
        }

        for name, (content, env) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("prod.sh", 'echo "prod:$ENV"\n')
                project.write("dev.sh", 'echo "dev:$ENV"\n')
                project.write("default.sh", 'echo "default:$ENV"\n')
                project.write("main.sh", content)

                project.assert_compiled_matches(self, "main.sh", env=env)

    def test_case_fallthrough_sources_match_bash(self):
        cases = {
            "fallthrough executes next arm": textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) source ./prod.sh ;&
                  *) source ./default.sh ;;
                esac
                echo "main"
                """),
            "fallthrough test matches later arm": textwrap.dedent("""\
                ENV=prod-eu
                case "$ENV" in
                  prod-*) source ./prod.sh ;;&
                  *-eu) source ./default.sh ;;
                  dev) source ./missing-dev.sh ;;
                esac
                echo "main"
                """),
            "fallthrough preserves sequential state": textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) DEP=./prod.sh ;&
                  *) source "$DEP" ;;
                esac
                echo "main"
                """),
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("prod.sh", 'echo "prod:$ENV"\n')
                project.write("default.sh", 'echo "default:$ENV"\n')
                project.write("main.sh", content)

                project.assert_compiled_matches(self, "main.sh")

    def test_runtime_case_fallthrough_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod:$ENV"\n')
            project.write("default.sh", 'echo "default:$ENV"\n')
            project.write("main.sh", textwrap.dedent("""\
                case "$ENV" in
                  prod) source ./prod.sh ;&
                  *) source ./default.sh ;;
                esac
                echo "main"
                """))

            output = project.compile("main.sh", mode="executable")
            compiled_content = output.read_text()

            self.assertNotIn("source ./prod.sh", compiled_content)
            self.assertNotIn("source ./default.sh", compiled_content)
            for env in ("prod", "dev"):
                with self.subTest(env=env):
                    expected = project.run("main.sh", env={"ENV": env})
                    actual = project.run(output, env={"ENV": env})
                    self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                    self.assertEqual(actual.stdout, expected.stdout)

    def test_case_block_state_after_arm_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "after:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) DEP=./dep.sh ;;
                  dev) DEP=./missing.sh ;;
                esac
                source "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_if_branch_local_state_matches_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "branch:a:$DEP"\n')
            project.write("b.sh", 'echo "branch:b:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_A" ]]; then
                  DEP=./a.sh
                  source "$DEP"
                else
                  DEP=./b.sh
                  source "$DEP"
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"USE_A": "1"})
            project.assert_compiled_matches(self, "main.sh", env={"USE_A": ""})

    def test_if_branch_local_cd_matches_bash(self):
        with ScriptProject() as project:
            project.write("enabled/dep.sh", 'echo "enabled:$PWD"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$LOAD_ENABLED" ]]; then
                  cd enabled
                  source ./dep.sh
                fi
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"LOAD_ENABLED": "1"})
            project.assert_compiled_matches(self, "main.sh", env={"LOAD_ENABLED": ""})

    def test_if_converged_state_after_branch_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "after:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$FLAG" ]]; then
                  DEP=./dep.sh
                else
                  DEP=./dep.sh
                fi
                source "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh", env={"FLAG": "1"})
            project.assert_compiled_matches(self, "main.sh", env={"FLAG": ""})

    def test_environment_absolute_source_matches_bash(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep from env"\n')
            project.write("main.sh", 'source "$DEP_PATH"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh", env={"DEP_PATH": dep})

    def test_safe_cat_dynamic_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep from cat"\n')
            project.write("dep-path.txt", "./dep.sh\n")
            project.write("main.sh", 'source "$(cat dep-path.txt)"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps dir#tag/dep file.sh", 'echo "dep from special cat path"\n')
            project.write("path files/dep#path.txt", "./deps dir#tag/dep file.sh\n")
            project.write("main.sh", 'source "$(cat \'path files/dep#path.txt\')"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_safe_find_dynamic_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("plugins/init.sh", 'echo "dep from find"\n')
            project.write("main.sh", 'source "$(find ./plugins -type f -name init.sh -print -quit)"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/root.sh", 'echo "root should not match depth"\n')
            project.write("plugins/nested/init.sh", 'echo "dep from filtered find"\n')
            project.write("main.sh", (
                'source "$(find ./plugins -maxdepth 2 -mindepth 2 '
                '-path ./plugins/nested/init.sh -print -quit)"\n'
                'echo "main"\n'
            ))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugin dir#tag/init.sh", 'echo "dep from special find root"\n')
            project.write("main.sh", 'source "$(find \'./plugin dir#tag\' -type f -name init.sh -print -quit)"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_safe_eval_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep from eval"\n')
            project.write("main.sh", 'eval "source ./dep.sh"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep from eval var"\n')
            project.write("main.sh", 'DEP_PATH="{0}"\neval ". \\"$DEP_PATH\\""\necho "main"\n'.format(dep))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep from eval command var"\n')
            project.write("main.sh", 'COMMAND="source ./dep.sh"\neval "$COMMAND"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_eval_source_replacement_ignores_quoted_decoys(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep from eval"\n')
            project.write("main.sh", 'echo \'eval "source ./dep.sh"\'; eval "source ./dep.sh"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_eval_source_after_logical_operator_matches_bash(self):
        with ScriptProject() as project:
            project.write("subdir/dep.sh", 'echo "dep from chained eval"\n')
            project.write("main.sh", 'cd subdir && eval "source ./dep.sh"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

        for operator_script in (
            'false && eval "source ./missing.sh"\necho "after:$?"\n',
            'true || eval "source ./missing.sh"\necho "after:$?"\n',
        ):
            with ScriptProject() as project:
                project.write("main.sh", operator_script)

                project.assert_compiled_matches(self, "main.sh")
                self.assertNotIn("source ./missing.sh", project.path("compiled.sh").read_text())

    def test_non_source_eval_and_bash_c_commands_match_bash(self):
        with ScriptProject() as project:
            project.write("main.sh", 'eval "echo from eval"\nbash -c "echo from child"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_non_source_continued_eval_does_not_block_later_source(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write(
                "lib.sh",
                "run_tool() {\n"
                "  eval '\"$merge_tool_path\"' \\\n"
                "    -f \"$FINAL_CMD\" '\"$LOCAL\"' '\"$REMOTE\"'\n"
                "}\n",
            )
            project.write("main.sh", "source ./lib.sh\nsource ./dep.sh\n")

            compiled = project.compile("main.sh", mode="executable")
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\n")

    def test_source_state_on_same_logical_line_matches_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'FROM_A=./b.sh\n')
            project.write("b.sh", 'echo "dep from variable set by first source"\n')
            project.write("main.sh", 'source ./a.sh && source "$FROM_A"\necho "main"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_runtime_source_reference_before_source_on_same_line_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep after bash source"\n')
            project.write("main.sh", 'echo "$BASH_SOURCE"; source ./dep.sh\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_loop_heredoc_source_text_is_not_treated_as_dependency(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "loop dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./dep.sh; do
                  cat <<EOF
                source "$dep"
                EOF
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_command_substitution_loop_word_lists_match_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./a.sh\n./b.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                echo done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(cat deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "b"\n')
            project.write("plugins/a.sh", 'echo "a"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(find ./plugins -type f -name '*.sh' -print); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(find ./plugins", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "absolute find dep"\n')
            project.write("main.sh", textwrap.dedent(f"""\
                for dep in $(find {project.root}/plugins -type f -name '*.sh' -print); do
                  echo "dep=$dep"
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("$(find ", compiled)
            self.assertIn(str(project.root / "plugins" / "a.sh"), compiled)

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/b.sh", 'echo "b"\n')
            project.write("deps.txt", "./plugins/*.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(cat deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("deps dir#tag/a dep.sh", 'echo "special loop dep"\n')
            project.write("dep-path.txt", "./deps dir#tag/a dep.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                for dep in "$(cat dep-path.txt)"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(cat dep-path.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("a.sh", 'echo "sorted:a"\n')
            project.write("b.sh", 'echo "sorted:b"\n')
            project.write("deps.txt", "./b.sh\n./a.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(sort deps.txt); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(sort deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("a.sh", 'echo "head:a"\n')
            project.write("b.sh", 'echo "head:b"\n')
            project.write("deps.txt", "./a.sh\n./b.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(head -n 1 deps.txt); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(head -n 1 deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "needle"\n')
            project.write("deps/b.sh", 'echo "needle"\n')
            project.write("deps/c.sh", 'echo "other"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(grep -lF needle ./deps/*.sh); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(grep -lF needle", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("plugins/a/init.sh", 'echo "dirname:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dir in $(dirname ./plugins/a/file.txt); do
                  dep="$dir/init.sh"
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dirname bare:$dir"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dir in $(dirname dep.sh); do
                  source "$dir/dep.sh"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/dep.sh", 'echo "basename trailing:$name"\n')
            project.write("main.sh", textwrap.dedent("""\
                for name in $(basename ./plugins/dep.sh/); do
                  source "./plugins/$name"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/dep.sh", 'echo "basename suffix:$name"\n')
            project.write("main.sh", textwrap.dedent("""\
                for name in $(basename ./plugins/dep.sh .sh); do
                  source "./plugins/$name.sh"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/-dep.sh", 'echo "basename dash:$name"\n')
            project.write("main.sh", textwrap.dedent("""\
                for name in $(basename -- ./plugins/-dep.sh .sh); do
                  source "./plugins/$name.sh"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            dep = project.write("a.sh", 'echo "realpath:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in $(realpath ./a.sh); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertIn(str(dep), project.path("compiled.sh").read_text())

    def test_while_until_and_read_loops_match_bash(self):
        with ScriptProject() as project:
            project.write("deps/0.sh", 'echo "zero:$i"\n')
            project.write("deps/1.sh", 'echo "one:$i"\n')
            project.write("main.sh", textwrap.dedent("""\
                i=0
                while (( i < 2 )); do
                  source "./deps/$i.sh"
                  ((i++))
                done
                echo "i=$i"
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("deps.txt", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("deps/0.sh", 'echo "zero:$i"\n')
            project.write("deps/1.sh", 'echo "one:$i"\n')
            project.write("main.sh", textwrap.dedent("""\
                i=0
                until (( i == 2 )); do
                  source "./deps/$i.sh"
                  ((i++))
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("mapfile", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("deps dir#tag/a dep.sh", 'echo "special read dep"\n')
            project.write("regular.sh", 'echo "regular read dep"\n')
            project.write("deps.txt", "./deps dir#tag/a dep.sh\n./regular.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                while IFS= read -r dep; do
                  source "$dep"
                done < deps.txt
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(cat deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("inline.sh", 'echo "inline read dep"\n')
            project.write("deps.txt", "./inline.sh\n")
            project.write("main.sh", 'while IFS= read -r dep; do echo "$dep"; source "$dep"; done < deps.txt\n')

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("deps.txt", compiled)
            self.assertIn("for dep in './inline.sh'; do", compiled)

        with ScriptProject() as project:
            project.write("a.sh", 'echo "a guarded read dep"\n')
            project.write("b.sh", 'echo "b guarded read dep"\n')
            project.write("deps.txt", "./a.sh\n./b.sh")
            project.write("main.sh", textwrap.dedent("""\
                while read -r dep || [[ -n "$dep" ]]; do
                  echo "$dep"
                  source "$dep"
                done < deps.txt
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("deps.txt", compiled)
            self.assertIn("for dep in './a.sh' './b.sh'", compiled)

        with ScriptProject() as project:
            project.write("a.sh", 'echo "plain read dep should not run"\n')
            project.write("deps.txt", "./a.sh")
            project.write("main.sh", textwrap.dedent("""\
                while read -r dep; do
                  echo "$dep"
                  source "$dep"
                done < deps.txt
                echo done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("deps.txt", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("a.sh\r", 'echo "crlf:$dep"\n')
            project.write("deps.txt", "./a.sh\r\n")
            project.write("main.sh", textwrap.dedent("""\
                while read -r dep; do
                  source "$dep"
                done < deps.txt
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "pipeline:a"; VALUE=a\n')
            project.write("plugins/b.sh", 'echo "pipeline:b"; VALUE=b\n')
            project.write("main.sh", textwrap.dedent("""\
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  echo "dep=$dep"
                  source "$dep"
                done
                echo "value:${VALUE:-unset}"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("find ./plugins", compiled)
            self.assertIn("( for dep in", compiled)
            self.assertIn("'./plugins/a.sh'", compiled)
            self.assertIn("'./plugins/b.sh'", compiled)

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "lastpipe:a"; VALUE=a\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s lastpipe
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  echo "dep=$dep"
                  source "$dep"
                done
                echo "value:${VALUE:-unset}"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("( for dep", compiled)
            self.assertIn("for dep in './plugins/a.sh'; do", compiled)

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "monitor:a"; VALUE=a\n')
            project.write("main.sh", textwrap.dedent("""\
                set -m
                shopt -s lastpipe
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  echo "dep=$dep"
                  source "$dep"
                done
                echo "value:${VALUE:-unset}"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertIn("( for dep in './plugins/a.sh'; do", compiled)

        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "process:a"; VALUE=a\n')
            project.write("plugins/b.sh", 'echo "process:b"; VALUE=b\n')
            project.write("main.sh", textwrap.dedent("""\
                while read -r dep; do
                  echo "dep=$dep"
                  source "$dep"
                done < <(find ./plugins -type f -name '*.sh' -print)
                echo "value:${VALUE:-unset}"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("<(find ./plugins", compiled)
            self.assertIn("for dep in", compiled)
            self.assertIn("'./plugins/a.sh'", compiled)
            self.assertIn("'./plugins/b.sh'", compiled)

    def test_c_style_for_loops_match_bash(self):
        with ScriptProject() as project:
            project.write("deps/0.sh", 'echo "zero:$i"\n')
            project.write("deps/1.sh", 'echo "one:$i"\n')
            project.write("main.sh", textwrap.dedent("""\
                for (( i=0; i<2; i++ )); do
                  echo "i=$i"
                  source "./deps/$i.sh"
                done
                echo "final:$i"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps/1.sh", 'echo "one:$j:$i"\n')
            project.write("deps/2.sh", 'echo "two:$j:$i"\n')
            project.write("main.sh", textwrap.dedent("""\
                for (( i=0, j=1; j<3; i++, j++ )); do
                  source "./deps/$j.sh"
                done
                echo "final:$i:$j"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_custom_ifs_loop_word_splitting_matches_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a:$dep"\n')
            project.write("b.sh", 'echo "b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                IFS=:
                DEPS="./a.sh:./b.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("a.sh", 'echo "a:$dep"\n')
            project.write("b.sh", 'echo "b:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                IFS=$'\\n'
                DEPS=$'./a.sh\\n./b.sh'
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("a.sh", 'echo "a:$dep"\n')
            project.write("b.sh", 'echo "b:$dep"\n')
            project.write("deps.txt", "./a.sh:./b.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                IFS=:
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")
            self.assertNotIn("$(cat deps.txt)", project.path("compiled.sh").read_text())

        with ScriptProject() as project:
            project.write("a.sh", 'echo "a:$dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                IFS=:
                DEPS=":./a.sh"
                for dep in $DEPS; do
                  echo "<$dep>"
                  if [[ -n "$dep" ]]; then
                    source "$dep"
                  fi
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_richer_array_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("c.sh", 'echo "c"\n')
            project.write("prod.sh", 'echo "prod"\n')
            project.write("mapped.sh", 'echo "mapped"\n')
            project.write("deps.txt", "./mapped.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                deps=(./a.sh)
                deps+=(./b.sh)
                i=2
                deps[$i]=./c.sh
                source "${deps[0]}"
                source "${deps[$i]}"
                declare -A by_env=([prod]=./prod.sh)
                ENV=prod
                source "${by_env[$ENV]}"
                mapfile -t loaded < deps.txt
                source "${loaded[0]}"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("mapfile", compiled)
            self.assertNotIn("deps.txt", compiled)
            self.assertIn("loaded=('./mapped.sh')", compiled)

        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./a.sh\n./b.sh\n")
            project.write("main.sh", textwrap.dedent("""\
                project_deps=($(cat deps.txt))
                for dep in "${project_deps[@]}"; do
                  source "$dep"
                done
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_heredoc_source_text_is_not_treated_as_dependency(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep should not run"\n')
            project.write("main.sh", textwrap.dedent("""\
                cat <<EOF
                source ./dep.sh
                EOF
                echo "main"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", 'echo "<<EOF"\necho $((1 << 2))\nsource ./dep.sh\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_source_dispatch_preserves_inlined_heredoc_delimiters(self):
        with ScriptProject() as project:
            project.write(
                "a.sh",
                "cat >actual <<-\\EOF\n"
                "\tfrom heredoc\n"
                "\tEOF\n"
                "cat actual\n",
            )
            project.write("b.sh", 'echo "from b"\n')
            project.write("main.sh", 'for dep in ./a.sh ./b.sh; do source "$dep"; done\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_parent_variables_are_available_before_sourced_file_runs(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep:${FOO:-missing}"\n')
            project.write("main.sh", "FOO=bar\nsource ./dep.sh\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_nounset_state_before_source_is_preserved(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep:${UNSET_VAR}"\n')
            project.write("main.sh", "set +u\nsource ./dep.sh\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_shell_option_command_status_controls_chained_sources(self):
        def normalize_shell_error_locations(output: str):
            return re.sub(r'/tmp/[^:\n]+/(?:main|compiled)\.sh: line \d+', '<script>: line N', output)

        cases = {
            "invalid shopt": "shopt -s madeup || source ./dep.sh\necho done\n",
            "mixed shopt applies known option but fails": (
                "shopt -s madeup nullglob || source ./dep.sh\n"
                "for dep in ./missing/*.sh; do source \"$dep\"; done\n"
                "echo done\n"
            ),
            "invalid compact set flag": "set -z || source ./dep.sh\necho done\n",
            "invalid set option": "set -o madeup || source ./dep.sh\necho done\n",
            "valid monitor set flag": "set -m || source ./dep.sh\necho done\n",
            "set positional arguments": "set -- || source ./dep.sh\necho done\n",
            "set bare option listing": "set -o || source ./dep.sh\necho done\n",
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("main.sh", content)

                output = project.compile("main.sh", mode="executable")
                expected = project.run("main.sh")
                actual = project.run(output)

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(
                normalize_shell_error_locations(actual.stdout),
                normalize_shell_error_locations(expected.stdout),
            )

    def test_unknown_status_guarded_source_preserves_runtime_guard(self):
        cases = {
            "guard skips source": "needle\n",
            "guard runs source": "other\n",
        }

        for name, config in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("config", config)
                project.write("dep.sh", 'echo "dep"\n')
                project.write("main.sh", textwrap.dedent("""\
                    grep -q needle config || source ./dep.sh
                    echo done
                    """))

                project.assert_compiled_matches(self, "main.sh")

    def test_unknown_sourced_file_status_preserves_guarded_followup_source(self):
        for fail, expected_state in (("0", "unset"), ("1", "loaded")):
            with self.subTest(fail=fail), ScriptProject() as project:
                project.write("dep.sh", "awk 'BEGIN { exit ENVIRON[\"SOURCE_FAIL\"] == \"1\" ? 1 : 0 }'\n")
                project.write("fallback.sh", textwrap.dedent("""\
                    echo fallback
                    FOLLOWUP_STATE=loaded
                    """))
                project.write("main.sh", textwrap.dedent("""\
                    FOLLOWUP_STATE=unset
                    source ./dep.sh || source ./fallback.sh
                    echo "state=$FOLLOWUP_STATE"
                    """))

                output = project.compile("main.sh", mode="executable")
                compiled_text = output.read_text()
                expected = project.run("main.sh", env={"SOURCE_FAIL": fail})
                actual = project.run(output, env={"SOURCE_FAIL": fail})

                self.assertNotIn("source ./fallback.sh", compiled_text)
                self.assertIn("echo fallback", compiled_text)
                self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                self.assertEqual(actual.stdout, expected.stdout)
                self.assertIn(f"state={expected_state}", actual.stdout)

    def test_source_inside_multiline_function_matches_bash(self):
        with ScriptProject() as project:
            project.write("runtime.sh", 'echo "runtime"\n')
            project.write("main.sh", textwrap.dedent("""\
                helper() {
                  echo "before"
                  source ./runtime.sh
                  echo "after"
                }

                helper
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_source_inside_compact_function_matches_bash(self):
        with ScriptProject() as project:
            project.write("runtime.sh", 'echo "runtime"\n')
            project.write("main.sh", 'helper(){ echo "before"; source ./runtime.sh; echo "after"; }\nhelper\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("runtime.sh", 'echo "runtime keyword"\n')
            project.write("main.sh", 'function helper { source ./runtime.sh; }\nhelper\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("runtime.sh", 'echo "runtime split brace"\n')
            project.write("main.sh", textwrap.dedent("""\
                helper()
                {
                  source ./runtime.sh
                }
                helper
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_function_source_argument_matches_bash(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a:$1"\n')
            project.write("b.sh", 'echo "b:$1"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }

                load_dep ./a.sh
                load_dep ./b.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps dir#tag/a dep.sh", 'echo "special function arg:$1"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }

                load_dep "./deps dir#tag/a dep.sh"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_function_defined_by_sourced_file_matches_bash(self):
        with ScriptProject() as project:
            project.write("lib.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }
                """))
            project.write("dep.sh", 'echo "dep from sourced function"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./lib.sh
                load_dep ./dep.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_function_source_state_matches_bash(self):
        with ScriptProject() as project:
            project.write("deps/inside.sh", 'echo "inside:$DEP:$PWD"\n')
            project.write("outside.sh", 'echo "outside:$DEP:$PWD"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEP=./outside.sh
                load_inside() {
                  local DEP=./inside.sh
                  cd deps
                  source "$DEP"
                }

                load_inside
                cd ..
                source "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps/inside.sh", 'echo "expanded local:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                ROOT=./deps
                load_inside() {
                  local DEP="${ROOT}/inside.sh"
                  source "$DEP"
                }

                load_inside
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("deps/inside.sh", 'echo "inside prefix:$DEP"\n')
            project.write("outside.sh", 'echo "outside restored:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEP=./outside.sh
                load_dep() {
                  source "$DEP"
                }

                DEP="./deps/inside.sh" load_dep
                source "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("outer.sh", 'echo "outer arg:$DEP"\n')
            project.write("inner.sh", 'echo "inner prefix:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEP=./outer.sh
                load_dep() {
                  source "$1"
                  source "$DEP"
                }

                DEP=./inner.sh load_dep "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("outer.sh", 'echo "outer redirected:$DEP"\n')
            project.write("inner.sh", 'echo "inner redirected:$DEP"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEP=./outer.sh
                load_dep() {
                  local DEP=./inner.sh
                  source "$DEP"
                } > out.txt

                load_dep
                cat out.txt
                source "$DEP"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_function_control_flow_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "shifted:$1"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  shift
                  source "$1"
                }

                load_dep ignored ./dep.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("missing.sh", 'echo "should not run"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  return 0
                  source ./missing.sh
                }

                load_dep
                echo done
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("should not run", compiled_content)

        with ScriptProject() as project:
            project.write("after.sh", 'echo "after should not run"\n')
            project.write("fallback.sh", 'echo "fallback runs"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  return 1
                }

                load_dep && source ./after.sh
                load_dep || source ./fallback.sh
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("after should not run", compiled_content)

        with ScriptProject() as project:
            project.write("after.sh", 'echo "after shift should not run"\n')
            project.write("fallback.sh", 'echo "fallback shift runs"\n')
            project.write("main.sh", textwrap.dedent("""\
                shift_too_far() {
                  shift 9
                }

                shift_too_far ignored && source ./after.sh
                shift_too_far ignored || source ./fallback.sh
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("after shift should not run", compiled_content)

        with ScriptProject() as project:
            project.write("after.sh", 'echo "after implicit status"\n')
            project.write("fallback.sh", 'echo "fallback implicit status"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_ok() {
                  true
                }
                load_fail() {
                  false
                }

                load_ok && source ./after.sh
                load_ok || source ./skipped-ok.sh
                load_fail && source ./skipped-fail.sh
                load_fail || source ./fallback.sh
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("skipped-ok", compiled_content)
        self.assertNotIn("skipped-fail", compiled_content)

    def test_nested_function_control_flow_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "nested function dep"\n')
            project.write("missing.sh", 'echo "missing"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [[ -f ./dep.sh ]]; then
                    source ./dep.sh
                  else
                    source ./missing.sh
                  fi
                }

                load_dep
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_dynamic_function_dispatch_and_same_line_tail_match_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dynamic dispatch"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() { source "$1"; }; FN=load_dep
                "$FN" ./dep.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "same line tail"\n')
            project.write("main.sh", 'load_dep() { source ./dep.sh; }; load_dep\n')

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "same line function body"\n')
            project.write("tail.sh", 'echo "same line tail source"\n')
            project.write(
                "main.sh",
                'load_dep() { source ./dep.sh; }; load_dep; source ./tail.sh\n',
            )

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "same line chained body"\n')
            project.write("tail.sh", 'echo "same line chained tail"\n')
            project.write(
                "main.sh",
                'load_dep() { source ./dep.sh; }; load_dep && source ./tail.sh\n',
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_branch_dependent_function_definitions_match_bash_when_equivalent(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "branch function dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_ALT" ]]; then
                  load_dep() { source ./dep.sh; }
                else
                  load_dep() { source ./dep.sh; }
                fi

                load_dep
                """))

            project.assert_compiled_matches(self, "main.sh")
            project.assert_compiled_matches(self, "main.sh", env={"USE_ALT": "1"})

    def test_branch_dependent_function_definitions_reject_when_different(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("b.sh", 'echo "b"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_A" ]]; then
                  load_dep() { source ./a.sh; }
                else
                  load_dep() { source ./b.sh; }
                fi

                load_dep
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent function"):
                project.compile("main.sh", output=output, mode="executable")

            self.assertFalse(output.exists())

    def test_unresolved_dynamic_function_dispatch_rejects_before_output(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source ./dep.sh
                }
                "$FN"
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "dynamic function dispatch"):
                project.compile("main.sh", output=output, mode="executable")

            self.assertFalse(output.exists())

    def test_duplicate_sources_execute_each_time_bash_would_execute_them(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", "source ./dep.sh\nsource ./dep.sh\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_shared_dependency_preserves_state_at_each_source_site(self):
        with ScriptProject() as project:
            project.write("shared.sh", 'echo "shared:$VALUE"\n')
            project.write("main.sh", textwrap.dedent("""\
                VALUE=one
                source ./shared.sh
                VALUE=two
                source ./shared.sh
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_direct_source_positional_arguments_match_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s:%s:%s\\n' "$1" "$2" "$#"
                VALUE="$1:$2"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer-one outer-two
                source ./dep.sh alpha beta
                printf 'after:%s:%s:%s:%s\\n' "$1" "$2" "$VALUE" "$?"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("source ./dep.sh alpha beta", compiled)

    def test_direct_source_quoted_positional_arguments_match_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s|%s|%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh "alpha beta" 'gamma delta'
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_direct_source_single_quoted_argument_remains_literal(self):
        with ScriptProject() as project:
            project.write("dep.sh", "printf 'dep:%s\\n' \"$1\"\n")
            project.write("main.sh", "source ./dep.sh '$1'\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_direct_source_glob_multiple_matches_passes_remaining_matches_as_arguments(self):
        with ScriptProject() as project:
            project.write("plugins/00-loader.sh", textwrap.dedent("""\
                printf 'loader:%s:%s:%s\\n' "$1" "$2" "$#"
                VALUE="$1:$2"
                """))
            project.write("plugins/10-first-arg.sh", "echo wrong-first\n")
            project.write("plugins/20-second-arg.sh", "echo wrong-second\n")
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./plugins/*.sh
                printf 'after:%s:%s\\n' "$1" "$VALUE"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh").read_text()
            self.assertNotIn("source ./plugins/*.sh", compiled)

    def test_direct_source_glob_arguments_precede_explicit_source_arguments(self):
        with ScriptProject() as project:
            project.write("plugins/00-loader.sh", textwrap.dedent("""\
                printf 'loader:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("plugins/10-glob-arg.sh", "echo wrong\n")
            project.write("main.sh", 'source ./plugins/*.sh explicit\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_nested_source_inherits_source_positionals_with_quoted_at(self):
        with ScriptProject() as project:
            project.write("nested.sh", textwrap.dedent("""\
                printf 'nested:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("lib.sh", textwrap.dedent("""\
                printf 'lib-before:%s:%s:%s\\n' "$1" "$2" "$#"
                source ./nested.sh "$@"
                printf 'lib-after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./lib.sh one two
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_nested_source_overrides_source_positionals(self):
        with ScriptProject() as project:
            project.write("nested.sh", textwrap.dedent("""\
                printf 'nested:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("lib.sh", textwrap.dedent("""\
                source ./nested.sh nested "$2"
                printf 'lib:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./lib.sh one two
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_return_with_positional_arguments_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                VALUE="$1"
                printf 'dep:%s:%s\\n' "$1" "$#"
                return 6
                VALUE=unreachable
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                status=$?
                printf 'after:%s:%s:%s\\n' "$1" "$VALUE" "$status"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_syncs_top_level_positional_mutation(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                printf 'dep:%s:%s\\n' "$1" "$#"
                return 7
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                status=$?
                printf 'after:%s:%s:%s:%s\\n' "$1" "$2" "$#" "$status"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_preserves_caller_after_top_level_shift(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                shift
                printf 'dep:%s:%s\\n' "$1" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg keep
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_syncs_set_followed_by_shift(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                shift
                printf 'dep:%s:%s\\n' "$1" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_restores_frame_after_nested_explicit_source(self):
        with ScriptProject() as project:
            project.write("nested.sh", "printf 'nested:%s:%s:%s\\n' \"$1\" \"$2\" \"$#\"\n")
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                source ./nested.sh "$@"
                return 0
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_preserves_capture_when_default_guard_skips_nested_source(self):
        with ScriptProject() as project:
            project.write("nested.sh", ":\n")
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                if [[ ${RUN_NESTED:-} ]]; then
                  source ./nested.sh "$@"
                fi
                printf 'dep:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "${2-}" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_keeps_shift_after_nested_source_temporary(self):
        with ScriptProject() as project:
            project.write("nested.sh", ":\n")
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                source ./nested.sh
                shift
                printf 'dep:%s:%s\\n' "$1" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_restores_frame_after_helper_source(self):
        with ScriptProject() as project:
            project.write("nested.sh", "printf 'nested:%s:%s\\n' \"$1\" \"$#\"\n")
            project.write("dep.sh", textwrap.dedent("""\
                helper() {
                  source ./nested.sh "$@"
                }
                set -- changed one
                helper
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_syncs_nested_no_argument_positional_mutation(self):
        with ScriptProject() as project:
            project.write("nested.sh", textwrap.dedent("""\
                set -- nested value
                printf 'nested:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                source ./nested.sh
                printf 'dep-after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_preserves_nested_no_argument_source_status(self):
        with ScriptProject() as project:
            project.write("nested.sh", textwrap.dedent("""\
                set -- nested value
                return 4
                """))
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                source ./nested.sh
                nested_status=$?
                printf 'dep-after:%s:%s:%s:%s\\n' "$1" "$2" "$#" "$nested_status"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_restores_nested_explicit_mutation_until_later_set(self):
        with ScriptProject() as project:
            project.write("nested.sh", textwrap.dedent("""\
                set -- nested value
                printf 'nested:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed one
                source ./nested.sh "$@"
                printf 'dep-after-nested:%s:%s:%s\\n' "$1" "$2" "$#"
                set -- final value
                printf 'dep-after-set:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_with_arguments_syncs_positional_mutation_after_nested_source(self):
        with ScriptProject() as project:
            project.write("nested.sh", "printf 'nested:%s:%s:%s\\n' \"$1\" \"$2\" \"$#\"\n")
            project.write("dep.sh", textwrap.dedent("""\
                source ./nested.sh
                set -- changed one
                return 0
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s:%s\\n' "$1" "$2" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_wrapped_positional_mutation_rejects_unsupported_syntax(self):
        cases = {
            "set option with positionals": (
                "set -m -- changed\nreturn 0\n",
                "source ./dep.sh arg\n",
            ),
            "dynamic shift": (
                'shift "$COUNT"\nreturn 0\n',
                "COUNT=1\nset -- one two\nsource ./dep.sh\n",
            ),
        }
        for name, (dep, main) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", dep)
                project.write("main.sh", main)
                output = project.path("compiled.sh")

                with self.assertRaisesRegex(NotImplementedError, "positional|shift") as cm:
                    project.compile("main.sh", output=output, mode="executable")

                self.assertEqual(cm.exception.code, "unsupported.source.positionals")
                self.assertFalse(output.exists())

    def test_sourced_file_function_local_positional_mutation_stays_local(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                helper() {
                  set -- local
                }
                helper
                printf 'dep:%s:%s\\n' "$1" "$#"
                return 0
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh arg
                printf 'after:%s:%s\\n' "$1" "$#"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_return_without_arguments_preserves_caller_positionals(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s:%s\\n' "$1" "$#"
                return 3
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh
                status=$?
                printf 'after:%s:%s\\n' "$1" "$status"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_return_without_arguments_syncs_caller_positional_mutation(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                set -- changed
                return 5
                """))
            project.write("main.sh", textwrap.dedent("""\
                set -- outer
                source ./dep.sh
                status=$?
                printf 'after:%s:%s:%s\\n' "$1" "$#" "$status"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_dynamic_positional_assignment_before_static_source_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'printf "dep:%s:%s\\n" "$1" "$#"\n')
            project.write("main.sh", textwrap.dedent("""\
                set -- "$UNKNOWN"
                source ./dep.sh
                printf 'after:%s:%s\\n' "$1" "$#"
                """))
            compiled = project.compile("main.sh", mode="executable")

            expected = project.run("main.sh", env={"UNKNOWN": "runtime value"})
            actual = project.run(compiled, env={"UNKNOWN": "runtime value"})

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(actual.stdout, expected.stdout)

    def test_dynamic_positional_assignment_rejects_positional_source_resolution(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                set -- "$UNKNOWN"
                source "$1"
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "source") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertTrue(cm.exception.diagnostic.code.startswith("unsupported.source."))
            self.assertFalse(output.exists())

    def test_source_arguments_must_resolve_to_exact_values(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep:$1"\n')
            project.write("main.sh", 'source ./dep.sh "$UNKNOWN"\n')
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "source argument") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.argument")
            self.assertFalse(output.exists())

    def test_source_free_unsupported_read_loop_producer_does_not_block_later_source(self):
        with ScriptProject() as project:
            project.write("deps.txt", "one\n")
            project.write("dep.sh", "echo dep\n")
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    sed -n '1p' deps.txt | while read -r item; do
                        echo "item:$item"
                    done
                    source ./dep.sh
                    """),
            )

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stderr)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertIn("sed -n '1p' deps.txt | while read -r item; do", compiled_content)
        self.assertIn("echo dep", compiled_content)

    def test_circular_sources_raise_clear_error(self):
        with ScriptProject() as project:
            project.write("a.sh", "source ./b.sh\n")
            project.write("b.sh", "source ./a.sh\n")

            with self.assertRaises(RecursionError):
                project.compile("a.sh")

    def test_unsupported_source_families_fail_without_writing_output(self):
        cases = {
            "unknown scalar": ('source "$DEP"\n', 'source "$DEP"'),
            "unsupported command substitution loop pipeline": (
                'for file in $(cat deps.txt | sort); do source "$file"; done\n',
                'for file in $(cat deps.txt | sort); do source "$file"; done',
            ),
            "unsupported read loop producer pipeline": (
                "sed -n '1p' deps.txt | while read -r file; do source \"$file\"; done\n",
                "sed -n '1p' deps.txt | while read -r file; do",
            ),
            "unknown while condition source": (
                'while [[ -n "$LOAD_DEP" ]]; do\n  source ./dep.sh\n  break\n done\n',
                'while [[ -n "$LOAD_DEP" ]]',
            ),
            "quoted glob loop": (
                'for file in "./plugins/*.sh"; do source "$file"; done\n',
                'for file in "./plugins/*.sh"; do source "$file"; done',
            ),
            "disabled extglob loop": (
                'for file in ./plugins/@(a|b).sh; do source "$file"; done\n',
                'for file in ./plugins/@(a|b).sh; do source "$file"; done',
            ),
            "noglob loop": (
                'set -f\nfor file in ./plugins/*.sh; do source "$file"; done\n',
                'for file in ./plugins/*.sh; do source "$file"; done',
            ),
            "unknown guarded failglob source": (
                'shopt -s failglob\nmaybe && source ./missing/*.sh || echo fallback\n',
                'source ./missing/*.sh',
            ),
            "unknown guarded source function": (
                'load_dep() { source ./dep.sh; }\nmaybe && load_dep || echo fallback\n',
                'load_dep',
            ),
            "divergent if branch state": (
                'if [[ -n "$USE_A" ]]; then\n  DEP=./a.sh\nelse\n  DEP=./b.sh\nfi\nsource "$DEP"\n',
                'source "$DEP"',
            ),
            "unknown status guarded source state": (
                'grep -q yes config || source ./setdep.sh\nsource "$DEP"\n',
                'source "$DEP"',
            ),
            "case dynamic subject": (
                'case "$(cat env.txt)" in\n  prod) source ./prod.sh ;;\nesac\n',
                'case "$(cat env.txt)" in',
            ),
            "case unresolved variable pattern": (
                'ENV=prod\ncase "$ENV" in\n  "$PATTERN") source ./prod.sh ;;\nesac\n',
                'case "$ENV" in',
            ),
            "case dynamic pattern": (
                'ENV=prod\ncase "$ENV" in\n  "$(echo prod)") source ./prod.sh ;;\nesac\n',
                'case "$ENV" in',
            ),
            "case dynamic pattern inside double quoted apostrophes": (
                'ENV=prod\ncase "$ENV" in\n  "\'$(echo prod)\'") source ./prod.sh ;;\nesac\n',
                'case "$ENV" in',
            ),
            "case divergent state": (
                'case "$ENV" in\n  prod) DEP=./a.sh ;;\n  dev) DEP=./b.sh ;;\nesac\nsource "$DEP"\n',
                'source "$DEP"',
            ),
            "case empty pattern is not default": (
                'case "$ENV" in\n  "") DEP=./prod.sh ;;\nesac\nsource "$DEP"\n',
                'source "$DEP"',
            ),
            "case hidden eval source": (
                'case "$ENV" in\n  prod) COMMAND="source ./prod.sh"; eval "$COMMAND" ;;\nesac\n',
                'eval "$COMMAND"',
            ),
            "case disabled extglob pattern": (
                'ENV=prod\ncase "$ENV" in\n  @(prod|stage)) source ./prod.sh ;;\nesac\n',
                'case "$ENV" in',
            ),
            "case disabled variable extglob pattern": (
                'ENV=prod\nPATTERN="@(prod|stage)"\ncase "$ENV" in\n  $PATTERN) source ./prod.sh ;;\nesac\n',
                'case "$ENV" in',
            ),
            "backtick substitution source": (
                'echo `source ./dep.sh`\n',
                'echo `source ./dep.sh`',
            ),
            "arithmetic nested source": (
                'echo $(( $(source ./dep.sh) + 1 ))\n',
                'echo $(( $(source ./dep.sh) + 1 ))',
            ),
            "command substitution if source": (
                'echo "$(if true; then source ./dep.sh; fi)"\n',
                'echo "$(if true; then source ./dep.sh; fi)"',
            ),
            "process substitution loop source": (
                'cat <(for f in ./dep.sh; do source "$f"; done)\n',
                'cat <(for f in ./dep.sh; do source "$f"; done)',
            ),
            "backtick case dot source": (
                'echo `case "$ENV" in prod) . ./dep.sh ;; esac`\n',
                'echo `case "$ENV" in prod) . ./dep.sh ;; esac`',
            ),
            "branch-dependent function return": (
                'load_dep() {\n  if [[ -n "$SKIP" ]]; then return 0; fi\n  source ./dep.sh\n}\nload_dep\n',
                'if [[ -n "$SKIP" ]]',
            ),
            "assignment-prefixed source": (
                'FOO=bar source ./dep.sh\n',
                'FOO=bar source ./dep.sh',
            ),
            "assignment-prefixed command source": (
                'FOO=bar command source ./dep.sh\n',
                'FOO=bar command source ./dep.sh',
            ),
        }

        for name, (content, expected_fragment) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("prod.sh", 'echo "prod"\n')
                project.write("setdep.sh", 'DEP=./dep.sh\n')
                project.write("a.sh", 'echo "a"\n')
                project.write("b.sh", 'echo "b"\n')
                project.write("config", "enabled=true\n")
                project.write("plugins/a.sh", 'echo "plugin"\n')
                project.write("plugins/b.sh", 'echo "plugin b"\n')
                project.write("main.sh", content)
                output = project.write("compiled.sh", "existing output\n")

                with self.assertRaisesRegex((ValueError, NotImplementedError), "unsupported|unresolved|control flow") as cm:
                    project.compile("main.sh", output=output, mode="executable")

                self.assertIn(expected_fragment, str(cm.exception))
                self.assertIsNotNone(cm.exception.diagnostic)
                self.assertEqual(cm.exception.diagnostic.severity, DiagnosticSeverity.ERROR)
                self.assertEqual(cm.exception.diagnostic.location.path, project.path("main.sh"))
                self.assertGreater(cm.exception.diagnostic.location.line, 0)
                self.assertIn(expected_fragment, cm.exception.diagnostic.fragment)
                self.assertTrue(cm.exception.diagnostic.code.startswith("unsupported.source."))
                self.assertEqual(output.read_text(), "existing output\n")

    def test_source_free_shopt_if_compiles_in_executable_mode(self):
        with ScriptProject() as project:
            project.write("main.sh", textwrap.dedent("""\
                handle_cd() {
                  :
                }
                if shopt -q cdable_vars; then
                  complete -v -F handle_cd cd
                else
                  complete -F handle_cd cd
                fi
                """))
            output = project.write("compiled.sh", "existing output\n")

            project.compile("main.sh", output=output, mode="executable")

            compiled = output.read_text()
            self.assertIn("if shopt -q cdable_vars; then", compiled)
            self.assertIn("complete -v -F handle_cd cd", compiled)

    def test_source_free_unknown_if_mutation_keeps_later_source_fail_closed(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit 0 }'; then
                  DEP=./dep.sh
                fi
                source "$DEP"
                """))
            output = project.write("compiled.sh", "existing output\n")

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent variable") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.branch-state")
            self.assertEqual(output.read_text(), "existing output\n")

    def test_runtime_guarded_if_source_lowering_matches_bash(self):
        with ScriptProject() as project:
            project.write("enabled.sh", 'echo "enabled"; FEATURE_STATE=enabled\n')
            project.write("disabled.sh", 'echo "disabled"; FEATURE_STATE=disabled\n')
            project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit ENVIRON["LOAD_FEATURE"] == "1" ? 0 : 1 }'; then
                  source ./enabled.sh
                else
                  source ./disabled.sh
                fi
                echo "state=$FEATURE_STATE"
                """))

            compiled = project.compile("main.sh", mode="executable")
            compiled_text = compiled.read_text()

            self.assertIn('if awk \'BEGIN { exit ENVIRON["LOAD_FEATURE"] == "1" ? 0 : 1 }\'; then', compiled_text)
            self.assertNotIn("source ./enabled.sh", compiled_text)
            self.assertNotIn("source ./disabled.sh", compiled_text)
            for env in ({"LOAD_FEATURE": "1"}, {"LOAD_FEATURE": "0"}):
                with self.subTest(env=env):
                    expected = project.run("main.sh", env=env)
                    actual = project.run(compiled, env=env)
                    self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                    self.assertEqual(actual.stdout, expected.stdout)

    def test_runtime_guarded_if_unsupported_predicates_preserve_bash_behavior(self):
        def normalize_shell_error_locations(output: str):
            return re.sub(r'/tmp/[^:\n]+/(?:main|compiled)\.sh: line \d+', '<script>: line N', output)

        cases = {
            "multi-match file glob": 'if [ -f ./plugins/*.sh ]; then\n  source ./dep.sh\nfi\n',
            "bracket string glob": 'MODE=prod\nif [ "$MODE" = prod* ]; then\n  source ./dep.sh\nfi\n',
            "grep basic regex": 'if grep -q "enabled.*" config; then\n  source ./dep.sh\nfi\n',
            "posix regex": 'MODE=5\nif [[ "$MODE" =~ [[:digit:]] ]]; then\n  source ./dep.sh\nfi\n',
            "python regex": 'MODE=5\nif [[ "$MODE" =~ \\d+ ]]; then\n  source ./dep.sh\nfi\n',
            "grep python regex": 'if grep -Eq "\\d+" config; then\n  source ./dep.sh\nfi\n',
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("config", "enabled=true\n")
                project.write("plugins/a.sh", 'echo "plugin"\n')
                project.write("plugins/b.sh", 'echo "plugin b"\n')
                project.write("main.sh", content)

                compiled = project.compile("main.sh", mode="executable")
                expected = project.run("main.sh")
                actual = project.run(compiled)

                self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                self.assertEqual(
                    normalize_shell_error_locations(actual.stdout),
                    normalize_shell_error_locations(expected.stdout),
                )

    def test_runtime_guarded_if_dynamic_source_path_fails_before_output(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit 0 }'; then
                  source "$DEP"
                fi
                """))
            output = project.write("compiled.sh", "existing output\n")

            with self.assertRaisesRegex(NotImplementedError, "unresolved source") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.unresolved")
            self.assertEqual(output.read_text(), "existing output\n")

    def test_runtime_guarded_case_source_lowering_matches_bash(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod"; CASE_STATE=prod\n')
            project.write("dev.sh", 'echo "dev"; CASE_STATE=dev\n')
            project.write("main.sh", textwrap.dedent("""\
                case "$MODE" in
                  prod) source ./prod.sh ;;
                  dev) source ./dev.sh ;;
                  *) echo "fallback"; CASE_STATE=fallback ;;
                esac
                echo "case=$CASE_STATE"
                """))

            compiled = project.compile("main.sh", mode="executable")
            compiled_text = compiled.read_text()

            self.assertIn('case "$MODE" in', compiled_text)
            self.assertNotIn("source ./prod.sh", compiled_text)
            self.assertNotIn("source ./dev.sh", compiled_text)
            for env in ({"MODE": "prod"}, {"MODE": "dev"}, {"MODE": "qa"}):
                with self.subTest(env=env):
                    expected = project.run("main.sh", env=env)
                    actual = project.run(compiled, env=env)
                    self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                    self.assertEqual(actual.stdout, expected.stdout)

    def test_shopt_query_source_guard_matches_bash(self):
        with ScriptProject() as project:
            project.write("enabled.sh", 'echo "enabled"\n')
            project.write("disabled.sh", 'echo "disabled"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s dotglob
                if shopt -q dotglob; then
                  source ./enabled.sh
                else
                  source ./disabled.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("enabled.sh", 'echo "enabled"\n')
            project.write("disabled.sh", 'echo "disabled"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s cdable_vars
                if shopt -q cdable_vars; then
                  source ./enabled.sh
                else
                  source ./disabled.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("enabled.sh", 'echo "enabled"\n')
            project.write("disabled.sh", 'echo "disabled"\n')
            project.write("main.sh", textwrap.dedent("""\
                if shopt -q checkwinsize; then
                  source ./enabled.sh
                else
                  source ./disabled.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_exact_glob_file_source_guard_matches_bash(self):
        with ScriptProject() as project:
            project.write("plugins/only.sh", 'echo "plugin marker"\n')
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [ -f ./plugins/*.sh ]; then
                  source ./dep.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/only.sh", 'echo "plugin marker"\n')
            project.write("then.sh", 'echo "wrong double bracket glob"\n')
            project.write("else.sh", 'echo "literal double bracket glob"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -f ./plugins/*.sh ]]; then
                  source ./then.sh
                else
                  source ./else.sh
                fi
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("wrong double bracket glob", compiled_content)

        with ScriptProject() as project:
            project.write("then.sh", 'echo "wrong missing glob"\n')
            project.write("else.sh", 'echo "missing single bracket glob"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [ -f ./missing/*.sh ]; then
                  source ./then.sh
                else
                  source ./else.sh
                fi
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh")
            actual = project.run(output)
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertNotIn("wrong missing glob", compiled_content)

        with ScriptProject() as project:
            project.write("plugins/only.sh", 'echo "plugin marker"\n')
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if test -f ./plugins/*.sh; then
                  source ./dep.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

        with ScriptProject() as project:
            project.write("plugins/core.sh", 'echo "core plugin"\n')
            project.write("plugins/extra.sh", 'echo "unexpected extra plugin"\n')
            project.write("dep.sh", 'echo "extglob guard dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                GLOBIGNORE=./plugins/extra.sh
                if [ -f ./plugins/@(core|extra).sh ]; then
                  source ./dep.sh
                fi
                """))

            output = project.compile("main.sh", mode="executable")
            expected = project.run("main.sh", env={"LC_ALL": "C"})
            actual = project.run(output, env={"LC_ALL": "C"})
            compiled_content = output.read_text()

        self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
        self.assertEqual(actual.stdout, expected.stdout)
        self.assertIn("extglob guard dep", compiled_content)

        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [ -r ./dep.sh ]; then
                  source ./dep.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_negated_source_in_rendered_function_reports_retained_helper(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }
                """))
            project.write("main.sh", "source ./dep.sh\n")
            output = project.write("compiled.sh", "existing output\n")

            with self.assertRaisesRegex(NotImplementedError, "if ! source") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.retained-helper")
            self.assertEqual(output.read_text(), "existing output\n")

    def test_exact_quoted_all_positionals_helper_source_matches_bash(self):
        cases = ('"$@"', '"${@}"', '"$*"', '"${*}"')
        for positional_expression in cases:
            with self.subTest(positional_expression=positional_expression), ScriptProject() as project:
                project.write("PKGBUILD", 'PKGNAME=demo\necho "pkgbuild:$PKGNAME"\n')
                project.write("helpers.sh", textwrap.dedent(f"""\
                    source_safe() {{
                      if ! source {positional_expression}; then
                        return 1
                      fi
                    }}

                    source_safe ./PKGBUILD
                    echo "helper:$PKGNAME"
                    """))
                project.write("main.sh", 'source ./helpers.sh\necho "main:$PKGNAME"\n')

                project.assert_compiled_matches(self, "main.sh")

    def test_pacman_style_source_safe_with_shopt_restore_matches_bash(self):
        with ScriptProject() as project:
            project.write("PKGBUILD", 'PKGNAME=demo\necho "pkgbuild:$PKGNAME"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  local shellopts=$(shopt -p extglob)
                  shopt -u extglob

                  if ! source "$@"; then
                    return 1
                  fi

                  eval "$shellopts"
                }

                shopt -s extglob
                source_safe ./PKGBUILD
                shopt -q extglob; echo "extglob:$?"
                echo "helper:$PKGNAME"
                """))
            project.write("main.sh", 'source ./helpers.sh\necho "main:$PKGNAME"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_context_exact_false_if_skips_unreachable_function_call(self):
        with ScriptProject() as project:
            project.write("main.sh", textwrap.dedent("""\
                error() {
                  :
                }

                if false; then
                  error "$(gettext "unused")" "$1"
                fi
                echo done
                """))

            project.compile("main.sh", mode="context")

    def test_source_supplement_variable_supports_makepkg_style_helper_source(self):
        with ScriptProject() as project:
            project.write("makepkg/util/message.sh", 'MESSAGE_LOADED=yes\necho "message loaded"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }

                source_safe "$MAKEPKG_LIBRARY/util/message.sh"
                echo "helper:$MESSAGE_LOADED"
                """))
            project.write("main.sh", 'source ./helpers.sh\necho "main:$MESSAGE_LOADED"\n')
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "variables": {
                    "MAKEPKG_LIBRARY": "./makepkg",
                },
            }))

            expected = project.run("main.sh", env={"MAKEPKG_LIBRARY": str(project.path("makepkg"))})
            actual = project.run(project.compile(
                "main.sh",
                mode="executable",
                source_supplement=supplement,
            ))

            self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
            self.assertEqual(actual.stdout, expected.stdout)

    def test_script_assignment_overrides_source_supplement_variable(self):
        with ScriptProject() as project:
            project.write("from-script/util/message.sh", 'MESSAGE_LOADED=script\necho "script message"\n')
            project.write("from-supplement/util/message.sh", 'MESSAGE_LOADED=supplement\necho "supplement message"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }

                MAKEPKG_LIBRARY=./from-script
                source_safe "$MAKEPKG_LIBRARY/util/message.sh"
                echo "helper:$MESSAGE_LOADED"
                """))
            project.write("main.sh", 'source ./helpers.sh\necho "main:$MESSAGE_LOADED"\n')
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "variables": {
                    "MAKEPKG_LIBRARY": "./from-supplement",
                },
            }))

            result = project.run(project.compile("main.sh", mode="executable", source_supplement=supplement))

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("script message", result.stdout)
            self.assertIn("helper:script", result.stdout)
            self.assertIn("main:script", result.stdout)
            self.assertNotIn("supplement message", result.stdout)

    def test_source_supplement_function_signature_resolves_unresolved_helper_argument(self):
        with ScriptProject() as project:
            project.write("PKGBUILD", 'PKGNAME=supplemented\necho "$PKGNAME"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }

                source_safe "$UNRESOLVED_BUILDFILE"
                echo "helper:$PKGNAME"
                """))
            project.write("main.sh", 'source ./helpers.sh\necho "main:$PKGNAME"\n')
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [
                        {
                            "arguments": ["./PKGBUILD"],
                        },
                    ],
                },
            }))

            result = project.run(project.compile("main.sh", mode="executable", source_supplement=supplement))

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("supplemented", result.stdout)
            self.assertIn("helper:supplemented", result.stdout)
            self.assertIn("main:supplemented", result.stdout)

    def test_retained_source_helper_dispatch_uses_supplement_allowlist(self):
        with ScriptProject() as project:
            one = project.write("one.sh", 'VALUE=one\necho "loaded:$VALUE"\n')
            two = project.write("two.sh", 'VALUE=two\necho "loaded:$VALUE"\n')
            missing = project.path("missing.sh")
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    echo "failed:$1"
                    return 7
                  fi
                }
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [
                        {"arguments": [str(one)]},
                        {"arguments": [str(two)]},
                    ],
                },
            }))

            compiled = project.compile("helpers.sh", mode="executable", source_supplement=supplement)
            content = compiled.read_text()
            self.assertNotIn('source "$@"', content)
            self.assertNotRegex(content, r'(?m)^\s*(?:source|\.)\s+')

            driver = textwrap.dedent(f"""\
                source {shlex.quote(str(compiled))}
                source_safe {shlex.quote(str(one))}
                echo "after-one:$VALUE:$?"
                source_safe {shlex.quote(str(two))}
                echo "after-two:$VALUE:$?"
                source_safe {shlex.quote(str(missing))}
                echo "after-missing:$?"
                """)
            result = subprocess.run(
                ["bash", "-c", driver],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("loaded:one", result.stdout)
            self.assertIn("after-one:one:0", result.stdout)
            self.assertIn("loaded:two", result.stdout)
            self.assertIn("after-two:two:0", result.stdout)
            self.assertIn(f"failed:{missing}", result.stdout)
            self.assertIn("after-missing:7", result.stdout)
            self.assertNotIn("modash:", result.stdout)

    def test_retained_source_helper_dispatch_matches_relative_runtime_argument(self):
        with ScriptProject() as project:
            project.write("PKGBUILD", 'PKGNAME=relative\necho "pkg:$PKGNAME"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    echo "failed:$1"
                    return 1
                  fi
                }
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [
                        {"arguments": ["./PKGBUILD"]},
                    ],
                },
            }))

            compiled = project.compile("helpers.sh", mode="executable", source_supplement=supplement)
            driver = textwrap.dedent(f"""\
                source {shlex.quote(str(compiled))}
                source_safe ./PKGBUILD
                echo "after:$PKGNAME:$?"
                """)
            result = subprocess.run(
                ["bash", "-c", driver],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("pkg:relative", result.stdout)
            self.assertIn("after:relative:0", result.stdout)

    def test_retained_source_helper_dispatch_supports_quoted_first_positional(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'VALUE=first-positional\necho "$VALUE"\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  source "$1"
                }
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [
                        {"arguments": [str(dep)]},
                    ],
                },
            }))

            compiled = project.compile("helpers.sh", mode="executable", source_supplement=supplement)
            driver = textwrap.dedent(f"""\
                source {shlex.quote(str(compiled))}
                source_safe {shlex.quote(str(dep))}
                echo "after:$VALUE:$?"
                """)
            result = subprocess.run(
                ["bash", "-c", driver],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("first-positional", result.stdout)
            self.assertIn("after:first-positional:0", result.stdout)

    def test_missing_retained_source_helper_supplement_fails_before_output(self):
        with ScriptProject() as project:
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }
                """))
            output = project.write("compiled.sh", "existing output\n")

            with self.assertRaisesRegex(NotImplementedError, "retained source helper") as cm:
                project.compile("helpers.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.retained-helper")
            self.assertEqual(
                cm.exception.diagnostic.details["supplement_skeleton"]["functions"],
                {"source_safe": [{"arguments": ["<source-path>"]}]},
            )
            self.assertEqual(output.read_text(), "existing output\n")

    def test_source_supplement_rejects_empty_helper_signature(self):
        cases = {
            "zero args": [],
        }
        for name, arguments in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("one.sh", 'echo one\n')
                project.write("two.sh", 'echo two\n')
                project.write("helpers.sh", textwrap.dedent("""\
                    source_safe() {
                      source "$@"
                    }
                    """))
                supplement = project.write("source-supplement.json", json.dumps({
                    "version": 1,
                    "functions": {
                        "source_safe": [{"arguments": arguments}],
                    },
                }))
                output = project.path("compiled.sh")

                with self.assertRaisesRegex(NotImplementedError, "source supplement") as cm:
                    project.compile("helpers.sh", output=output, mode="executable", source_supplement=supplement)

                self.assertEqual(cm.exception.code, "unsupported.source.supplement")
                self.assertFalse(output.exists())

    def test_retained_first_positional_helper_rejects_multi_argument_supplement(self):
        with ScriptProject() as project:
            project.write("one.sh", 'echo one\n')
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  source "$1"
                }
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [{"arguments": ["./one.sh", "arg"]}],
                },
            }))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "retained source helper") as cm:
                project.compile("helpers.sh", output=output, mode="executable", source_supplement=supplement)

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.retained-helper")
            self.assertFalse(output.exists())

    def test_retained_source_helper_dispatch_supports_source_arguments(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    echo "failed:$?"
                    return 7
                  fi
                }
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [{"arguments": [str(dep), "alpha beta", "gamma"]}],
                },
            }))

            compiled = project.compile("helpers.sh", mode="executable", source_supplement=supplement)
            driver = textwrap.dedent(f"""\
                source {shlex.quote(str(compiled))}
                source_safe {shlex.quote(str(dep))} "alpha beta" gamma
                """)
            result = subprocess.run(
                ["bash", "-c", driver],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "dep:alpha beta:gamma:2\n")

    def test_retained_source_helper_lowers_top_level_return_in_supplemented_source(self):
        with ScriptProject() as project:
            project.write("guarded.sh", textwrap.dedent("""\
                [[ -n "$GUARDED_SH" ]] && return 5
                GUARDED_SH=1
                echo "guarded-loaded"
                """))
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    echo "failed:$?"
                    return 7
                  fi
                  echo "helper-after:$?"
                }
                GUARDED_SH=1
                """))
            supplement = project.write("source-supplement.json", json.dumps({
                "version": 1,
                "functions": {
                    "source_safe": [
                        {"arguments": ["./guarded.sh"]},
                    ],
                },
            }))

            compiled = project.compile("helpers.sh", mode="executable", source_supplement=supplement)
            driver = textwrap.dedent(f"""\
                source {shlex.quote(str(compiled))}
                source_safe ./guarded.sh
                echo "after:$?"
                """)
            result = subprocess.run(
                ["bash", "-c", driver],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("failed:0", result.stdout)
            self.assertIn("after:7", result.stdout)
            self.assertNotIn("guarded-loaded", result.stdout)

    def test_sourced_file_top_level_return_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                VALUE=before
                echo "dep:$VALUE"
                return 7
                VALUE=after
                echo "unreachable"
                """))
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh
                echo "after:$VALUE:$?"
                """))

            project.assert_compiled_matches(self, "main.sh")
            compiled = project.path("compiled.sh")
            generated_functions = sorted(set(
                re.findall(r'(__modash_source_[0-9a-f]+(?:_run)?)\(\)', compiled.read_text())
            ))
            self.assertGreaterEqual(len(generated_functions), 2)

            leak_checks = "\n".join(
                f"type {shlex.quote(function_name)} >/dev/null 2>&1 && "
                f"echo leaked:{shlex.quote(function_name)}"
                for function_name in generated_functions
            )
            result = subprocess.run(
                ["bash", "-c", f"source {shlex.quote(str(compiled))}\n{leak_checks}\ntrue"],
                cwd=project.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertNotIn("leaked:", result.stdout)

    def test_sourced_file_return_disables_later_sources(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                echo "before-return"
                return 0
                source ./missing.sh
                """))
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh
                echo "after:$?"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_guarded_return_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                FLAG=
                [[ -n "$FLAG" ]] && return 9
                VALUE=kept
                echo "$VALUE"
                """))
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh
                echo "after:$VALUE:$?"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_sourced_file_include_guard_matches_bash(self):
        with ScriptProject() as project:
            project.write("lib.sh", textwrap.dedent("""\
                [[ -n "$LIB_SH" ]] && return
                LIB_SH=1
                COUNT=${COUNT:-0}
                COUNT=loaded
                echo "loaded:$COUNT"
                """))
            project.write("main.sh", textwrap.dedent("""\
                source ./lib.sh
                source ./lib.sh
                echo "after:$COUNT:$?"
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_missing_source_supplement_values_emit_skeleton(self):
        with ScriptProject() as project:
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  if ! source "$@"; then
                    return 1
                  fi
                }

                source_safe "$MAKEPKG_LIBRARY/util/message.sh"
                """))
            project.write("main.sh", 'source ./helpers.sh\n')
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "unsupported unresolved function argument") as cm:
                project.compile("main.sh", output=output, mode="executable")

            diagnostic = cm.exception.diagnostic
            self.assertEqual(diagnostic.code, "unsupported.source.function-argument")
            skeleton = diagnostic.details["supplement_skeleton"]
            self.assertEqual(skeleton["version"], 1)
            self.assertEqual(skeleton["variables"], {"MAKEPKG_LIBRARY": "<path>"})
            self.assertEqual(
                skeleton["functions"],
                {"source_safe": [{"arguments": ["<source-path>"]}]},
            )
            self.assertFalse(output.exists())

    def test_invalid_source_supplements_fail_before_output(self):
        cases = {
            "missing": None,
            "invalid json": "{",
            "wrong version": json.dumps({"version": 2}),
            "unknown top-level key": json.dumps({"version": 1, "extra": {}}),
            "bad variable name": json.dumps({"version": 1, "variables": {"1BAD": "./dep.sh"}}),
            "non-string argument": json.dumps({
                "version": 1,
                "functions": {"source_safe": [{"arguments": [1]}]},
            }),
            "boolean source index": json.dumps({
                "version": 1,
                "functions": {"source_safe": [{"arguments": ["./dep.sh"], "source_index": True}]},
            }),
            "out-of-range source index": json.dumps({
                "version": 1,
                "functions": {"source_safe": [{"arguments": ["./dep.sh"], "source_index": 1}]},
            }),
        }
        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("main.sh", 'source ./dep.sh\n')
                supplement = project.path("source-supplement.json")
                if content is not None:
                    supplement.write_text(content)
                output = project.path("compiled.sh")

                with self.assertRaisesRegex(NotImplementedError, "source supplement") as cm:
                    project.compile("main.sh", output=output, mode="executable", source_supplement=supplement)

                self.assertEqual(cm.exception.code, "unsupported.source.supplement")
                self.assertFalse(output.exists())

    def test_positional_helper_source_safety_rejections(self):
        cases = {
            "zero args": 'source_safe\n',
            "unquoted all args": 'source_safe() { source $@; }\nsource_safe ./dep.sh\n',
        }
        for name, call_or_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("other.sh", 'echo "other"\n')
                if name == "unquoted all args":
                    helpers = call_or_content
                else:
                    helpers = textwrap.dedent(f"""\
                        source_safe() {{
                          source "$@"
                        }}
                        {call_or_content}
                        """)
                project.write("helpers.sh", helpers)
                project.write("main.sh", 'source ./helpers.sh\n')

                with self.assertRaisesRegex(NotImplementedError, "positional source") as cm:
                    project.compile("main.sh", mode="executable")

                self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-positionals")

    def test_positional_helper_source_with_arguments_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  source "$@"
                }
                source_safe ./dep.sh alpha beta
                """))
            project.write("main.sh", "source ./helpers.sh\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_positional_helper_source_preserves_single_quoted_arguments(self):
        with ScriptProject() as project:
            project.write("dep.sh", "printf 'dep:%s\\n' \"$1\"\n")
            project.write("helpers.sh", textwrap.dedent("""\
                source_safe() {
                  source "$@"
                }
                source_safe ./dep.sh '$1'
                """))
            project.write("main.sh", "source ./helpers.sh\n")

            project.assert_compiled_matches(self, "main.sh")

    def test_shifted_helper_source_with_arguments_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                printf 'dep:%s:%s:%s\\n' "$1" "$2" "$#"
                """))
            project.write("main.sh", textwrap.dedent("""\
                load_source() {
                  local source_file=$1
                  shift
                  source "$source_file" "$@"
                }
                load_source ./dep.sh "alpha beta" gamma
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_direct_source_if_condition_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                echo "dep:$1"
                return 0
                """))
            project.write("then.sh", 'echo "then:$1"\n')
            project.write("else.sh", 'echo "else"\n')
            project.write("main.sh", textwrap.dedent("""\
                if source ./dep.sh alpha; then
                  source ./then.sh beta
                else
                  source ./else.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_negated_direct_source_if_condition_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                echo "dep"
                return 3
                """))
            project.write("fallback.sh", 'echo "fallback"\n')
            project.write("main.sh", textwrap.dedent("""\
                if ! source ./dep.sh; then
                  source ./fallback.sh
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_compound_source_if_condition_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", textwrap.dedent("""\
                if source ./dep.sh && true; then
                  echo "loaded"
                fi
                """))

            compiled = project.compile("main.sh", mode="executable")
            compiled_text = compiled.read_text()

            self.assertNotIn("source ./dep.sh", compiled_text)
            project.assert_compiled_matches(self, "main.sh")

    def test_compound_source_if_condition_preserves_source_status(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\nfalse\n')
            project.write("then.sh", 'NEXT=real.sh\n')
            project.write("real.sh", 'echo "real"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh && source ./then.sh
                source "$NEXT"
                echo done
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent|unresolved") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertTrue(cm.exception.diagnostic.code.startswith("unsupported.source."))
            self.assertFalse(output.exists())

    def test_compound_source_if_condition_fallback_matches_bash(self):
        with ScriptProject() as project:
            project.write("primary.sh", textwrap.dedent("""\
                echo primary
                return 7
                """))
            project.write("fallback.sh", textwrap.dedent("""\
                echo fallback
                FALLBACK_STATE=loaded
                """))
            project.write("main.sh", textwrap.dedent("""\
                if source ./primary.sh || source ./fallback.sh; then
                  echo "state=$FALLBACK_STATE"
                fi
                """))

            project.assert_compiled_matches(self, "main.sh")

    def test_compound_source_if_condition_preserves_unknown_source_status(self):
        for fail, expected_state in (("0", "unset"), ("1", "loaded")):
            with self.subTest(fail=fail), ScriptProject() as project:
                project.write("dep.sh", "awk 'BEGIN { exit ENVIRON[\"SOURCE_FAIL\"] == \"1\" ? 1 : 0 }'\n")
                project.write("fallback.sh", textwrap.dedent("""\
                    echo fallback
                    FALLBACK_STATE=loaded
                    """))
                project.write("main.sh", textwrap.dedent("""\
                    FALLBACK_STATE=unset
                    if source ./dep.sh || source ./fallback.sh; then
                      echo "state=$FALLBACK_STATE"
                    else
                      echo failed
                    fi
                    """))

                output = project.compile("main.sh", mode="executable")
                compiled_text = output.read_text()

                self.assertNotIn("source ./fallback.sh", compiled_text)
                self.assertIn("echo fallback", compiled_text)
                expected = project.run("main.sh", env={"SOURCE_FAIL": fail})
                actual = project.run(output, env={"SOURCE_FAIL": fail})

                self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                self.assertEqual(actual.stdout, expected.stdout)
                self.assertIn(f"state={expected_state}", actual.stdout)

    def test_negated_compound_source_if_condition_matches_bash(self):
        with ScriptProject() as project:
            project.write("dep.sh", textwrap.dedent("""\
                echo dep
                return 4
                """))
            project.write("fallback.sh", 'echo fallback\n')
            project.write("main.sh", textwrap.dedent("""\
                if ! source ./dep.sh || source ./fallback.sh; then
                  echo loaded
                fi
                """))

            compiled = project.compile("main.sh", mode="executable")
            compiled_text = compiled.read_text()

            self.assertIn("if ! {", compiled_text)
            self.assertNotIn("source ./dep.sh", compiled_text)
            project.assert_compiled_matches(self, "main.sh")

    def test_runtime_guarded_compound_source_if_condition_matches_bash(self):
        for enabled in ("1", "0"):
            with self.subTest(enabled=enabled), ScriptProject() as project:
                project.write("dep.sh", 'echo dep\nSTATE=loaded\n')
                project.write("main.sh", textwrap.dedent("""\
                    if awk 'BEGIN { exit ENVIRON["LOAD_DEP"] == "1" ? 0 : 1 }' && source ./dep.sh; then
                      echo "then=$STATE"
                    else
                      echo "else=${STATE-unset}"
                    fi
                    """))

                project.assert_compiled_matches(self, "main.sh", env={"LOAD_DEP": enabled})

    def test_compound_source_if_condition_rejects_branch_dependent_state(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'NEXT=next.sh\n')
            project.write("next.sh", 'echo next\n')
            project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit ENVIRON["LOAD_DEP"] == "1" ? 0 : 1 }' && source ./dep.sh; then
                  echo loaded
                fi
                source "$NEXT"
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.branch-state")
            self.assertFalse(output.exists())

    def test_runtime_guarded_compound_source_if_condition_rejects_dynamic_path(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo dep\n')
            project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit 0 }' && source "$DEP"; then
                  echo loaded
                fi
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "unresolved source") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.unresolved")
            self.assertFalse(output.exists())

    def test_unreachable_elif_source_condition_is_removed(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo dep\n')
            project.write("main.sh", textwrap.dedent("""\
                if true; then
                  echo first
                elif source ./dep.sh; then
                  echo second
                fi
                """))

            compiled = project.compile("main.sh", mode="executable")
            compiled_text = compiled.read_text()

            self.assertNotIn("source ./dep.sh", compiled_text)
            project.assert_compiled_matches(self, "main.sh")

    def test_compound_source_if_condition_rejects_pipeline(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo dep\n')
            project.write("main.sh", textwrap.dedent("""\
                if source ./dep.sh | cat; then
                  echo loaded
                fi
                """))
            output = project.path("compiled.sh")

            with self.assertRaisesRegex(NotImplementedError, "pipeline") as cm:
                project.compile("main.sh", output=output, mode="executable")

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.if-condition")
            self.assertFalse(output.exists())

    def test_cli_prints_source_supplement_skeleton(self):
        with ScriptProject() as project:
            project.write("main.sh", 'source "$MISSING_DEP"\n')
            output = project.path("compiled.sh")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "modash.py"),
                    str(project.path("main.sh")),
                    str(output),
                    "--mode",
                    "executable",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source supplement skeleton", result.stderr)
            self.assertIn('"MISSING_DEP": "<path>"', result.stderr)
            self.assertFalse(output.exists())

    def test_runtime_dynamic_sources_raise_clear_diagnostic(self):
        cases = {
            "cat multiple operands": (
                'source "$(cat dep-path.txt other.txt)"\n',
                'source "$(cat dep-path.txt other.txt)"',
            ),
            "cat multiple lines": (
                'source "$(cat dep-path.txt)"\n',
                'source "$(cat dep-path.txt)"',
            ),
            "cat pipe": (
                'source "$(cat dep-path.txt | head -1)"\n',
                'source "$(cat dep-path.txt | head -1)"',
            ),
            "cat missing path file": (
                'source "$(cat missing-path.txt)"\n',
                'source "$(cat missing-path.txt)"',
            ),
            "cat empty path file": (
                'source "$(cat empty-path.txt)"\n',
                'source "$(cat empty-path.txt)"',
            ),
            "find multiple matches": (
                'source "$(find . -name dep.sh)"\n',
                'source "$(find . -name dep.sh)"',
            ),
            "find no match": (
                'source "$(find ./nested -type f -name missing.sh -print -quit)"\n',
                'source "$(find ./nested -type f -name missing.sh -print -quit)"',
            ),
            "find exec": (
                'source "$(find . -name dep.sh -exec echo {} \\;)"\n',
                'source "$(find . -name dep.sh -exec echo {} \\;)"',
            ),
            "find quit without print": (
                'source "$(find ./nested -type f -name dep.sh -quit)"\n',
                'source "$(find ./nested -type f -name dep.sh -quit)"',
            ),
            "eval extra command": (
                'eval "source ./dep.sh; echo unsafe"\n',
                'eval "source ./dep.sh; echo unsafe"',
            ),
            "eval nested dynamic": (
                'eval "source $(cat dep-path.txt)"\n',
                'eval "source $(cat dep-path.txt)"',
            ),
            "eval unresolved payload source": (
                'COMMAND="source $DEP"\neval "$COMMAND"\n',
                'eval "$COMMAND"',
            ),
            "backticks": (
                "source `cat dep-path.txt`\n",
                "source `cat dep-path.txt`",
            ),
        }

        for name, (content, expected_fragment) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write("dep.sh", 'echo "dep"\n')
                project.write("nested/dep.sh", 'echo "nested dep"\n')
                if name == "cat multiple lines":
                    project.write("dep-path.txt", "./dep.sh\n./nested/dep.sh\n")
                else:
                    project.write("dep-path.txt", "./dep.sh\n")
                project.write("empty-path.txt", "\n")
                project.write("other.txt", "./nested/dep.sh\n")
                project.write("main.sh", content)
                output = project.path("compiled.sh")

                with self.assertRaisesRegex((ValueError, NotImplementedError), "unsupported|ambiguous|dynamic|source") as cm:
                    project.compile("main.sh", output=output, mode="executable")

                self.assertIn(expected_fragment, str(cm.exception))
                self.assertIsNotNone(cm.exception.diagnostic)
                self.assertEqual(cm.exception.diagnostic.severity, DiagnosticSeverity.ERROR)
                self.assertEqual(cm.exception.diagnostic.location.path, project.path("main.sh"))
                self.assertGreater(cm.exception.diagnostic.location.line, 0)
                self.assertIn(expected_fragment, cm.exception.diagnostic.fragment)
                self.assertTrue(cm.exception.diagnostic.code.startswith("unsupported.source."))
                self.assertFalse(output.exists())

    def test_bash_c_source_is_lowered_for_executable_mode(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            project.write("main.sh", 'bash -c "source ./dep.sh"\n')

            result = project.run(project.compile("main.sh", mode="executable"))

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep\n")


if __name__ == "__main__":
    unittest.main()
