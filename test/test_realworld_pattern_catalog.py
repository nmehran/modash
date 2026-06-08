import re
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.runtime_evaluator.reports import build_observation_report  # noqa: E402
from methods.runtime_evaluator.graph import build_observed_source_graph, write_observed_source_graph  # noqa: E402
from methods.runtime_evaluator.supplements import generate_source_supplement, write_generated_supplement  # noqa: E402
from modash import compile_observed_main  # noqa: E402
from test.support import ScriptProject  # noqa: E402


class RealWorldPatternCatalogTestCase(unittest.TestCase):
    """Named synthetic counterparts for lessons promoted from real projects."""

    def test_bash_completion_shopt_guarded_command_definition_matches_bash(self):
        with ScriptProject() as project:
            project.write("enabled.sh", 'printf "enabled\\n"\n')
            project.write("disabled.sh", 'printf "disabled\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    shopt -s cdable_vars
                    if shopt -q cdable_vars; then
                      source ./enabled.sh
                    else
                      source ./disabled.sh
                    fi
                    """),
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_completion_runtime_loader_graph_replays_observed_completion_files(self):
        with ScriptProject() as project:
            project.write("completions/alias", 'printf "completion:alias\\n"\n')
            project.write("completions/cd", 'printf "completion:cd\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    for completion in alias cd; do
                      . "$MODASH_COMPLETION_ROOT/$completion"
                    done
                    """),
            )
            output = project.path("compiled-static.sh")
            env = {"MODASH_COMPLETION_ROOT": str(project.path("completions"))}

            with self.assertRaises(NotImplementedError):
                project.compile("main.sh", output=output, mode="executable")
            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "completion:alias\ncompletion:cd\n")
        self.assert_edge_names(graph, ["alias", "cd"])

    def test_makepkg_source_safe_quoted_at_with_shopt_restore_matches_bash(self):
        with ScriptProject() as project:
            project.write("PKGBUILD", 'PKGNAME=demo\nprintf "pkgbuild:%s\\n" "$PKGNAME"\n')
            project.write(
                "helpers.sh",
                textwrap.dedent("""\
                    source_safe() {
                      local shellopts=$(shopt -p extglob)
                      shopt -u extglob
                      if ! source "$@"; then
                        return 1
                      fi
                      eval "$shellopts"
                    }

                    shopt -s extglob
                    source_safe ./PKGBUILD
                    shopt -q extglob; printf "extglob:%s\\n" "$?"
                    printf "helper:%s\\n" "$PKGNAME"
                    """),
            )
            project.write("main.sh", 'source ./helpers.sh\nprintf "main:%s\\n" "$PKGNAME"\n')

            project.assert_compiled_matches(self, "main.sh")

    def test_makepkg_library_prefix_supplement_replays_observed_source_safe(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                textwrap.dedent("""\
                    source_safe() {
                      if ! source "$@"; then
                        return 1
                      fi
                    }

                    source_safe "$MAKEPKG_LIBRARY/util/message.sh" notice
                    printf "main:%s\\n" "$MESSAGE"
                    """),
            )
            project.write("libmakepkg/util/message.sh", 'MESSAGE="$1"\nprintf "message:%s\\n" "$MESSAGE"\n')
            env = {"MAKEPKG_LIBRARY": str(project.path("libmakepkg"))}
            observation = project.trace("main.sh", env=env).observation
            supplement = generate_source_supplement(entrypoint, observation)
            supplement_path = project.path("generated/source-supplement.json")
            write_generated_supplement(supplement, supplement_path)

            compiled = project.compile("main.sh", mode="executable", source_supplement=supplement_path)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "message:notice\nmain:notice\n")
        self.assertEqual(supplement.to_dict()["functions"], {
            "source_safe": [
                {
                    "arguments": ["libmakepkg/util/message.sh", "notice"],
                },
            ],
        })

    def test_wrapped_source_invocation_forms_match_bash(self):
        cases = {
            "builtin source": 'builtin source ./dep.sh builtin-source "two words"\n',
            "builtin dot": 'builtin . ./dep.sh builtin-dot\n',
            "command source": 'command source ./dep.sh command-source\n',
            "command dot": 'command . ./dep.sh command-dot\n',
            "command path source": 'command -p source ./dep.sh command-path\n',
            "command delimiter source": 'command -- source ./dep.sh command-delimiter\n',
        }

        for name, main_content in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                project.write(
                    "dep.sh",
                    'VALUE="${1-default}:${2-none}"\nprintf "dep:%s\\n" "$VALUE"\n',
                )
                project.write("main.sh", main_content + 'printf "main:%s\\n" "$VALUE"\n')

                project.assert_compiled_matches(self, "main.sh")

    def test_direct_glob_source_arguments_match_bash(self):
        with ScriptProject() as project:
            project.write(
                "glob-args/00-loader.sh",
                'printf "loader:%s:%s:%s\\n" "$1" "$2" "$3"\nVALUE="$1|$2|$3"\n',
            )
            project.write("glob-args/10-first-arg.sh", "unused\n")
            project.write("glob-args/20-second-arg.sh", "unused\n")
            project.write(
                "main.sh",
                'source ./glob-args/*.sh explicit\nprintf "main:%s\\n" "$VALUE"\n',
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_runtime_state_cwd_return_and_short_circuit_patterns_match_bash(self):
        cases = {
            "runtime state": (
                {"lib.sh": 'VALUE=loaded\nprintf "lib:%s\\n" "$VALUE"\n'},
                'source ./lib.sh && printf "main:%s\\n" "$VALUE"\n',
            ),
            "runtime cwd": (
                {
                    "lib.sh": 'cd nested\nsource ./inner.sh\nprintf "lib-cwd:%s\\n" "$VALUE"\n',
                    "nested/inner.sh": 'VALUE=inner\nprintf "inner\\n"\n',
                },
                'source ./lib.sh\nprintf "main:%s\\n" "$VALUE"\n',
            ),
            "runtime return": (
                {
                    "lib.sh": 'VALUE=before\nprintf "lib:%s\\n" "$VALUE"\nreturn 3\nVALUE=after\n',
                },
                'source ./lib.sh\nprintf "main:%s:%s\\n" "$VALUE" "$?"\n',
            ),
            "runtime short circuit": (
                {
                    "primary.sh": 'printf "primary\\n"\nreturn 7\n',
                    "fallback.sh": 'printf "fallback\\n"\nVALUE=fallback\n',
                },
                'source ./primary.sh || source ./fallback.sh\nprintf "main:%s\\n" "$VALUE"\n',
            ),
        }

        for name, (files, main_content) in cases.items():
            with self.subTest(name=name), ScriptProject() as project:
                for path, content in files.items():
                    project.write(path, content)
                project.write("main.sh", main_content)

                project.assert_compiled_matches(self, "main.sh")

    def test_source_argument_frame_restoration_matches_bash(self):
        with ScriptProject() as project:
            project.write(
                "frame.sh",
                textwrap.dedent("""\
                    printf "frame-before:%s:%s\\n" "$1" "$2"
                    set -- mutated nested
                    source ./nested.sh
                    printf "frame-after:%s:%s\\n" "$1" "$2"
                    """),
            )
            project.write("nested.sh", 'printf "nested:%s:%s\\n" "$1" "$2"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    set -- outer original
                    source ./frame.sh alpha beta
                    printf "main:%s:%s\\n" "$1" "$2"
                    """),
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_child_shell_source_context_preserves_parent_state(self):
        with ScriptProject() as project:
            project.write("dep.sh", 'VALUE=child\nprintf "dep:%s\\n" "$VALUE"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    ( source ./dep.sh; printf "child:%s\\n" "$VALUE" )
                    printf "parent:%s\\n" "${VALUE-unset}"
                    """),
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_runtime_guarded_if_and_case_sources_match_bash(self):
        with ScriptProject() as project:
            project.write("enabled.sh", 'STATE=enabled\nprintf "enabled\\n"\n')
            project.write("disabled.sh", 'STATE=disabled\nprintf "disabled\\n"\n')
            project.write("prod.sh", 'MODE_STATE=prod\nprintf "prod\\n"\n')
            project.write("dev.sh", 'MODE_STATE=dev\nprintf "dev\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    if awk 'BEGIN { exit ENVIRON["LOAD_FEATURE"] == "1" ? 0 : 1 }'; then
                      source ./enabled.sh
                    else
                      source ./disabled.sh
                    fi
                    case "$MODE" in
                      prod) source ./prod.sh ;;
                      dev) source ./dev.sh ;;
                    esac
                    printf "state:%s:%s\\n" "$STATE" "$MODE_STATE"
                    """),
            )
            compiled = project.compile("main.sh", mode="executable")

            for env in ({"LOAD_FEATURE": "1", "MODE": "prod"}, {"LOAD_FEATURE": "0", "MODE": "dev"}):
                with self.subTest(env=env):
                    expected = project.run("main.sh", env=env)
                    actual = project.run(compiled, env=env)
                    self.assertEqual(actual.returncode, expected.returncode, actual.stdout)
                    self.assertEqual(actual.stdout, expected.stdout)
            self.assert_no_live_source(compiled)

    def test_compound_source_condition_and_fallback_match_bash(self):
        with ScriptProject() as project:
            project.write("primary.sh", 'printf "primary\\n"\nreturn 4\n')
            project.write("fallback.sh", 'printf "fallback\\n"\nVALUE=fallback\n')
            project.write("after.sh", 'printf "after\\n"\nVALUE=after\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    if source ./primary.sh && source ./after.sh; then
                      printf "ok\\n"
                    else
                      source ./fallback.sh
                    fi
                    printf "main:%s:%s\\n" "$VALUE" "$?"
                    """),
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_pattern_semantics_for_extglob_globignore_and_case_match_bash(self):
        with ScriptProject() as project:
            project.write("plugins/core.sh", 'printf "core\\n"\n')
            project.write("plugins/extra.sh", 'printf "extra\\n"\n')
            project.write("plugins/skip.sh", 'printf "skip\\n"\n')
            project.write("case-core.sh", 'printf "case-core\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    shopt -s extglob
                    GLOBIGNORE="./plugins/skip.sh"
                    for dep in ./plugins/@(core|extra|skip).sh; do
                      source "$dep"
                    done
                    case "${MODE:-core}" in
                      @(core|extra)) source ./case-core.sh ;;
                    esac
                    """),
            )

            project.assert_compiled_matches(self, "main.sh")

    def test_coverage_report_tracks_unobserved_runtime_branch(self):
        with ScriptProject() as project:
            entrypoint = project.write(
                "main.sh",
                textwrap.dedent("""\
                    if [[ "${MODE:-prod}" == prod ]]; then
                      source ./prod.sh
                    else
                      source ./dev.sh
                    fi
                    """),
            )
            project.write("prod.sh", 'printf "prod\\n"\n')
            project.write("dev.sh", 'printf "dev\\n"\n')
            observation = project.trace("main.sh", env={"MODE": "prod"}).observation
            report = build_observation_report(entrypoint, observation)

        self.assertEqual(report["summary"]["warnings"], 1)
        self.assertEqual(report["warnings"][0]["code"], "runtime.coverage.unobserved_source_site")
        self.assertEqual(report["unobserved_source_sites"][0]["source_expression"], "./dev.sh")

    def test_git_plugin_module_loader_graph_replays_runtime_env_path(self):
        with ScriptProject() as project:
            project.write("mergetools/bc", 'printf "tool:bc\\n"\n')
            project.write("mergetools/vimdiff", 'printf "tool:vimdiff\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    for tool in bc vimdiff; do
                      . "$MODASH_GIT_MERGETOOLS_DIR/$tool"
                    done
                    """),
            )
            env = {"MODASH_GIT_MERGETOOLS_DIR": str(project.path("mergetools"))}

            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "tool:bc\ntool:vimdiff\n")
        self.assert_edge_names(graph, ["bc", "vimdiff"])

    def test_git_child_bash_command_string_graph_replays_positional_source(self):
        with ScriptProject() as project:
            project.write("completion/git-prompt.sh", 'PROMPT_VALUE=git\nprintf "prompt-source\\n"\n')
            project.write(
                "main.sh",
                "bash -c '. \"$1\"; printf \"prompt:%s\\n\" \"$PROMPT_VALUE\"' bash "
                '"$MODASH_GIT_COMPLETION_DIR/git-prompt.sh"\n'
                'printf "parent:%s\\n" "${PROMPT_VALUE-unset}"\n',
            )
            env = {"MODASH_GIT_COMPLETION_DIR": str(project.path("completion"))}

            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "prompt-source\nprompt:git\nparent:unset\n")
        self.assert_edge_names(graph, ["git-prompt.sh"])

    def test_git_runtime_selected_helper_name_graph_replays_observed_edges(self):
        with ScriptProject() as project:
            project.write("mergetools/bc", 'printf "tool:bc\\n"\n')
            project.write("mergetools/vimdiff", 'printf "tool:vimdiff\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    load_mergetool() {
                      source "$1"
                    }
                    "$MODASH_GIT_HELPER" ./mergetools/bc
                    "$MODASH_GIT_HELPER" ./mergetools/vimdiff
                    """),
            )
            env = {
                "MODASH_GIT_HELPER": "load_mergetool",
            }
            output = project.path("compiled-static.sh")

            with self.assertRaisesRegex(NotImplementedError, "dynamic function dispatch"):
                project.compile("main.sh", output=output, mode="executable")
            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "tool:bc\ntool:vimdiff\n")
        self.assert_edge_names(graph, ["bc", "vimdiff"])

    def test_git_runtime_selected_helper_name_with_dynamic_path_args_replays_with_env_constraint(self):
        with ScriptProject() as project:
            project.write("mergetools/bc", 'printf "tool:bc\\n"\n')
            project.write("drift/bc", 'printf "drift\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    load_mergetool() {
                      source "$1"
                    }
                    "$MODASH_GIT_HELPER" "$MODASH_GIT_MERGETOOLS_DIR/bc"
                    """),
            )
            env = {
                "MODASH_GIT_HELPER": "load_mergetool",
                "MODASH_GIT_MERGETOOLS_DIR": str(project.path("mergetools")),
            }
            entrypoint = project.path("main.sh")
            trace = project.trace("main.sh", env=env)
            graph = build_observed_source_graph(entrypoint, trace.observation)
            graph_path = project.path("graph/runtime-source-graph.json")
            write_observed_source_graph(graph, graph_path)
            compiled = project.path("compiled.sh")

            compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
            result = project.run(compiled, env=env)
            drifted = project.run(compiled, env={
                **env,
                "MODASH_GIT_MERGETOOLS_DIR": str(project.path("drift")),
            })
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "tool:bc\n")
        self.assertEqual(drifted.returncode, 125, drifted.stdout)
        self.assertIn("observed environment drift: MODASH_GIT_MERGETOOLS_DIR", drifted.stdout)
        self.assertEqual(graph["environment"]["values"]["MODASH_GIT_HELPER"], "load_mergetool")

    def test_mkinitcpio_runtime_hook_dispatch_graph_replays_observed_hooks(self):
        with ScriptProject() as project:
            project.write("hooks/consolefont", 'printf "hook:consolefont\\n"\n')
            project.write("hooks/keymap", 'printf "hook:keymap\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    run_hookfunctions() {
                      for hook in "$@"; do
                        [ -r "$MODASH_MKINITCPIO_HOOK_ROOT/$hook" ] || continue
                        . "$MODASH_MKINITCPIO_HOOK_ROOT/$hook"
                      done
                    }
                    run_hookfunctions consolefont missing keymap
                    """),
            )
            env = {"MODASH_MKINITCPIO_HOOK_ROOT": str(project.path("hooks"))}
            output = project.path("compiled-static.sh")

            with self.assertRaises(NotImplementedError):
                project.compile("main.sh", output=output, mode="executable")
            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "hook:consolefont\nhook:keymap\n")
        self.assert_edge_names(graph, ["consolefont", "keymap"])

    def test_mkinitcpio_recursive_hook_dispatch_graph_replays_observed_hooks(self):
        with ScriptProject() as project:
            project.write("hooks/consolefont", 'printf "hook:consolefont\\n"\n')
            project.write("hooks/keymap", 'printf "hook:keymap\\n"\n')
            project.write(
                "main.sh",
                textwrap.dedent("""\
                    run_hookfunctions_recursive() {
                      if [ "$#" -eq 0 ]; then
                        return 0
                      fi
                      local hook="$1"
                      if [ -r "$MODASH_MKINITCPIO_HOOK_ROOT/$hook" ]; then
                        . "$MODASH_MKINITCPIO_HOOK_ROOT/$hook"
                      fi
                      shift
                      run_hookfunctions_recursive "$@"
                    }
                    run_hookfunctions_recursive consolefont missing keymap
                    """),
            )
            env = {"MODASH_MKINITCPIO_HOOK_ROOT": str(project.path("hooks"))}
            output = project.path("compiled-static.sh")

            with self.assertRaises(NotImplementedError):
                project.compile("main.sh", output=output, mode="executable")
            compiled, graph = self.compile_from_trace(project, "main.sh", env=env)
            result = project.run(compiled, env=env)
            self.assert_no_live_source(compiled)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "hook:consolefont\nhook:keymap\n")
        self.assert_edge_names(graph, ["consolefont", "keymap"])

    def compile_from_trace(self, project, entry, *, env=None, argv=None):
        entrypoint = project.path(entry)
        trace = project.trace(entry, env=env, argv=argv)
        graph = build_observed_source_graph(entrypoint, trace.observation)
        graph_path = project.path("graph/runtime-source-graph.json")
        compiled = project.path("compiled.sh")
        write_observed_source_graph(graph, graph_path)
        compile_observed_main(str(entrypoint), str(compiled), graph=str(graph_path))
        return compiled, graph

    def assert_edge_names(self, graph, names):
        self.assertEqual([Path(edge["resolved_path"]).name for edge in graph["edges"]], names)

    def assert_no_live_source(self, script):
        text = Path(script).read_text()
        live_source_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not re.match(r"^(?:source|\.)[ \t]+", stripped):
                continue
            if re.match(
                r"^(?:source|\.)[ \t]+[\"']?\$?\{?__modash_source_payload_[A-Fa-f0-9]{12}_file\}?[\"']?(?:[ \t]|$)",
                stripped,
            ):
                continue
            live_source_lines.append(line)
        self.assertEqual(live_source_lines, [])


if __name__ == "__main__":
    unittest.main()
