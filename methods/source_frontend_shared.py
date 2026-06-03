from __future__ import annotations

import re
from pathlib import Path

from methods.shell_assignments import VARIABLE_ASSIGNMENT_PATTERN
from methods.shell_text import remove_comments
from methods.source_commands import SOURCE_PATTERN
from methods.source_effects import (
    ArrayAssignment,
    Assignment,
    CaseArm,
    CaseBlock,
    CdCommand,
    CStyleForLoop,
    FunctionDef,
    ForLoop,
    IfBlock,
    IfBranch,
    RawCommand,
    ScriptIR,
    SetCommand,
    SourceLocation,
    SourceSite,
    WhileLoop,
)
from methods.source_patterns import extglob_operator_at
from methods.source_commands import (
    contains_nested_source_command,
    contains_source_command,
    source_command_index,
    source_command_invocation,
)
from methods.source_resolver import (
    ends_unsupported_control_block,
    extract_heredoc_delimiters,
    is_unsupported_control_flow_source,
    is_heredoc_end,
    parse_shell_words,
    parse_shell_words_preserving_quotes,
    strip_shell_word_quotes,
    starts_unsupported_control_block,
    UnsupportedSourceError,
)
from methods.shell.line import first_top_level_pipeline_index, get_commands

ARRAY_ASSIGNMENT_PATTERN = re.compile(r'^(?:(declare)\s+(-[aA])\s+)?([a-zA-Z_]\w*)(\+?)=\((.*)\)$')
ARRAY_INDEX_ASSIGNMENT_PATTERN = re.compile(r'^([a-zA-Z_]\w*)\[([^\]]+)\](\+?)=(.*)$')
FUNCTION_HEADER_PATTERN = re.compile(
    r'^\s*(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*\{\s*(.*)$'
)
FUNCTION_SIGNATURE_PATTERN = re.compile(
    r'^\s*(?:(?:function\s+([a-zA-Z_]\w*)(?:\s*\(\s*\))?)|([a-zA-Z_]\w*)\s*\(\s*\))\s*$'
)
FUNCTION_OPEN_PATTERN = re.compile(r'^\s*\{\s*(.*)$')
FOR_LOOP_PATTERN = re.compile(r'^\s*for\s+([a-zA-Z_]\w*)\s+in\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
FOR_HEADER_PATTERN = re.compile(r'^\s*for\s+([a-zA-Z_]\w*)\s+in\s+(.+?)\s*$')
C_FOR_LOOP_PATTERN = re.compile(r'^\s*for\s*\(\(\s*(.*?)\s*;\s*(.*?)\s*;\s*(.*?)\s*\)\)\s*;\s*do(?:\s*(.*))?$')
C_FOR_HEADER_PATTERN = re.compile(r'^\s*for\s*\(\(\s*(.*?)\s*;\s*(.*?)\s*;\s*(.*?)\s*\)\)\s*$')
WHILE_LOOP_PATTERN = re.compile(r'^\s*(while|until)\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
WHILE_HEADER_PATTERN = re.compile(r'^\s*(while|until)\s+(.+?)\s*$')
PIPELINE_WHILE_LOOP_PATTERN = re.compile(r'^\s*(.+?)\s*\|\s*while\s+(.+?)\s*;\s*do(?:\s*(.*))?$')
PIPELINE_WHILE_HEADER_PATTERN = re.compile(r'^\s*(.+?)\s*\|\s*while\s+(.+?)\s*$')
DO_LINE_PATTERN = re.compile(r'^\s*do\s*$')
INLINE_DONE_PATTERN = re.compile(r'^(.*?)(?:;\s*)?done(?:\s+(.*))?$')
IF_COMMAND_PATTERN = re.compile(r'^\s*if\s+(.+?)\s*$')
ELIF_COMMAND_PATTERN = re.compile(r'^\s*elif\s+(.+?)\s*$')
IF_INLINE_THEN_PATTERN = re.compile(r'^\s*((?:if|elif)\s+.+?)\s*;\s*then(?:\s*(.*))?\s*$')
THEN_COMMAND_PATTERN = re.compile(r'^\s*then(?:\s+(.+?))?\s*$')
ELSE_COMMAND_PATTERN = re.compile(r'^\s*else(?:\s+(.+?))?\s*$')
FI_COMMAND_PATTERN = re.compile(r'^\s*fi\s*$')
ESAC_COMMAND_PATTERN = re.compile(r'^\s*esac\s*$')
CASE_TERMINATOR_COMMANDS = {
    "__MODASH_CASE_TERM_END__": ";;",
    "__MODASH_CASE_TERM_FALLTHROUGH__": ";&",
    "__MODASH_CASE_TERM_FALLTHROUGH_TEST__": ";;&",
}

