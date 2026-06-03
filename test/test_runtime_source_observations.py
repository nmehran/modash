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
    RuntimeProcess,
    RuntimeSourceEvent,
    RuntimeSourceObservation,
    RuntimeXtraceSourceCommand,
    RuntimeSourceObservationError,
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
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
                processes=(
                    RuntimeProcess(
                        index=0,
                        pid=100,
                        parent_pid=50,
                        parent_index=None,
                        entrypoint=str(entrypoint),
                        cwd=str(project.root),
                        argv=("--flag",),
                        command=str(entrypoint),
                    ),
                ),
                sources=(
                    RuntimeSourceEvent(
                        index=0,
                        source_identity="",
                        process_index=0,
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
                files=(
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")),
                    fingerprint_file(dependency, roles=("source",)),
                ),
            )

            path = project.write_observation(".modash/observations/run.json", observation)
            loaded = project.load_observation(".modash/observations/run.json")
            text = path.read_text()
            data = json.loads(text)

        self.assertEqual(loaded, observation)
        self.assertTrue(text.endswith("\n"))
        self.assertTrue(text.startswith('{\n  "version": 8,\n  "entrypoint": '))
        self.assertEqual(data["environment"]["recorded_keys"], ["A_VAR", "Z_VAR"])
        self.assertEqual(data["run"]["shell"], "bash")
        self.assertEqual(data["run"]["target_status"], 0)
        self.assertIsNone(data["run"]["timeout_seconds"])
        self.assertEqual(data["processes"][0]["pid"], 100)
        self.assertIsNone(data["processes"][0]["parent_index"])
        self.assertEqual(data["sources"][0]["process_index"], 0)
        self.assertEqual(data["sources"][0]["arguments"], ["arg"])
        self.assertEqual(
            {tuple(file["roles"]) for file in data["files"]},
            {("entrypoint", "call-site"), ("source",)},
        )
        self.assertTrue(all(len(file["sha256"]) == 64 for file in data["files"]))

    def test_validate_accepts_plain_json_object(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")

            observation = validate_observation({
                "version": 8,
                "entrypoint": str(entrypoint),
                "cwd": str(project.root),
                "argv": [],
                "bash": {"version": "5.2.21"},
                "trace": {"version": "schema-test"},
                "environment": {"policy": "allowlist", "recorded_keys": []},
                "run": _run_info_payload(),
                "processes": [
                    {
                        "index": 0,
                        "pid": 100,
                        "parent_index": None,
                        "parent_pid": 50,
                        "entrypoint": str(entrypoint),
                        "cwd": str(project.root),
                        "argv": [],
                        "command": str(entrypoint),
                    }
                ],
                "sources": [
                    {
                        "index": 0,
                        "source_identity": "",
                        "process_index": 0,
                        "xtrace_index": None,
                        "call_site": {
                            "file": str(entrypoint),
                            "line": 1,
                            "command": "source ./dep.sh",
                        },
                        "function_stack": [],
                        "function_call": None,
                        "resolved_path": str(dependency),
                        "arguments": [],
                        "status": 0,
                    }
                ],
                "xtrace": [],
                "files": [
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")).to_dict(),
                    fingerprint_file(dependency, roles=("source",)).to_dict(),
                ],
            })

        self.assertEqual(observation.entrypoint, str(entrypoint.resolve(strict=False)))
        self.assertEqual(observation.sources[0].resolved_path, str(dependency.resolve(strict=False)))

    def test_validate_allows_process_command_call_site_without_call_site_fingerprint(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "bash -c 'source ./dep.sh'\n")
            project.write("dep.sh", "echo dep\n")

            observation = project.trace("main.sh").observation
            event = observation.sources[0]
            fingerprint_paths = {fingerprint.path for fingerprint in observation.files}
            loaded = validate_observation(observation.to_dict())

        self.assertNotIn(event.call_site.file, fingerprint_paths)
        self.assertEqual(loaded, observation)

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
                "version": 8,
                "entrypoint": str(entrypoint),
                "cwd": str(project.root),
                "argv": [],
                "bash": {"version": "5.2.21"},
                "trace": {"version": "schema-test"},
                "environment": {"policy": "allowlist", "recorded_keys": []},
                "run": _run_info_payload(),
                "processes": [
                    {
                        "index": 0,
                        "pid": 100,
                        "parent_index": None,
                        "parent_pid": 50,
                        "entrypoint": str(entrypoint),
                        "cwd": str(project.root),
                        "argv": [],
                        "command": str(entrypoint),
                    }
                ],
                "sources": [
                    {
                        "index": 0,
                        "source_identity": "",
                        "process_index": 0,
                        "xtrace_index": None,
                        "call_site": {
                            "file": str(entrypoint),
                            "line": 1,
                            "command": "source ./dep.sh",
                        },
                        "function_stack": [],
                        "function_call": None,
                        "resolved_path": str(dependency),
                        "arguments": [],
                        "status": 0,
                    }
                ],
                "xtrace": [],
                "files": [
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")).to_dict(),
                    fingerprint_file(dependency, roles=("source",)).to_dict(),
                ],
            }

            cases = [
                ("wrong version", {**valid, "version": 1}, "version must be 8"),
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
                    "bad process index",
                    {**valid, "processes": [{**valid["processes"][0], "index": 1}]},
                    "processes must be indexed contiguously",
                ),
                (
                    "bad process parent",
                    {**valid, "processes": [{**valid["processes"][0], "parent_index": 99}]},
                    "parent_index must reference an existing process",
                ),
                (
                    "bad source process index",
                    _replace_source(valid, process_index=99),
                    "process_index must reference an existing process",
                ),
                (
                    "bad environment key",
                    {**valid, "environment": {"policy": "allowlist", "recorded_keys": ["BAD=KEY"]}},
                    "must not contain '='",
                ),
                (
                    "bad file role",
                    _replace_file(valid, roles=["entrypoint", "unknown"]),
                    "unsupported role",
                ),
                (
                    "bad file digest",
                    _replace_file(valid, sha256="not-a-digest"),
                    "SHA-256",
                ),
                (
                    "uppercase file digest",
                    _replace_file(valid, sha256=valid["files"][0]["sha256"].upper()),
                    "lowercase",
                ),
                (
                    "duplicate file path",
                    {**valid, "files": [valid["files"][0], valid["files"][0]]},
                    "path values must be unique",
                ),
                (
                    "missing entrypoint fingerprint",
                    {**valid, "files": [valid["files"][1]]},
                    "entrypoint must have a file fingerprint",
                ),
                (
                    "missing call-site fingerprint role",
                    _replace_file(valid, roles=["entrypoint"]),
                    "call_site.file must have a file fingerprint",
                ),
                (
                    "missing source fingerprint",
                    {**valid, "files": [valid["files"][0]]},
                    "resolved_path must have a file fingerprint",
                ),
                (
                    "missing source fingerprint role",
                    _replace_file(valid, index=1, roles=["call-site"]),
                    "resolved_path must have a file fingerprint",
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
                write_observation(project.observation_path(), {"version": 8})

            self.assertFalse(project.observation_path().exists())

    def test_schema_links_xtrace_source_provenance_to_source_events(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = RuntimeSourceObservation(
                entrypoint=str(entrypoint),
                cwd=str(project.root),
                argv=(),
                bash=BashInfo(version="test"),
                trace=TraceInfo(version="test"),
                environment=EnvironmentInfo(policy="inherit", recorded_keys=()),
                processes=(
                    RuntimeProcess(
                        index=0,
                        pid=100,
                        parent_pid=50,
                        parent_index=None,
                        entrypoint=str(entrypoint),
                        cwd=str(project.root),
                        argv=(),
                        command=str(entrypoint),
                    ),
                ),
                sources=(
                    RuntimeSourceEvent(
                        index=0,
                        source_identity="src:test",
                        process_index=0,
                        xtrace_index=0,
                        call_site=SourceCallSite(
                            file=str(entrypoint),
                            line=1,
                            command="source ./dep.sh",
                        ),
                        resolved_path=str(dependency),
                        status=0,
                    ),
                ),
                xtrace=(
                    RuntimeXtraceSourceCommand(
                        index=0,
                        source_identity="src:test",
                        process_index=0,
                        file=str(entrypoint),
                        line=1,
                        function="",
                        cwd=str(project.root),
                        command="source ./dep.sh",
                    ),
                ),
                files=(
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")),
                    fingerprint_file(dependency, roles=("source",)),
                ),
            )
            payload = observation.to_dict()
            loaded = validate_observation(payload)

        self.assertEqual(loaded.sources[0].xtrace_index, 0)
        self.assertEqual(loaded.xtrace[0].command, "source ./dep.sh")

    def test_schema_rejects_untrusted_xtrace_links(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            valid = {
                "version": 8,
                "entrypoint": str(entrypoint),
                "cwd": str(project.root),
                "argv": [],
                "bash": {"version": "test"},
                "trace": {"version": "test"},
                "environment": {"policy": "inherit", "recorded_keys": []},
                "run": _run_info_payload(),
                "processes": [
                    {
                        "index": 0,
                        "pid": 100,
                        "parent_index": None,
                        "parent_pid": 50,
                        "entrypoint": str(entrypoint),
                        "cwd": str(project.root),
                        "argv": [],
                        "command": str(entrypoint),
                    },
                ],
                "sources": [
                    {
                        "index": 0,
                        "source_identity": "src:test",
                        "process_index": 0,
                        "xtrace_index": 0,
                        "call_site": {
                            "file": str(entrypoint),
                            "line": 1,
                            "command": "source ./dep.sh",
                        },
                        "function_stack": [],
                        "function_call": None,
                        "resolved_path": str(dependency),
                        "arguments": [],
                        "status": 0,
                    },
                ],
                "xtrace": [
                    {
                        "index": 0,
                        "source_identity": "src:test",
                        "process_index": 0,
                        "file": str(entrypoint),
                        "line": 1,
                        "function": "",
                        "cwd": str(project.root),
                        "command": "source ./dep.sh",
                    },
                ],
                "files": [
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")).to_dict(),
                    fingerprint_file(dependency, roles=("source",)).to_dict(),
                ],
            }

            cases = [
                (
                    "source missing xtrace index",
                    _replace_source(valid, xtrace_index=None),
                    "xtrace_index is required",
                ),
                (
                    "source bad xtrace index",
                    _replace_source(valid, xtrace_index=1),
                    "reference an existing xtrace",
                ),
                (
                    "xtrace bad process",
                    _replace_xtrace(valid, process_index=1),
                    "process_index must reference an existing process",
                ),
                (
                    "xtrace identity mismatch",
                    _replace_xtrace(valid, source_identity="src:other"),
                    "source_identity must match",
                ),
                (
                    "xtrace unreferenced",
                    {**valid, "sources": [{**valid["sources"][0], "xtrace_index": None}]},
                    "xtrace_index is required",
                ),
            ]

            for name, data, expected_message in cases:
                with self.subTest(name=name):
                    with self.assertRaises(RuntimeSourceObservationError) as context:
                        validate_observation(data)
                    self.assertIn(expected_message, str(context.exception))


def _replace_source(observation, **updates):
    copied = json.loads(json.dumps(observation))
    copied["sources"][0].update(updates)
    return copied


def _run_info_payload(**updates):
    payload = {
        "observed_at_utc": "2026-06-02T00:00:00Z",
        "modash_version": "test",
        "platform": "test-platform",
        "python_version": "3.14.0",
        "shell": "/bin/bash",
        "target_status": 0,
        "timeout_seconds": 30,
    }
    payload.update(updates)
    return payload


def _replace_file(observation, index=0, **updates):
    copied = json.loads(json.dumps(observation))
    copied["files"][index].update(updates)
    return copied


def _replace_xtrace(observation, index=0, **updates):
    copied = json.loads(json.dumps(observation))
    copied["xtrace"][index].update(updates)
    return copied


if __name__ == "__main__":
    unittest.main()
