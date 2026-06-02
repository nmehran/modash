import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_source_observations import (  # noqa: E402
    BashInfo,
    EnvironmentInfo,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeSourceObservationError,
    SourceCallSite,
    TraceInfo,
    load_observation,
    validate_observation,
    write_observation,
)
from test.support import ScriptProject  # noqa: E402


class RuntimeSourceObservationTestCase(unittest.TestCase):
    def test_round_trip_observation_uses_stable_json_shape(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./dep.sh "arg"\n')
            dependency = project.write("dep.sh", "echo dep\n")
            observation = RuntimeSourceObservation(
                entrypoint=str(entrypoint),
                cwd=str(project.root),
                argv=("--flag",),
                bash=BashInfo(version="5.2.21"),
                trace=TraceInfo(version="schema-test"),
                environment=EnvironmentInfo(
                    policy="allowlist",
                    recorded_keys=("Z_VAR", "A_VAR", "Z_VAR"),
                ),
                sources=(
                    RuntimeSourceEvent(
                        index=0,
                        call_site=SourceCallSite(
                            file=str(entrypoint),
                            line=1,
                            command='source ./dep.sh "arg"',
                        ),
                        resolved_path=str(dependency),
                        arguments=("arg",),
                        status=0,
                    ),
                ),
            )

            path = project.write_observation(".modash/observations/run.json", observation)
            loaded = project.load_observation(".modash/observations/run.json")
            text = path.read_text()
            data = json.loads(text)

        self.assertEqual(loaded, observation)
        self.assertTrue(text.endswith("\n"))
        self.assertTrue(text.startswith('{\n  "version": 1,\n  "entrypoint": '))
        self.assertEqual(data["environment"]["recorded_keys"], ["A_VAR", "Z_VAR"])
        self.assertEqual(data["sources"][0]["arguments"], ["arg"])

    def test_validate_accepts_plain_json_object(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")

            observation = validate_observation({
                "version": 1,
                "entrypoint": str(entrypoint),
                "cwd": str(project.root),
                "argv": [],
                "bash": {"version": "5.2.21"},
                "trace": {"version": "schema-test"},
                "environment": {"policy": "allowlist", "recorded_keys": []},
                "sources": [
                    {
                        "index": 0,
                        "call_site": {
                            "file": str(entrypoint),
                            "line": 1,
                            "command": "source ./dep.sh",
                        },
                        "resolved_path": str(dependency),
                        "arguments": [],
                        "status": 0,
                    }
                ],
            })

        self.assertEqual(observation.entrypoint, str(entrypoint.resolve(strict=False)))
        self.assertEqual(observation.sources[0].resolved_path, str(dependency.resolve(strict=False)))

    def test_invalid_json_fails_with_stable_code(self):
        with ScriptProject() as project:
            path = project.write("bad.json", "{")

            with self.assertRaises(RuntimeSourceObservationError) as context:
                load_observation(path)

        self.assertEqual(context.exception.code, "runtime.observation.invalid_json")

    def test_missing_file_fails_with_stable_code(self):
        with ScriptProject() as project:
            with self.assertRaises(RuntimeSourceObservationError) as context:
                load_observation(project.path("missing.json"))

        self.assertEqual(context.exception.code, "runtime.observation.missing")

    def test_schema_rejects_invalid_documents(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            valid = {
                "version": 1,
                "entrypoint": str(entrypoint),
                "cwd": str(project.root),
                "argv": [],
                "bash": {"version": "5.2.21"},
                "trace": {"version": "schema-test"},
                "environment": {"policy": "allowlist", "recorded_keys": []},
                "sources": [
                    {
                        "index": 0,
                        "call_site": {
                            "file": str(entrypoint),
                            "line": 1,
                            "command": "source ./dep.sh",
                        },
                        "resolved_path": str(dependency),
                        "arguments": [],
                        "status": 0,
                    }
                ],
            }

            cases = [
                ("wrong version", {**valid, "version": 2}, "version must be 1"),
                ("unknown top-level key", {**valid, "extra": True}, "unknown keys"),
                (
                    "missing argv",
                    {key: value for key, value in valid.items() if key != "argv"},
                    "missing required keys",
                ),
                ("relative entrypoint", {**valid, "entrypoint": "main.sh"}, "absolute path"),
                ("bad argument", _replace_source(valid, arguments=[1]), "arguments[] values must be strings"),
                ("bad status", _replace_source(valid, status=-1), "status must be greater than or equal to 0"),
                ("bad event index", _replace_source(valid, index=1), "indexed contiguously"),
                (
                    "bad environment key",
                    {**valid, "environment": {"policy": "allowlist", "recorded_keys": ["BAD=KEY"]}},
                    "must not contain '='",
                ),
            ]

            for name, data, expected_message in cases:
                with self.subTest(name=name):
                    with self.assertRaises(RuntimeSourceObservationError) as context:
                        validate_observation(data)
                    self.assertIn(expected_message, str(context.exception))

    def test_write_observation_validates_plain_json_before_writing(self):
        with ScriptProject() as project:
            with self.assertRaises(RuntimeSourceObservationError):
                write_observation(project.observation_path(), {"version": 1})

            self.assertFalse(project.observation_path().exists())


def _replace_source(observation, **updates):
    copied = json.loads(json.dumps(observation))
    copied["sources"][0].update(updates)
    return copied


if __name__ == "__main__":
    unittest.main()
