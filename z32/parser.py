"""Z32 parser -- Lua 5.1 / Lua 5.2+ / Luau recursive-descent parser.

Produces the AST described in :mod:`z32.ast`.  The parser accepts the *union*
of the dialects (superset) so that both "plain Lua" and "Roblox Luau" sources
compile, and it mirrors Luau's semantics precisely where the dialects differ:

* compound assignment evaluates an indexed target **once** (verified against
  the reference implementation),
* ``continue`` is contextual -- ``local continue = 1`` still works,
* Luau type annotations, ``typeof`` types, attributes and ``export type`` are
  parsed and then dropped (they have no runtime meaning),
* string interpolation ``\\`a{b}c\\``` is desugared into ``tostring`` calls and
  concatenation exactly the way Luau does.
"""

from __future__ import annotations

from typing import Optional

from . import ast as A
from .lexer import LexError, Lexer, Token

__all__ = ["Parser", "ParseError", "parse"]


class ParseError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"{line}:{col}: {msg}")
        self.msg = msg
        self.line = line
        self.col = col


# Binary operator priorities -- a single scale that is order-isomorphic to
# both Lua 5.1/5.3 and Luau for every operator they share.
BINARY_PRI = {
    "or": (1, 1),
    "and": (2, 2),
    "<": (3, 3), ">": (3, 3), "<=": (3, 3), ">=": (3, 3),
    "~=": (3, 3), "==": (3, 3),
    "|": (4, 4),
    "~": (5, 5),
    "&": (6, 6),
    "<<": (7, 7), ">>": (7, 7),
    "..": (9, 8),          # right associative
    "+": (10, 10), "-": (10, 10),
    "*": (11, 11), "/": (11, 11), "//": (11, 11), "%": (11, 11),
    "^": (14, 13),         # right associative, binds tighter than unary
}
UNARY_PRI = 12

LUAU_ONLY_ABSENT = frozenset({"<<", ">>"})   # real Luau has no bitwise shifts
BINOPS = frozenset(BINARY_PRI)
UNOPS = frozenset({"not", "-", "#", "~"})

# tokens that may end a statement block
BLOCK_FOLLOW = frozenset({"end", "else", "elseif", "until", "eof"})

# compound-assignment operators (Luau) -> the underlying binary operator
COMPOUND = {
    "+=": "+", "-=": "-", "*=": "*", "/=": "/", "//=": "//", "%=": "%",
    "^=": "^", "..=": "..",
}

# tokens that can continue a name into a larger expression/statement
_SUFFIX_START = frozenset({".", "[", ":", "(", "{", "string", "interp"})


class Parser:
    def __init__(self, src: str, *, luau: bool = True, name: str = "chunk",
                 allow_goto: bool = True):
        self.luau = luau
        self.name = name
        self.allow_goto = allow_goto
        try:
            self.toks = Lexer(src, luau=luau, name=name).tokenize()
        except LexError as e:
            raise ParseError(e.msg, e.line, e.col) from None
        self.pos = 0

    # ------------------------------------------------------------------
    # token helpers
    # ------------------------------------------------------------------
    def peek(self, off: int = 0) -> Token:
        i = self.pos + off
        return self.toks[i] if i < len(self.toks) else self.toks[-1]

    def kind(self, off: int = 0) -> str:
        return self.peek(off).kind

    def next(self) -> Token:
        t = self.toks[self.pos]
        if t.kind != "eof":
            self.pos += 1
        return t

    def check(self, kind: str) -> bool:
        return self.kind() == kind

    def accept(self, kind: str) -> bool:
        if self.check(kind):
            self.pos += 1
            return True
        return False

    def expect(self, kind: str) -> Token:
        if not self.check(kind):
            t = self.peek()
            near = "<eof>" if t.kind == "eof" else repr(t.value)
            raise ParseError(f"'{kind}' expected near {near}", t.line, t.col)
        return self.next()

    def error(self, msg: str, tok: Optional[Token] = None) -> ParseError:
        t = tok or self.peek()
        near = "<eof>" if t.kind == "eof" else repr(t.value)
        return ParseError(f"{msg} near {near}", t.line, t.col)

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def parse_chunk(self) -> A.Block:
        body = self.parse_block()
        if not self.check("eof"):
            t = self.peek()
            raise self.error(f"unexpected symbol (got {t.kind!r})", t)
        return body

    # ------------------------------------------------------------------
    # statements
    # ------------------------------------------------------------------
    def parse_block(self) -> A.Block:
        stmts: list = []
        while self.kind() not in BLOCK_FOLLOW:
            if self.check("return"):
                stmts.append(self.parse_return())
                break
            st = self.parse_statement()
            if st is not None:
                stmts.append(st)
        return A.Block(stmts)

    def parse_return(self) -> A.Return:
        self.expect("return")
        exprs: list = []
        if self.kind() not in BLOCK_FOLLOW and not self.check(";"):
            exprs = self.parse_exprlist()
        self.accept(";")
        return A.Return(exprs)

    def parse_statement(self):
        k = self.kind()

        if k == ";":
            self.next()
            return None
        if k == "@":
            return self.parse_attribute()
        if k == "if":
            return self.parse_if()
        if k == "while":
            return self.parse_while()
        if k == "do":
            self.next()
            body = self.parse_block()
            self.expect("end")
            return A.Do(body)
        if k == "for":
            return self.parse_for()
        if k == "repeat":
            return self.parse_repeat()
        if k == "function":
            return self.parse_function_stat()
        if k == "local":
            return self.parse_local()
        if k == "return":
            return self.parse_return()
        if k == "break":
            self.next()
            return A.Break()
        if k == "::" :
            self.next()
            nm = self.expect("name")
            self.expect("::")
            return A.Label(nm.value)
        if k == "eof":
            raise self.error("'end' expected")

        # ---- Luau contextual statements ---------------------------------
        if k == "name":
            word = self.peek().value
            nxt = self.kind(1)
            if word == "continue" and nxt not in (".", "[", ":", "(", "{",
                                                  "=", ",", "string", "interp",
                                                  "+=", "-=", "*=", "/=", "//=",
                                                  "%=", "^=", "..=", "::"):
                self.next()
                return A.Continue()
            if word == "class" and nxt == "name":
                raise self.error(
                    "`class` declarations are an experimental Luau feature "
                    "that Roblox does not ship yet; Z32 refuses to guess at "
                    "their semantics", self.peek())
            if word == "goto" and self.allow_goto and nxt == "name":
                self.next()
                nm = self.next()
                return A.Goto(nm.value)
            if word == "type" and nxt == "name":
                self.next()
                nm = self.next()
                self.skip_type_alias()
                return A.TypeAlias(nm.value, exported=False)
            if (word == "export" and self.peek(1).kind == "name"
                    and self.peek(1).value == "type"):
                self.next()   # export
                self.next()   # type
                nm = self.expect("name")
                self.skip_type_alias()
                return A.TypeAlias(nm.value, exported=True)

        # ---- expression statement / assignment --------------------------
        return self.parse_expr_statement()

    def parse_attribute(self):
        """Luau ``@native``-style attributes.  Parsed, recorded, dropped."""
        self.expect("@")
        nm = self.expect("name")
        self.accept("(")  # tolerate attribute arguments
        if self.kind() != ")":
            self.skip_balanced("(", ")")
        return A.Attr(nm.value)

    def parse_expr_statement(self):
        start = self.peek()
        expr = self.parse_suffixed_expr()

        # Luau compound assignment
        k = self.kind()
        if k in COMPOUND:
            op = COMPOUND[k]
            if not isinstance(expr, (A.Name, A.Index)):
                raise self.error("left-hand side of assignment must be a variable",
                                 start)
            self.next()
            val = self.parse_expr()
            return self.make_compound(expr, op, val)

        if k == "=" or k == ",":
            targets = [expr]
            while self.accept(","):
                targets.append(self.parse_suffixed_expr())
            self.expect("=")
            exprs = self.parse_exprlist()
            for t in targets:
                if not isinstance(t, (A.Name, A.Index)):
                    raise self.error(
                        "cannot assign to this expression", start)
            return A.Assign(targets, exprs)

        if isinstance(expr, A.Call):
            return A.CallStat(expr)
        raise self.error("syntax error (expected assignment or function call)",
                         start)

    def make_compound(self, target, op: str, value) -> A.Assign:
        """Lower ``t[k] op= v`` the way Luau does: evaluate the target once.

        Uses a fresh internal local so that side effects in ``t``/``k`` happen
        exactly once, matching the reference implementation.
        """
        if isinstance(target, A.Name):
            return A.Assign([target], [A.BinOp(op, target, value)])
        return _CompoundIndex(target, op, value)

    # ---- control flow ---------------------------------------------------
    def parse_if(self):
        self.expect("if")
        if self.check("local"):
            raise self.error(
                "`if local` is an experimental Luau feature that Roblox does "
                "not ship yet")
        clauses = []
        cond = self.parse_expr()
        self.expect("then")
        body = self.parse_block()
        clauses.append((cond, body))
        while self.check("elseif"):
            self.next()
            c = self.parse_expr()
            self.expect("then")
            b = self.parse_block()
            clauses.append((c, b))
        orelse = None
        if self.accept("else"):
            orelse = self.parse_block()
        self.expect("end")
        return A.If(clauses, orelse)

    def parse_while(self):
        self.expect("while")
        cond = self.parse_expr()
        self.expect("do")
        body = self.parse_block()
        self.expect("end")
        return A.While(cond, body)

    def parse_repeat(self):
        self.expect("repeat")
        body = self.parse_block()
        self.expect("until")
        cond = self.parse_expr()
        return A.Repeat(body, cond)

    def parse_for(self):
        self.expect("for")
        first = self.expect("name").value

        if self.accept("="):
            start = self.parse_expr()
            self.expect(",")
            stop = self.parse_expr()
            step = None
            if self.accept(","):
                step = self.parse_expr()
            self.expect("do")
            body = self.parse_block()
            self.expect("end")
            return A.NumericFor(first, start, stop, step, body)

        names = [first]
        if self.accept(":"):
            self.skip_type()
        while self.accept(","):
            nm = self.expect("name").value
            if self.accept(":"):
                self.skip_type()
            names.append(nm)
        self.expect("in")
        exprs = self.parse_exprlist()
        self.expect("do")
        body = self.parse_block()
        self.expect("end")
        return A.GenericFor(names, exprs, body)

    def parse_function_stat(self):
        self.expect("function")
        start = self.peek()
        base = A.Name(self.expect("name").value)
        target = base
        # `function a.b.c` -- keep the raw dotted chain for assignment
        while True:
            if self.accept("."):
                key = self.expect("name")
                target = A.Index(target, A.Str(key.value.encode()), dot=True)
            else:
                break
        method = None
        if self.accept(":"):
            key = self.expect("name")
            method = key.value
            target = A.Index(target, A.Str(key.value.encode()), dot=True)

        func = self.parse_function_body(name_hint=base.name, method=method)
        return A.FunctionStat(target, func, is_local=False)

    def parse_local(self):
        self.expect("local")
        if self.accept("function"):
            nm = self.expect("name")
            func = self.parse_function_body(name_hint=nm.value)
            return A.FunctionStat(A.Name(nm.value), func, is_local=True)

        names = []
        attribs = []
        while True:
            nm = self.expect("name").value
            names.append(nm)
            if self.accept("<"):
                at = self.expect("name").value
                self.expect(">")
                attribs.append(at)
            else:
                attribs.append(None)
            if self.check(":"):
                self.next()
                self.skip_type()
            if not self.accept(","):
                break
        exprs: list = []
        if self.accept("="):
            exprs = self.parse_exprlist()
        return A.Local(names, exprs, attribs)

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------
    def parse_exprlist(self) -> list:
        exprs = [self.parse_expr()]
        while self.accept(","):
            exprs.append(self.parse_expr())
        return exprs

    def parse_expr(self, limit: int = 0) -> A.Node:
        # unary
        if self.kind() in UNOPS:
            op = self.next().kind
            operand = self.parse_expr(UNARY_PRI)
            lhs = A.UnOp(op, operand)
        else:
            lhs = self.parse_simple_expr()

        while True:
            k = self.kind()
            if self.luau and k in LUAU_ONLY_ABSENT:
                break
            pri = BINARY_PRI.get(k)
            if pri is None or pri[0] <= limit:
                break
            self.next()
            rhs = self.parse_expr(pri[1])
            lhs = A.BinOp(k, lhs, rhs)
        return lhs

    def parse_type_assertions(self, node: A.Node) -> A.Node:
        """Luau ``expr :: Type`` -- produces the same value, so drop the type."""
        while self.luau and self.check("::"):
            self.next()
            self.skip_type()
        return node

    def parse_simple_expr(self) -> A.Node:
        k = self.kind()
        if k == "nil":
            self.next()
            return self.parse_type_assertions(A.Nil())
        if k == "true":
            self.next()
            return self.parse_type_assertions(A.True_())
        if k == "false":
            self.next()
            return self.parse_type_assertions(A.False_())
        if k == "integer":
            raise self.error(
                "Luau 64-bit integer literals ('123i') are not available on "
                "Roblox; Z32 will not silently demote them to doubles")
        if k == "number":
            return self.parse_type_assertions(A.Number(self.next().value))
        if k == "string":
            return self.parse_type_assertions(A.Str(self.next().value))
        if k == "interp":
            return self.parse_type_assertions(self.parse_interp(self.next()))
        if k == "...":
            self.next()
            return A.Vararg()
        if k == "function":
            self.next()
            return self.parse_type_assertions(self.parse_function_body())
        if k == "{":
            return self.parse_type_assertions(self.parse_table())
        if k == "if" and self.luau:
            return self.parse_if_expr()
        return self.parse_suffixed_expr()

    def parse_if_expr(self) -> A.IfExpr:
        self.expect("if")
        cond = self.parse_expr()
        self.expect("then")
        then = self.parse_expr()
        if self.check("elseif"):
            self.next()
            nested = self.parse_if_expr_after_elseif()
            return A.IfExpr(cond, then, nested)
        self.expect("else")
        orelse = self.parse_expr()
        return A.IfExpr(cond, then, orelse)

    def parse_if_expr_after_elseif(self) -> A.IfExpr:
        """Continue an if-expression after its ``elseif`` keyword."""
        cond = self.parse_expr()
        self.expect("then")
        then = self.parse_expr()
        if self.check("elseif"):
            self.next()
            return A.IfExpr(cond, then, self.parse_if_expr_after_elseif())
        self.expect("else")
        return A.IfExpr(cond, then, self.parse_expr())

    def parse_interp(self, tok: Token) -> A.Node:
        """Desugar `` `a{b}c` `` exactly the way Luau's compiler does.

        Luau builds one format string with ``%%`` escaping for literal halves
        and ``%*`` for each interpolated expression, then calls
        ``string.format(fmt, e1, e2, ...)``.  Reproducing that (rather than a
        ``..``/``tostring`` chain) keeps even edge cases like ``__tostring``,
        NaN and ``%%`` identical to the reference compiler.
        """
        parts = tok.extra
        fmt = bytearray()
        exprs: list = []
        for p in parts:
            if isinstance(p, bytes):
                fmt.extend(p.replace(b"%", b"%%"))
            else:
                _, inner_src, line, col = p
                sub = Parser(inner_src, luau=self.luau, name=self.name)
                expr = sub.parse_expr()
                if not sub.check("eof"):
                    raise ParseError("unexpected token in interpolation",
                                     line, col)
                fmt.extend(b"%*")
                exprs.append(expr)
        call = A.Call(A.Index(A.Name("string"), A.Str(b"format")),
                      [A.Str(bytes(fmt))] + exprs)
        return call

    def parse_suffixed_expr(self) -> A.Node:
        """prefixexp followed by any number of ``.x [k] :m() (args) {t} "s"``."""
        if self.check("("):
            self.next()
            inner = self.parse_expr()
            self.expect(")")
            expr: A.Node = A.Group(inner)
        elif self.check("name"):
            expr = A.Name(self.next().value)
        else:
            raise self.error("unexpected symbol")

        while True:
            k = self.kind()
            if k == ".":
                self.next()
                nm = self.expect("name")
                expr = A.Index(expr, A.Str(nm.value.encode()), dot=True)
            elif k == "[":
                self.next()
                key = self.parse_expr()
                self.expect("]")
                expr = A.Index(expr, key, dot=False)
            elif k == ":":
                self.next()
                nm = self.expect("name")
                if self.luau and self.check("<") and self.kind(1) == "<":
                    self.skip_type_instantiation()
                args = self.parse_call_args()
                expr = A.Call(expr, args, method=nm.value)
            elif k in ("(", "{", "string", "interp"):
                if k == "(" and self.pos > 0:
                    # Reference rule (Lua 5.1 and Luau agree): a `(` that
                    # begins call arguments on a *new* line is ambiguous --
                    # it could be a new statement.  Both reference compilers
                    # reject it and demand an explicit `;`.  Binding it
                    # silently would merge two statements into one call, so
                    # we reject it too rather than change the program.
                    prev = self.toks[self.pos - 1]
                    if prev.line != self.peek().line:
                        raise ParseError(
                            "ambiguous syntax: this looks like an argument "
                            "list for a function call, but could also be the "
                            "start of a new statement; separate the two "
                            "statements with ';'", self.peek().line,
                            self.peek().col)
                args = self.parse_call_args()
                expr = A.Call(expr, args)
            elif k == "::":
                # Luau type assertion: `expr :: T` -- value is unchanged
                self.next()
                self.skip_type()
            elif (k == "<" and self.luau and self.kind(1) == "<"):
                # Luau explicit type instantiation: `f<<T, U>>(args)`.
                # Luau has no `<<` operator, so this is unambiguous.
                self.skip_type_instantiation()
            else:
                break
        return expr

    def skip_type_instantiation(self) -> None:
        """Consume ``<<T, U>>`` of an explicit type instantiation."""
        self.expect("<")
        self.expect("<")
        depth = 2
        while depth:
            kk = self.kind()
            if kk == "eof":
                raise self.error("'>' expected")
            if kk == "<":
                depth += 1
            elif kk == ">":
                depth -= 1
            self.next()

    def parse_call_args(self) -> list:
        k = self.kind()
        if k == "(":
            self.next()
            args: list = []
            if self.kind() != ")":
                args = self.parse_exprlist()
            self.expect(")")
            return args
        if k == "{":
            return [self.parse_table()]
        if k == "string":
            return [A.Str(self.next().value)]
        if k == "interp":
            return [self.parse_interp(self.next())]
        raise self.error("function arguments expected")

    def parse_table(self) -> A.Table:
        self.expect("{")
        fields: list = []
        while not self.check("}"):
            if self.check("["):
                self.next()
                key = self.parse_expr()
                self.expect("]")
                self.expect("=")
                fields.append(("keyed", key, self.parse_expr()))
            elif self.kind() == "name" and self.kind(1) == "=":
                nm = self.next().value
                self.next()  # '='
                fields.append(("named", nm.encode(), self.parse_expr()))
            else:
                fields.append(("array", None, self.parse_expr()))
            if not (self.accept(",") or self.accept(";")):
                break
        self.expect("}")
        return A.Table(fields)

    def parse_function_body(self, name_hint: Optional[str] = None,
                            method: Optional[str] = None) -> A.Function:
        line = self.peek().line
        if self.check("<"):
            # generic function: `function f<T, U>(...)`
            self.skip_balanced("<", ">")
        self.expect("(")
        params: list = []
        is_vararg = False
        if method:
            params.append("self")
        if self.kind() != ")":
            while True:
                if self.check("..."):
                    self.next()
                    if self.accept(":"):
                        self.skip_type()
                    is_vararg = True
                    break
                nm = self.expect("name")
                params.append(nm.value)
                if self.accept(":"):
                    self.skip_type()
                if not self.accept(","):
                    break
        self.expect(")")
        if self.accept(":"):        # return type annotation
            self.skip_type()
        body = self.parse_block()
        self.expect("end")
        return A.Function(params, is_vararg, body, name_hint, line)

    # ------------------------------------------------------------------
    # Luau type grammar (parsed only to be skipped)
    # ------------------------------------------------------------------
    def skip_type_alias(self) -> None:
        """Consume ``= <type>`` for ``type X = ...`` / ``export type X = ...``."""
        # `type Map<K, V> = ...`
        if self.check("<"):
            self.skip_balanced("<", ">")
        if self.accept("="):
            self.skip_type()

    def skip_type(self) -> None:
        """Skip one complete Luau type expression."""
        self._skip_type_union()

    def _skip_type_union(self) -> None:
        while True:
            self._skip_type_prefix()
            if self.accept("|") or self.accept("&"):
                continue
            break

    def _skip_type_prefix(self) -> None:
        # `~T` negation is not a Luau thing, but tolerate it
        self.accept("~")
        self._skip_type_maybe()

    def _skip_type_maybe(self) -> None:
        self._skip_type_simple()
        if self.check("..."):
            # type pack suffix, e.g. `<T...>(...: T...)`
            self.next()
        # optional marker and/or function arrow
        while True:
            if self.check("?"):
                self.next()
                continue
            if self.check("->"):
                self.next()
                self._skip_type_union()
                return
            break

    def _skip_type_simple(self) -> None:
        k = self.kind()
        if k == "(":
            self.skip_balanced("(", ")")
            return
        if k == "{":
            self.skip_balanced("{", "}")
            return
        if k == "[":
            self.skip_balanced("[", "]")
            return
        if k == "<":
            self.skip_balanced("<", ">")
            return
        if k == "string":
            self.next()
            return
        if k == "number":
            self.next()
            return
        if k in ("nil", "true", "false"):
            self.next()
            return
        if k == "...":
            self.next()
            return
        if k == "name":
            word = self.peek().value
            self.next()
            if word == "typeof" and self.check("("):
                # typeof(<expr>) -- balanced skip is enough
                self.skip_balanced("(", ")")
            if self.check("."):
                # qualified name in a type
                while self.accept("."):
                    self.expect("name")
            if self.check("<"):
                self.skip_balanced("<", ">")
            return
        if k == "-":
            # negative literal type
            self.next()
            self._skip_type_simple()
            return
        raise self.error("malformed type annotation")

    def skip_balanced(self, open_kind: str, close_kind: str) -> None:
        self.expect(open_kind)
        depth = 1
        while depth:
            k = self.kind()
            if k == "eof":
                raise self.error(f"'{close_kind}' expected")
            if k == open_kind:
                depth += 1
            elif k == close_kind:
                depth -= 1
            self.next()


# --------------------------------------------------------------------------
# internal node for indexed compound assignment
# --------------------------------------------------------------------------
class _CompoundIndex(A.Assign):
    """``t[k] op= v`` with single evaluation of ``t`` and ``k``."""

    __slots__ = ("target", "op", "value")

    def __init__(self, target: A.Index, op: str, value):
        self.target = target
        self.op = op
        self.value = value
        super().__init__([target], [value])


def parse(src: str, *, luau: bool = True, name: str = "chunk") -> A.Block:
    return Parser(src, luau=luau, name=name).parse_chunk()
