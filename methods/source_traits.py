import os
import re

from methods.source_effects import (
    CaseBlock,
    CStyleForLoop,
    ForLoop,
    FunctionDef,
    IfBlock,
    RawCommand,
    SetCommand,
    WhileLoop,
)
from methods.source_frontend import LineParserFrontend


def raw_command_is_return(node: RawCommand):
    return bool(re.match(r"^return(?:\s|$)", node.text.strip()))


def raw_command_is_shift(node: RawCommand):
    return bool(re.match(r"^shift(?:\s|$)", node.text.strip()))


def raw_command_is_eval(node: RawCommand):
    return bool(re.match(r"^eval(?:\s|$)", node.text.strip()))


def raw_command_eval_assigns_positionals(node: RawCommand):
    text = node.text.strip()
    if not re.match(r"^eval(?:\s|$)", text):
        return False
    return bool(re.search(r"(?:^|[\s;&|({\"'])set(?:\s|$|[\"'])", text))


def raw_command_is_simple_shift(node: RawCommand):
    return bool(re.fullmatch(r"shift(?:\s+\d+)?", node.text.strip()))


def set_command_assigns_positionals(node: SetCommand):
    arguments = node.arguments
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return True
        if not argument.startswith(("-", "+")):
            return True
        if argument in {"-o", "+o"}:
            if index + 1 >= len(arguments):
                return False
            index += 2
            continue
        if argument in {"-", "+"}:
            return False
        index += 1
    return False


def set_command_is_simple_positional_assignment(node: SetCommand):
    return bool(node.arguments) and node.arguments[0] == "--"


def nodes_have_top_level_return(nodes):
    for node in nodes:
        if isinstance(node, RawCommand) and raw_command_is_return(node):
            return True
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if nodes_have_top_level_return(node.body):
                return True
        elif isinstance(node, IfBlock):
            if any(nodes_have_top_level_return(branch.body) for branch in node.branches):
                return True
        elif isinstance(node, CaseBlock):
            if any(nodes_have_top_level_return(arm.body) for arm in node.arms):
                return True
    return False


def nodes_have_top_level_positional_mutation(nodes):
    for node in nodes:
        if isinstance(node, RawCommand) and raw_command_is_shift(node):
            return True
        if isinstance(node, RawCommand) and raw_command_is_eval(node):
            return True
        if isinstance(node, SetCommand) and set_command_assigns_positionals(node):
            return True
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if nodes_have_top_level_positional_mutation(node.body):
                return True
        elif isinstance(node, IfBlock):
            if any(nodes_have_top_level_positional_mutation(branch.body) for branch in node.branches):
                return True
        elif isinstance(node, CaseBlock):
            if any(nodes_have_top_level_positional_mutation(arm.body) for arm in node.arms):
                return True
    return False


def nodes_have_top_level_positional_assignment(nodes):
    for node in nodes:
        if isinstance(node, RawCommand) and raw_command_eval_assigns_positionals(node):
            return True
        if isinstance(node, SetCommand) and set_command_assigns_positionals(node):
            return True
        if isinstance(node, FunctionDef):
            continue
        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if nodes_have_top_level_positional_assignment(node.body):
                return True
        elif isinstance(node, IfBlock):
            if any(nodes_have_top_level_positional_assignment(branch.body) for branch in node.branches):
                return True
        elif isinstance(node, CaseBlock):
            if any(nodes_have_top_level_positional_assignment(arm.body) for arm in node.arms):
                return True
    return False


def file_top_level_source_traits(filepath: str, content: str):
    has_return_text = "return" in content
    has_positional_mutation_text = bool(re.search(r"\b(?:eval|set|shift)\b", content))
    if not has_return_text and not has_positional_mutation_text:
        return False, False
    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    return (
        nodes_have_top_level_return(ir.nodes) if has_return_text else False,
        nodes_have_top_level_positional_mutation(ir.nodes) if has_positional_mutation_text else False,
    )


def file_has_top_level_positional_assignment(filepath: str, content: str):
    if not re.search(r"\b(?:eval|set)\b", content):
        return False
    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    return nodes_have_top_level_positional_assignment(ir.nodes)
