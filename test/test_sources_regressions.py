import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.shell_line import first_top_level_pipeline_index, get_commands
from methods.sources import get_sources, resolve_variable_references
from test.support import ScriptProject


class SourceRegressionTestCase(unittest.TestCase):
    def test_empty_parameter_default_resolves_to_empty_string(self):
        context = {"vars": {}, "current_directory": os.getcwd()}

        self.assertEqual(resolve_variable_references("${MISSING:-}", context), "")
        self.assertEqual(resolve_variable_references("${MISSING-}", context), "")

    def test_parameter_alternate_value_resolves_without_recursive_self_reference(self):
        context = {
            "vars": {
                "VALUE": "loaded",
                "1": "arg",
            },
            "current_directory": os.getcwd(),
        }

        self.assertEqual(resolve_variable_references("${VALUE:+$VALUE,}$1", context), "loaded,arg")
        self.assertEqual(resolve_variable_references("${VALUE+set}", context), "set")
        self.assertEqual(resolve_variable_references("${MISSING:+nope}", context), "")
        self.assertEqual(
            resolve_variable_references("${EMPTY+set}", {"vars": {"EMPTY": ""}, "current_directory": os.getcwd()}),
            "set",
        )
        self.assertEqual(
            resolve_variable_references("${EMPTY:+set}", {"vars": {"EMPTY": ""}, "current_directory": os.getcwd()}),
            "",
        )

    def test_get_commands_keeps_hash_inside_words_and_paths(self):
        self.assertEqual(
            list(get_commands('echo foo#bar; source dep.sh')),
            ['echo foo#bar', 'source dep.sh'],
        )
        self.assertEqual(
            list(get_commands('source ./dir#1/dep.sh; echo done')),
            ['source ./dir#1/dep.sh', 'echo done'],
        )
        self.assertEqual(
            list(get_commands('echo foo # comment; source dep.sh')),
            ['echo foo'],
        )

    def test_get_commands_ignores_quoted_separators_and_comments(self):
        self.assertEqual(
            list(get_commands('echo "a;b"; source file.sh')),
            ['echo "a;b"', 'source file.sh'],
        )
        self.assertEqual(
            list(get_commands('echo "not # comment"; source file.sh # trailing')),
            ['echo "not # comment"', 'source file.sh'],
        )
        self.assertEqual(
            list(get_commands('echo "a&&b"; source file.sh')),
            ['echo "a&&b"', 'source file.sh'],
        )

    def test_get_commands_splits_top_level_logical_operators(self):
        self.assertEqual(
            list(get_commands('cd subdir && source ./dep.sh || echo missing')),
            ['cd subdir', 'source ./dep.sh', 'echo missing'],
        )
        self.assertEqual(
            list(get_commands('if [[ -f ./dep.sh && -n "$LOAD_DEP" ]]; then source ./dep.sh; fi')),
            ['if [[ -f ./dep.sh && -n "$LOAD_DEP" ]]', 'then source ./dep.sh', 'fi'],
        )
        self.assertEqual(
            list(get_commands('[[ -f ./dep.sh ]] && source ./dep.sh')),
            ['[[ -f ./dep.sh ]]', 'source ./dep.sh'],
        )

    def test_top_level_pipeline_detection_ignores_non_pipeline_bars(self):
        self.assertIsNone(first_top_level_pipeline_index('source ./dep.sh || echo missing'))
        self.assertIsNone(first_top_level_pipeline_index('[[ "$x" == a|b ]] && source ./dep.sh'))
        self.assertIsNone(first_top_level_pipeline_index('[[ "$x" == "]]|[[" ]] && source ./dep.sh'))
        self.assertIsNone(first_top_level_pipeline_index('cat <(printf "a|b")'))
        self.assertEqual(first_top_level_pipeline_index('source ./dep.sh | cat'), len('source ./dep.sh '))

    def test_get_commands_preserves_nested_shell_constructs(self):
        self.assertEqual(
            list(get_commands('cat <(echo before; source ./dep.sh); echo done')),
            ['cat <(echo before; source ./dep.sh)', 'echo done'],
        )
        self.assertEqual(
            list(get_commands('echo `echo before; source ./dep.sh`; echo done')),
            ['echo `echo before; source ./dep.sh`', 'echo done'],
        )
        self.assertEqual(
            list(get_commands('(source ./dep.sh; echo nested); echo done')),
            ['(source ./dep.sh; echo nested)', 'echo done'],
        )

    def test_command_wrapped_source_is_detected_as_source_command(self):
        from methods.source_resolver import contains_nested_source_command, contains_source_command, source_command_invocation

        self.assertTrue(contains_source_command('command source ./dep.sh'))
        self.assertTrue(contains_source_command('command -p source ./dep.sh'))
        self.assertTrue(contains_source_command('command -- source ./dep.sh'))
        self.assertTrue(contains_source_command('builtin source ./dep.sh'))
        self.assertTrue(contains_source_command('builtin -- source ./dep.sh'))
        self.assertTrue(contains_source_command('builtin -- . ./dep.sh'))
        self.assertTrue(contains_source_command('FOO=bar source ./dep.sh'))
        self.assertTrue(contains_source_command('FOO=bar command source ./dep.sh'))
        self.assertTrue(contains_source_command('! source ./dep.sh'))
        self.assertTrue(contains_source_command('if ! source ./dep.sh'))
        self.assertTrue(contains_source_command('helper(){ source ./dep.sh'))
        self.assertTrue(contains_source_command('function helper { source ./dep.sh'))
        self.assertFalse(contains_source_command('command echo source ./dep.sh'))
        self.assertFalse(contains_source_command('command -v source'))
        self.assertFalse(contains_source_command('command -V source'))
        self.assertFalse(contains_source_command('FOO=bar echo source ./dep.sh'))
        self.assertTrue(contains_nested_source_command('(source ./dep.sh)'))
        self.assertTrue(contains_nested_source_command('(. ./dep.sh)'))
        self.assertTrue(contains_nested_source_command('cat <(source ./dep.sh)'))
        self.assertTrue(contains_nested_source_command('echo "$(source ./dep.sh)"'))
        self.assertTrue(contains_nested_source_command('echo `source ./dep.sh`'))
        self.assertTrue(contains_nested_source_command('echo $(( $(source ./dep.sh) + 1 ))'))
        self.assertTrue(contains_nested_source_command('echo "$(if true; then source ./dep.sh; fi)"'))
        self.assertTrue(contains_nested_source_command('cat <(for f in ./dep.sh; do source "$f"; done)'))
        self.assertTrue(contains_nested_source_command('echo `case "$ENV" in prod) . ./dep.sh ;; esac`'))
        self.assertFalse(contains_nested_source_command('echo "source ./dep.sh"'))
        self.assertFalse(contains_nested_source_command("echo 'source ./dep.sh'"))
        self.assertFalse(contains_nested_source_command('echo $((1 + 2))'))
        self.assertFalse(contains_nested_source_command('tokens=(source ./dep.sh)'))
        self.assertFalse(contains_nested_source_command('declare -a tokens=(source ./dep.sh)'))

        invocation = source_command_invocation('command -p source ./dep.sh arg')
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.command_name, 'source')
        self.assertEqual(invocation.source_expression, './dep.sh arg')
        self.assertEqual(invocation.source_site, 'command -p source ./dep.sh arg')
        self.assertTrue(invocation.wrapped)

        invocation = source_command_invocation('builtin . ./dep.sh')
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.command_name, '.')
        self.assertEqual(invocation.source_expression, './dep.sh')
        self.assertEqual(invocation.source_site, 'builtin . ./dep.sh')

        invocation = source_command_invocation('builtin -- source ./dep.sh arg')
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.command_name, 'source')
        self.assertEqual(invocation.source_expression, './dep.sh arg')
        self.assertEqual(invocation.source_site, 'builtin -- source ./dep.sh arg')
        self.assertTrue(invocation.wrapped)

    def test_heredoc_detection_ignores_quotes_and_arithmetic(self):
        from methods.source_resolver import extract_heredoc_delimiters

        self.assertEqual([item.value for item in extract_heredoc_delimiters('cat <<EOF')], ['EOF'])
        self.assertEqual([item.value for item in extract_heredoc_delimiters("cat <<'EOF'")], ['EOF'])
        self.assertEqual([item.value for item in extract_heredoc_delimiters(r'cat <<-\EOF')], ['EOF'])
        self.assertEqual(extract_heredoc_delimiters('echo "<<EOF"'), [])
        self.assertEqual(extract_heredoc_delimiters('echo $((1 << 2))'), [])
        self.assertEqual(extract_heredoc_delimiters('(( value << 2 ))'), [])
        self.assertEqual(extract_heredoc_delimiters('done <<< "$output"'), [])

    def test_static_source_discovery_matrix(self):
        with ScriptProject() as project:
            absolute_dep = project.write("absolute.sh", 'echo "absolute"\n')
            project.write("dep.sh", 'echo "relative"\n')
            project.write("dot.sh", 'echo "dot"\n')
            project.write("dir with spaces/dep.sh", 'echo "spaces"\n')
            project.write("dir#tag/dep.sh", 'echo "hash"\n')
            project.write("config", 'echo "config"\n')
            project.write("main.sh", "\n".join([
                "source ./dep.sh",
                ". ./dot.sh",
                'source "./dir with spaces/dep.sh"',
                'source ./dir#tag/dep.sh',
                "source ./config",
                f'source "{absolute_dep}"',
                "",
            ]))

            project.assert_sources(self, "main.sh", [
                "dep.sh",
                "dot.sh",
                "dir with spaces/dep.sh",
                "dir#tag/dep.sh",
                "config",
                "absolute.sh",
                "main.sh",
            ])

    def test_get_sources_does_not_mutate_process_cwd(self):
        with ScriptProject() as project:
            project.write("dir1/dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", 'cd dir1\nsource ./dep.sh\n')

            before = os.getcwd()
            try:
                get_sources(str(entry))
                after = os.getcwd()
            finally:
                os.chdir(before)

        self.assertEqual(after, before)

    def test_get_sources_uses_ir_evaluator_for_modeled_control_flow(self):
        with ScriptProject() as project:
            project.write("loop-a.sh", 'echo "loop a"\n')
            project.write("loop-b.sh", 'echo "loop b"\n')
            project.write("if-dep.sh", 'echo "if dep"\n')
            project.write("case-dep.sh", 'echo "case dep"\n')
            project.write("main.sh", "\n".join([
                "for dep in ./loop-a.sh ./loop-b.sh; do",
                '  source "$dep"',
                "done",
                "MODE=prod",
                'if [[ "$MODE" == prod ]]; then',
                "  source ./if-dep.sh",
                "fi",
                "ENV=prod",
                'case "$ENV" in',
                "  prod) source ./case-dep.sh ;;",
                "esac",
                "",
            ]))

            actual = [
                path.relative_to(project.root).as_posix()
                for path in project.sources("main.sh")
            ]

        self.assertEqual(actual, [
            "loop-a.sh",
            "loop-b.sh",
            "if-dep.sh",
            "case-dep.sh",
            "main.sh",
        ])

    def test_relative_source_resolution_does_not_fall_back_to_process_cwd(self):
        before = os.getcwd()
        with ScriptProject() as project:
            project.write("caller/dep.sh", 'echo "wrong cwd dependency"\n')
            project.write("script/main.sh", 'source ./dep.sh\n')

            try:
                os.chdir(project.path("caller"))
                actual = [
                    path.relative_to(project.root).as_posix()
                    for path in project.sources("script/main.sh", mode="context")
                ]
            finally:
                os.chdir(before)

        self.assertEqual(actual, ["script/main.sh"])

    def test_sample_dir_discovery_graph_stays_explicit(self):
        before = os.getcwd()
        try:
            actual_sources, _ = get_sources(str(REPO_ROOT / "test" / "sample_dir" / "script_main.sh"))
            entry_directory = REPO_ROOT / "test" / "sample_dir"
            actual = [Path(path).relative_to(entry_directory).as_posix() for path in actual_sources]
        finally:
            os.chdir(before)

        self.assertEqual(actual, [
            "dir1/script6.sh",
            "script5.sh",
            "script4.sh",
            "dir with spaces/script3.sh",
            "dir2/script2.sh",
            "dir1/script1.sh",
            "script_main.sh",
        ])


if __name__ == "__main__":
    unittest.main()
