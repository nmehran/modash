import contextlib
import io
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_evaluator.scanners import functions_main, main, positionals_main  # noqa: E402


class RuntimeSourceScannersTestCase(unittest.TestCase):
    def test_positionals_scanner_reports_top_level_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dep.sh"
            path.write_text("shift\n", encoding="utf-8")

            self.assertEqual(positionals_main([str(path)]), 0)

            path.write_text("echo safe\n", encoding="utf-8")
            self.assertEqual(positionals_main([str(path)]), 1)

    def test_function_scanner_marks_live_dead_and_ambiguous_definitions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lib.sh"
            path.write_text(textwrap.dedent("""\
                live() { :; }
                if false; then
                  dead() { :; }
                fi
                if "$FLAG"; then
                  maybe() { :; }
                fi
                eval "$DYNAMIC"
                """), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = functions_main([str(path)])

        records = set(output.getvalue().splitlines())
        self.assertEqual(status, 0)
        self.assertIn("live\tlive\t1", records)
        self.assertIn("unknown\tmaybe\t6", records)
        self.assertIn("unknown\t*\t8", records)
        self.assertNotIn("unknown\tdead\t3", records)

    def test_scanner_entrypoint_dispatches_by_name(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main([]), 2)
            self.assertEqual(main(["unknown"]), 2)
        self.assertIn("scanner name", stderr.getvalue())
        self.assertIn("unknown", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
