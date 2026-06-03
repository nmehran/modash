import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.source_commands import (  # noqa: E402
    clean_shell_word,
    is_source_like_command_text,
    is_trace_wrapper_source_command,
    normalized_trace_wrapper_words,
    source_invocation_from_command,
)


class RuntimeSourceCommandsTestCase(unittest.TestCase):
    def assert_invocation(self, command, source_path, arguments):
        invocation = source_invocation_from_command(command)
        self.assertIsNotNone(invocation, command)
        self.assertEqual(invocation.source_path, source_path)
        self.assertEqual(invocation.arguments, tuple(arguments))

    def test_source_invocation_parses_supported_source_wrappers(self):
        cases = {
            "direct source": ("source ./dep.sh arg", "./dep.sh", ("arg",)),
            "direct dot": (". ./dep.sh dot-arg", "./dep.sh", ("dot-arg",)),
            "builtin delimiter": ("builtin -- source ./dep.sh builtin-arg", "./dep.sh", ("builtin-arg",)),
            "command path": ("command -p source ./dep.sh command-arg", "./dep.sh", ("command-arg",)),
            "command path delimiter": ("command -p -- . ./dep.sh dot-arg", "./dep.sh", ("dot-arg",)),
        }

        for name, (command, source_path, arguments) in cases.items():
            with self.subTest(name=name):
                self.assert_invocation(command, source_path, arguments)

    def test_source_invocation_ignores_command_query_forms(self):
        for command in ("command -v source", "command -V source", "command -pv source"):
            with self.subTest(command=command):
                self.assertIsNone(source_invocation_from_command(command))
                self.assertFalse(is_source_like_command_text(command))

    def test_trace_wrapper_normalization_preserves_wrapped_option_forms(self):
        cases = {
            "alias source": (
                ("__modash_trace_source_alias", "source", "source", "0", "--", "./dep.sh", "arg"),
                ("source", "./dep.sh", "arg"),
            ),
            "alias dot": (
                ("__modash_trace_source_alias", "dot", ".", "0", "--", "./dep.sh", "arg"),
                (".", "./dep.sh", "arg"),
            ),
            "builtin delimiter source": (
                ("__modash_trace_builtin", "0", "--", "--", "source", "./dep.sh", "arg"),
                ("builtin", "--", "source", "./dep.sh", "arg"),
            ),
            "command path source": (
                ("__modash_trace_command", "0", "--", "-p", "source", "./dep.sh", "arg"),
                ("command", "-p", "source", "./dep.sh", "arg"),
            ),
        }

        for name, (words, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(normalized_trace_wrapper_words(words), expected)
                self.assertTrue(is_trace_wrapper_source_command(" ".join(words)))

    def test_trace_wrapper_non_source_commands_are_not_trusted(self):
        cases = (
            "__modash_trace_command 0 -- echo not-source",
            "__modash_trace_command 0 -- -v source",
            "__modash_trace_builtin 0 -- echo not-source",
            "__modash_trace_source_alias source source 0",
            "__modash_trace_source_alias echo echo 0 -- ./dep.sh",
        )

        for command in cases:
            with self.subTest(command=command):
                self.assertFalse(is_trace_wrapper_source_command(command))

    def test_source_like_text_accepts_option_forms_and_legacy_prefixes(self):
        self.assertTrue(is_source_like_command_text("command -- source ./dep.sh"))
        self.assertTrue(is_source_like_command_text("builtin -- . ./dep.sh"))
        self.assertTrue(is_source_like_command_text("__modash_trace_source_alias source source"))
        self.assertFalse(is_source_like_command_text("echo source ./dep.sh"))

    def test_clean_shell_word_strips_quotes_and_trailing_semicolons(self):
        self.assertEqual(clean_shell_word("'./dep.sh';;"), "./dep.sh")


if __name__ == "__main__":
    unittest.main()
