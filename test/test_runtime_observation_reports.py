import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_evaluator.reports import (  # noqa: E402
    RuntimeObservationReportError,
    build_observation_report,
    write_observation_report,
)
from test.support import ScriptProject  # noqa: E402


class RuntimeObservationReportTestCase(unittest.TestCase):
    def test_reports_unobserved_source_capable_branch_as_warning(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                "\n".join([
                    'if [[ "${MODE:-prod}" == prod ]]; then',
                    "  source ./prod.sh",
                    "else",
                    "  source ./dev.sh",
                    "fi",
                    "",
                ]),
            )
            project.write("prod.sh", "echo prod\n")
            project.write("dev.sh", "echo dev\n")

            observation = project.trace("main.sh", env={"MODE": "prod"}).observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["observed_sources"], 1)
        self.assertEqual(report["summary"]["xtrace_source_commands"], 1)
        self.assertEqual(report["summary"]["trusted_xtrace_links"], 1)
        self.assertEqual(report["summary"]["file_backed_source_sites"], 2)
        self.assertEqual(report["summary"]["unobserved_source_sites"], 1)
        self.assertEqual(report["summary"]["warnings"], 1)
        warning = report["warnings"][0]
        self.assertEqual(warning["code"], "runtime.coverage.unobserved_source_site")
        self.assertEqual(warning["line"], 4)
        self.assertEqual(warning["command"], "source ./dev.sh")
        self.assertEqual(report["unobserved_source_sites"][0]["source_expression"], "./dev.sh")

    def test_does_not_warn_when_multiple_same_line_source_sites_are_all_observed(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./one.sh && source ./two.sh\n")
            project.write("one.sh", "echo one\n")
            project.write("two.sh", "echo two\n")

            observation = project.trace("main.sh").observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["observed_sources"], 2)
        self.assertEqual(report["summary"]["file_backed_source_sites"], 2)
        self.assertEqual(report["summary"]["warnings"], 0)
        self.assertEqual(
            [site["source_expression"] for site in report["observed_source_sites"]],
            ["./one.sh", "./two.sh"],
        )
        self.assertEqual(
            [site["xtrace_command"] for site in report["observed_source_sites"]],
            ["source ./one.sh", "source ./two.sh"],
        )
        self.assertTrue(all(site["source_identity"].startswith("src:") for site in report["observed_source_sites"]))

    def test_does_not_warn_for_observed_command_option_source_forms(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "command -- source ./dep.sh observed\n")
            project.write("dep.sh", "echo dep:$1\n")

            observation = project.trace("main.sh").observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["observed_sources"], 1)
        self.assertEqual(report["summary"]["file_backed_source_sites"], 1)
        self.assertEqual(report["summary"]["warnings"], 0)
        self.assertEqual(report["observed_source_sites"][0]["source_expression"], "./dep.sh observed")
        self.assertEqual(report["observed_source_sites"][0]["xtrace_command"], "command -- source ./dep.sh observed")

    def test_warns_for_unobserved_source_on_partially_covered_same_line(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                '[[ "${LOAD_SECOND:-0}" == 1 ]] && source ./second.sh; source ./first.sh\n',
            )
            project.write("first.sh", "echo first\n")
            project.write("second.sh", "echo second\n")

            observation = project.trace("main.sh", env={"LOAD_SECOND": "0"}).observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["observed_sources"], 1)
        self.assertEqual(report["summary"]["file_backed_source_sites"], 2)
        self.assertEqual(report["summary"]["warnings"], 1)
        self.assertEqual(report["observed_source_sites"][0]["source_expression"], "./first.sh")
        self.assertEqual(report["unobserved_source_sites"][0]["source_expression"], "./second.sh")

    def test_reports_child_bash_c_source_as_process_command_provenance(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "bash -c 'source ./dep.sh child'\n")
            dependency = project.write("dep.sh", 'printf "dep:%s\\n" "$1"\n')

            observation = project.trace("main.sh").observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["observed_sources"], 1)
        self.assertEqual(report["summary"]["file_backed_source_sites"], 0)
        self.assertEqual(report["summary"]["process_command_source_sites"], 1)
        self.assertEqual(report["summary"]["warnings"], 0)
        process_site = report["process_command_source_sites"][0]
        self.assertEqual(process_site["process_command"], "source ./dep.sh child")
        self.assertEqual(process_site["xtrace_index"], 0)
        self.assertEqual(process_site["resolved_path"], str(dependency.resolve(strict=False)))
        self.assertEqual(process_site["arguments"], ["child"])
        self.assertEqual(report["xtrace_source_commands"][0]["command"], "source ./dep.sh child")

    def test_rejects_stale_observation_before_building_report(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            dependency = project.write("dep.sh", "echo dep\n")
            observation = project.trace("main.sh").observation

            dependency.write_text("echo changed\n", encoding="utf-8")

            with self.assertRaises(RuntimeObservationReportError) as context:
                build_observation_report(entrypoint, observation)

        self.assertEqual(context.exception.code, "runtime.report.stale_observation")

    def test_writes_report_json_with_trailing_newline(self):
        with ScriptProject() as project:
            entrypoint = project.write("main.sh", "source ./dep.sh\n")
            project.write("dep.sh", "echo dep\n")
            report = build_observation_report(entrypoint, project.trace("main.sh").observation)
            report_path = project.path("reports/runtime-report.json")

            written = write_observation_report(report, report_path)
            text = written.read_text()
            payload = json.loads(text)

        self.assertEqual(payload["version"], 1)
        self.assertTrue(text.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
