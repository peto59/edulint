from astroid import nodes  # type: ignore
from collections import namedtuple
from typing import TYPE_CHECKING, Optional, Tuple, List, Union, Set, Any, Dict, Generator

from pylint.checkers import BaseChecker  # type: ignore
from pylint.checkers.utils import only_required_for_messages

if TYPE_CHECKING:
    from pylint.lint import PyLinter  # type: ignore

from edulint.linting.analyses.antiunify import antiunify, AunifyVar, core_as_string
from edulint.linting.analyses.variable_modification import VarEventType
from edulint.linting.analyses.reaching_definitions import (
    vars_in,
    is_changed_between,
    get_vars_defined_before,
    get_vars_used_after,
    get_control_statements,
)
from edulint.linting.analyses.cfg.utils import (
    get_cfg_loc,
    get_stmt_locs,
    syntactic_children_locs_from,
    successors_from_loc,
)
from edulint.linting.checkers.utils import (
    is_parents_elif,
    BaseVisitor,
    is_any_assign,
    get_lines_between,
    is_main_block,
    is_block_comment,
    get_statements_count,
    eprint,
    cformat,  # noqa: F401
    get_token_count,
    has_else_block,
)


class InvalidExpression(Exception):
    pass


class DuplicateExprVisitor(BaseVisitor[None]):
    EXPR_FUNCTIONS = {
        "abs",
        "max",
        "min",
        "round",
        "sqrt",
        "len",
        "all",
        "any",
        "sum",
        "map",
        "filter",
        "sorted",
        "reversed",
        "int",
        "str",
        "ord",
        "chr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.long_expressions: Dict[str, List[nodes.NodeNG]] = {}

    @classmethod
    def _compute_complexity(cls, node: nodes.NodeNG) -> int:
        fun = cls._compute_complexity

        if isinstance(node, (nodes.Attribute, nodes.Subscript)) and is_any_assign(node.parent):
            raise InvalidExpression

        if isinstance(node, nodes.Call) and node.func.as_string() not in cls.EXPR_FUNCTIONS:
            raise InvalidExpression

        if isinstance(node, nodes.BinOp):
            return 2 + fun(node.left) + fun(node.right)

        if isinstance(node, nodes.BoolOp):
            return len(node.values) - 1 + sum(map(fun, node.values))

        if isinstance(node, nodes.Compare):
            return fun(node.left) + sum(1 + fun(op) for _, op in node.ops)

        if isinstance(node, (nodes.Name, nodes.Const)):
            return 1

        if isinstance(node, nodes.Attribute):
            return 1 + sum(map(fun, node.get_children()))

        return sum(map(fun, node.get_children()))

    def _process_expr(self, node: Optional[nodes.NodeNG]) -> None:
        if node is None:
            return

        try:
            complexity = DuplicateExprVisitor._compute_complexity(node)
        except InvalidExpression:
            self.visit_many(node.get_children())
            return

        if complexity >= 8:  # TODO extract into parameter
            others = self.long_expressions.get(node.as_string(), [])
            others.append(node)
            self.long_expressions[node.as_string()] = others

            self.visit_many(node.get_children())

    def visit_assert(self, node: nodes.Assert) -> None:
        return

    def visit_attribute(self, node: nodes.Attribute) -> None:
        self._process_expr(node)

    def visit_binop(self, node: nodes.BinOp) -> None:
        self._process_expr(node)

    def visit_boolop(self, node: nodes.BoolOp) -> None:
        self._process_expr(node)

    def visit_call(self, node: nodes.Call) -> None:
        self._process_expr(node)

    def visit_compare(self, node: nodes.Compare) -> None:
        self._process_expr(node)

    def visit_dict(self, node: nodes.Dict) -> None:
        self._process_expr(node)

    def visit_dictcomp(self, node: nodes.DictComp) -> None:
        self._process_expr(node)

    def visit_ifexp(self, node: nodes.IfExp) -> None:
        self._process_expr(node)

    def visit_joinedstr(self, node: nodes.JoinedStr) -> None:
        self._process_expr(node)

    def visit_lambda(self, node: nodes.Lambda) -> None:
        self._process_expr(node)

    def visit_list(self, node: nodes.List) -> None:
        self._process_expr(node)

    def visit_listcomp(self, node: nodes.ListComp) -> None:
        self._process_expr(node)

    # def visit_name(self, node: nodes.Name) -> None:
    #     self._process_expr(node)

    def visit_set(self, node: nodes.Set) -> None:
        self._process_expr(node)

    def visit_setcomp(self, node: nodes.SetComp) -> None:
        self._process_expr(node)

    def visit_starred(self, node: nodes.Starred) -> None:
        self._process_expr(node)

    def visit_subscript(self, node: nodes.Subscript) -> None:
        self._process_expr(node)

    def visit_tuple(self, node: nodes.Tuple) -> None:
        self._process_expr(node)

    def visit_unaryop(self, node: nodes.UnaryOp) -> None:
        self._process_expr(node)


class CollectBlocksVisitor(BaseVisitor[None]):
    def __init__(self) -> None:
        self.blocks: List[List[nodes.NodeNG]] = []

    def visit_if(self, node: nodes.If) -> None:
        self.blocks.append(node.body)

        if len(node.orelse) > 0 and not node.has_elif_block():
            self.blocks.append(node.orelse)

        self.visit_many(node.body)
        self.visit_many(node.orelse)

    def visit_loop(self, node: Union[nodes.For, nodes.While]) -> None:
        self.blocks.append(node.body)

        if len(node.orelse) > 0:
            self.blocks.append(node.orelse)

        self.visit_many(node.body)
        self.visit_many(node.orelse)

    def visit_for(self, node: nodes.For) -> None:
        self.visit_loop(node)

    def visit_while(self, node: nodes.While) -> None:
        self.visit_loop(node)

    def visit_with(self, node: nodes.With) -> None:
        self.blocks.append(node.body)
        self.visit_many(node.body)

    def visit_tryexcept(self, node: nodes.TryExcept) -> None:
        self.blocks.append(node.body)

        if len(node.handlers) > 0:
            self.blocks.extend(h.body for h in node.handlers)

        if len(node.orelse) > 0:
            self.blocks.append(node.orelse)

        self.visit_many(node.body)
        self.visit_many([stmt for h in node.handlers for stmt in h.body])
        self.visit_many(node.orelse)

    def visit_functiondef(self, node: nodes.FunctionDef) -> None:
        self.blocks.append(node.body)

        self.visit_many(node.body)

    def visit_module(self, node: nodes.Module) -> None:
        self.blocks.append(
            [
                b
                for b in node.body
                if b
                and not isinstance(b, (nodes.FunctionDef, nodes.ClassDef))
                and not is_main_block(b)
            ]
        )

        self.visit_many(node.body)


class NoDuplicateCode(BaseChecker):  # type: ignore
    name = "no-duplicate-code"
    msgs = {
        "R6502": (
            "Identical code inside all if's branches, move %d lines %s the if.",
            "duplicate-if-branches",
            "Emitted when identical code starts or ends all branches of an if statement.",
        ),
        "R6503": (
            "Identical code inside %d consecutive ifs, join their conditions using 'or'.",
            "duplicate-seq-ifs",
            "Emitted when several consecutive if statements have identical bodies and thus can be "
            "joined by or in their conditions.",
        ),
        "R6504": (
            "A complex expression '%s' used repeatedly (on lines %s). Extract it to a local variable or "
            "create a helper function.",
            "duplicate-exprs",
            "Emitted when an overly complex expression is used multiple times.",
        ),
        "R6505": (
            "Duplicate blocks starting on lines %s. Extract the code to a helper function.",
            "duplicate-blocks",
            "Emitted when there are duplicate blocks of code as a body of an if/elif/else/for/while/with/try-except "
            "block.",
        ),
        "R6506": (
            "Duplicate sequence of %d repetitions of %d lines of code. Use a loop to avoid this.",
            "duplicate-sequence",
            "Emitted when there is a sequence of similar sub-blocks inside a block that can be replaced by a loop.",
        ),
    }

    @only_required_for_messages("duplicate-if-branches", "duplicate-seq-ifs")
    def visit_if(self, node: nodes.If) -> None:
        self.duplicate_if_branches(node)
        self.duplicate_seq_ifs(node)

    def duplicate_if_branches(self, node: nodes.If) -> None:
        def extract_branch_bodies(node: nodes.If) -> Optional[List[nodes.NodeNG]]:
            branches = [node.body]
            current = node
            while current.has_elif_block():
                elif_ = current.orelse[0]
                if not elif_.orelse:
                    return None

                branches.append(elif_.body)
                current = elif_
            branches.append(current.orelse)
            return branches

        def get_stmts_difference(branches: List[nodes.NodeNG], forward: bool) -> int:
            reference = branches[0]
            compare = branches[1:]
            for i in range(min(map(len, branches))):
                for branch in compare:
                    index = i if forward else -i - 1
                    if reference[index].as_string() != branch[index].as_string():
                        return i
            return i + 1

        def add_message(
            branches: List[nodes.NodeNG],
            stmts_difference: int,
            defect_node: nodes.NodeNG,
            forward: bool = True,
        ) -> None:
            reference = branches[0]
            first = reference[0 if forward else -stmts_difference]
            last = reference[stmts_difference - 1 if forward else -1]
            lines_difference = get_lines_between(first, last, including_last=True)

            self.add_message(
                "duplicate-if-branches",
                node=defect_node,
                args=(lines_difference, "before" if forward else "after"),
            )

        if not node.orelse or is_parents_elif(node):
            return

        branches = extract_branch_bodies(node)
        if branches is None:
            return

        same_prefix_len = get_stmts_difference(branches, forward=True)
        if same_prefix_len >= 1:
            add_message(branches, same_prefix_len, node, forward=True)
            if any(same_prefix_len == len(b) for b in branches):
                return

        same_suffix_len = get_stmts_difference(branches, forward=False)
        if same_suffix_len >= 1:
            # allow early returns
            if same_suffix_len == 1 and isinstance(branches[0][-1], nodes.Return):
                i = 0
                while len(branches[i]) == 1:
                    i += 1
                branches = branches[i:]
                if len(branches) < 2:
                    return
            defect_node = branches[0][-1].parent

            # disallow breaking up coherent segments
            same_part = branches[0][-same_suffix_len:]
            if (
                get_statements_count(same_part, include_defs=True, include_name_main=True)
                / (
                    min(
                        get_statements_count(branch, include_defs=True, include_name_main=True)
                        for branch in branches
                    )
                    - same_prefix_len
                )
                < 1 / 2
            ):  # TODO extract into parameter
                return

            add_message(branches, same_suffix_len, defect_node, forward=False)

    def duplicate_seq_ifs(self, node: nodes.If) -> None:
        """
        returns False iff elifs end with else
        """

        def extract_from_elif(node: nodes.If, seq_ifs: List[nodes.NodeNG]) -> bool:
            if len(node.orelse) > 0 and not node.has_elif_block():
                return False

            current = node
            while current.has_elif_block():
                elif_ = current.orelse[0]
                seq_ifs.append(elif_)
                if len(elif_.orelse) > 0 and not elif_.has_elif_block():
                    return False
                current = elif_
            return True

        def extract_from_siblings(node: nodes.If, seq_ifs: List[nodes.NodeNG]) -> None:
            sibling = node.next_sibling()
            while sibling is not None and isinstance(sibling, nodes.If):
                new: List[nodes.NodeNG] = []
                if not extract_from_elif(sibling, new):
                    return
                seq_ifs.append(sibling)
                seq_ifs.extend(new)
                sibling = sibling.next_sibling()

        def same_ifs_count(seq_ifs: List[nodes.NodeNG], start: int) -> int:
            reference = seq_ifs[start].body
            for i in range(start + 1, len(seq_ifs)):
                # do not suggest join of elif and sibling
                if seq_ifs[start].parent not in seq_ifs[i].node_ancestors():
                    return i - start

                compared = seq_ifs[i].body
                if len(reference) != len(compared):
                    return i - start
                for j in range(len(reference)):
                    if reference[j].as_string() != compared[j].as_string():
                        return i - start
            return len(seq_ifs) - start

        prev_sibling = node.previous_sibling()
        if is_parents_elif(node) or (
            isinstance(prev_sibling, nodes.If) and extract_from_elif(prev_sibling, [])
        ):
            return

        seq_ifs = [node]

        if not extract_from_elif(node, seq_ifs):
            return
        extract_from_siblings(node, seq_ifs)

        if len(seq_ifs) == 1:
            return

        i = 0
        while i < len(seq_ifs) - 1:
            count = same_ifs_count(seq_ifs, i)
            if count > 1:
                first = seq_ifs[i]
                assert isinstance(seq_ifs[i + count - 1], nodes.If)
                last = seq_ifs[i + count - 1].body[-1]

                self.add_message(
                    "duplicate-seq-ifs",
                    line=first.fromlineno,
                    col_offset=first.col_offset,
                    end_lineno=last.tolineno,
                    end_col_offset=last.end_col_offset,
                    args=(count,),
                )
            i += count

    def duplicate_exprs(self, node: nodes.Module) -> None:
        visitor = DuplicateExprVisitor()
        visitor.visit(node)

        emitted = set()

        for name, exprs in sorted(
            visitor.long_expressions.items(), key=lambda pair: -len(pair[0][0])
        ):
            if len(exprs) >= 2:
                if exprs[0].parent not in emitted:
                    expr_lines = [
                        str(expr.fromlineno)
                        for expr in sorted(exprs, key=lambda e: (e.fromlineno, e.tolineno))
                    ]
                    self.add_message(
                        "duplicate-exprs", node=exprs[0], args=(name, ", ".join(expr_lines))
                    )
                emitted.update(exprs)

    def duplicate_blocks(self, node: nodes.Module) -> None:
        def update_diffs(
            stmt1: nodes.NodeNG, stmt2: nodes.NodeNG, current_diffs: Set[Tuple[str, str]]
        ) -> bool:
            if isinstance(stmt1, nodes.Compare):
                for (op1, _), (op2, _) in zip(stmt1.ops, stmt2.ops):
                    if op1 != op2:
                        current_diffs.add((op1, op2))
            elif isinstance(stmt1, nodes.BinOp) and stmt1.op != stmt2.op:
                current_diffs.add((stmt1.op, stmt2.op))
            elif isinstance(stmt1, nodes.BoolOp) and stmt1.op != stmt2.op:
                return False
            elif isinstance(stmt1, nodes.Name) and stmt1.name != stmt2.name:
                current_diffs.add((stmt1.name, stmt2.name))
            elif isinstance(stmt1, nodes.Const) and stmt1.value != stmt2.value:
                current_diffs.add((str(stmt1.value), str(stmt2.value)))
            elif isinstance(stmt1, nodes.Attribute) and stmt1.attrname != stmt2.attrname:
                current_diffs.add((stmt1.attrname, stmt2.attrname))
            elif isinstance(stmt1, nodes.UnaryOp) and stmt1.op != stmt2.op:
                current_diffs.add((stmt1.op, stmt2.op))
            return True

        def duplicate_blocks(
            block1: List[nodes.NodeNG],
            block2: List[nodes.NodeNG],
            max_diff: int,
            current_diffs: Set[Tuple[str, str]],
        ) -> bool:
            if len(block1) != len(block2):
                return False
            for stmt1, stmt2 in zip(block1, block2):
                if not isinstance(stmt1, type(stmt2)):
                    return False

                children1 = list(stmt1.get_children())
                children2 = list(stmt2.get_children())

                if len(children1) != len(children2):
                    return False

                if not update_diffs(stmt1, stmt2, current_diffs) or len(current_diffs) > max_diff:
                    return False

                if not duplicate_blocks(children1, children2, max_diff, current_diffs):
                    return False
            return True

        MIN_LINES = 3
        MAX_DIFF = 3

        visitor = CollectBlocksVisitor()
        visitor.visit(node)

        blocks = [
            block
            for block in visitor.blocks
            if len(block) > 0
            and get_lines_between(block[0], block[-1], including_last=True) >= MIN_LINES
        ]

        if len(blocks) < 2:
            return

        blocks.sort(key=lambda block: (block[0].fromlineno, block[-1].tolineno))
        max_closed_line = 0

        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                block1 = blocks[i]
                block2 = blocks[j]

                if (
                    not isinstance(block1[0].parent, type(block2[0].parent))
                    or block1[-1].tolineno <= max_closed_line
                ):
                    continue

                if duplicate_blocks(block1, block2, MAX_DIFF, set()):
                    self.add_message(
                        "duplicate-blocks",
                        line=block1[0].fromlineno,
                        col_offset=block1[0].col_offset,
                        end_lineno=block1[-1].tolineno,
                        end_col_offset=block1[-1].end_col_offset,
                        args=(f"{block1[0].fromlineno} and {block2[0].fromlineno}",),
                    )
                    max_closed_line = block1[-1].tolineno
                    break

    def duplicate_sequence(self, node: nodes.Module) -> None:
        def can_use_range(diffs: List[Any]) -> bool:
            if all(e is None for e in diffs):
                return True
            if not all(isinstance(e, int) for e in diffs):
                return False

            assert len(diffs) >= 2
            step = diffs[1] - diffs[0]
            return all(e1 + step == e2 for e1, e2 in zip(diffs, diffs[1:]))

        def get_single_diff_list(
            stmts1: List[nodes.NodeNG], stmts2: List[nodes.NodeNG]
        ) -> Optional[Tuple[Tuple[Any, Any], List[int]]]:
            if len(stmts1) != len(stmts2):
                return None

            result = None
            for i, (stmt1, stmt2) in enumerate(zip(stmts1, stmts2)):
                subresult = get_single_diff(stmt1, stmt2)
                if subresult is None or (result is not None and result[0] != subresult[0]):
                    return None

                diff, path = subresult
                if diff != (None, None):
                    path.append(i)
                    result = diff, path

            if result is None:
                return (None, None), []
            return result

        def get_single_diff(
            stmt1: nodes.NodeNG, stmt2: nodes.NodeNG
        ) -> Optional[Tuple[Tuple[Any, Any], List[int]]]:
            if not isinstance(stmt1, type(stmt2)):
                return None
            if isinstance(stmt1, nodes.Const):
                if not isinstance(stmt1.value, type(stmt2.value)):
                    return None
                if stmt1.value == stmt2.value:
                    return (None, None), []
                return (stmt1.value, stmt2.value), []
            if isinstance(stmt1, nodes.Name) and stmt1.name != stmt2.name:
                return None
            if isinstance(stmt1, nodes.Attribute) and stmt1.attrname != stmt2.attrname:
                return None
            if (
                isinstance(stmt1, (nodes.BinOp, nodes.BoolOp, nodes.UnaryOp))
                and stmt1.op != stmt2.op
            ):
                return None
            if isinstance(stmt1, nodes.Compare) and any(
                o1 != o2 for (o1, _), (o2, _) in zip(stmt1.ops, stmt2.ops)
            ):
                return None
            if isinstance(stmt1, nodes.Assign) and any(
                t1.as_string() != t2.as_string() for t1, t2 in zip(stmt1.targets, stmt2.targets)
            ):
                return None
            if (
                isinstance(stmt1, (nodes.AugAssign, nodes.AnnAssign))
                and stmt1.target.as_string() != stmt2.target.as_string()
            ):
                return None
            if is_block_comment(stmt1):
                return None
            if isinstance(stmt1, (nodes.Assert, nodes.Import, nodes.ImportFrom)):
                return None
            if (
                isinstance(stmt1, nodes.Call)
                and isinstance(stmt1.func, nodes.Name)
                and stmt1.func.name == "print"
            ):
                return None

            return get_single_diff_list(list(stmt1.get_children()), list(stmt2.get_children()))

        def get_seq_diffs(block: List[nodes.NodeNG], subblock_len: int, start: int) -> List[Any]:
            path = None
            diffs: List[Any] = []
            for i in range(start, len(block) - subblock_len, subblock_len):
                subblock1 = block[i : i + subblock_len]
                subblock2 = block[i + subblock_len : i + 2 * subblock_len]

                subresult = get_single_diff_list(subblock1, subblock2)
                if subresult is None:
                    return diffs

                diff, subpath = subresult
                if path is not None and len(path) > 0 and len(subpath) > 0 and path != subpath:
                    return diffs
                if (
                    path is not None
                    and len(path) == 0
                    and len(subpath) > 0
                    and len(diffs) >= DUPL_SEQ_LEN
                ):
                    return diffs
                if path is None or (len(path) == 0 and len(subpath) > 0):
                    path = subpath

                if len(diffs) == 0:
                    diffs.extend(diff)
                else:
                    diffs.append(diff[1])

            return diffs

        def process_block(self: "NoDuplicateCode", block: List[nodes.NodeNG]) -> None:
            max_subblock_len = len(block) // DUPL_SEQ_LEN

            if max_subblock_len == 0:
                return

            start = 0
            while start < len(block) - 1:
                for subblock_len in range(1, max_subblock_len + 1):
                    diffs = get_seq_diffs(block, subblock_len, start)
                    if (len(diffs) >= DUPL_SEQ_LEN and can_use_range(diffs)) or len(
                        diffs
                    ) >= DUPL_SEQ_LEN_NO_RANGE:
                        first_subblock = block[start : start + subblock_len]
                        last_subblock = block[
                            start
                            + (len(diffs) - 1) * subblock_len : start
                            + len(diffs) * subblock_len
                        ]
                        self.add_message(
                            "duplicate-sequence",
                            line=first_subblock[0].fromlineno,
                            col_offset=first_subblock[0].col_offset,
                            end_lineno=last_subblock[-1].tolineno,
                            end_col_offset=last_subblock[-1].end_col_offset,
                            args=(
                                len(diffs),
                                get_lines_between(
                                    first_subblock[0], first_subblock[-1], including_last=True
                                ),
                            ),
                        )
                        start += len(diffs) * subblock_len
                        break
                else:
                    start += 1

        DUPL_SEQ_LEN = 4
        DUPL_SEQ_LEN_NO_RANGE = 5

        visitor = CollectBlocksVisitor()
        visitor.visit(node)

        blocks = [block for block in visitor.blocks if len(block) > 0]
        blocks.sort(key=lambda block: (block[0].fromlineno, block[-1].tolineno))

        for block in blocks:
            if len(block) >= 2:
                process_block(self, block)

    @only_required_for_messages("duplicate-exprs", "duplicate-blocks", "duplicate-sequence")
    def visit_module(self, node: nodes.Module) -> None:
        self.duplicate_exprs(node)
        self.duplicate_blocks(node)
        self.duplicate_sequence(node)


OPS = {
    "+": 0,
    "+=": 0,
    "-": 0,
    "-=": 0,
    "*": 1,
    "*=": 1,
    "/": 2,
    "/=": 2,
    "//": 2,
    "//=": 2,
    "%": 3,
    "%=": 3,
    "**": 4,
    "**=": 4,
    "==": 5,
    "!=": 5,
    ">": 6,
    "<": 6,
    ">=": 6,
    "<=": 6,
    "and": 7,
    "or": 7,
    "not": 8,
    "is": 9,
    "is not": 9,
    "in": 15,
    "not in": 15,
    "&": 10,
    "&=": 10,
    "|": 10,
    "|=": 10,
    "^": 11,
    "^-": 11,
    "~": 12,
    "~=": 12,
    "<<": 13,
    "<<=": 13,
    ">>": 14,
    ">>=": 14,
}


def length_or_type_mismatch(avars) -> bool:
    return any("-" in id_.name for id_ in avars)


def assignment_to_aunify_var(avars) -> bool:
    return any(isinstance(avar.parent, (nodes.AssignName, nodes.AssignAttr)) for avar in avars)


def called_aunify_var(avars) -> bool:
    for avar in avars:
        node = avar.parent
        if (
            (isinstance(node, nodes.Compare) and avar in [o for o, n in node.ops])
            or (isinstance(node, nodes.BinOp) and avar == node.op)
            or (isinstance(node, nodes.AugAssign) and avar == node.op)
            or (isinstance(node, nodes.Attribute) and avar == node.attrname)
        ):
            return True

        while node is not None:
            if isinstance(node.parent, nodes.Call) and node == node.parent.func:
                return True
            node = node.parent
    return False


def extract_from_elif(node: nodes.If, result: List[nodes.If] = None) -> bool:
    """
    returns True iff elifs end with else
    """

    def count_elses(node: nodes.If) -> int:
        if len(node.body) != 1 or not isinstance(node.body[0], nodes.If):
            return 0

        node = node.body[0]
        result = 0
        while node.has_elif_block():
            node = node.orelse[0]
            result += 1
        return result + (1 if has_else_block(node) else 0)

    result = [node] if result is None else result
    if has_else_block(node):
        return True, result

    current = node
    else_count = count_elses(node)
    while current.has_elif_block():
        elif_ = current.orelse[0]
        result.append(elif_)
        if has_else_block(elif_):
            return True, (
                result if else_count == 0 or else_count >= len(result) else result[:-else_count]
            )
        current = elif_
    return False, result


def extract_from_siblings(node: nodes.If, seq_ifs: List[nodes.NodeNG]) -> None:
    sibling = node.next_sibling()
    while sibling is not None and isinstance(sibling, nodes.If):
        new: List[nodes.NodeNG] = []
        if not extract_from_elif(sibling, new):
            return
        seq_ifs.append(sibling)
        seq_ifs.extend(new)
        sibling = sibling.next_sibling()


Fixed = namedtuple("Fixed", ["symbol", "tokens", "statements", "message_args"])


def to_node(val, avar=None) -> nodes.NodeNG:
    assert not isinstance(val, list)
    if isinstance(val, nodes.NodeNG):
        return val
    if avar is not None:
        assert not isinstance(avar.parent, nodes.FunctionDef)
        return type(avar.parent)(val)
    return nodes.Const(val)


def saves_enough_tokens(tokens_before: int, stmts_before: int, fixed: Fixed):
    return fixed.statements <= stmts_before and fixed.tokens < 0.8 * tokens_before


def duplicate_blocks_in_if(self, node: nodes.If) -> bool:

    def get_bodies(ifs: List[nodes.If]) -> List[List[nodes.NodeNG]]:
        result = []
        for i, if_ in enumerate(ifs):
            result.append(if_.body)
            if i == len(ifs) - 1:
                result.append(if_.orelse)
        return result

    def to_parent(val: AunifyVar) -> nodes.NodeNG:
        parent = val.parent
        if isinstance(parent, (nodes.Const, nodes.Name)):
            parent = parent.parent
        assert parent is not None
        return parent

    def check_enabled(message_id: str):
        def middle(func):
            def inner(*args, **kwargs):
                if not self.linter.is_message_enabled(message_id):
                    return None
                result = func(*args, **kwargs)
                if result is None:
                    return result
                return Fixed(message_id, *result)

            return inner

        return middle

    COMPLEX_EXPRESSION_TYPES = (nodes.BinOp,)
    # SIMPLE_EXPRESSION_TYPES = (nodes.AugAssign, nodes.Call, nodes.BoolOp, nodes.Compare)

    def is_part_of_complex_expression(avars) -> bool:
        for avar in avars:
            parent = to_parent(avar)
            assert parent is not None

            # if not isinstance(parent, SIMPLE_EXPRESSION_TYPES):
            if isinstance(parent, COMPLEX_EXPRESSION_TYPES):
                return True
        return False

    def test_variables_change(tests, core, avars):
        vars = vars_in(tests, {VarEventType.READ})
        first_loc = tests[0].cfg_loc
        avars_locs = [get_cfg_loc(to_parent(avar)).node.sub_locs for avar in avars]
        return any(
            is_changed_between(varname, first_loc, avar_locs)
            for varname, _scope in vars
            for avar_locs in avars_locs
        )

    @check_enabled("if-to-ternary")
    def get_fixed_by_ternary(tests, core, avars):
        if len(tests) > 2:
            return None
        if is_part_of_complex_expression(avars):
            return None

        # generate exprs
        exprs = []
        for avar in avars:
            assert len(avar.subs) == len(tests) + 1
            expr = to_node(avar.subs[-1], avar)
            for test, avar_val in reversed(list(zip(tests, avar.subs))):
                new = nodes.IfExp()
                new.postinit(test=test, body=to_node(avar_val, avar), orelse=expr)
                expr = new

            exprs.append(expr)

        return (
            get_token_count(core) - len(avars) + get_token_count(exprs),  # subtract aunify vars
            get_statements_count(core, include_defs=False, include_name_main=True),
            (),
        )

    def contains_avar(node: nodes.NodeNG, avars):
        for avar in avars:
            for ancestor in avar.node_ancestors():
                if ancestor == node:
                    return True
        return False

    HEADER_ATTRIBUTES = {
        nodes.For: ["target", "iter"],
        nodes.While: ["test"],
        # nodes.If: ["test"],
        nodes.FunctionDef: ["name", "args"],
        nodes.ExceptHandler: ["name", "type"],
        nodes.TryExcept: [],
        nodes.TryFinally: [],
        nodes.With: ["items"],
    }

    BODY_ATTRIBUTES = {
        nodes.For: ["body", "orelse"],
        nodes.While: ["body", "orelse"],
        # nodes.If: ["body", "orelse"],
        nodes.FunctionDef: ["body"],
        nodes.ExceptHandler: ["body"],
        nodes.TryExcept: ["body", "handlers", "orelse"],
        nodes.TryFinally: ["body", "finalbody"],
        nodes.With: ["body"],
    }

    def if_can_be_moved(core, avars):
        return type(core) in HEADER_ATTRIBUTES.keys() and not any(
            contains_avar(getattr(core, attr), avars) for attr in HEADER_ATTRIBUTES[type(core)]
        )

    def get_fixed_by_moving_if_rec(tests, core, avars):
        if isinstance(core, list):
            if len(core) == 0:
                return []

            avar_indices = []
            for i, stmt in enumerate(core):
                if contains_avar(stmt, avars):
                    avar_indices.append(i)

            assert len(avar_indices) > 0
            min_ = avar_indices[0]
            max_ = avar_indices[-1]

            if min_ == max_ and if_can_be_moved(core[min_], avars):
                root = get_fixed_by_moving_if_rec(tests, core[min_], avars)
            else:
                root, if_bodies = create_ifs(tests)
                for if_body in if_bodies:
                    if_body.extend(core[min_ : max_ + 1])

            return core[:min_] + [root] + core[max_ + 1 :]

        assert contains_avar(core, avars) and if_can_be_moved(core, avars)
        new_core = type(core)()

        for attr in HEADER_ATTRIBUTES[type(core)]:
            setattr(new_core, attr, getattr(core, attr))

        for attr in BODY_ATTRIBUTES[type(core)]:
            new_body = get_fixed_by_moving_if_rec(tests, getattr(core, attr), avars)
            setattr(new_core, attr, new_body)

        return new_core

    @check_enabled("if-into-block")
    def get_fixed_by_moving_if(tests, core, avars):
        # too restrictive -- the change may be before the avar but after the place
        # where the if would be inserted
        if (not isinstance(core, list) and not if_can_be_moved(core, avars)) or (
            isinstance(core, list) and len(core) == 1 and not if_can_be_moved(core[0], avars)
        ):
            return None

        fixed = get_fixed_by_moving_if_rec(tests, core, avars)
        return (
            get_token_count(fixed)
            + sum(
                get_token_count(v) if isinstance(v, nodes.NodeNG) else 0
                for avar in avars
                for v in avar.subs
            ),
            get_statements_count(fixed, include_defs=True, include_name_main=False),
            (),
        )

    def create_ifs(tests: List[nodes.NodeNG]) -> nodes.If:
        root = nodes.If()
        if_ = root
        if_bodies = []
        for i, test in enumerate(tests):
            if_.test = test
            if_bodies.append(if_.body)
            if i != len(tests) - 1:
                elif_ = nodes.If()
                if_.orelse = [elif_]
                # elif_.parent = if_
                if_ = elif_
            else:
                if_bodies.append(if_.orelse)
        return root, if_bodies

    @check_enabled("if-to-variables")
    def get_fixed_by_vars(tests, core, avars):
        root, if_bodies = create_ifs(tests)
        seen = {}
        for avar in avars:
            var_vals = tuple(avar.subs)
            varname = seen.get(var_vals, avar.name)

            if varname != avar.name:
                continue
            seen[var_vals] = avar.name

            for val, body in zip(var_vals, if_bodies):
                assign = nodes.Assign()
                assign.targets = [nodes.AssignName(avar.name)]
                assign.value = to_node(val, avar)
                body.append(assign)

        return (
            get_token_count(root) + get_token_count(core),
            get_statements_count(root, include_defs=False, include_name_main=True)
            + get_statements_count(core, include_defs=False, include_name_main=True),
            (),
        )

    @check_enabled("similar-to-function")
    def get_fixed_by_function(tests, core, avars):
        root, if_bodies = create_ifs(tests)

        # compute necessary arguments from different values
        seen = {}
        for avar in avars:
            var_vals = tuple(avar.subs)
            old_avar = seen.get(var_vals, avar)

            if old_avar != avar:
                continue
            seen[var_vals] = avar

        # compute extras
        extra_args = get_vars_defined_before(core)
        return_vals_needed = len(get_vars_used_after(core))
        control_needed = len(get_control_statements(core))

        # generate calls in ifs
        vals = [[s[i] for s in seen] for i in range(len(tests) + 1)]
        for if_vals, body in zip(vals, if_bodies):
            call = nodes.Call()
            call.func = nodes.Name("AUX")
            call.args = [to_node(val) for val in if_vals] + [
                nodes.Name(varname) for varname, _scope in extra_args
            ]
            if return_vals_needed + control_needed == 0:
                body.append(call)
            else:
                assign = nodes.Assign()
                assign.targets = [
                    nodes.AssignName(f"<r{i}>") for i in range(control_needed + return_vals_needed)
                ]
                assign.value = call
                body.append(assign)

        # generate function
        fun_def = nodes.FunctionDef(name="AUX")
        fun_def.args = nodes.Arguments()
        fun_def.args.postinit(
            args=[nodes.AssignName(avar.name) for avar in seen.values()]
            + [nodes.AssignName(varname) for varname, _scope in extra_args],
            defaults=None,
            kwonlyargs=[],
            kw_defaults=None,
            annotations=[],
        )
        fun_def.body = core if isinstance(core, list) else [core]

        # generate management for returned values
        if control_needed > 0:
            root = [root]
            for i in range(control_needed):
                test = nodes.BinOp("is")
                test.postinit(left=nodes.Name(f"<r{i}>"), right=nodes.Const(None))
                if_ = nodes.If()
                if_.test = test
                if_.body = [nodes.Return()]  # placeholder for a control
                root.append(if_)

        return (
            get_token_count(root) + get_token_count(fun_def),
            get_statements_count(root, include_defs=False, include_name_main=True)
            + get_statements_count(fun_def, include_defs=False, include_name_main=True),
            (),
        )

    if is_parents_elif(node):
        return False

    ends_with_else, ifs = extract_from_elif(node)
    if not ends_with_else:
        return False

    if_bodies = get_bodies(ifs)
    assert len(if_bodies) >= 2
    core, avars = antiunify(if_bodies)

    if length_or_type_mismatch(avars) or assignment_to_aunify_var(avars):
        return False

    tokens_before = get_token_count(node)
    stmts_before = get_statements_count(node, include_defs=False, include_name_main=True)

    tests = [if_.test for if_ in ifs]
    called_avar = called_aunify_var(avars)
    tvs_change = test_variables_change(tests, core, avars)
    fixed = [
        get_fixed_by_ternary(tests, core, avars) if not called_avar and not tvs_change else None,
        get_fixed_by_moving_if(tests, core, avars) if not tvs_change else None,
        get_fixed_by_vars(tests, core, avars) if not called_avar else None,
        get_fixed_by_function(tests, core, avars) if not called_avar else None,
    ]

    fixed = [
        f for f in fixed if f is not None and saves_enough_tokens(tokens_before, stmts_before, f)
    ]
    if len(fixed) == 0:
        return False

    suggestion = min(fixed, key=lambda f: f.tokens)

    message_id, _tokens, _statements, message_args = suggestion
    self.add_message(message_id, node=node, args=message_args)
    return True


def similar_to_function(self, to_aunify: List[List[nodes.NodeNG]], core, avars) -> bool:
    def get_fixed_by_function(to_aunify, core, avars):
        # compute necessary arguments from different values
        seen = {}
        for avar in avars:
            var_vals = tuple(avar.subs)
            old_avar = seen.get(var_vals, avar)

            if old_avar != avar:
                continue
            seen[var_vals] = avar

        # compute extras
        extra_args = get_vars_defined_before(core)
        return_vals_needed = len(get_vars_used_after(core))
        control_needed = len(get_control_statements(core))

        # generate calls in ifs
        calls = []
        for i in range(len(to_aunify)):
            params = [s[i] for s in seen]
            call = nodes.Call()
            call.func = nodes.Name("AUX")
            call.args = [to_node(param) for param in params] + [
                nodes.Name(varname) for varname, _scope in extra_args
            ]
            if return_vals_needed + control_needed == 0:
                calls.append(call)
            else:
                assign = nodes.Assign()
                assign.targets = [
                    nodes.AssignName(f"<r{i}>") for i in range(control_needed + return_vals_needed)
                ]
                assign.value = call
                calls.append(assign)

                # generate management for returned control flow
                for i in range(control_needed):
                    test = nodes.BinOp("is")
                    test.postinit(left=nodes.Name(f"<r{i}>"), right=nodes.Const(None))
                    if_ = nodes.If()
                    if_.test = test
                    if_.body = [nodes.Return()]  # placeholder for a control
                    calls.append(if_)

        # generate function
        fun_def = nodes.FunctionDef(name="AUX")
        fun_def.args = nodes.Arguments()
        fun_def.args.postinit(
            args=[nodes.AssignName(avar.name) for avar in seen.values()]
            + [nodes.AssignName(varname) for varname, _scope in extra_args],
            defaults=None,
            kwonlyargs=[],
            kw_defaults=None,
            annotations=[],
        )
        fun_def.body = core if isinstance(core, list) else [core]

        return Fixed(
            "similar-to-function",
            get_token_count(calls) + get_token_count(fun_def),
            get_statements_count(calls, include_defs=False, include_name_main=True)
            + get_statements_count(fun_def, include_defs=False, include_name_main=True),
            (),
        )

    tokens_before = sum(get_token_count(node) for node in to_aunify)
    stmts_before = sum(
        get_statements_count(node, include_defs=False, include_name_main=True) for node in to_aunify
    )

    fixed = get_fixed_by_function(to_aunify, core, avars)
    if not saves_enough_tokens(tokens_before, stmts_before, fixed):
        return False

    message_id, _tokens, _statements, message_args = fixed

    first = to_aunify[0][0]
    last = to_aunify[0][-1]
    self.add_message(
        message_id,
        line=first.fromlineno,
        col_offset=first.col_offset,
        end_lineno=last.tolineno,
        end_col_offset=last.col_offset,
        args=message_args,
    )
    return True


def similar_to_loop(self, to_aunify: List[List[nodes.NodeNG]], core, avars) -> bool:
    def to_range_node(sequence):
        start = None
        step = None
        previous = None
        for s in sequence:
            if not isinstance(s, int):
                return None
            if start is None:
                start = s
            elif step is None:
                step = s - start
            else:
                assert previous is not None
                if s - previous != step:
                    return None
            previous = s

        range = nodes.Call()
        range.func = nodes.Name("range")
        start_node = nodes.Const(start)
        stop_node = nodes.Const(previous + 1)
        step_node = nodes.Const(step)
        if step != 1:
            range.args = [start_node, stop_node, step_node]
        elif start != 0:
            range.args = [start_node, stop_node]
        else:
            range.args = [stop_node]
        return range

    def get_iter(to_aunify, sequences):
        if len(sequences) == 0:
            range_node = nodes.Call()
            range_node.func = nodes.Name("range")
            range_node.args = [nodes.Const(len(to_aunify))]
            return range_node

        if len(sequences) == 1:
            range_node = to_range_node(sequences[0])
            if range_node is not None:
                return range_node

            iter = nodes.Tuple()
            iter.elts = [to_node(s, avars[0]) for s in sequences[0]]
            return iter

        ranges = [to_range_node(sequence) for sequence in sequences]
        if all(range is not None for range in ranges):
            iter = nodes.Call()
            iter.func = nodes.Name("zip")
            iter.args = ranges
            return iter

        tuples = []
        for i in range(len(sequences[0])):
            tuple_node = nodes.Tuple()
            tuple_node.elts = [to_node(sequences[j][i], avars[j]) for j in range(len(avars))]
            tuples.append(tuple_node)

        iter = nodes.List()
        iter.elts = tuples
        return iter

    def get_target(avars, sequences):
        if len(sequences) == 0:
            return nodes.Name("_")

        if len(sequences) == 1:
            return avars[0]

        target = nodes.Tuple()
        target.elts = avars
        return target

    def get_fixed_by_loop(to_aunify, core, avars):
        sequences = [avar.subs for avar in avars]

        for_ = nodes.For()
        for_.iter = get_iter(to_aunify, sequences)
        for_.target = get_target(avars, sequences)
        for_.body = core

        return Fixed(
            "similar-to-loop",
            get_token_count(for_),
            get_statements_count(for_, include_defs=False, include_name_main=True),
            (
                len(to_aunify),
                get_statements_count(to_aunify[0], include_defs=False, include_name_main=True),
            ),
        )

    if (
        length_or_type_mismatch(avars)
        or assignment_to_aunify_var(avars)
        or called_aunify_var(avars)
        or max(len(to_aunify), len(to_aunify[0])) <= 2  # TODO parametrize?
    ):
        return False

    tokens_before = sum(get_token_count(node) for node in to_aunify)
    stmts_before = sum(
        get_statements_count(node, include_defs=False, include_name_main=True) for node in to_aunify
    )

    fixed = get_fixed_by_loop(to_aunify, core, avars)
    if not saves_enough_tokens(tokens_before, stmts_before, fixed):
        return False

    message_id, _tokens, _statements, message_args = fixed

    first = to_aunify[0][0]
    last = to_aunify[-1][-1]
    self.add_message(
        message_id,
        line=first.fromlineno,
        col_offset=first.col_offset,
        end_lineno=last.tolineno,
        end_col_offset=last.col_offset,
        args=message_args,
    )
    return True


class BigNoDuplicateCode(BaseChecker):  # type: ignore
    name = "big-no-duplicate-code"
    msgs = {
        "R6801": (
            # "Lines %i to %i are similar to lines %i through %i. Extract them to a common function.",
            "Extract to a common function.",
            "similar-to-function",
            "",
        ),
        "R6802": (
            "Extract code into loop (%d repetitions of %d statements)",
            "similar-to-loop",
            "",
        ),
        "R6803": (
            "Use existing function",
            "similar-to-call",
            "",
        ),
        "R6804": (
            "Extract ifs to ternary",
            "if-to-ternary",
            "",
        ),
        "R6805": (
            "Combine",
            "seq-into-similar",
            "",
        ),
        "R6806": (
            "Extract ifs to variables",
            "if-to-variables",
            "",
        ),
        "R6807": (
            "Move if into block",
            "if-into-block",
            "",
        ),
        "R6851": (
            "Identical code inside all if's branches, move %d lines %s the if.",
            "identical-before-after-branch",
            "Emitted when identical code starts or ends all branches of an if statement.",
        ),
        "R6852": (
            "Identical code inside %d consecutive ifs, join their conditions using 'or'.",
            "identical-seq-ifs",
            "Emitted when several consecutive if statements have identical bodies and thus can be "
            "joined by or in their conditions.",
        ),
        "R6853": (
            "A complex expression '%s' used repeatedly (on lines %s). Extract it to a local variable.",
            "identical-exprs-to-variable",
            "Emitted when an overly complex expression is used multiple times.",
        ),
        "R6854": (
            "A complex expression '%s' used repeatedly (on lines %s). Extract it to a local variable.",
            "identical-exprs-to-function",
            "Emitted when an overly complex expression is used multiple times.",
        ),
    }

    def identical_before_after_branch(self, node: nodes.If) -> bool:
        def extract_branch_bodies(node: nodes.If) -> Optional[List[nodes.NodeNG]]:
            branches = [node.body]
            current = node
            while current.has_elif_block():
                elif_ = current.orelse[0]
                if not elif_.orelse:
                    return None

                branches.append(elif_.body)
                current = elif_
            branches.append(current.orelse)
            return branches

        def get_stmts_difference(branches: List[nodes.NodeNG], forward: bool) -> int:
            reference = branches[0]
            compare = branches[1:]
            for i in range(min(map(len, branches))):
                for branch in compare:
                    index = i if forward else -i - 1
                    if reference[index].as_string() != branch[index].as_string():
                        return i
            return i + 1

        def add_message(
            branches: List[nodes.NodeNG],
            stmts_difference: int,
            defect_node: nodes.NodeNG,
            forward: bool = True,
        ) -> None:
            reference = branches[0]
            first = reference[0 if forward else -stmts_difference]
            last = reference[stmts_difference - 1 if forward else -1]
            lines_difference = get_lines_between(first, last, including_last=True)

            self.add_message(
                "identical-before-after-branch",
                node=defect_node,
                args=(lines_difference, "before" if forward else "after"),
            )

        if not node.orelse or is_parents_elif(node):
            return False

        branches = extract_branch_bodies(node)
        if branches is None:
            return False

        any_message = False
        same_prefix_len = get_stmts_difference(branches, forward=True)
        if same_prefix_len >= 1:
            add_message(branches, same_prefix_len, node, forward=True)
            any_message = True
            if any(same_prefix_len == len(b) for b in branches):
                return any_message

        same_suffix_len = get_stmts_difference(branches, forward=False)
        if same_suffix_len >= 1:
            # allow early returns
            if same_suffix_len == 1 and isinstance(branches[0][-1], nodes.Return):
                i = 0
                while len(branches[i]) == 1:
                    i += 1
                branches = branches[i:]
                if len(branches) < 2:
                    return any_message
            defect_node = branches[0][-1].parent

            # disallow breaking up coherent segments
            same_part = branches[0][-same_suffix_len:]
            if (
                get_statements_count(same_part, include_defs=True, include_name_main=True)
                / (
                    min(
                        get_statements_count(branch, include_defs=True, include_name_main=True)
                        for branch in branches
                    )
                    - same_prefix_len
                )
                < 1 / 2
            ):  # TODO extract into parameter
                return any_message

            add_message(branches, same_suffix_len, defect_node, forward=False)
            any_message = True
        return any_message

    def identical_seq_ifs(self, node: nodes.If) -> bool:

        def same_ifs_count(seq_ifs: List[nodes.NodeNG], start: int) -> int:
            reference = seq_ifs[start].body
            for i in range(start + 1, len(seq_ifs)):
                # do not suggest join of elif and sibling
                if seq_ifs[start].parent not in seq_ifs[i].node_ancestors():
                    return i - start

                compared = seq_ifs[i].body
                if len(reference) != len(compared):
                    return i - start
                for j in range(len(reference)):
                    if reference[j].as_string() != compared[j].as_string():
                        return i - start
            return len(seq_ifs) - start

        prev_sibling = node.previous_sibling()
        if is_parents_elif(node) or (
            isinstance(prev_sibling, nodes.If) and not extract_from_elif(prev_sibling)[0]
        ):
            return False

        ends_with_else, seq_ifs = extract_from_elif(node)
        if ends_with_else:
            return False
        extract_from_siblings(node, seq_ifs)

        if len(seq_ifs) == 1:
            return False

        i = 0
        any_message = False
        while i < len(seq_ifs) - 1:
            count = same_ifs_count(seq_ifs, i)
            if count > 1:
                first = seq_ifs[i]
                assert isinstance(seq_ifs[i + count - 1], nodes.If)
                last = seq_ifs[i + count - 1].body[-1]

                self.add_message(
                    "identical-seq-ifs",
                    line=first.fromlineno,
                    col_offset=first.col_offset,
                    end_lineno=last.tolineno,
                    end_col_offset=last.end_col_offset,
                    args=(count,),
                )
                any_message = True
            i += count
        return any_message

    def visit_module(self, node: nodes.Module):
        def candidate_fst(nodes):
            yield from enumerate(nodes)

        def candidate_snd(nodes, i):
            for j in range(i + 1, len(nodes)):
                fst = nodes[i]
                snd = nodes[j]

                if fst not in snd.node_ancestors():
                    yield j, snd

        def get_siblings(node):
            siblings = []
            sibling = node
            while sibling is not None:
                if include_in_stmts(sibling):
                    siblings.append(sibling)
                sibling = sibling.next_sibling()
            return siblings

        def overlap(stmt_nodes, i, j, to_aunify) -> bool:
            if i + len(to_aunify[0]) - 1 >= j:
                return True

            fsts, snds = to_aunify
            return stmt_nodes.index(fsts[-1], i) >= stmt_nodes.index(snds[-1], j)

        def is_duplication_candidate(to_aunify) -> bool:
            for ns in zip(*to_aunify):
                if not all(isinstance(n, type(ns[0])) for n in ns):
                    return False
            return True

        def get_loop_repetitions(
            block: List[nodes.NodeNG],
        ) -> Generator[Tuple[int, List[List[nodes.NodeNG]]], None, None]:
            for end in range(len(fst_siblings), 0, -1):
                for subblock_len in range(1, end // 2 + 1):
                    subblocks = [block[i : i + subblock_len] for i in range(0, end, subblock_len)]
                    yield ((end // subblock_len) * subblock_len, subblocks)

        def include_in_stmts(node):
            return not is_block_comment(node) and not isinstance(
                node, (nodes.Pass, nodes.Assert, nodes.ClassDef)
            )

        if len(node.body) == 0:
            return

        stmt_nodes = sorted(
            (
                stmt_loc.node
                for loc in successors_from_loc(
                    node.cfg_loc, include_start=True, explore_functions=True, explore_classes=True
                )
                for stmt_loc in get_stmt_locs(loc)
                if stmt_loc is not None and include_in_stmts(stmt_loc.node)
            ),
            key=lambda node: (
                node.fromlineno,
                node.col_offset if node.col_offset is not None else float("inf"),
            ),
        )

        duplicate = set()
        candidates = {}
        break_snd_loop = False
        for i, fst in candidate_fst(stmt_nodes):
            if fst in duplicate:
                continue

            if isinstance(fst, nodes.If):
                any_message1 = self.identical_before_after_branch(fst)
                any_message2 = self.identical_seq_ifs(fst)
                any_message3 = duplicate_blocks_in_if(self, fst)

                if any_message1 or any_message2 or any_message3:
                    duplicate.update(
                        {loc.node for loc in syntactic_children_locs_from(fst.cfg_loc, fst)}
                    )
                    continue

            fst_siblings = get_siblings(fst)

            if len(fst_siblings) >= 3 and not any(
                isinstance(node, nodes.FunctionDef) for node in fst_siblings
            ):
                for end, to_aunify in get_loop_repetitions(fst_siblings):
                    core, avars = antiunify(to_aunify)
                    if similar_to_loop(self, to_aunify, core, avars):
                        duplicate.update(
                            {
                                stmt_loc.node
                                for loc in syntactic_children_locs_from(
                                    get_cfg_loc(to_aunify[0][0]),
                                    [n for ns in to_aunify for n in ns],
                                )
                                for stmt_loc in get_stmt_locs(loc)
                                if stmt_loc is not None
                            }
                        )
                        break

                if fst in duplicate:
                    continue

            for j, snd in candidate_snd(stmt_nodes, i):
                if break_snd_loop:
                    break_snd_loop = False
                    break
                snd_siblings = get_siblings(snd)

                for length in range(min(len(fst_siblings), len(snd_siblings)), 0, -1):
                    to_aunify = [fst_siblings[:length], snd_siblings[:length]]

                    if not overlap(stmt_nodes, i, j, to_aunify) and is_duplication_candidate(
                        to_aunify
                    ):
                        # TODO or larger?
                        id_ = candidates.get((fst, length), len(candidates))
                        candidates[(fst, length)] = id_
                        candidates[(snd, length)] = id_

                        # duplicate.update(
                        #     {loc.node for loc in syntactic_children_locs_from(fst.cfg_loc, fst)}
                        # )
                        break_snd_loop = True
                        break

        for this_id in set(candidates.values()):
            to_aunify = [
                get_siblings(node)[:length]
                for (node, length), id_ in candidates.items()
                if id_ == this_id
            ]
            # if any(n in duplicate for ns in to_aunify for n in ns):
            #     continue

            core, avars = antiunify(to_aunify)

            if (
                length_or_type_mismatch(avars)
                or assignment_to_aunify_var(avars)
                or called_aunify_var(avars)
            ):
                continue

            if all(isinstance(vals[0], nodes.FunctionDef) for vals in to_aunify):
                pass  # TODO hint use common helper function
            else:
                similar_to_function(self, to_aunify, core, avars)


def register(linter: "PyLinter") -> None:
    """This required method auto registers the checker during initialization.
    :param linter: The linter to register the checker to.
    """
    linter.register_checker(NoDuplicateCode(linter))
    linter.register_checker(BigNoDuplicateCode(linter))
