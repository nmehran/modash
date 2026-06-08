import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Path to `modash.py`
COMPILE_SCRIPT = str(REPO_ROOT / "modash.py")

ENTRY_POINT = str(TEST_DIR / "sample_dir" / "script_main.sh")


class TestCompile(unittest.TestCase):
    def test_compile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "merged_script.sh"
            compile_command = [sys.executable, COMPILE_SCRIPT, ENTRY_POINT, str(output_file), '--mode', 'executable']
            compile_result = subprocess.run(compile_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            self.assertEqual(first=compile_result.returncode,
                             second=0,
                             msg=f"Error compiling output to '{output_file}' using `modash.py`\n"
                                 f"Error: {compile_result.stdout}")
            self.assertTrue(output_file.exists(), "Output file was not created")
            self.assertTrue(os.access(output_file, os.X_OK), "Executable output was not marked executable")

            execution_command = [str(output_file)]
            execution_result = subprocess.run(execution_command,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                              text=True)
            self.assertEqual(execution_result.returncode, 0, execution_result.stdout)

        expected_output = (
            "This is the last dependency: script6.sh in dir1\n"
            "This directory contains the compiled outputs used by the `modash` test suite.\n"
            "This is the last dependency: script6.sh in dir1\n"
            "This directory contains the compiled outputs used by the `modash` test suite.\n"
            "This is script5.sh in the root directory\n"
            "This is script4.sh in the root directory\n"
            "This is script3.sh in 'dir with spaces'\n"
            "This is script2.sh in dir2\n"
            "This is script1.sh in dir1\n"
            "This is the main script\n"
        )

        self.assertEqual(execution_result.stdout, expected_output,
                         "The execution output did not match the expected result")

    def test_executable_output_respects_umask(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            entrypoint = work / "main.sh"
            output_file = work / "compiled.sh"
            entrypoint.write_text("printf 'ok\\n'\n", encoding="utf-8")

            old_umask = os.umask(0o077)
            try:
                compile_result = subprocess.run(
                    [sys.executable, COMPILE_SCRIPT, str(entrypoint), str(output_file), '--mode', 'executable'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            finally:
                os.umask(old_umask)

            self.assertEqual(compile_result.returncode, 0, compile_result.stdout)
            self.assertEqual(output_file.stat().st_mode & 0o777, 0o700)
            execution_result = subprocess.run(
                [str(output_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(execution_result.returncode, 0, execution_result.stdout)
            self.assertEqual(execution_result.stdout, "ok\n")


if __name__ == '__main__':
    unittest.main()
