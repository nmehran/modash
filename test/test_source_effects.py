import unittest
from pathlib import Path

from methods.source_effects import (
    Diagnostic,
    DiagnosticSeverity,
    ExecutionModel,
    OccurrenceModel,
    SourceEvent,
    SourceLocation,
    StateSnapshot,
)


class SourceEffectsTestCase(unittest.TestCase):
    def test_source_event_carries_renderer_contract(self):
        location = SourceLocation(Path("main.sh"), 3, 5)
        state = StateSnapshot(
            cwd=Path("/workspace/project"),
            variables={"DEP": "./dep.sh"},
            shell_options=frozenset({"nounset"}),
            bash_source_stack=("main.sh",),
        )

        event = SourceEvent(
            path=Path("/workspace/project/dep.sh"),
            location=location,
            source_expression='"$DEP"',
            source_site='source "$DEP"',
            execution_model=ExecutionModel.PARENT_SOURCE,
            occurrence_model=OccurrenceModel.ONCE,
            state_before=state,
            sync_positionals=True,
        )

        self.assertEqual(event.location, location)
        self.assertEqual(event.state_before.variables["DEP"], "./dep.sh")
        self.assertEqual(event.execution_model, ExecutionModel.PARENT_SOURCE)
        self.assertEqual(event.occurrence_model, OccurrenceModel.ONCE)
        self.assertTrue(event.sync_positionals)

    def test_diagnostic_has_stable_code_and_source_location(self):
        diagnostic = Diagnostic(
            code="unsupported.loop.dynamic-word-list",
            severity=DiagnosticSeverity.ERROR,
            location=SourceLocation(Path("main.sh"), 12, 1),
            fragment='for f in $(cat deps.txt); do',
            message="loop word list is runtime-dynamic",
            hint="Use an exact array or literal word list.",
        )

        self.assertEqual(diagnostic.code, "unsupported.loop.dynamic-word-list")
        self.assertEqual(diagnostic.severity, DiagnosticSeverity.ERROR)
        self.assertEqual(diagnostic.location.line, 12)
        self.assertIn("runtime-dynamic", diagnostic.message)


if __name__ == "__main__":
    unittest.main()
