import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test.support import ScriptProject


class SetupShellHelperRegressionTestCase(unittest.TestCase):
    def run_helper(self, target):
        return subprocess.run(
            ["bash", str(REPO_ROOT / "setup" / "modash_shell.sh"), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
        )

    def test_modash_shell_executes_script_argument_non_interactively(self):
        with ScriptProject() as project:
            marker = project.path("marker")
            target = project.write("target.sh", f'#!/bin/bash\necho "ran" > "{marker}"\n', executable=True)

            result = self.run_helper(target)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(marker.exists(), result.stdout)
            self.assertEqual(marker.read_text(), "ran\n")

    def test_modash_shell_rejects_disallowed_script_command(self):
        with ScriptProject() as project:
            marker = project.path("marker")
            target = project.write(
                "target.sh",
                f'#!/bin/bash\nuname > "{marker}"\necho "after"\n',
                executable=True,
            )

            result = self.run_helper(target)

            self.assertEqual(result.returncode, 126, result.stdout)
            self.assertIn("Command not allowed: uname", result.stdout)
            self.assertFalse(marker.exists(), result.stdout)

    def test_modash_shell_rejects_disallowed_command_substitution(self):
        with ScriptProject() as project:
            marker = project.path("marker")
            target = project.write(
                "target.sh",
                f'#!/bin/bash\necho "$(uname)"\necho "after" > "{marker}"\n',
                executable=True,
            )

            result = self.run_helper(target)

            self.assertEqual(result.returncode, 126, result.stdout)
            self.assertIn("Command not allowed: uname", result.stdout)
            self.assertFalse(marker.exists(), result.stdout)

    def test_modash_shell_allows_quoted_environment_assignment_prefixes(self):
        with ScriptProject() as project:
            target = project.write(
                "target.sh",
                '#!/bin/bash\nVALUE="a b" printenv VALUE\nitems=(one two)\nitems[0]=zero\necho "${items[0]} ${items[1]}"\n',
                executable=True,
            )

            result = self.run_helper(target)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "a b\nzero two\n")

    def test_modash_shell_allows_control_flow_keywords(self):
        with ScriptProject() as project:
            target = project.write(
                "target.sh",
                '#!/bin/bash\nfor value in one two; do echo "$value"; done\ncase two in two) echo "case";; esac\n',
                executable=True,
            )

            result = self.run_helper(target)

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stdout, "one\ntwo\ncase\n")

    def test_modash_shell_rejects_command_spawning_helper_tools(self):
        cases = {
            "env": "env /usr/bin/uname\n",
            "find": "find . -exec /usr/bin/uname \\;\n",
            "awk": "awk 'BEGIN { system(\"/usr/bin/uname\") }'\n",
            "xargs": 'printf "%s\\n" /usr/bin/uname | xargs\n',
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                target = project.write("target.sh", f"#!/bin/bash\n{content}echo after\n", executable=True)

                result = self.run_helper(target)

                self.assertEqual(result.returncode, 126, result.stdout)
                self.assertIn(f"Command not allowed: {name}", result.stdout)
                self.assertNotIn("after", result.stdout)

    def test_runner_invokes_modash_shell_instead_of_bash(self):
        runner = (REPO_ROOT / "setup" / "run_modash_shell.sh").read_text()

        self.assertIn('sudo -u modash "$MODASH_SHELL_PATH" "$SCRIPT_PATH"', runner)
        self.assertNotIn('sudo -u modash /bin/bash "$SCRIPT_PATH"', runner)


if __name__ == "__main__":
    unittest.main()
