import unittest

from methods.source_conditions import (
    condition_exit_status_not,
    condition_status_and,
    condition_status_not,
    condition_status_or,
    literal_command_condition_exit_status,
    literal_command_condition_status,
)


class SourceConditionsTestCase(unittest.TestCase):
    def test_condition_status_primitives_preserve_unknown(self):
        self.assertEqual(condition_status_and("true", "true"), "true")
        self.assertEqual(condition_status_and("true", "unknown"), "unknown")
        self.assertEqual(condition_status_and("unknown", "false"), "false")

        self.assertEqual(condition_status_or("false", "false"), "false")
        self.assertEqual(condition_status_or("false", "unknown"), "unknown")
        self.assertEqual(condition_status_or("unknown", "true"), "true")

        self.assertEqual(condition_status_not("true"), "false")
        self.assertEqual(condition_status_not("false"), "true")
        self.assertEqual(condition_status_not("unknown"), "unknown")

    def test_literal_command_condition_statuses(self):
        self.assertEqual(literal_command_condition_status(":"), "true")
        self.assertEqual(literal_command_condition_status("true"), "true")
        self.assertEqual(literal_command_condition_status("false"), "false")
        self.assertIsNone(literal_command_condition_status("test -f file"))

        self.assertEqual(literal_command_condition_exit_status(":"), 0)
        self.assertEqual(literal_command_condition_exit_status("false"), 1)
        self.assertIsNone(literal_command_condition_exit_status("grep -q x file"))

        self.assertEqual(condition_exit_status_not(0), 1)
        self.assertEqual(condition_exit_status_not(1), 0)


if __name__ == "__main__":
    unittest.main()
