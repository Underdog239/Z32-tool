"""Z32 unparser -- AST back to Lua source.

Two uses:

1. **Differential testing.**  Regenerating source from a parsed AST and
   comparing the *output of the program* against the original file is a
   very strong correctness check on the parser: if the parser drops,
   reorders or mis-parenthesises anything, the outputs diverge.

2. **Recovery / deobfuscation.**  Given a Z32 bytecode module, the same
   printer renders a readable program, which is what the ``z32 dump``
   command uses.
"""

from __future__ import annotations

import math

from . import ast as A

__all__ = ["unparse", "Unparser"]

# binary operator precedence, mirroring the parser table
_PRI = {
    "or": 1, "and": 2,
    "<": 3, ">": 3, "<=": 3, ">=": 3, "~=": 3, "==": 3,
    "|": 4, "~": 5, "&": 6, "<<": 7, ">>": 7,
    "..": 9, "+": 10, "-": 10,
    "*": 11, "/": 11, "//": 11, "%": 11, "^": 14,
}
_UNARY_PRI = 12

_ESCAPES = {
    7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r",
    92: "\\\\", 34: '\\"',
}


def quote_string(data: bytes) -> str:
    """Render ``data`` as a Lua string literal that every dialect accepts.

    Uses 3-digit decimal escapes for anything outside printable ASCII so the
    literal is unambiguous in Lua 5.1, 5.4 and Luau alike (``\\x`` and ``\\u``
    are not portable, and a bare ``\\1`` next to a digit would be misread).
    """
    out = ['"']
    for b in data:
        if b in _ESCAPES:
            out.append(_ESCAPES[b])
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append("\\%03d" % b)
    out.append('"')
    return "".join(out)


def quote_number(x: float) -> str:
    """Render a Lua number so it round-trips exactly."""
    if x != x:
        return "(0 / 0)"
    if x == math.inf:
        return "(1 / 0)"
    if x == -math.inf:
        return "(-1 / 0)"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    for prec in (14, 15, 16, 17):
        s = "%.*g" % (prec, x)
        try:
            if float(s) == x:
                # make sure it is still a float literal in Lua
                if "." not in s and "e" not in s and "E" not in s:
                    s += ".0"
                return s
        except ValueError:  # pragma: no cover
            continue
    return "%.17g" % x


class Unparser:
    def __init__(self, indent: str = "  "):
        self.indent = indent

    # ------------------------------------------------------------------
    def block(self, blk: A.Block, level: int) -> list:
        lines: list = []
        prev_end = -1          # index of the last line of the previous statement
        prev_open = False      # previous statement ended with an expression
        for st in blk.stmts:
            chunk = self.stat(st, level)
            if not chunk:
                continue
            # `f()` newline `(x).y = 1` parses as a single call in Lua/Luau
            # unless separated by `;`, so insert one when required.
            if prev_open and chunk[0].lstrip().startswith("("):
                if not lines[prev_end].rstrip().endswith(";"):
                    lines[prev_end] = lines[prev_end].rstrip() + ";"
            prev_open = self._ends_with_expr(st)
            lines.extend(chunk)
            prev_end = len(lines) - 1
        return lines

    @staticmethod
    def _ends_with_expr(st) -> bool:
        """True if a following `(` would be read as call arguments."""
        if isinstance(st, A.CallStat):
            return True
        if isinstance(st, A.Assign):
            return True
        if isinstance(st, A.Local):
            return bool(st.exprs)
        return False

    def _pad(self, level: int) -> str:
        return self.indent * level

    # ------------------------------------------------------------------
    # statements
    # ------------------------------------------------------------------
    def stat(self, st, level: int) -> list:
        p = self._pad(level)
        out: list = []

        if isinstance(st, A.Local):
            names = []
            for i, n in enumerate(st.names):
                at = st.attribs[i] if st.attribs and i < len(st.attribs) else None
                names.append(f"{n} <{at}>" if at else n)
            body = "local " + ", ".join(names)
            if st.exprs:
                body += " = " + ", ".join(self.expr(e) for e in st.exprs)
            out.append(p + body)
        elif isinstance(st, A.Assign):
            out.extend(self._assign(st, p))
        elif isinstance(st, A.CallStat):
            out.append(p + self.expr(st.call))
        elif isinstance(st, A.Do):
            out.append(p + "do")
            out.extend(self.block(st.body, level + 1))
            out.append(p + "end")
        elif isinstance(st, A.While):
            out.append(p + "while " + self.expr(st.cond) + " do")
            out.extend(self.block(st.body, level + 1))
            out.append(p + "end")
        elif isinstance(st, A.Repeat):
            out.append(p + "repeat")
            out.extend(self.block(st.body, level + 1))
            out.append(p + "until " + self.expr(st.cond))
        elif isinstance(st, A.If):
            for i, (cond, body) in enumerate(st.clauses):
                kw = "if" if i == 0 else "elseif"
                out.append(p + kw + " " + self.expr(cond) + " then")
                out.extend(self.block(body, level + 1))
            if st.orelse is not None:
                out.append(p + "else")
                out.extend(self.block(st.orelse, level + 1))
            out.append(p + "end")
        elif isinstance(st, A.NumericFor):
            head = "for %s = %s, %s" % (st.var, self.expr(st.start),
                                        self.expr(st.stop))
            if st.step is not None:
                head += ", " + self.expr(st.step)
            out.append(p + head + " do")
            out.extend(self.block(st.body, level + 1))
            out.append(p + "end")
        elif isinstance(st, A.GenericFor):
            out.append(p + "for %s in %s do" % (
                ", ".join(st.names), ", ".join(self.expr(e) for e in st.exprs)))
            out.extend(self.block(st.body, level + 1))
            out.append(p + "end")
        elif isinstance(st, A.FunctionStat):
            out.extend(self._func_stat(st, p, level))
        elif isinstance(st, A.Return):
            if st.exprs:
                out.append(p + "return " + ", ".join(self.expr(e) for e in st.exprs))
            else:
                out.append(p + "return")
        elif isinstance(st, A.Break):
            out.append(p + "break")
        elif isinstance(st, A.Continue):
            out.append(p + "continue")
        elif isinstance(st, A.Goto):
            out.append(p + "goto " + st.label)
        elif isinstance(st, A.Label):
            out.append(p + "::" + st.name + "::")
        elif isinstance(st, A.TypeAlias):
            out.append(p + ("export type %s = nil" % st.name if st.exported
                            else "type %s = nil" % st.name))
        elif isinstance(st, A.Attr):
            out.append(p + "@" + st.name)
        elif isinstance(st, A.Empty):
            pass
        else:  # pragma: no cover
            raise TypeError(f"cannot unparse statement {type(st).__name__}")
        return out

    def _assign(self, st: A.Assign, p: str) -> list:
        from .parser import _CompoundIndex
        if isinstance(st, _CompoundIndex):
            op = st.op + "="
            return [p + self.expr(st.target, 90) + " " + op + " "
                    + self.expr(st.value)]
        return [p + ", ".join(self.expr(t, 90) for t in st.targets)
                + " = " + ", ".join(self.expr(e) for e in st.exprs)]

    def _func_stat(self, st: A.FunctionStat, p: str, level: int) -> list:
        f = st.func
        if st.is_local:
            head = "local function " + st.target.name
        else:
            head = "function " + self.dotted(st.target)
        args = list(f.params)
        if f.is_vararg:
            args.append("...")
        out = [p + head + "(" + ", ".join(args) + ")"]
        out.extend(self.block(f.body, level + 1))
        out.append(p + "end")
        return out

    def dotted(self, node) -> str:
        """Render a function-declaration target (``a.b.c``) without [] quoting."""
        if isinstance(node, A.Name):
            return node.name
        if isinstance(node, A.Index):
            key = node.key
            if isinstance(key, A.Str):
                try:
                    return self.dotted(node.obj) + "." + key.value.decode()
                except UnicodeDecodeError:
                    pass
            return self.expr(node.obj, 90) + "[" + self.expr(key) + "]"
        return self.expr(node)

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------
    def expr(self, e, parent_pri: int = 0) -> str:
        """Render ``e``, adding parentheses when its precedence is too low."""
        if isinstance(e, A.Nil):
            return "nil"
        if isinstance(e, A.True_):
            return "true"
        if isinstance(e, A.False_):
            return "false"
        if isinstance(e, A.Vararg):
            return "..."
        if isinstance(e, A.Number):
            return quote_number(e.value)
        if isinstance(e, A.Str):
            return quote_string(e.value)
        if isinstance(e, A.Name):
            return e.name
        if isinstance(e, A.Group):
            # `(x)` truncates multiple results -- must be preserved
            return "(" + self.expr(e.expr) + ")"
        if isinstance(e, A.Index):
            obj = self.expr(e.obj, 90)
            if e.dot and isinstance(e.key, A.Str):
                try:
                    return obj + "." + e.key.value.decode()
                except UnicodeDecodeError:
                    pass
            return obj + "[" + self.expr(e.key) + "]"
        if isinstance(e, A.Call):
            if e.method is not None:
                head = self.expr(e.func, 90) + ":" + e.method
            else:
                head = self.expr(e.func, 90)
            if len(e.args) == 1 and isinstance(e.args[0], (A.Str, A.Table)):
                # keep string/table call sugar only when unambiguous
                return head + "(" + ", ".join(self.expr(a) for a in e.args) + ")"
            return head + "(" + ", ".join(self.expr(a) for a in e.args) + ")"
        if isinstance(e, A.Function):
            args = list(e.params)
            if e.is_vararg:
                args.append("...")
            inner = Unparser(self.indent)
            lines = inner.block(e.body, 1)
            body = " ".join(x.strip() for x in lines)
            if body:
                return "function(" + ", ".join(args) + ") " + body + " end"
            return "function(" + ", ".join(args) + ") end"
        if isinstance(e, A.Table):
            parts = []
            for kind, key, val in e.fields:
                if kind == "named":
                    parts.append(bytes(key).decode("utf-8", "surrogateescape")
                                 + " = " + self.expr(val))
                elif kind == "keyed":
                    parts.append("[" + self.expr(key) + "] = " + self.expr(val))
                else:
                    parts.append(self.expr(val))
            return "{" + ", ".join(parts) + "}"
        if isinstance(e, A.BinOp):
            pri = _PRI[e.op]
            # left-assoc ops need the lhs at same priority, rhs one higher;
            # right-assoc (`..` `^`) the other way round
            if e.op in ("..", "^"):
                l = self.expr(e.lhs, pri + 1)
                r = self.expr(e.rhs, pri)
            else:
                l = self.expr(e.lhs, pri)
                r = self.expr(e.rhs, pri + 1)
            s = f"{l} {e.op} {r}"
            return "(" + s + ")" if pri <= parent_pri else s
        if isinstance(e, A.UnOp):
            s = e.op + " " + self.expr(e.expr, _UNARY_PRI)
            return "(" + s + ")" if _UNARY_PRI <= parent_pri else s
        if isinstance(e, A.IfExpr):
            s = "if %s then %s else %s" % (self.expr(e.cond), self.expr(e.then),
                                           self.expr(e.orelse))
            return "(" + s + ")" if parent_pri > 0 else s
        raise TypeError(f"cannot unparse expression {type(e).__name__}")  # pragma: no cover


def unparse(block: A.Block, indent: str = "  ") -> str:
    body = Unparser(indent).block(block, 0)
    return "\n".join(body) + "\n"
