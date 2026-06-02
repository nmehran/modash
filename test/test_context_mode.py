import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test.support import ScriptProject


class ContextModeTestCase(unittest.TestCase):
    def test_cli_default_mode_is_context(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            entry = project.write("main.sh", 'source ./dep.sh\necho "main body"\n')
            output = project.path("context-output.sh")

            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "modash.py"), str(entry), str(output)],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            content = output.read_text()
            self.assertIn("# modash context", content)
            self.assertIn("# mode: context", content)
            self.assertIn("# modash: source ./dep.sh -> dep.sh", content)

    def test_context_output_uses_unique_dependency_first_sections(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                source ./dep.sh
                source ./dep.sh
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertLess(content.index('echo "dep body"'), content.index('echo "main body"'))
        self.assertEqual(content.count('echo "dep body"'), 1)
        self.assertEqual(content.count("# modash: source ./dep.sh -> dep.sh"), 2)

    def test_context_output_includes_non_sh_sourced_files(self):
        with ScriptProject() as project:
            project.write("config", 'export FEATURE_FLAG=yes\n')
            project.write("main.sh", 'source ./config\necho "$FEATURE_FLAG"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn("config", content)
        self.assertIn("export FEATURE_FLAG=yes", content)
        self.assertIn("# modash: source ./config -> config", content)

    def test_context_output_preserves_original_source_lines(self):
        with ScriptProject() as project:
            project.write("dir with spaces/dep.sh", 'echo "dep body"\n')
            project.write("main.sh", 'source "./dir with spaces/dep.sh"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('# modash: source "./dir with spaces/dep.sh" -> dir with spaces/dep.sh', content)
        self.assertIn('source "./dir with spaces/dep.sh"', content)

    def test_context_output_resolves_safe_cat_source(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("dep-path.txt", "./dep.sh\n")
            project.write("main.sh", 'source "$(cat dep-path.txt)"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "dep body"', content)
        self.assertIn('# modash: source "$(cat dep-path.txt)" -> dep.sh', content)
        self.assertIn('source "$(cat dep-path.txt)"', content)

    def test_context_output_resolves_safe_find_source(self):
        with ScriptProject() as project:
            project.write("plugins/init.sh", 'echo "dep body"\n')
            project.write("main.sh", 'source "$(find ./plugins -type f -name init.sh -print -quit)"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "dep body"', content)
        self.assertIn('# modash: source "$(find ./plugins -type f -name init.sh -print -quit)" -> plugins/init.sh', content)
        self.assertIn('source "$(find ./plugins -type f -name init.sh -print -quit)"', content)

    def test_context_output_resolves_safe_eval_source(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", 'eval "source ./dep.sh"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "dep body"', content)
        self.assertIn('# modash: source ./dep.sh -> dep.sh', content)
        self.assertIn('eval "source ./dep.sh"', content)

    def test_context_output_resolves_exact_array_index_source(self):
        with ScriptProject() as project:
            project.write("deps/feature.sh", 'echo "feature body"\n')
            project.write("main.sh", textwrap.dedent("""\
                deps=(./unused.sh ./deps/feature.sh)
                source "${deps[1]}"
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "feature body"', content)
        self.assertIn('# modash: source "${deps[1]}" -> deps/feature.sh', content)
        self.assertIn('source "${deps[1]}"', content)

    def test_context_output_resolves_exact_for_loop_sources(self):
        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "loop a body"\n')
            project.write("deps/b.sh", 'echo "loop b body"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./deps/a.sh ./deps/b.sh; do
                  source "$dep"
                done
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "loop a body"', content)
        self.assertIn('echo "loop b body"', content)
        self.assertIn('# modash: source "$dep" -> deps/a.sh', content)
        self.assertIn('# modash: source "$dep" -> deps/b.sh', content)
        self.assertIn('source "$dep"', content)
        self.assertEqual(content.count('echo "loop a body"'), 1)

    def test_context_output_resolves_scalar_word_list_loop_sources(self):
        with ScriptProject() as project:
            project.write("deps/a.sh", 'echo "scalar loop a body"\n')
            project.write("deps/b.sh", 'echo "scalar loop b body"\n')
            project.write("main.sh", textwrap.dedent("""\
                DEPS="./deps/a.sh ./deps/b.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "scalar loop a body"', content)
        self.assertIn('echo "scalar loop b body"', content)
        self.assertIn('# modash: source "$dep" -> deps/a.sh', content)
        self.assertIn('# modash: source "$dep" -> deps/b.sh', content)

    def test_context_output_resolves_glob_for_loop_sources(self):
        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "glob b body"\n')
            project.write("plugins/a.sh", 'echo "glob a body"\n')
            project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "glob a body"', content)
        self.assertIn('echo "glob b body"', content)
        self.assertIn('# modash: source "$dep" -> plugins/a.sh', content)
        self.assertIn('# modash: source "$dep" -> plugins/b.sh', content)
        self.assertEqual(content.count('echo "glob a body"'), 1)

    def test_context_output_preserves_unsupported_c_style_loop_without_failing(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                for (( i=$(cat start.txt); i<2; i++ )); do
                  source ./dep.sh
                done
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "dep body"', content)
        self.assertIn('# modash: source ./dep.sh -> dep.sh (conditional)', content)
        self.assertIn('for (( i=$(cat start.txt); i<2; i++ )); do', content)
        self.assertIn('echo "main body"', content)

    def test_context_output_preserves_unresolved_source_without_failing(self):
        with ScriptProject() as project:
            project.write("main.sh", 'source "$OPTIONAL_DEP"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('source "$OPTIONAL_DEP"', content)
        self.assertIn('echo "main body"', content)

    def test_context_output_does_not_leak_state_from_control_flow_sources(self):
        with ScriptProject() as project:
            project.write("optional.sh", 'NEXT=./next.sh\n')
            project.write("next.sh", 'echo "next body"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$LOAD_OPTIONAL" ]]; then
                  source ./optional.sh
                fi
                source "$NEXT"
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('source "$NEXT"', content)
        self.assertNotIn('echo "next body"', content)
        self.assertIn('# modash: source ./optional.sh -> optional.sh (conditional: [[ -n "$LOAD_OPTIONAL" ]])', content)
        self.assertIn('echo "main body"', content)

    def test_context_output_marks_mutually_exclusive_if_sources(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod body"\n')
            project.write("dev.sh", 'echo "dev body"\n')
            project.write("main.sh", textwrap.dedent("""\
                if [[ "$MODE" == prod ]]; then
                  source ./prod.sh
                else
                  source ./dev.sh
                fi
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('# modash: source ./prod.sh -> prod.sh (mutually-exclusive: [[ "$MODE" == prod ]])', content)
        self.assertIn('# modash: source ./dev.sh -> dev.sh (mutually-exclusive: else)', content)

    def test_context_output_marks_mutually_exclusive_case_sources(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod body"\n')
            project.write("dev.sh", 'echo "dev body"\n')
            project.write("default.sh", 'echo "default body"\n')
            project.write("main.sh", textwrap.dedent("""\
                case "$ENV" in
                  prod|stage) source ./prod.sh ;;
                  dev) source ./dev.sh ;;
                  *) source ./default.sh ;;
                esac
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('# modash: source ./prod.sh -> prod.sh (mutually-exclusive: case "$ENV" in prod|stage)', content)
        self.assertIn('# modash: source ./dev.sh -> dev.sh (mutually-exclusive: case "$ENV" in dev)', content)
        self.assertIn('# modash: source ./default.sh -> default.sh (mutually-exclusive: case "$ENV" in *)', content)

    def test_context_output_classifies_bash_c_source_as_child_shell(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", 'bash -c "source ./dep.sh"\necho "main body"\n')

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "dep body"', content)
        self.assertIn('# modash: bash -c "source ./dep.sh" -> dep.sh (child-shell)', content)
        self.assertIn('bash -c "source ./dep.sh"', content)

    def test_context_output_indents_source_relationship_comments(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                helper() {
                  source ./dep.sh
                }
                helper
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn("  # modash: source ./dep.sh -> dep.sh\n  source ./dep.sh", content)

    def test_context_output_resolves_function_source_arguments(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "function a body"\n')
            project.write("b.sh", 'echo "function b body"\n')
            project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }
                load_dep ./a.sh
                load_dep ./b.sh
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('echo "function a body"', content)
        self.assertIn('echo "function b body"', content)
        self.assertIn('# modash: source "$1" -> a.sh', content)
        self.assertIn('# modash: source "$1" -> b.sh', content)

    def test_context_output_marks_source_arguments(self):
        with ScriptProject() as project:
            project.write("plugins/00-loader.sh", 'echo "loader body"\n')
            project.write("plugins/10-arg.sh", 'echo "arg body"\n')
            project.write("main.sh", "source ./plugins/*.sh explicit\n")

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn("echo \"loader body\"", content)
        self.assertIn(
            "# modash: source ./plugins/*.sh explicit -> plugins/00-loader.sh "
            "(args: './plugins/10-arg.sh' 'explicit')",
            content,
        )

    def test_context_output_preserves_dynamic_positional_assignment(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                set -- "$UNKNOWN"
                source ./dep.sh
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('set -- "$UNKNOWN"', content)
        self.assertIn("# modash: source ./dep.sh -> dep.sh", content)

    def test_context_output_does_not_resolve_heredoc_source_text(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                cat <<EOF
                source ./dep.sh
                EOF
                echo "main body"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertIn('source ./dep.sh', content)
        self.assertNotIn('# modash: source ./dep.sh -> dep.sh', content)

    def test_context_mode_is_not_runtime_parity_mode(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", textwrap.dedent("""\
                echo "before source"
                source ./dep.sh
                echo "after source"
                """))

            output = project.compile("main.sh")
            content = output.read_text()

        self.assertLess(content.index('echo "dep body"'), content.index('echo "before source"'))

    def test_executable_mode_has_no_context_annotations(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep body"\n')
            project.write("main.sh", 'source ./dep.sh\necho "main body"\n')

            output = project.compile("main.sh", mode="executable")
            content = output.read_text()

        self.assertNotIn("# modash: source", content)
        self.assertNotIn("# [", content)


if __name__ == "__main__":
    unittest.main()
