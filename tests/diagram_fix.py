"""Test-only grammar check for the Mermaid subset `reader/diagram.py` writes. Not a test file itself.

`mermaid_ok(text)` accepts exactly: the header `flowchart TD|LR`, node lines, edge lines (optionally labelled),
`subgraph cK ["title"]` and `end` (balanced), and `%%` comments; a node is declared at most once and only when an
edge names it, and an edge may name a node never declared (a blank one outside every subgraph: Mermaid makes it
from its first use).
"""
import re

_HEADER = re.compile(r"flowchart (TD|LR)")
_NODE = re.compile(r'\s*(n\d+)(\["[^"\n]*"\]|\{"[^"\n]*"\}|\(\["[^"\n]*"\]\)|\(\("[^"\n]*"\)\)|\[\("[^"\n]*"\)\])')
_EDGE = re.compile(r'\s*(n\d+) (-->|<-->|---)(\|"[^"\n]*"\|)? (n\d+)')
_SUB = re.compile(r'\s*subgraph c\d+ \["[^"\n]*"\]')
_END = re.compile(r"\s*end")
_COMMENT = re.compile(r"\s*%% .*")


def mermaid_ok(text: str) -> bool:
    lines = text.split("\n")
    if not lines or not _HEADER.fullmatch(lines[0]):
        return False
    declared, used, depth = set(), [], 0
    for line in lines[1:]:
        if mo := _NODE.fullmatch(line):
            if mo.group(1) in declared:
                return False
            declared.add(mo.group(1))
        elif mo := _EDGE.fullmatch(line):
            used += [mo.group(1), mo.group(4)]
        elif _SUB.fullmatch(line):
            depth += 1
        elif _END.fullmatch(line):
            depth -= 1
            if depth < 0:
                return False
        elif not _COMMENT.fullmatch(line):
            return False
    return depth == 0 and declared <= set(used)
