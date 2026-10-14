"""Z32 AST node definitions.

A small, explicit node set covering the union of Lua 5.1 and Luau.  Nodes are
plain classes with ``__slots__``; the lowering pass (``z32/lower.py``) walks
them to emit Z32 bytecode and never needs source positions again.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    # expressions
    "Nil", "True_", "False_", "Vararg", "Number", "Str", "Name", "Index",
    "Call", "Function", "Table", "BinOp", "UnOp", "Group", "IfExpr",
    # statements
    "Local", "Assign", "CallStat", "Do", "While", "Repeat", "If",
    "NumericFor", "GenericFor", "FunctionStat", "Return", "Break",
    "Continue", "Goto", "Label", "TypeAlias", "Attr", "Empty",
    "Block",
]

# --------------------------------------------------------------------------
# expressions
# --------------------------------------------------------------------------


class Node:
    __slots__ = ()


class Nil(Node):
    __slots__ = ()


class True_(Node):
    __slots__ = ()


class False_(Node):
    __slots__ = ()


class Vararg(Node):
    __slots__ = ()


class Number(Node):
    __slots__ = ("value",)

    def __init__(self, value: float):
        self.value = value


class Str(Node):
    """A string literal.  ``value`` is ``bytes`` (Lua strings are byte strings)."""
    __slots__ = ("value",)

    def __init__(self, value: bytes):
        self.value = value


class Name(Node):
    """A variable reference resolved later to local / upvalue / global."""
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class Index(Node):
    __slots__ = ("obj", "key", "dot")

    def __init__(self, obj: Node, key: Node, dot: bool = False):
        self.obj = obj
        self.key = key
        self.dot = dot


class Call(Node):
    __slots__ = ("func", "args", "method")

    def __init__(self, func: Node, args: list, method: Optional[str] = None):
        self.func = func
        self.args = args
        self.method = method  # set for `a:b(...)`; func is then `a`


class Function(Node):
    __slots__ = ("params", "is_vararg", "body", "name", "line")

    def __init__(self, params: list, is_vararg: bool, body: "Block",
                 name: Optional[str] = None, line: int = 0):
        self.params = params          # list[str]
        self.is_vararg = is_vararg
        self.body = body              # Block
        self.name = name              # debug name hint
        self.line = line


class Table(Node):
    """``fields`` is a list of tuples:

    ``('array', None, expr)`` | ``('named', bytes_key, expr)`` |
    ``('keyed', key_expr, expr)``
    """
    __slots__ = ("fields",)

    def __init__(self, fields: list):
        self.fields = fields


class BinOp(Node):
    __slots__ = ("op", "lhs", "rhs")

    def __init__(self, op: str, lhs: Node, rhs: Node):
        self.op = op
        self.lhs = lhs
        self.rhs = rhs


class UnOp(Node):
    __slots__ = ("op", "expr")

    def __init__(self, op: str, expr: Node):
        self.op = op
        self.expr = expr


class Group(Node):
    """``(expr)`` -- truncates multiple results, so it must be preserved."""
    __slots__ = ("expr",)

    def __init__(self, expr: Node):
        self.expr = expr


class IfExpr(Node):
    """Luau ``if c then a else b`` expression."""
    __slots__ = ("cond", "then", "orelse")

    def __init__(self, cond: Node, then: Node, orelse: Node):
        self.cond = cond
        self.then = then
        self.orelse = orelse


# --------------------------------------------------------------------------
# statements
# --------------------------------------------------------------------------


class Block(Node):
    __slots__ = ("stmts",)

    def __init__(self, stmts: Optional[list] = None):
        self.stmts = stmts if stmts is not None else []


class Local(Node):
    __slots__ = ("names", "exprs", "attribs")

    def __init__(self, names: list, exprs: list, attribs: Optional[list] = None):
        self.names = names        # list[str]
        self.exprs = exprs        # list[Node]
        self.attribs = attribs    # list[str] or None (e.g. <const>)


class Assign(Node):
    __slots__ = ("targets", "exprs")

    def __init__(self, targets: list, exprs: list):
        self.targets = targets
        self.exprs = exprs


class CallStat(Node):
    __slots__ = ("call",)

    def __init__(self, call: Call):
        self.call = call


class Do(Node):
    __slots__ = ("body",)

    def __init__(self, body: Block):
        self.body = body


class While(Node):
    __slots__ = ("cond", "body")

    def __init__(self, cond: Node, body: Block):
        self.cond = cond
        self.body = body


class Repeat(Node):
    __slots__ = ("body", "cond")

    def __init__(self, body: Block, cond: Node):
        self.body = body
        self.cond = cond


class If(Node):
    """``clauses`` is ``[(cond, block), ...]``; ``orelse`` is a Block or None."""
    __slots__ = ("clauses", "orelse")

    def __init__(self, clauses: list, orelse: Optional[Block]):
        self.clauses = clauses
        self.orelse = orelse


class NumericFor(Node):
    __slots__ = ("var", "start", "stop", "step", "body")

    def __init__(self, var: str, start: Node, stop: Node, step: Optional[Node],
                 body: Block):
        self.var = var
        self.start = start
        self.stop = stop
        self.step = step
        self.body = body


class GenericFor(Node):
    __slots__ = ("names", "exprs", "body")

    def __init__(self, names: list, exprs: list, body: Block):
        self.names = names
        self.exprs = exprs
        self.body = body


class FunctionStat(Node):
    """``function a.b:c() end`` or ``local function f() end``.

    ``target`` is the *raw* dotted name (``Name``/``Index`` chain); for
    ``local function`` it is a plain ``Name``.
    """
    __slots__ = ("target", "func", "is_local")

    def __init__(self, target: Node, func: Function, is_local: bool = False):
        self.target = target
        self.func = func
        self.is_local = is_local


class Return(Node):
    __slots__ = ("exprs",)

    def __init__(self, exprs: list):
        self.exprs = exprs


class Break(Node):
    __slots__ = ()


class Continue(Node):
    __slots__ = ()


class Goto(Node):
    __slots__ = ("label",)

    def __init__(self, label: str):
        self.label = label


class Label(Node):
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class TypeAlias(Node):
    """Luau ``type X = ...`` -- parsed, then dropped (types never reach runtime)."""
    __slots__ = ("name", "exported")

    def __init__(self, name: str, exported: bool = False):
        self.name = name
        self.exported = exported


class Attr(Node):
    """A Luau attribute such as ``@native`` -- parsed and dropped."""
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class Empty(Node):
    __slots__ = ()
