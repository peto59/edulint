from edulint.linting.checkers.basic_checker import ModifiedListener
from edulint.linting.analyses.cfg.visitor import CFGVisitor
from edulint.linting.analyses.cfg.utils import successors_from_loc
from edulint.linting.analyses.variable_modification import VarModificationAnalysis, VarEventType
from edulint.linting.checkers.utils import get_name

from typing import List, Tuple, Dict
import astroid  # type: ignore
from astroid import nodes as astns
import pytest


def split_n_lines(nodes: List[astns.NodeNG], lines: int) -> Tuple[List[astns.NodeNG], List[astns.NodeNG]]:
    if lines == 0:
        return [], nodes

    consumed_lines = []
    for i, node in enumerate(nodes):
        consumed_lines.append(node)
        if hasattr(node, "body"):
            consumed_lines_rec, result_rec = split_n_lines(node.body, lines - len(consumed_lines))
            consumed_lines += consumed_lines_rec
        else:
            result_rec = []

        if len(consumed_lines) == lines:
            return consumed_lines, result_rec + nodes[i+1:]
    return consumed_lines, []


@pytest.mark.filterwarnings("ignore:The 'Module.doc' attribute")
@pytest.mark.parametrize("program,init_lines,before_types,after_types", [
    ("x = 5", 1, [astns.Assign], []),
    ("""
x = 5
x += 1""", 1, [astns.Assign], [astns.AugAssign]),
    ("""
x = 5
x = 1""", 1, [astns.Assign], [astns.Assign]),
    ("""
x = 5
y = 1
x += 1""", 2, [astns.Assign, astns.Assign], [astns.AugAssign]),
    ("""
for x in range(10):
    x = 1""", 1, [astns.For], [astns.Assign]),
    ("""
for x in range(10):
    for y in range(11):
        x = 1""", 1, [astns.For], [astns.For]),
    ("""
for x in range(10):
    for y in range(11):
        x = 1""", 2, [astns.For, astns.For], [astns.Assign]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1""", 2, [astns.For, astns.Assign], [astns.For]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1""", 1, [astns.For], [astns.Assign, astns.For]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1
    x += 1""", 1, [astns.For], [astns.Assign, astns.For, astns.AugAssign]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1
    x += 1""", 2, [astns.For, astns.Assign], [astns.For, astns.AugAssign]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1
    x += 1""", 3, [astns.For, astns.Assign, astns.For], [astns.Assign, astns.AugAssign]),
    ("""
for x in range(10):
    x = 1
    for y in range(11):
        x = 1
    x += 1""", 4, [astns.For, astns.Assign, astns.For, astns.Assign], [astns.AugAssign]),
])
def test_split_n_lines(program: str, init_lines: int, before_types: List[type], after_types: List[type]):
    module = astroid.parse(program)
    before, after = split_n_lines(module.body, init_lines)

    def compare(nodes: List[astns.NodeNG], node_types: List[type]):
        assert len(nodes) == len(node_types)

        for node, node_type in zip(nodes, node_types):
            assert isinstance(node, node_type)

    compare(before, before_types)
    compare(after, after_types)


@pytest.mark.parametrize("program,init_lines,modified", [
    (["x = 5"], 1, {"x": False}),
    ([
        "x = 5",
        "x += 1"
    ], 1, {"x": True}),
    ([
        "x = 5",
        "x = 1"
    ], 1, {"x": True}),
    ([
        "x = 5",
        "y = 1",
        "x += 1"
    ], 2, {"x": True, "y": False}),
    ([
        "for x in range(10):",
        "    for x in range(11):",
        "        pass"
    ], 1, {"x": True}),
    ([
        "for x in range(10):",
        "    for y in range(11):",
        "        pass"
    ], 1, {"x": False}),
    ([
        "x = 0",
        "def foo():",
        "    global x",
        "    x = 1"
    ], 1, {"x": True}),
    ([
        "x = 0",
        "def foo():",
        "    global x",
        "    x += 1"
    ], 1, {"x": True}),
    ([
        "x = 0",
        "def foo():",
        "    nonlocal x",
        "    x = 1"
    ], 1, {"x": True}),
    ([
        "x = 0",
        "def foo():",
        "    x = 1",
        "    def bar():",
        "        nonlocal x",
        "        x = 2"
    ], 1, {"x": False}),
    ([
        "x = []",
        "def foo():",
        "    x.append(1)"
    ], 1, {"x": True}),
    ([
        "x = []",
        "def foo():",
        "    x[0] = 1"
    ], 1, {"x": True}),
    ([
        "x = None",
        "def foo():",
        "    x.y = 1"
    ], 1, {"x": True}),
    ([
        "x = None",
        "def foo():",
        "    x.y.z = 1"
    ], 1, {"x": True}),
    ([
        "x = None",
        "def foo():",
        "    x[0].y[1].z = 1"
    ], 1, {"x": True}),
    ([
        "x = None",
        "def foo():",
        "    x = []",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([
        "x = None",
        "def foo():",
        "    x, y = [], 0",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([
        "x = None",
        "def foo():",
        "    (x, y) = [], 0",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([
        "x = None",
        "def foo(x):",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([
        "x = None",
        "def foo(x: List[int] = []):",
        "    x.append(0)"
    ], 1, {"x": False}),
])
def test_modified_listener(program: List[str], init_lines: int, modified: Dict[str, bool]):
    module = astroid.parse("\n".join(program))
    before, after = split_n_lines(module.body, init_lines)

    def extract_vars(nodes: List[astns.NodeNG]) -> List[astns.NodeNG]:
        result = []
        for node in nodes:
            if hasattr(node, "target"):
                result.append(node.target)
            if hasattr(node, "targets"):
                result.extend(node.targets)
        return result

    watched = extract_vars(before)
    listener: ModifiedListener = ModifiedListener(watched)
    listener.visit_many(after)
    assert {get_name(n): listener.was_modified(n, allow_definition=False) for n in watched} == modified


@pytest.mark.parametrize("program,init_lines,modified", [
    (["x = 5"], 1, {"x": False}), # 0
    ([ # 1
        "x = 5",
        "x += 1"
    ], 1, {"x": True}),
    ([ # 2
        "x = 5",
        "x = 1"
    ], 1, {"x": True}),
    ([ # 3
        "x = 5",
        "y = 1",
        "x += 1"
    ], 2, {"x": True, "y": False}),
    ([ # 4
        "for x in range(10):",
        "    for x in range(11):",
        "        pass"
    ], 1, {"x": True}),
    ([ # 5
        "for x in range(10):",
        "    for y in range(11):",
        "        pass"
    ], 1, {"x": False}),
    ([ # 6
        "x = 0",
        "def foo():",
        "    global x",
        "    x = 1"
    ], 1, {"x": True}),
    ([ # 7
        "x = 0",
        "def foo():",
        "    global x",
        "    x += 1"
    ], 1, {"x": True}),
    ([ # 8
        "x = 0",
        "def foo():",
        "    nonlocal x",
        "    x = 1"
    ], 1, {"x": True}),
    ([ # 9
        "x = 0",
        "def foo():",
        "    x = 1",
        "    def bar():",
        "        nonlocal x",
        "        x = 2"
    ], 1, {"x": False}),
    ([ # 10
        "x = []",
        "def foo():",
        "    x.append(1)"
    ], 1, {"x": True}),
    ([ # 11
        "x = []",
        "def foo():",
        "    x[0] = 1"
    ], 1, {"x": True}),
    ([ # 12
        "x = None",
        "def foo():",
        "    x.y = 1"
    ], 1, {"x": True}),
    ([ # 13
        "x = None",
        "def foo():",
        "    x.y.z = 1"
    ], 1, {"x": True}),
    ([ # 14
        "x = None",
        "def foo():",
        "    x[0].y[1].z = 1"
    ], 1, {"x": True}),
    ([ # 15
        "x = None",
        "def foo():",
        "    x = []",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([ # 16
        "x = None",
        "def foo():",
        "    x, y = [], 0",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([ # 17
        "x = None",
        "def foo():",
        "    (x, y) = [], 0",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([ # 18
        "x = None",
        "def foo(x):",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([ # 19
        "x = None",
        "def foo(x: List[int] = []):",
        "    x.append(0)"
    ], 1, {"x": False}),
    ([ # 20
        "x = 0",
        "if False:",
        "    x = 1",
    ], 1, {"x": True}),
    ([  # 21
        "x = 0",
        "while x < 10:",
        "    x += 1",
    ], 1, {"x": True}),
    ([  # 22
        "x = 0",
        "try:",
        "    y = 0",
        "except:",
        "    x = 1",
    ], 1, {"x": True}),
    ([  # 23
        "x = 0",
        "try:",
        "    y = 0",
        "except Exception:",
        "    x = 1",
    ], 1, {"x": True}),
    ([  # 24
        "x = 0",
        "try:",
        "    y = 0",
        "except Exception as e:",
        "    x = 1",
    ], 1, {"x": True}),
    ([  # 25
        "x = 0",
        "try:",
        "    y = 0",
        "except Exception as x:",
        "    y = 1",
    ], 1, {"x": True, "y": True}),
    ([  # 26
        "x = 0",
        "with open('f.txt') as x:",
        "    pass"
    ], 1, {"x": True}),
])
def test_var_event_listener(program: List[str], init_lines: int, modified: Dict[str, bool]):
    module = astroid.parse("\n".join(program))
    module.accept(CFGVisitor())

    def was_modified(in_varname: str) -> bool:
        in_scope = None
        for stmt in successors_from_loc(module.body[0].cfg_loc, explore_functions=True, include_start=True):
            for (varname, scope), event in stmt.var_events.items():
                if in_varname == varname:
                    if in_scope is None and event.type == VarEventType.ASSIGN:
                        in_scope = scope
                    if in_scope == scope and event.type in (VarEventType.REASSIGN, VarEventType.MODIFY):
                        return True
        return False

    VarModificationAnalysis.collect(module)
    for varname, mod in modified.items():
        assert was_modified(varname) == mod
