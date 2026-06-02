import subprocess
import textwrap
import unittest

from methods.source_effects import ExecutionModel, OccurrenceModel
from methods.source_evaluator import SourceEvaluator
from test.support import ScriptProject


class SourceEvaluatorTestCase(unittest.TestCase):
    @staticmethod
    def _find_words(project, root, name):
        completed = subprocess.run(
            ["find", root, "-type", "f", "-name", name, "-print"],
            cwd=project.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        return completed.stdout.splitlines()

    @staticmethod
    def _paths_for_words(project, words):
        return [project.path(word).resolve() for word in words]

    def test_static_source_event_includes_state_before_source(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep:$FOO"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                FOO=bar
                source ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.path, dep)
        self.assertEqual(event.execution_model, ExecutionModel.PARENT_SOURCE)
        self.assertEqual(event.state_before.variables["FOO"], "bar")
        self.assertEqual(event.state_before.cwd, entry.parent)
        self.assertEqual(event.state_before.bash_source_stack, (entry,))

    def test_cd_updates_state_for_relative_source_resolution(self):
        with ScriptProject() as project:
            dep = project.write("subdir/dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                cd subdir
                source ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.path, dep)
        self.assertEqual(event.state_before.cwd, dep.parent)

    def test_set_options_are_captured_before_source(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                set -eu
                set +e
                set -o pipefail
                source ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertTrue({"nounset", "pipefail"} <= result.events[0].state_before.shell_options)
        self.assertNotIn("errexit", result.events[0].state_before.shell_options)

    def test_exact_array_index_source_is_resolved(self):
        with ScriptProject() as project:
            dep = project.write("deps/feature.sh", 'echo "feature"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                deps=(./base.sh ./deps/feature.sh)
                source "${deps[1]}"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.path, dep)
        self.assertEqual(event.source_expression, '"${deps[1]}"')
        self.assertEqual(event.state_before.arrays["deps"], ("./base.sh", "./deps/feature.sh"))

    def test_duplicate_source_events_are_marked_repeated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", "source ./dep.sh\nsource ./dep.sh\n")

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep, dep])
        self.assertEqual({event.occurrence_model for event in result.events}, {OccurrenceModel.REPEATED})

    def test_command_level_eval_source_is_resolved(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", 'COMMAND="source ./dep.sh"\neval "$COMMAND"\n')

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].path, dep)
        self.assertEqual(result.events[0].source_site, 'eval "$COMMAND"')

    def test_function_call_resolves_positional_source_argument(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }
                load_dep ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].source_expression, '"$1"')
        self.assertEqual(result.events[0].source_value, "./dep.sh")
        self.assertEqual(result.events[0].location.line, 2)

    def test_function_call_mutates_parent_state(self):
        with ScriptProject() as project:
            dep = project.write("deps/dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                select_dep() {
                  cd deps
                  DEP=./dep.sh
                }
                select_dep
                source "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].state_before.cwd, dep.parent)

    def test_function_local_assignment_does_not_leak(self):
        with ScriptProject() as project:
            dep = project.write("inside.sh", 'echo "inside"\n')
            outside = project.write("outside.sh", 'echo "outside"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEP=./outside.sh
                load_dep() {
                  local DEP=./inside.sh
                  source "$DEP"
                }
                load_dep
                source "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep, outside])

    def test_function_assignment_prefix_is_temporary(self):
        with ScriptProject() as project:
            dep = project.write("inside.sh", 'echo "inside"\n')
            outside = project.write("outside.sh", 'echo "outside"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEP=./outside.sh
                load_dep() {
                  source "$DEP"
                }
                DEP=./inside.sh load_dep
                source "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep, outside])

    def test_function_arguments_expand_before_assignment_prefixes(self):
        with ScriptProject() as project:
            outer = project.write("outer.sh", 'echo "outer"\n')
            inner = project.write("inner.sh", 'echo "inner"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEP=./outer.sh
                load_dep() {
                  source "$1"
                  source "$DEP"
                }
                DEP=./inner.sh load_dep "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [outer, inner])

    def test_same_line_function_definition_tail_is_evaluated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", 'load_dep() { source ./dep.sh; }; load_dep\n')

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_dynamic_function_dispatch_is_evaluated_when_exact(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source "$1"
                }
                FN=load_dep
                "$FN" ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_function_shift_updates_positional_source_arguments(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  shift
                  source "$1"
                }
                load_dep ignored ./dep.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].source_value, "./dep.sh")

    def test_function_return_disables_later_sources(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  return 0
                  source ./missing.sh
                }
                load_dep
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(result.events, ())
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./missing.sh"])
        self.assertEqual(result.disabled_sources[0].condition, "return")

    def test_nested_function_control_flow_is_evaluated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [[ -f ./dep.sh ]]; then
                    source ./dep.sh
                  fi
                }
                load_dep
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_branch_dependent_function_return_raises_structured_diagnostic(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [[ -n "$SKIP" ]]; then
                    return 0
                  fi
                  source ./dep.sh
                }
                load_dep
                """))

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent function return") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-control")
        self.assertEqual(cm.exception.diagnostic.location.line, 2)

    def test_function_return_guarded_by_exact_positional_count_allows_reachable_source(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [ "$#" -eq 0 ]; then
                    return 0
                  fi
                  source ./dep.sh
                }
                load_dep arg
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_function_return_guarded_by_exact_positional_count_skips_source(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [ "$#" -eq 0 ]; then
                    return 0
                  fi
                  source ./dep.sh
                }
                load_dep
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(result.events, ())

    def test_branch_dependent_function_return_status_raises_structured_diagnostic(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  if [[ -n "$SKIP" ]]; then
                    return 0
                  else
                    return 1
                  fi
                }
                load_dep
                """))

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent function return") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-control")
        self.assertEqual(cm.exception.diagnostic.location.line, 2)

    def test_function_return_status_controls_chained_sources(self):
        with ScriptProject() as project:
            fallback = project.write("fallback.sh", 'echo "fallback"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  return 1
                }
                load_dep && source ./skipped.sh
                load_dep || source ./fallback.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [fallback])
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./skipped.sh"])
        self.assertEqual(result.disabled_sources[0].condition, "&& previous command status")

    def test_function_shift_status_controls_chained_sources(self):
        with ScriptProject() as project:
            fallback = project.write("fallback.sh", 'echo "fallback"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shift_too_far() {
                  shift 9
                }
                shift_too_far ignored && source ./skipped.sh
                shift_too_far ignored || source ./fallback.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [fallback])
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./skipped.sh"])

    def test_function_implicit_builtin_status_controls_chained_sources(self):
        with ScriptProject() as project:
            after = project.write("after.sh", 'echo "after"\n')
            fallback = project.write("fallback.sh", 'echo "fallback"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_ok() {
                  true
                }
                load_fail() {
                  false
                }
                load_colon() {
                  :
                }
                load_ok && source ./after.sh
                load_ok || source ./skipped-ok.sh
                load_fail && source ./skipped-fail.sh
                load_fail || source ./fallback.sh
                load_colon || source ./skipped-colon.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [after, fallback])
        self.assertEqual(
            [disabled.source_site for disabled in result.disabled_sources],
            ["source ./skipped-ok.sh", "source ./skipped-fail.sh", "source ./skipped-colon.sh"],
        )

    def test_branch_dependent_equivalent_function_definitions_are_evaluated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_ALT" ]]; then
                  load_dep() { source ./dep.sh; }
                else
                  load_dep() { source ./dep.sh; }
                fi
                load_dep
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep, dep])
        self.assertEqual({event.occurrence_model for event in result.events}, {OccurrenceModel.MUTUALLY_EXCLUSIVE})

    def test_branch_merge_preserves_existing_function_without_duplicate_variant(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() { source ./dep.sh; }
                if [[ -n "$NOISE" ]]; then
                  :
                else
                  :
                fi
                load_dep
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.ONCE)

    def test_branch_dependent_different_function_definitions_raise_structured_diagnostic(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_A" ]]; then
                  load_dep() { source ./a.sh; }
                else
                  load_dep() { source ./b.sh; }
                fi
                load_dep
                """))

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent function") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-dispatch")
        self.assertEqual(cm.exception.diagnostic.location.line, 6)

    def test_unresolved_dynamic_function_dispatch_rejects_source_relevant_functions(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  source ./dep.sh
                }
                "$FN"
                """))

            with self.assertRaisesRegex(NotImplementedError, "dynamic function dispatch") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-dispatch")
        self.assertEqual(cm.exception.diagnostic.location.line, 4)

    def test_recursive_function_source_raises_structured_diagnostic(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                load_dep() {
                  load_dep
                  source ./dep.sh
                }
                load_dep
                """))

            with self.assertRaisesRegex(NotImplementedError, "recursive function") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.function-recursion")
        self.assertEqual(cm.exception.diagnostic.location.line, 2)

    def test_unknown_array_source_raises_structured_diagnostic(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", 'source "${deps[0]}"\n')

            with self.assertRaisesRegex(NotImplementedError, "array source") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.array-index")
        self.assertEqual(cm.exception.diagnostic.location.line, 1)
        self.assertEqual(cm.exception.diagnostic.fragment, 'source "${deps[0]}"')

    def test_if_block_source_is_evaluated_as_conditional(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -f ./dep.sh ]]; then
                  source ./dep.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.CONDITIONAL)
        self.assertEqual(result.events[0].condition, "[[ -f ./dep.sh ]]")

    def test_if_block_unreachable_sources_are_disabled(self):
        with ScriptProject() as project:
            prod = project.write("prod.sh", 'echo "prod"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                MODE=prod
                if [[ "$MODE" == prod ]]; then
                  source ./prod.sh
                else
                  source ./missing.sh
                fi
                if [[ -f ./missing-optional.sh ]]; then
                  source ./missing-optional.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [prod])
        self.assertEqual(
            [disabled.source_site for disabled in result.disabled_sources],
            ["source ./missing.sh", "source ./missing-optional.sh"],
        )

    def test_if_block_compound_condition_is_evaluated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            mismatch = project.write("mismatch.sh", 'echo "mismatch"\n')
            variable_pattern = project.write("variable-pattern.sh", 'echo "variable pattern"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                LOAD_DEP=1
                MODE=prod-eu
                PATTERN=prod*
                if [[ -f ./dep.sh && -n "$LOAD_DEP" ]]; then
                  source ./dep.sh
                fi
                if [[ -f ./missing.sh || "$LOAD_DEP" == 1 ]]; then
                  source ./dep.sh
                fi
                if [[ ! -f ./missing.sh ]]; then
                  source ./dep.sh
                fi
                if [[ "$MODE" != prod* ]]; then
                  source ./mismatch.sh
                fi
                if [[ "$MODE" != dev* ]]; then
                  source ./mismatch.sh
                fi
                if [[ "$MODE" == $PATTERN ]]; then
                  source ./variable-pattern.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep, dep, dep, mismatch, variable_pattern])
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./mismatch.sh"])

    def test_if_block_arithmetic_regex_and_grep_conditions_are_evaluated(self):
        with ScriptProject() as project:
            arithmetic = project.write("arithmetic.sh", 'echo "arithmetic"\n')
            numeric = project.write("numeric.sh", 'echo "numeric"\n')
            regex = project.write("regex.sh", 'echo "regex"\n')
            pattern = project.write("pattern.sh", 'echo "pattern"\n')
            grep_dep = project.write("grep.sh", 'echo "grep"\n')
            grep_regex = project.write("grep-regex.sh", 'echo "grep regex"\n')
            project.write("config", "enabled=true\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                COUNT=2
                MODE=prod-eu
                if (( COUNT > 1 )); then
                  source ./arithmetic.sh
                fi
                if [[ "$COUNT" -gt 1 ]]; then
                  source ./numeric.sh
                fi
                if [[ "$MODE" =~ ^prod ]]; then
                  source ./regex.sh
                fi
                if [[ "$MODE" == prod* ]]; then
                  source ./pattern.sh
                fi
                if grep -q enabled config; then
                  source ./grep.sh
                fi
                if grep -Eq '^enabled=true$' config; then
                  source ./grep-regex.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(
            [event.path for event in result.events],
            [arithmetic, numeric, regex, pattern, grep_dep, grep_regex],
        )

    def test_bitwise_arithmetic_assignments_and_conditions_are_evaluated(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "bitwise"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                flag_file=$(( 1 << 0 ))
                flag_console=$(( 1 << 2 ))
                mask=$(( flag_file | flag_console ))
                mask=$(( mask & ~2 ))
                if (( mask == 5 )); then
                  source ./dep.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_if_block_unsupported_condition_lowers_exact_branch_source(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if awk 'BEGIN { exit 0 }'; then
                  source ./dep.sh
                fi
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.CONDITIONAL)
        self.assertEqual(result.events[0].condition, "awk 'BEGIN { exit 0 }'")

    def test_case_block_exact_subject_selects_matching_arm(self):
        with ScriptProject() as project:
            prod = project.write("prod.sh", 'echo "prod"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) source ./prod.sh ;;
                  dev) source ./missing-dev.sh ;;
                esac
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [prod])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.MUTUALLY_EXCLUSIVE)
        self.assertEqual(result.events[0].condition, 'case "$ENV" in prod')
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./missing-dev.sh"])

    def test_case_block_default_arm_is_selected_when_no_pattern_matches(self):
        with ScriptProject() as project:
            default = project.write("default.sh", 'echo "default"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                ENV=qa
                case "$ENV" in
                  prod) source ./missing-prod.sh ;;
                  *) source ./default.sh ;;
                esac
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [default])
        self.assertEqual(result.events[0].condition, 'case "$ENV" in *')
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ["source ./missing-prod.sh"])

    def test_case_block_converged_arm_state_is_available_after_merge(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) DEP=./dep.sh ;;
                  dev) DEP=./missing.sh ;;
                esac
                source "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertIsNone(result.events[0].condition)

    def test_case_block_unknown_subject_lowers_exact_arm_sources(self):
        with ScriptProject() as project:
            prod = project.write("prod.sh", 'echo "prod"\n')
            dev = project.write("dev.sh", 'echo "dev"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                case "$ENV" in
                  prod) source ./prod.sh ;;
                  dev) source ./dev.sh ;;
                esac
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [prod, dev])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.MUTUALLY_EXCLUSIVE)
        self.assertEqual(result.events[0].condition, 'case "$ENV" in prod')
        self.assertEqual(result.events[1].condition, 'case "$ENV" in dev')

    def test_case_block_unknown_subject_rejects_state_expanded_eval_source(self):
        with ScriptProject() as project:
            project.write("prod.sh", 'echo "prod"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                case "$ENV" in
                  prod) COMMAND="source ./prod.sh"; eval "$COMMAND" ;;
                esac
                """))

            with self.assertRaisesRegex(NotImplementedError, "unresolved source command") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.command-unresolved")
        self.assertEqual(cm.exception.diagnostic.location.line, 2)

    def test_case_block_unknown_subject_rejects_positional_eval_payload(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                case "$ENV" in
                  prod) eval "$1" ;;
                esac
                """))

            with self.assertRaisesRegex(NotImplementedError, "unresolved source command") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.command-unresolved")
        self.assertEqual(cm.exception.diagnostic.location.line, 2)

    def test_case_block_unknown_subject_return_restores_outer_source_context(self):
        with ScriptProject() as project:
            lib = project.write("lib.sh", textwrap.dedent("""\
                case "$MODE" in
                  *) return 0 ;;
                esac
                """))
            after = project.write("after.sh", 'echo "after"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                source ./lib.sh
                source ./after.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [lib, after])
        self.assertEqual(result.events[0].condition, None)
        self.assertEqual(result.events[1].condition, None)
        self.assertEqual(result.events[1].occurrence_model, OccurrenceModel.ONCE)

    def test_case_block_practical_patterns_are_evaluated(self):
        with ScriptProject() as project:
            escaped = project.write("escaped.sh", 'echo "escaped"\n')
            mixed = project.write("mixed.sh", 'echo "mixed"\n')
            digit = project.write("digit.sh", 'echo "digit"\n')
            variable = project.write("variable.sh", 'echo "variable"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                ENV=prod-us
                PATTERN='prod-*'
                case "$ENV" in
                  prod\\*) source ./escaped.sh ;;
                  prod"-"eu) source ./mixed.sh ;;
                  [[:digit:]]) source ./digit.sh ;;
                  $PATTERN) source ./variable.sh ;;
                esac
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [variable])
        self.assertEqual(
            [disabled.source_site for disabled in result.disabled_sources],
            ["source ./escaped.sh", "source ./mixed.sh", "source ./digit.sh"],
        )

    def test_case_block_fallthrough_terminators_are_evaluated(self):
        with ScriptProject() as project:
            prod = project.write("prod.sh", 'echo "prod"\n')
            default = project.write("default.sh", 'echo "default"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                ENV=prod
                case "$ENV" in
                  prod) source ./prod.sh ;&
                  *) source ./default.sh ;;
                esac
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [prod, default])
        self.assertEqual([event.condition for event in result.events], ['case "$ENV" in prod', 'case "$ENV" in *'])

    def test_context_control_flow_source_is_conditional_and_does_not_leak_state(self):
        with ScriptProject() as project:
            optional = project.write("optional.sh", 'NEXT=./next.sh\nsource ./nested.sh\n')
            nested = project.write("nested.sh", 'echo "nested"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$LOAD_OPTIONAL" ]]; then
                  source ./optional.sh
                fi
                source "$NEXT"
                """))

            result = SourceEvaluator(mode="context").evaluate(entry)

        self.assertEqual([event.path for event in result.events], [optional, nested])
        self.assertEqual([event.occurrence_model for event in result.events], [
            OccurrenceModel.CONDITIONAL,
            OccurrenceModel.CONDITIONAL,
        ])
        self.assertEqual([event.condition for event in result.events], ['[[ -n "$LOAD_OPTIONAL" ]]', '[[ -n "$LOAD_OPTIONAL" ]]'])
        self.assertNotIn("NEXT", result.final_state.variables)

    def test_if_block_converged_branch_state_is_available_after_merge(self):
        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dep"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_A" ]]; then
                  DEP=./dep.sh
                else
                  DEP=./dep.sh
                fi
                source "$DEP"
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.events[0].occurrence_model, OccurrenceModel.ONCE)
        self.assertIsNone(result.events[0].condition)

    def test_if_block_divergent_branch_state_rejects_later_source(self):
        with ScriptProject() as project:
            project.write("a.sh", 'echo "a"\n')
            project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_A" ]]; then
                  DEP=./a.sh
                else
                  DEP=./b.sh
                fi
                source "$DEP"
                """))

            with self.assertRaisesRegex(NotImplementedError, "branch-dependent variable") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.branch-state")
        self.assertEqual(cm.exception.diagnostic.location.line, 6)

    def test_if_block_divergent_cwd_rejects_later_relative_cd(self):
        with ScriptProject() as project:
            project.mkdir("subdir")
            project.mkdir("relative")
            entry = project.write("main.sh", textwrap.dedent("""\
                if [[ -n "$USE_SUBDIR" ]]; then
                  cd subdir
                fi
                cd relative
                """))

            with self.assertRaisesRegex(NotImplementedError, "relative cd after branch-dependent cwd") as cm:
                SourceEvaluator().evaluate(entry)

        self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.branch-state")
        self.assertEqual(cm.exception.diagnostic.location.line, 4)

    def test_exact_literal_for_loop_sources_are_evaluated_in_order(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in ./a.sh ./b.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual([event.source_value for event in result.events], ["./a.sh", "./b.sh"])
        self.assertEqual(result.final_state.variables["dep"], "./b.sh")

    def test_exact_array_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                deps=(./a.sh ./b.sh)
                for dep in "${deps[@]}"; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_exact_scalar_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                A=./a.sh
                B=./b.sh
                for dep in "$A" "$B"; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            first = project.write("plugins/a.sh", 'echo "a"\n')
            second = project.write("plugins/b.sh", 'echo "b"\n')
            project.write("deps.txt", "./plugins/*.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEPS="./a.sh   ./b.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual([event.source_value for event in result.events], ["./a.sh", "./b.sh"])

    def test_command_substitution_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./a.sh\n./b.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual([event.source_value for event in result.events], ["./a.sh", "./b.sh"])

        with ScriptProject() as project:
            project.write("plugins/b.sh", 'echo "b"\n')
            project.write("plugins/a.sh", 'echo "a"\n')
            expected_words = self._find_words(project, "./plugins", "*.sh")
            expected_paths = self._paths_for_words(project, expected_words)
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(find ./plugins -type f -name '*.sh' -print); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], expected_paths)
        self.assertEqual([event.source_value for event in result.events], expected_words)

        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(printf '%s\\n' ./a.sh ./b.sh); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_quoted_command_substitution_for_loop_preserves_single_word(self):
        with ScriptProject() as project:
            dep = project.write("deps dir#tag/a dep.sh", 'echo "special"\n')
            project.write("dep-path.txt", "./deps dir#tag/a dep.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in "$(cat dep-path.txt)"; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual([event.source_value for event in result.events], ["./deps dir#tag/a dep.sh"])

    def test_while_until_and_read_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("deps/0.sh", 'echo "zero"\n')
            second = project.write("deps/1.sh", 'echo "one"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                i=0
                while (( i < 2 )); do
                  source "./deps/$i.sh"
                  ((i++))
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual(result.final_state.variables["i"], "2")

        with ScriptProject() as project:
            first = project.write("deps/0.sh", 'echo "zero"\n')
            second = project.write("deps/1.sh", 'echo "one"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                i=0
                until (( i == 2 )); do
                  source "./deps/$i.sh"
                  ((i++))
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            special = project.write("deps dir#tag/a dep.sh", 'echo "special"\n')
            regular = project.write("regular.sh", 'echo "regular"\n')
            project.write("deps.txt", "./deps dir#tag/a dep.sh\n./regular.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                while IFS= read -r dep; do
                  source "$dep"
                done < deps.txt
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [special, regular])

    def test_c_style_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("deps/0.sh", 'echo "zero"\n')
            second = project.write("deps/1.sh", 'echo "one"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for (( i=0; i<2; i++ )); do
                  source "./deps/$i.sh"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual(result.final_state.variables["i"], "2")

        with ScriptProject() as project:
            first = project.write("deps/1.sh", 'echo "one"\n')
            second = project.write("deps/2.sh", 'echo "two"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for (( i=0, j=1; j<3; i++, j++ )); do
                  source "./deps/$j.sh"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual(result.final_state.variables["i"], "2")
        self.assertEqual(result.final_state.variables["j"], "3")

    def test_custom_ifs_loop_word_splitting_is_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                IFS=:
                DEPS="./a.sh:./b.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                IFS=$'\\n'
                DEPS=$'./a.sh\\n./b.sh'
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./a.sh:./b.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                IFS=:
                for dep in $(cat deps.txt); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_read_loop_nonempty_guard_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./a.sh\n./b.sh")
            entry = project.write("main.sh", textwrap.dedent("""\
                while read -r dep || [[ -n "$dep" ]]; do
                  source "$dep"
                done < deps.txt
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_producer_read_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/b.sh", 'echo "b"\n')
            expected_paths = self._paths_for_words(project, self._find_words(project, "./plugins", "*.sh"))
            entry = project.write("main.sh", textwrap.dedent("""\
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], expected_paths)
        self.assertNotIn("dep", result.final_state.variables)

        with ScriptProject() as project:
            dep = project.write("plugins/a.sh", 'VALUE=a\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s lastpipe
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual(result.final_state.variables["VALUE"], "a")

        with ScriptProject() as project:
            dep = project.write("plugins/a.sh", 'VALUE=a\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                set -m
                shopt -s lastpipe
                find ./plugins -type f -name '*.sh' -print | while read -r dep; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertNotIn("VALUE", result.final_state.variables)

        with ScriptProject() as project:
            project.write("generated/a.sh", 'echo "a"\n')
            project.write("generated/b.sh", 'echo "b"\n')
            expected_words = self._find_words(project, "./generated", "*.sh")
            expected_paths = self._paths_for_words(project, expected_words)
            entry = project.write("main.sh", textwrap.dedent("""\
                while read -r dep; do
                  source "$dep"
                done < <(find ./generated -type f -name '*.sh' -print)
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], expected_paths)
        self.assertEqual(result.final_state.variables["dep"], expected_words[-1])

    def test_practical_command_producer_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            project.write("deps.txt", "./b.sh\n./a.sh\n")
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(sort deps.txt); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            first = project.write("deps/a.sh", 'echo "needle"\n')
            second = project.write("deps/b.sh", 'echo "needle"\n')
            project.write("deps/c.sh", 'echo "other"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in $(grep -lF needle ./deps/*.sh); do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

        with ScriptProject() as project:
            dep = project.write("dep.sh", 'echo "dirname bare"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dir in $(dirname dep.sh); do
                  source "$dir/dep.sh"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

        with ScriptProject() as project:
            dep = project.write("plugins/dep.sh", 'echo "basename trailing"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for name in $(basename ./plugins/dep.sh/); do
                  source "./plugins/$name"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

        with ScriptProject() as project:
            dep = project.write("plugins/dep.sh", 'echo "basename suffix"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for name in $(basename ./plugins/dep.sh .sh); do
                  source "./plugins/$name.sh"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

        with ScriptProject() as project:
            dep = project.write("plugins/-dep.sh", 'echo "basename dash"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for name in $(basename -- ./plugins/-dep.sh .sh); do
                  source "./plugins/$name.sh"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])

    def test_richer_array_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            third = project.write("c.sh", 'echo "c"\n')
            prod = project.write("prod.sh", 'echo "prod"\n')
            mapped = project.write("mapped.sh", 'echo "mapped"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                deps=(./a.sh)
                deps+=(./b.sh)
                i=2
                deps[$i]=./c.sh
                source "${deps[0]}"
                source "${deps[$i]}"
                declare -A by_env=([prod]=./prod.sh)
                ENV=prod
                source "${by_env[$ENV]}"
                mapfile -t loaded < deps.txt
                source "${loaded[0]}"
                """))
            project.write("deps.txt", "./mapped.sh\n")

            result = SourceEvaluator(mode="executable").evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, third, prod, mapped])
        self.assertEqual(result.final_state.arrays["deps"], ("./a.sh", "./b.sh", "./c.sh"))
        self.assertEqual(
            [(replacement.old, replacement.new) for replacement in result.line_replacements],
            [("mapfile -t loaded < deps.txt", "loaded=('./mapped.sh')")],
        )

        with ScriptProject() as project:
            first = project.write("a.sh", 'echo "a"\n')
            second = project.write("b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                project_deps=($(cat deps.txt))
                for dep in "${project_deps[@]}"; do
                  source "$dep"
                done
                """))
            project.write("deps.txt", "./a.sh\n./b.sh\n")

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_scalar_for_loop_glob_sources_are_evaluated(self):
        with ScriptProject() as project:
            second = project.write("plugins/b.sh", 'echo "b"\n')
            first = project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/readme.txt", 'echo "not sourced"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEPS="./plugins/*.sh"
                for dep in $DEPS; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual([event.source_value for event in result.events], ["./plugins/a.sh", "./plugins/b.sh"])

    def test_quoted_scalar_for_loop_preserves_single_word(self):
        with ScriptProject() as project:
            dep = project.write("deps dir#tag/a dep.sh", 'echo "special"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                DEP="./deps dir#tag/a dep.sh"
                for dep in "$DEP"; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual([event.source_value for event in result.events], ["./deps dir#tag/a dep.sh"])

    def test_empty_scalar_for_loop_sources_are_disabled(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                DEPS=""
                for dep in $DEPS; do source "$dep"; done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(result.events, ())
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ['source "$dep"'])
        self.assertEqual(result.disabled_sources[0].condition, "for dep in $DEPS")

    def test_exact_glob_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            second = project.write("plugins/b.sh", 'echo "b"\n')
            first = project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/readme.txt", 'echo "not sourced"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])
        self.assertEqual([event.source_value for event in result.events], ["./plugins/a.sh", "./plugins/b.sh"])

    def test_glob_options_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            hidden = project.write("plugins/.hidden.sh", 'echo "hidden"\n')
            visible = project.write("plugins/a.sh", 'echo "a"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s dotglob
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [hidden, visible])

        with ScriptProject() as project:
            top = project.write("plugins/a.sh", 'echo "a"\n')
            nested = project.write("plugins/nested/b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s globstar
                for dep in ./plugins/**/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [top, nested])

        with ScriptProject() as project:
            lower = project.write("plugins/a.sh", 'echo "a"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s nocaseglob
                for dep in ./plugins/*.SH; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [lower])

        with ScriptProject() as project:
            one_level = project.write("plugins/one/a.sh", 'echo "a"\n')
            project.write("plugins/one/deep/b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/**/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [one_level])

    def test_brace_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("plugins/a.sh", 'echo "a"\n')
            second = project.write("plugins/b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                for dep in ./plugins/{a,b}.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_nullglob_and_globignore_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s nullglob
                for dep in ./missing/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(result.events, ())
        self.assertEqual([disabled.source_site for disabled in result.disabled_sources], ['source "$dep"'])

        with ScriptProject() as project:
            hidden = project.write("plugins/.hidden.sh", 'echo "hidden"\n')
            kept = project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/b.sh", 'echo "b"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                GLOBIGNORE=./plugins/b.sh
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [hidden, kept])

        with ScriptProject() as project:
            kept = project.write("plugins/a.sh", 'echo "a"\n')
            project.write("plugins/b.sh", 'echo "b"\n')
            project.write("plugins/c.sh", 'echo "c"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                GLOBIGNORE=./plugins/@(b|c).sh
                for dep in ./plugins/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [kept])

    def test_extglob_for_loop_sources_are_evaluated(self):
        with ScriptProject() as project:
            first = project.write("plugins/a.sh", 'echo "a"\n')
            second = project.write("plugins/b.sh", 'echo "b"\n')
            project.write("plugins/c.sh", 'echo "c"\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s extglob
                for dep in ./plugins/@(a|b).sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [first, second])

    def test_direct_glob_source_event_uses_matched_runtime_word(self):
        with ScriptProject() as project:
            dep = project.write("plugins/only.sh", 'echo "only"\n')
            entry = project.write("main.sh", 'source ./plugins/*.sh\n')

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.path for event in result.events], [dep])
        self.assertEqual([event.source_value for event in result.events], ["./plugins/only.sh"])

    def test_missing_source_glob_events_are_evaluated(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                source ./missing/*.sh
                for dep in ./optional/*.sh; do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(
            [event.replacement_kind for event in result.events],
            ["missing-source", "missing-source"],
        )
        self.assertEqual(
            [event.source_value for event in result.events],
            ["./missing/*.sh", "./optional/*.sh"],
        )

    def test_nullglob_direct_source_event_is_no_filename_failure(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", 'shopt -s nullglob\nsource ./missing/*.sh\n')

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.replacement_kind for event in result.events], ["missing-source-no-filename"])
        self.assertEqual([event.source_value for event in result.events], [""])

    def test_direct_brace_and_nullglob_shift_source_events_are_evaluated(self):
        with ScriptProject() as project:
            project.write("real.sh", 'echo real\n')
            project.write("fallback.sh", 'echo fallback\n')
            entry = project.write("main.sh", textwrap.dedent("""\
                source ./{real,missing}.sh
                shopt -s nullglob
                source ./missing/*.sh ./fallback.sh arg
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.replacement_kind for event in result.events], ["source", "source"])
        self.assertEqual([event.source_value for event in result.events], ["./real.sh", "./fallback.sh"])
        self.assertEqual([event.source_arguments for event in result.events], [("./missing.sh",), ("arg",)])

    def test_failglob_direct_source_event_is_expansion_failure(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s failglob
                source ./missing/*.sh; source ./skipped/*.sh
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.replacement_kind for event in result.events], ["source-expansion-failure"])
        self.assertEqual([event.source_value for event in result.events], ["./missing/*.sh"])

    def test_failglob_function_source_event_returns_from_function(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s failglob
                load(){ source ./missing/*.sh; echo skipped; }
                load; echo skipped-line
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual([event.replacement_kind for event in result.events], ["source-expansion-failure-return"])
        self.assertEqual([event.source_value for event in result.events], ["./missing/*.sh"])
        self.assertEqual(
            [(replacement.old, replacement.new) for replacement in result.line_replacements],
            [("load", "load #")],
        )

    def test_failglob_condition_and_loop_record_block_replacements(self):
        with ScriptProject() as project:
            entry = project.write("main.sh", textwrap.dedent("""\
                shopt -s failglob
                if source ./missing/*.sh
                then
                  source ./unreachable-if.sh
                fi
                for dep in ./missing/*.bash
                do
                  source "$dep"
                done
                """))

            result = SourceEvaluator().evaluate(entry)

        self.assertEqual(result.events, ())
        self.assertEqual([source.source_site for source in result.disabled_sources], ['source "$dep"'])
        self.assertTrue(any("no match: %s" in replacement.new for replacement in result.line_replacements))
        self.assertTrue(any(replacement.old == "./missing/*.bash" for replacement in result.line_replacements))

    def test_unsupported_for_loop_words_raise_structured_diagnostic(self):
        cases = {
            "command substitution": 'for dep in $(cat deps.txt); do source "$dep"; done\n',
            "unknown scalar": 'for dep in "$DEP"; do source "$dep"; done\n',
            "unknown array": 'for dep in "${deps[@]}"; do source "$dep"; done\n',
            "quoted glob": 'for dep in "./plugins/*.sh"; do source "$dep"; done\n',
            "disabled extglob": 'for dep in ./plugins/@(a|b).sh; do source "$dep"; done\n',
        }

        for name, content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                entry = project.write("main.sh", content)

                with self.assertRaisesRegex(NotImplementedError, "loop word") as cm:
                    SourceEvaluator().evaluate(entry)

            self.assertEqual(cm.exception.diagnostic.code, "unsupported.source.loop-word-list")
            self.assertEqual(cm.exception.diagnostic.location.line, 1)

    def test_circular_source_raises_recursion_error(self):
        with ScriptProject() as project:
            entry = project.write("a.sh", "source ./b.sh\n")
            project.write("b.sh", "source ./a.sh\n")

            with self.assertRaises(RecursionError):
                SourceEvaluator().evaluate(entry)


if __name__ == "__main__":
    unittest.main()
