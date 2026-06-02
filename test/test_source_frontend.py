import textwrap
import unittest
from pathlib import Path

from methods.source_effects import (
    ArrayAssignment,
    Assignment,
    CaseBlock,
    CdCommand,
    CStyleForLoop,
    FunctionDef,
    ForLoop,
    IfBlock,
    RawCommand,
    SetCommand,
    SourceSite,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend


class LineParserFrontendTestCase(unittest.TestCase):
    def parse(self, content: str):
        return LineParserFrontend().parse(Path("main.sh"), textwrap.dedent(content))

    def test_parses_static_source_sites_with_locations(self):
        ir = self.parse("""\
            source ./dep.sh
            . ./dot.sh
            source "./dir with spaces/dep.sh"
            source ./dir#tag/dep.sh
            source ./config
            echo done
            """)

        source_sites = ir.source_sites

        self.assertEqual([site.source_expression for site in source_sites], [
            "./dep.sh",
            "./dot.sh",
            '"./dir with spaces/dep.sh"',
            "./dir#tag/dep.sh",
            "./config",
        ])
        self.assertEqual([site.command_name for site in source_sites], ["source", ".", "source", "source", "source"])
        self.assertEqual([site.location.line for site in source_sites], [1, 2, 3, 4, 5])
        self.assertEqual([site.location.column for site in source_sites], [1, 1, 1, 1, 1])
        self.assertEqual([site.is_control_flow for site in source_sites], [False, False, False, False, False])

        raw_commands = [node for node in ir.nodes if isinstance(node, RawCommand)]
        self.assertEqual([node.text for node in raw_commands], ["echo done"])

    def test_preserves_multiple_source_sites_on_one_line(self):
        ir = self.parse('source ./a.sh && source "$NEXT" || echo missing\n')

        self.assertEqual([site.text for site in ir.source_sites], [
            "source ./a.sh",
            '&& source "$NEXT"',
        ])
        self.assertEqual([site.separator for site in ir.source_sites], ["", "&&"])
        self.assertEqual([site.source_expression for site in ir.source_sites], ["./a.sh", '"$NEXT"'])

    def test_ignores_source_text_inside_comments_quotes_and_heredocs(self):
        ir = self.parse("""\
            echo "source ./quoted.sh"
            # source ./commented.sh
            cat <<EOF
            source ./heredoc.sh
            EOF
            source ./real.sh
            """)

        self.assertEqual([site.source_expression for site in ir.source_sites], ["./real.sh"])

    def test_keeps_indented_function_body_source_location(self):
        ir = self.parse("""\
            helper() {
              source ./runtime.sh
            }
            """)

        self.assertEqual(len(ir.source_sites), 1)
        site = ir.source_sites[0]
        self.assertEqual(site.source_expression, "./runtime.sh")
        self.assertEqual(site.location.line, 2)
        self.assertEqual(site.location.column, 3)

    def test_parses_function_definitions_as_ir_nodes(self):
        ir = self.parse("""\
            load_dep() {
              local dep="$1"
              source "$dep"
            }
            function compact { source ./static.sh; }
            """)

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, FunctionDef])
        self.assertEqual([node.name for node in ir.nodes], ["load_dep", "compact"])
        self.assertEqual([type(node) for node in ir.nodes[0].body], [Assignment, SourceSite])
        self.assertEqual(ir.nodes[0].body[0].prefix, "local")
        self.assertEqual(ir.nodes[1].body[0].source_expression, "./static.sh")

    def test_parses_function_definitions_with_split_opening_brace(self):
        ir = self.parse("""\
            load_dep()
            {
              source "$1"
            }
            function compact
            { source ./static.sh; }
            """)

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, FunctionDef])
        self.assertEqual([node.name for node in ir.nodes], ["load_dep", "compact"])
        self.assertEqual(ir.nodes[0].body[0].source_expression, '"$1"')
        self.assertEqual(ir.nodes[1].body[0].source_expression, "./static.sh")
        self.assertEqual(ir.nodes[1].body[0].location.line, 6)

    def test_parses_function_definition_with_closing_redirection(self):
        ir = self.parse("""\
            load_dep() {
              source "$1"
            } > out.txt
            load_dep ./dep.sh
            """)

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, RawCommand])
        self.assertEqual(ir.nodes[0].body[0].source_expression, '"$1"')
        self.assertEqual(ir.nodes[0].body[0].location.line, 2)

    def test_parses_same_line_function_definition_tail(self):
        ir = self.parse('load_dep() { source ./dep.sh; }; load_dep\n')

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, RawCommand])
        self.assertEqual(ir.nodes[0].body[0].source_expression, "./dep.sh")
        self.assertEqual(ir.nodes[1].text, "load_dep")

    def test_parses_same_line_function_definitions_inside_if_body(self):
        ir = self.parse("""\
            if [[ -n "$USE_ALT" ]]; then
              load_dep() { source ./dep.sh; }
            else
              load_dep() { source ./dep.sh; }
            fi
            load_dep
            """)

        self.assertEqual([type(node) for node in ir.nodes[0].branches[0].body], [FunctionDef])
        self.assertEqual([type(node) for node in ir.nodes[0].branches[1].body], [FunctionDef])

    def test_function_body_parameter_expansion_is_not_a_closing_brace(self):
        ir = self.parse("""\
            helper() {
              value="${ROOT}/dep.sh"
              source "$value"
            }
            helper
            """)

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, RawCommand])
        self.assertEqual([type(node) for node in ir.nodes[0].body], [Assignment, SourceSite])
        self.assertEqual(ir.nodes[0].body[0].value, '"${ROOT}/dep.sh"')

    def test_nested_function_definition_does_not_close_outer_function(self):
        ir = self.parse("""\
            outer() {
              before=1
              inner() {
                echo inner
              }
              after=1
            }
            after_outer=1
            """)

        self.assertEqual([type(node) for node in ir.nodes], [FunctionDef, Assignment])
        self.assertEqual(ir.nodes[0].name, "outer")
        self.assertEqual([type(node) for node in ir.nodes[0].body], [Assignment, FunctionDef, Assignment])
        self.assertEqual(ir.nodes[0].body[1].name, "inner")

    def test_emits_raw_commands_for_non_source_fragments(self):
        ir = self.parse('cd subdir && echo ready && source ./dep.sh\n')

        raw_commands = [node for node in ir.nodes if isinstance(node, RawCommand)]
        cd_commands = [node for node in ir.nodes if isinstance(node, CdCommand)]
        source_sites = [node for node in ir.nodes if isinstance(node, SourceSite)]

        self.assertEqual([node.path_expression for node in cd_commands], ["subdir"])
        self.assertEqual([node.text for node in raw_commands], ["echo ready"])
        self.assertEqual([node.source_expression for node in source_sites], ["./dep.sh"])
        self.assertEqual([type(node) for node in ir.nodes], [CdCommand, RawCommand, SourceSite])

    def test_emits_stateful_nodes_for_current_evaluator_inputs(self):
        ir = self.parse("""\
            ROOT="./dir with spaces"
            cd "$ROOT"
            set +u -e
            export DEP=./dep.sh
            deps=(./base.sh "./feature path.sh")
            echo done
            """)

        self.assertEqual([type(node) for node in ir.nodes], [
            Assignment,
            CdCommand,
            SetCommand,
            Assignment,
            ArrayAssignment,
            RawCommand,
        ])

        first_assignment = ir.nodes[0]
        self.assertEqual(first_assignment.name, "ROOT")
        self.assertEqual(first_assignment.value, '"./dir with spaces"')

        set_command = ir.nodes[2]
        self.assertEqual(set_command.arguments, ("+u", "-e"))

        export_assignment = ir.nodes[3]
        self.assertEqual(export_assignment.prefix, "export")
        self.assertEqual(export_assignment.name, "DEP")
        self.assertEqual(export_assignment.value, "./dep.sh")

        array_assignment = ir.nodes[4]
        self.assertEqual(array_assignment.name, "deps")
        self.assertEqual(array_assignment.values, ("./base.sh", "./feature path.sh"))
        self.assertTrue(array_assignment.is_exact)

    def test_marks_unparseable_array_assignment_as_not_exact(self):
        ir = self.parse('deps=("./unterminated)\n')

        self.assertEqual(len(ir.nodes), 1)
        array_assignment = ir.nodes[0]
        self.assertIsInstance(array_assignment, ArrayAssignment)
        self.assertEqual(array_assignment.name, "deps")
        self.assertEqual(array_assignment.values, ())
        self.assertFalse(array_assignment.is_exact)

    def test_finds_source_sites_inside_future_control_flow_fixtures(self):
        ir = self.parse("""\
            for file in ./plugins/*.sh; do source "$file"; done
            if [[ -f ./local.sh ]]; then
              source ./local.sh
            fi
            case "$ENV" in
              prod) source ./prod.sh ;;
              dev) source ./dev.sh ;;
            esac
            deps=(./base.sh ./feature.sh)
            source "${deps[0]}"
            """)

        self.assertEqual([site.source_expression for site in ir.source_sites], [
            '"$file"',
            "./local.sh",
            "./prod.sh",
            "./dev.sh",
            '"${deps[0]}"',
        ])
        self.assertEqual([site.location.line for site in ir.source_sites], [1, 3, 6, 7, 10])
        self.assertEqual([site.is_control_flow for site in ir.source_sites], [False, False, False, False, False])

    def test_control_flow_marking_is_source_site_specific_on_mixed_lines(self):
        ir = self.parse('source ./always.sh; if true; then source ./branch.sh; fi\n')

        self.assertEqual([site.source_expression for site in ir.source_sites], ["./always.sh", "./branch.sh"])
        self.assertEqual([site.is_control_flow for site in ir.source_sites], [False, True])

    def test_parses_simple_multiline_for_loop_node(self):
        ir = self.parse("""\
            for dep in ./a.sh "./b path.sh"; do
              echo "$dep"
              source "$dep"
            done
            """)

        self.assertEqual(len(ir.nodes), 1)
        loop = ir.nodes[0]
        self.assertIsInstance(loop, ForLoop)
        self.assertEqual(loop.variable, "dep")
        self.assertEqual(loop.words, ("./a.sh", "./b path.sh"))
        self.assertTrue(loop.is_exact)
        self.assertEqual([type(node) for node in loop.body], [RawCommand, SourceSite])
        self.assertEqual(loop.body[1].location.line, 3)
        self.assertEqual(loop.body[1].source_expression, '"$dep"')
        self.assertFalse(loop.body[1].is_control_flow)

    def test_parses_newline_do_for_loop_node(self):
        ir = self.parse("""\
            for dep in ./a.sh ./b.sh
            do
              source "$dep"
            done
            """)

        self.assertEqual(len(ir.nodes), 1)
        loop = ir.nodes[0]
        self.assertIsInstance(loop, ForLoop)
        self.assertEqual(loop.variable, "dep")
        self.assertEqual(loop.words, ("./a.sh", "./b.sh"))
        self.assertEqual(loop.body[0].location.line, 3)
        self.assertEqual(loop.body[0].source_expression, '"$dep"')

    def test_loop_body_ignores_source_text_inside_heredoc(self):
        ir = self.parse("""\
            for dep in ./a.sh; do
              cat <<EOF
              source "$dep"
            EOF
              source "$dep"
            done
            """)

        self.assertEqual([site.location.line for site in ir.source_sites], [5])
        self.assertEqual([site.source_expression for site in ir.source_sites], ['"$dep"'])

    def test_parses_simple_inline_for_loop_node(self):
        ir = self.parse('for dep in ./a.sh ./b.sh; do source "$dep"; done\n')

        self.assertEqual(len(ir.nodes), 1)
        loop = ir.nodes[0]
        self.assertIsInstance(loop, ForLoop)
        self.assertEqual(loop.variable, "dep")
        self.assertEqual(loop.words, ("./a.sh", "./b.sh"))
        self.assertEqual([site.source_expression for site in loop.body if isinstance(site, SourceSite)], ['"$dep"'])

    def test_parses_while_and_until_loop_nodes(self):
        ir = self.parse("""\
            while (( i < 2 )); do
              source "./deps/$i.sh"
              ((i++))
            done
            until false; do source ./once.sh; break; done
            while IFS= read -r dep; do
              source "$dep"
            done < deps.txt
            find ./plugins -name '*.sh' -print | while read -r plugin; do
              source "$plugin"
            done
            while read -r generated; do
              source "$generated"
            done < <(find ./generated -name '*.sh' -print)
            """)

        self.assertEqual([type(node) for node in ir.nodes], [WhileLoop, WhileLoop, WhileLoop, WhileLoop, WhileLoop])
        self.assertEqual([node.keyword for node in ir.nodes], ["while", "until", "while", "while", "while"])
        self.assertEqual([node.condition for node in ir.nodes], [
            "(( i < 2 ))",
            "false",
            "IFS= read -r dep",
            "read -r plugin",
            "read -r generated",
        ])
        self.assertEqual(ir.nodes[2].trailing, "< deps.txt")
        self.assertEqual(ir.nodes[3].producer, "find ./plugins -name '*.sh' -print")
        self.assertEqual(ir.nodes[4].trailing, "< <(find ./generated -name '*.sh' -print)")
        self.assertEqual([site.source_expression for site in ir.source_sites], [
            '"./deps/$i.sh"',
            "./once.sh",
            '"$dep"',
            '"$plugin"',
            '"$generated"',
        ])

    def test_parses_c_style_for_loop_nodes(self):
        ir = self.parse("""\
            for (( i=0; i<2; i++ )); do
              source "./deps/$i.sh"
            done
            for ((j=0; j<1; j++)); do source "./deps/$j.sh"; done
            """)

        self.assertEqual([type(node) for node in ir.nodes], [CStyleForLoop, CStyleForLoop])
        self.assertEqual([(node.init, node.condition, node.update) for node in ir.nodes], [
            ("i=0", "i<2", "i++"),
            ("j=0", "j<1", "j++"),
        ])
        self.assertEqual([site.source_expression for site in ir.source_sites], [
            '"./deps/$i.sh"',
            '"./deps/$j.sh"',
        ])

    def test_marks_unparseable_for_loop_words_as_not_exact(self):
        ir = self.parse('for dep in "./unterminated; do source "$dep"; done\n')

        self.assertEqual(len(ir.nodes), 1)
        loop = ir.nodes[0]
        self.assertIsInstance(loop, ForLoop)
        self.assertEqual(loop.words, ())
        self.assertFalse(loop.is_exact)

    def test_parses_simple_multiline_if_block_node(self):
        ir = self.parse("""\
            if [[ -f ./local.sh ]]; then
              source ./local.sh
            elif [[ -f ./fallback.sh ]]; then
              source ./fallback.sh
            else
              source ./default.sh
            fi
            """)

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, IfBlock)
        self.assertEqual([branch.keyword for branch in block.branches], ["if", "elif", "else"])
        self.assertEqual([branch.condition for branch in block.branches], [
            "[[ -f ./local.sh ]]",
            "[[ -f ./fallback.sh ]]",
            None,
        ])
        self.assertEqual([branch.body[0].source_expression for branch in block.branches], [
            "./local.sh",
            "./fallback.sh",
            "./default.sh",
        ])
        self.assertEqual([branch.body[0].location.line for branch in block.branches], [2, 4, 6])
        self.assertEqual([site.is_control_flow for site in ir.source_sites], [False, False, False])

    def test_parses_simple_inline_if_block_node(self):
        ir = self.parse('if [[ "$MODE" == prod ]]; then source ./prod.sh; else source ./dev.sh; fi\n')

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, IfBlock)
        self.assertEqual([branch.keyword for branch in block.branches], ["if", "else"])
        self.assertEqual([branch.condition for branch in block.branches], ['[[ "$MODE" == prod ]]', None])
        self.assertEqual([site.source_expression for site in ir.source_sites], ["./prod.sh", "./dev.sh"])

    def test_preserves_shell_logical_operators_in_if_conditions(self):
        ir = self.parse('if source ./dep.sh && true; then echo loaded; fi\n')

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, IfBlock)
        self.assertEqual(block.branches[0].condition, "source ./dep.sh && true")
        self.assertEqual(block.branches[0].condition_location.line, 1)
        self.assertEqual(block.branches[0].condition_location.column, 4)

    def test_parses_simple_multiline_case_block_node(self):
        ir = self.parse("""\
            case "$ENV" in
              prod|stage)
                source ./prod.sh
                ;;
              "dev")
                source ./dev.sh
                ;;
              *)
                source ./default.sh
                ;;
            esac
            """)

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, CaseBlock)
        self.assertEqual(block.subject, '"$ENV"')
        self.assertEqual([arm.patterns for arm in block.arms], [
            ("prod", "stage"),
            ('"dev"',),
            ("*",),
        ])
        self.assertEqual([arm.terminator for arm in block.arms], [";;", ";;", ";;"])
        self.assertEqual([arm.body[0].source_expression for arm in block.arms], [
            "./prod.sh",
            "./dev.sh",
            "./default.sh",
        ])
        self.assertEqual([arm.body[0].location.line for arm in block.arms], [3, 6, 9])
        self.assertEqual([site.is_control_flow for site in ir.source_sites], [False, False, False])

    def test_parses_simple_inline_case_block_node(self):
        ir = self.parse('case "$ENV" in prod) source ./prod.sh ;; dev) source ./dev.sh ;; esac\n')

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, CaseBlock)
        self.assertEqual(block.subject, '"$ENV"')
        self.assertEqual([arm.patterns for arm in block.arms], [("prod",), ("dev",)])
        self.assertEqual([site.source_expression for site in ir.source_sites], ["./prod.sh", "./dev.sh"])

    def test_parses_case_extglob_patterns_as_single_arm_patterns(self):
        ir = self.parse("""\
            case "$ENV" in
              @(prod|stage)) source ./prod.sh ;;
              !(prod|stage)) source ./default.sh ;;
              (@(blue|green)) source ./color.sh ;;
            esac
            """)

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, CaseBlock)
        self.assertEqual([arm.patterns for arm in block.arms], [
            ("@(prod|stage)",),
            ("!(prod|stage)",),
            ("@(blue|green)",),
        ])
        self.assertEqual([site.source_expression for site in ir.source_sites], [
            "./prod.sh",
            "./default.sh",
            "./color.sh",
        ])

    def test_parses_case_subject_containing_in_inside_quotes(self):
        ir = self.parse("""\
            case "value in prod" in
              *prod) source ./prod.sh ;;
            esac
            """)

        self.assertEqual(len(ir.nodes), 1)
        block = ir.nodes[0]
        self.assertIsInstance(block, CaseBlock)
        self.assertEqual(block.subject, '"value in prod"')
        self.assertEqual(block.arms[0].patterns, ("*prod",))
        self.assertEqual(ir.source_sites[0].source_expression, "./prod.sh")

    def test_parses_case_fallthrough_terminators(self):
        ir = self.parse("""\
            case "$ENV" in
              prod) source ./prod.sh ;&
              stage) source ./stage.sh ;;&
              *) source ./default.sh ;;
            esac
            """)

        block = ir.nodes[0]
        self.assertIsInstance(block, CaseBlock)
        self.assertEqual([arm.terminator for arm in block.arms], [";&", ";;&", ";;"])


if __name__ == "__main__":
    unittest.main()
