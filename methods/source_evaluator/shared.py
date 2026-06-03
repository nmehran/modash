from __future__ import annotations

import ast
import copy
import os
import re
from collections import Counter
from dataclasses import replace
from fnmatch import fnmatch
from pathlib import Path

from methods.shell.line import get_commands
from methods.shell.scan import (
    command_substitution_bodies,
    is_array_assignment_paren,
    process_substitution_bodies,
    read_balanced_body,
    subshell_bodies,
    top_level_pipeline_segments,
)
from methods.source_diagnostics import unsupported_source_error, with_source_diagnostic
from methods.source_conditions import (
    ConditionAtom,
    condition_status_and,
    condition_status_not,
    condition_status_or,
    source_logical_condition_atoms_from_text,
)
from methods.source_commands import (
    contains_nested_source_command,
    contains_source_command,
    shell_quote_words as quote_shell_words,
    shell_single_quote,
    source_command_index,
    source_command_invocation,
)
from methods.source_effects import (
    ArrayAssignment,
    Assignment,
    CaseBlock,
    CdCommand,
    CStyleForLoop,
    DisabledSourceSite,
    EvaluationResult,
    ExecutionModel,
    FunctionDef,
    ForLoop,
    IfBlock,
    LineReplacement,
    OccurrenceModel,
    RawCommand,
    SetCommand,
    SourceEvent,
    SourceSite,
    SourceLocation,
    StateSnapshot,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend, ParserFrontend
from methods.source_patterns import (
    UnsupportedPatternError,
    extglob_operator_at,
    read_extglob_body,
    shell_pattern_matches,
    split_extglob_alternatives,
)
from methods.source_resolver import (
    FailglobExpansionError,
    MISSING_SOURCE,
    MISSING_SOURCE_NO_FILENAME,
    SOURCE_EXPANSION_FAILURE,
    SOURCE_EXPANSION_FAILURE_RETURN,
    ResolvedSource,
    UnsupportedSourceError,
    extract_exact_command_substitution,
    expand_glob_word,
    has_unsupported_shell_operator,
    has_unquoted_brace_expansion,
    has_unquoted_extglob,
    has_unquoted_glob,
    is_missing_source_replacement_kind,
    is_source_expansion_failure_replacement_kind,
    missing_source_status,
    parse_shell_words_preserving_quotes,
    source_expansion_failure_result,
    strip_shell_word_quotes,
)
from methods.source_supplements import SourceSupplement, empty_source_supplement, supplement_skeleton
from methods.source_evaluator.state import *
from methods.sources import (
    SOURCE_RESOLVER,
    change_directory,
    resolve_command,
    resolve_shell_path_commands,
    resolve_variable_references,
    shell_utility_basename,
    shell_utility_dirname,
)
from methods.shell_text import strip_matching_quotes

ARRAY_INDEX_PATTERN = re.compile(r'\$\{([a-zA-Z_]\w*)\[(\d+)\]\}')
ARRAY_ANY_INDEX_PATTERN = re.compile(r'\$\{([a-zA-Z_]\w*)\[([^\]]+)\]\}')
ARRAY_EXPANSION_PATTERN = re.compile(r'^\$\{([a-zA-Z_]\w*)\[@\]\}$')
SCALAR_REFERENCE_PATTERN = re.compile(r'\$(?:\{([a-zA-Z_]\w*|[0-9]+)\}|([a-zA-Z_]\w*|[0-9]+))')
SCALAR_WORD_PATTERN = re.compile(r'^\$(?:\{([a-zA-Z_]\w*|[0-9]+)\}|([a-zA-Z_]\w*|[0-9]+))$')
ASSIGNMENT_WORD_PATTERN = re.compile(r'^[a-zA-Z_]\w*(?:\+)?=.*$')
DEFAULT_IFS = " \t\n"
MAX_MODELED_LOOP_ITERATIONS = 256
SHELL_OPTION_FLAGS = {
    'e': 'errexit',
    'E': 'errtrace',
    'f': 'noglob',
    'm': 'monitor',
    'u': 'nounset',
}
VALID_SET_FLAGS = frozenset("abefhkmnptuvxBCEHPT")
VALID_SET_OPTIONS = frozenset({
    'allexport',
    'braceexpand',
    'emacs',
    'errexit',
    'errtrace',
    'functrace',
    'hashall',
    'histexpand',
    'history',
    'ignoreeof',
    'interactive-comments',
    'keyword',
    'monitor',
    'noclobber',
    'noexec',
    'noglob',
    'nolog',
    'notify',
    'nounset',
    'onecmd',
    'physical',
    'pipefail',
    'posix',
    'privileged',
    'verbose',
    'vi',
    'xtrace',
})
SHOPT_SHELL_OPTIONS = frozenset({
    'lastpipe',
})
DEFAULT_ENABLED_SHOPT_OPTIONS = frozenset({
    'checkwinsize',
    'cmdhist',
    'complete_fullquote',
    'extquote',
    'force_fignore',
    'globasciiranges',
    'globskipdots',
    'hostcomplete',
    'interactive_comments',
    'patsub_replacement',
    'progcomp',
    'promptvars',
    'sourcepath',
})
GLOB_SHOPT_OPTIONS = frozenset({
    'dotglob',
    'extglob',
    'failglob',
    'globstar',
    'nocaseglob',
    'nullglob',
})
KNOWN_SHOPT_OPTIONS = frozenset({
    'assoc_expand_once',
    'autocd',
    'cdable_vars',
    'cdspell',
    'checkhash',
    'checkjobs',
    'checkwinsize',
    'cmdhist',
    'compat31',
    'compat32',
    'compat40',
    'compat41',
    'compat42',
    'compat43',
    'compat44',
    'complete_fullquote',
    'direxpand',
    'dirspell',
    'execfail',
    'expand_aliases',
    'extdebug',
    'extquote',
    'force_fignore',
    'globasciiranges',
    'globskipdots',
    'gnu_errfmt',
    'histappend',
    'histreedit',
    'histverify',
    'hostcomplete',
    'huponexit',
    'inherit_errexit',
    'interactive_comments',
    'lithist',
    'localvar_inherit',
    'localvar_unset',
    'login_shell',
    'mailwarn',
    'no_empty_cmd_completion',
    'nocasematch',
    'noexpand_translation',
    'patsub_replacement',
    'progcomp',
    'progcomp_alias',
    'promptvars',
    'restricted_shell',
    'shift_verbose',
    'sourcepath',
    'varredir_close',
    'xpg_echo',
}) | SHOPT_SHELL_OPTIONS | GLOB_SHOPT_OPTIONS
CONDITION_UNARY_FILE_OPERATORS = frozenset({'-e', '-f', '-d', '-r'})
CONDITION_UNARY_STRING_OPERATORS = frozenset({'-n', '-z'})
CONDITION_STRING_OPERATORS = frozenset({'=', '==', '!='})
CONDITION_INTEGER_OPERATORS = frozenset({'-eq', '-ne', '-gt', '-ge', '-lt', '-le'})
CONDITION_BINARY_OPERATORS = (
    CONDITION_STRING_OPERATORS
    | CONDITION_INTEGER_OPERATORS
    | frozenset({'=~'})
)
GREP_LITERAL_META_PATTERN = re.compile(r'[.\[\\*^$]')
POSIX_CLASS_PATTERN = re.compile(r'\[\[:[a-zA-Z_]+:\]\]')
PYTHON_ONLY_REGEX_PATTERN = re.compile(r'\(\?|\\[AbBdDsSwWZ]')
LAZY_REGEX_QUANTIFIER_PATTERN = re.compile(r'(?:[*+?]|\{[0-9]+(?:,[0-9]*)?\})\?')
QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS = frozenset({'"$@"', '"${@}"', '"$*"', '"${*}"'})
RETAINED_HELPER_POSITIONAL_SOURCE_EXPRESSIONS = QUOTED_ALL_POSITIONALS_SOURCE_EXPRESSIONS | frozenset({
    '"$1"',
    '"${1}"',
})
