import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_supplements import (  # noqa: E402
    generate_source_supplement,
    write_generated_supplement,
)
from test.support import ScriptProject  # noqa: E402


class RuntimeSupplementReplayTestCase(unittest.TestCase):
    def test_variable_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source "$LIB_DIR/dep.sh"\necho "main:$VALUE"\n',
            )
            project.write("lib/dep.sh", 'VALUE=loaded\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:loaded\nmain:loaded\n")

    def test_helper_observation_replays_through_executable_compile(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_safe "$TARGET" "arg one"\necho "main:$VALUE"\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_safe() {",
                    '  if ! source "$@"; then',
                    "    return 1",
                    "  fi",
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'VALUE="$1"\necho "dep:$VALUE"\n')
            trace = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))})
            supplement = generate_source_supplement(entrypoint, trace.observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "dep:arg one\nmain:arg one\n")


if __name__ == "__main__":
    unittest.main()
