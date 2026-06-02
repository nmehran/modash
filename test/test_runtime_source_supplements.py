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
    SourceCallSite,
    TraceInfo,
    fingerprint_file,
)
from methods.runtime_source_supplements import (  # noqa: E402
    RuntimeSupplementGenerationError,
    generate_source_supplement,
    generate_source_supplement_from_graph,
    load_source_supplement_from_payload,
    write_generated_supplement,
)
from methods.runtime_source_graph import build_observed_source_graph  # noqa: E402
from test.support import ScriptProject  # noqa: E402


class RuntimeSourceSupplementGenerationTestCase(unittest.TestCase):
    def test_generates_variable_from_observed_source_prefix(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {
                "LIB_DIR": "lib",
            },
            "functions": {},
        })

    def test_generates_function_signature_from_observed_helper_source(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                'source ./helpers.sh\nsource_safe "$TARGET" "arg one"\n',
            )
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_safe() {",
                    '  source "$@"',
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'echo "dep:$1"\n')
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {
                "source_safe": [
                    {
                        "arguments": ["dep.sh", "arg one"],
                    },
                ],
            },
        })

    def test_generates_function_signature_from_one_line_helper_definition(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_safe "$TARGET"\n')
            project.write("helpers.sh", 'source_safe() { source "$@"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict()["functions"], {
            "source_safe": [
                {
                    "arguments": ["dep.sh"],
                },
            ],
        })

    def test_generates_function_signature_from_first_positional_alias_and_shifted_variadic_args(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" "arg one" arg-two\n')
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_alias() {",
                    "  local source_path=$1",
                    "  shift",
                    '  source "$source_path" "$@"',
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'echo "dep:$1:$2"\n')
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation
            graph = build_observed_source_graph(entrypoint, observation)

            supplement = generate_source_supplement_from_graph(entrypoint, graph)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {
                "source_alias": [
                    {
                        "arguments": ["dep.sh", "arg one", "arg-two"],
                    },
                ],
            },
        })

    def test_generates_function_signature_from_one_line_first_positional_alias(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" extra\n')
            project.write("helpers.sh", 'source_alias() { local path=$1; shift; source "$path" "$@"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict()["functions"], {
            "source_alias": [
                {
                    "arguments": ["dep.sh", "extra"],
                },
            ],
        })
        self.assertEqual(supplement.to_dict()["variables"], {})

    def test_generates_function_signature_from_first_positional_alias_without_source_args(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET"\n')
            project.write("helpers.sh", 'source_alias() { local path=$1; source "$path"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict()["functions"], {
            "source_alias": [
                {
                    "arguments": ["dep.sh"],
                },
            ],
        })
        self.assertEqual(supplement.to_dict()["variables"], {})

    def test_generates_function_signature_from_first_positional_alias_and_explicit_shift_one(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" extra\n')
            project.write("helpers.sh", 'source_alias() { local path=$1; shift 1; source "$path" "$@"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict()["functions"], {
            "source_alias": [
                {
                    "arguments": ["dep.sh", "extra"],
                },
            ],
        })
        self.assertEqual(supplement.to_dict()["variables"], {})

    def test_does_not_generate_function_or_variable_for_dynamic_shifted_alias(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" extra\n')
            project.write("helpers.sh", 'source_alias() { local path=$1; shift "$COUNT"; source "$path" "$@"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace(
                "main.sh",
                env={
                    "COUNT": "1",
                    "TARGET": str(project.path("dep.sh")),
                },
            ).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {},
        })

    def test_does_not_generate_function_or_variable_for_multi_shifted_alias(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" skipped extra\n')
            project.write("helpers.sh", 'source_alias() { local path=$1; shift 2; source "$path" "$@"; }\n')
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {},
        })

    def test_generates_function_signature_from_first_positional_alias_and_direct_positional_args(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET" one two\n')
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_alias() {",
                    "  local path=$1",
                    '  source "$path" "$2" "$3"',
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", 'echo "dep:$1:$2"\n')
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict()["functions"], {
            "source_alias": [
                {
                    "arguments": ["dep.sh", "one", "two"],
                },
            ],
        })
        self.assertEqual(supplement.to_dict()["variables"], {})

    def test_does_not_generate_variable_or_function_for_local_alias_with_literal_source_args(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source ./helpers.sh\nsource_alias "$TARGET"\n')
            project.write(
                "helpers.sh",
                "\n".join([
                    "source_alias() {",
                    "  local path=$1",
                    '  source "$path" literal',
                    "}",
                    "",
                ]),
            )
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"TARGET": str(project.path("dep.sh"))}).observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {},
        })

    def test_generated_supplement_round_trips_through_existing_loader(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            project.write("lib/dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))}).observation
            supplement = generate_source_supplement(entrypoint, observation)

            loaded = load_source_supplement_from_payload(supplement.to_dict(), entrypoint.parent)
            output = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, output)
            output_text = output.read_text()

        self.assertEqual(loaded.variables, {"LIB_DIR": str((entrypoint.parent / "lib").resolve())})
        self.assertTrue(output_text.endswith("\n"))

    def test_rejects_conflicting_variable_candidates(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            project.write("lib-one/one.sh", "echo one\n")
            project.write("lib-two/two.sh", "echo two\n")
            observation = RuntimeSourceObservation(
                entrypoint=str(entrypoint),
                cwd=str(project.root),
                argv=(),
                bash=BashInfo(version="test"),
                trace=TraceInfo(version="test"),
                environment=EnvironmentInfo(policy="overlay", recorded_keys=("LIB",)),
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
                        source_identity="",
                        process_index=0,
                        call_site=SourceCallSite(
                            file=str(entrypoint),
                            line=1,
                            command='source "$LIB/one.sh"',
                        ),
                        resolved_path=str(project.path("lib-one/one.sh")),
                    ),
                    RuntimeSourceEvent(
                        index=1,
                        source_identity="",
                        process_index=0,
                        call_site=SourceCallSite(
                            file=str(entrypoint),
                            line=1,
                            command='source "$LIB/two.sh"',
                        ),
                        resolved_path=str(project.path("lib-two/two.sh")),
                    ),
                ),
                files=(
                    fingerprint_file(entrypoint, roles=("entrypoint", "call-site")),
                    fingerprint_file(project.path("lib-one/one.sh"), roles=("source",)),
                    fingerprint_file(project.path("lib-two/two.sh"), roles=("source",)),
                ),
            )

            with self.assertRaises(RuntimeSupplementGenerationError) as context:
                generate_source_supplement(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.supplement.variable_conflict")

    def test_rejects_entrypoint_mismatch(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "echo main\n")
            other = project.write("other.sh", "echo other\n")
            observation = project.trace("main.sh").observation

            with self.assertRaises(RuntimeSupplementGenerationError) as context:
                generate_source_supplement(other, observation)

        self.assertEqual(context.exception.code, "runtime.supplement.entrypoint_mismatch")

    def test_rejects_stale_observation_before_generating_supplement(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", 'source "$LIB_DIR/dep.sh"\n')
            dependency = project.write("lib/dep.sh", "echo dep\n")
            observation = project.trace("main.sh", env={"LIB_DIR": str(project.path("lib"))}).observation

            dependency.write_text("echo changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeSupplementGenerationError) as context:
                generate_source_supplement(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.supplement.stale_observation")
        self.assertIn("stale", str(context.exception))

    def test_unsupported_observation_generates_empty_valid_supplement(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            supplement = generate_source_supplement(entrypoint, observation)

        self.assertEqual(supplement.to_dict(), {
            "version": 1,
            "variables": {},
            "functions": {},
        })


if __name__ == "__main__":
    unittest.main()
