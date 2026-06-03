import unittest

from methods.shell.scan import (
    command_substitution_bodies,
    is_array_assignment_paren,
    process_substitution_bodies,
    read_balanced_body,
    subshell_bodies,
    top_level_pipeline_segments,
)


class ShellScanTestCase(unittest.TestCase):
    def test_read_balanced_body_ignores_quoted_parentheses(self):
        body, end = read_balanced_body("(echo '('; printf \"x)\"; (nested)) tail", 1)
        self.assertEqual(body, "echo '('; printf \"x)\"; (nested)")
        self.assertEqual(end, len("(echo '('; printf \"x)\"; (nested)"))

    def test_subshell_bodies_ignore_arrays_and_quoted_text(self):
        command = "items=(source ./ignored.sh); echo \"(source ./quoted.sh)\"; (source ./dep.sh)"
        bodies = tuple(subshell_bodies(command))
        self.assertEqual([(body, index) for body, _, index in bodies], [("source ./dep.sh", 1)])
        self.assertTrue(is_array_assignment_paren(command, command.index("(")))

    def test_command_substitution_bodies_include_double_quotes_but_not_single_quotes_or_arithmetic(self):
        command = "echo '$(source ./single.sh)' \"$(source ./double.sh)\" $(( $(source ./arith.sh) + 1 ))"
        bodies = tuple(command_substitution_bodies(command))
        self.assertEqual([(body, index) for body, _, index in bodies], [("source ./double.sh", 1)])

    def test_process_substitution_bodies_ignore_quoted_text(self):
        command = "cat \"<(source ./quoted.sh)\" <(source ./dep.sh)"
        bodies = tuple(process_substitution_bodies(command))
        self.assertEqual([(body, index) for body, _, index in bodies], [("source ./dep.sh", 1)])

    def test_top_level_pipeline_segments_ignore_or_and_nested_pipes(self):
        command = "source ./dep.sh || echo missed | (grep 'a|b') | cat"
        self.assertEqual(
            [segment for segment, _ in top_level_pipeline_segments(command)],
            ["source ./dep.sh || echo missed", "(grep 'a|b')", "cat"],
        )


if __name__ == "__main__":
    unittest.main()
