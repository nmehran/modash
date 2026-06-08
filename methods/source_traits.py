import os
import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class FunctionContextSourceTraits:
    references_funcname: bool = False
    invokes_caller: bool = False
    references_bash_lineno: bool = False


def raw_command_is_return(node: RawCommand):
    return bool(re.match(r"^return(?:\s|$)", node.text.strip()))


def raw_command_is_shift(node: RawCommand):
    return bool(re.match(r"^shift(?:\s|$)", node.text.strip()))


def raw_command_is_eval(node: RawCommand):
    return bool(re.match(r"^eval(?:\s|$)", node.text.strip()))


def raw_command_is_caller(node: RawCommand):
    return text_invokes_caller_command(node.text)


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


def file_top_level_function_context_traits(filepath: str, content: str):
    has_funcname_text = "FUNCNAME" in content
    has_caller_text = bool(re.search(r"\bcaller\b", content))
    has_bash_lineno_text = "BASH_LINENO" in content
    if not (has_funcname_text or has_caller_text or has_bash_lineno_text):
        return FunctionContextSourceTraits()

    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    return nodes_have_top_level_function_context_traits(ir.nodes)


def nodes_have_top_level_function_context_traits(nodes):
    traits = FunctionContextSourceTraits()
    for node in nodes:
        if isinstance(node, FunctionDef):
            traits = merge_function_context_traits(
                traits,
                nodes_have_top_level_function_context_traits(node.body),
            )
            continue

        node_traits = node_function_context_traits(node)
        traits = merge_function_context_traits(traits, node_traits)

        if isinstance(node, (ForLoop, CStyleForLoop, WhileLoop)):
            if isinstance(node, WhileLoop):
                traits = merge_function_context_traits(
                    traits,
                    command_text_function_context_traits(node.condition),
                )
            traits = merge_function_context_traits(
                traits,
                nodes_have_top_level_function_context_traits(node.body),
            )
        elif isinstance(node, IfBlock):
            for branch in node.branches:
                traits = merge_function_context_traits(
                    traits,
                    command_text_function_context_traits(branch.condition_text),
                )
                traits = merge_function_context_traits(
                    traits,
                    nodes_have_top_level_function_context_traits(branch.body),
                )
        elif isinstance(node, CaseBlock):
            traits = merge_function_context_traits(
                traits,
                text_function_context_traits(node.subject),
            )
            for arm in node.arms:
                traits = merge_function_context_traits(
                    traits,
                    nodes_have_top_level_function_context_traits(arm.body),
                )
    return traits


def node_function_context_traits(node):
    invokes_caller = isinstance(node, RawCommand) and text_invokes_caller_command(node.text)
    return merge_function_context_traits(
        text_function_context_traits(getattr(node, "text", "")),
        FunctionContextSourceTraits(invokes_caller=invokes_caller),
    )


def command_text_function_context_traits(text: str):
    return merge_function_context_traits(
        text_function_context_traits(text),
        FunctionContextSourceTraits(invokes_caller=text_invokes_caller_command(text)),
    )


def text_invokes_caller_command(text: str):
    return bool(re.match(r"^(?:(?:command|builtin)\s+)?caller(?:\s|$)", text.strip()))


def text_function_context_traits(text: str):
    return FunctionContextSourceTraits(
        references_funcname=bool(re.search(r"\bFUNCNAME\b", text)),
        references_bash_lineno=bool(re.search(r"\bBASH_LINENO\b", text)),
    )


def merge_function_context_traits(left, right):
    return FunctionContextSourceTraits(
        references_funcname=left.references_funcname or right.references_funcname,
        invokes_caller=left.invokes_caller or right.invokes_caller,
        references_bash_lineno=left.references_bash_lineno or right.references_bash_lineno,
    )


def file_has_top_level_positional_assignment(filepath: str, content: str):
    if not re.search(r"\b(?:eval|set)\b", content):
        return False
    ir = LineParserFrontend().parse(os.path.abspath(filepath), content)
    return nodes_have_top_level_positional_assignment(ir.nodes)
