"""Z32 compiler -- AST to Z32 bytecode.

Two passes over the tree:

**1. Resolution.**  Every ``Name`` node is bound to a local, an upvalue or a
global, and every declaration site is recorded.  Doing this *before* code
generation is what makes the captured-local ("cell") model possible: a local
that any nested function captures is *boxed* in a one-element table the moment
it is created, so capturing it is a plain copy of the box.

That design deliberately removes a whole failure class.  The classic
alternative -- capture the register, then "close" upvalues when a scope ends --
has to be exactly right at every ``break``, ``continue``, ``return``, loop
back-edge and ``goto``.  Obfuscators that got it wrong shipped closures which
all shared the last loop value, or which silently stopped tracking after a
``break``.  With boxes, closure capture cannot desynchronise from the variable
it captured: there is nothing to close.

Per-iteration freshness is handled the same way: a captured loop variable gets
a **new box each iteration**, which is exactly Lua's semantics (verified
against the reference implementation: ``for i=1,3 do t[i]=function() return i
end end`` yields 1, 2, 3).

**2. Code generation**, one :class:`Prototype` per function, using Lua's own
register discipline: locals occupy the bottom of the frame, temporaries live
above them, and temporaries are recycled at statement boundaries.

Evaluation order follows the reference implementation everywhere it is
observable -- in particular an indexed assignment evaluates its table and key
*before* the right-hand side (verified: ``t[k()] = v()`` calls ``k`` first),
and a compound assignment evaluates an indexed target exactly once.
"""

from __future__ import annotations

from typing import Optional

from . import ast as A
from .bytecode import K_MAX, Module, OpSpec, Prototype

__all__ = ["compile_chunk", "CompileError", "MAX_REGS", "Compiler"]

MAX_REGS = 240          # register-file guard (Lua's own limit is 200)


class CompileError(Exception):
    def __init__(self, msg: str, line: int = 0):
        super().__init__(f"{msg}")
        self.msg = msg
        self.line = line


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


class Local:
    __slots__ = ("id", "name", "func", "reg", "captured", "is_cell")

    def __init__(self, ident: int, name: str, func):
        self.id = ident
        self.name = name
        self.func = func
        self.reg = -1
        self.captured = False
        self.is_cell = False


class FuncInfo:
    __slots__ = ("parent", "node", "name", "blocks", "locals", "upvals",
                 "upval_lookup", "depth")

    def __init__(self, parent, node, name):
        self.parent = parent
        self.node = node
        self.name = name
        self.blocks: list = [{}]
        self.locals: list = []
        self.upvals: list = []          # ('local', Local) | ('upval', index)
        self.upval_lookup: dict = {}
        self.depth = 0 if parent is None else parent.depth + 1


class Resolver:
    """Binds names and records where every local is declared."""

    def __init__(self):
        self.res: dict = {}          # id(Name node)  -> ('local'|'upval'|'global', ...)
        self.decls: dict = {}        # id(AST node)   -> [Local, ...]
        self.next_id = 0
        self.funcs: list = []
        self.by_node: dict = {}

    # -- scope chain -------------------------------------------------------
    def find_local(self, fs: FuncInfo, name: str):
        for blk in reversed(fs.blocks):
            got = blk.get(name)
            if got is not None:
                return got
        return None

    def add_upval(self, fs: FuncInfo, name: str, kind: str, ref) -> int:
        ident = ref.id if kind == "local" else ref
        key = (kind, ident)
        got = fs.upval_lookup.get(key)
        if got is not None:
            return got
        idx = len(fs.upvals) + 1          # 1-based: UP is a Lua table
        fs.upvals.append((kind, ref))
        fs.upval_lookup[key] = idx
        return idx

    def resolve_upval(self, fs: FuncInfo, name: str) -> Optional[int]:
        if fs.parent is None:
            return None
        outer = self.find_local(fs.parent, name)
        if outer is not None:
            outer.captured = True
            outer.is_cell = True
            return self.add_upval(fs, name, "local", outer)
        idx = self.resolve_upval(fs.parent, name)
        if idx is not None:
            return self.add_upval(fs, name, "upval", idx)
        return None

    def declare(self, fs: FuncInfo, name: str) -> Local:
        loc = Local(self.next_id, name, fs)
        self.next_id += 1
        fs.blocks[-1][name] = loc
        fs.locals.append(loc)
        return loc

    # -- entry -------------------------------------------------------------
    def run(self, block: A.Block) -> FuncInfo:
        main = FuncInfo(None, None, "main")
        self.funcs.append(main)
        self.walk_block(main, block, new_scope=False)
        return main

    # -- walking -----------------------------------------------------------
    def walk_block(self, fs: FuncInfo, block, new_scope: bool = True):
        if new_scope:
            fs.blocks.append({})
        for st in block.stmts:
            self.walk_stat(fs, st)
        if new_scope:
            fs.blocks.pop()

    def walk_function(self, parent: FuncInfo, fn: A.Function):
        fs = FuncInfo(parent, fn, fn.name or "?")
        self.funcs.append(fs)
        self.by_node[id(fn)] = fs
        fs.blocks.append({})
        locs = [self.declare(fs, p) for p in fn.params]
        self.decls[id(fn)] = locs
        self.walk_block(fs, fn.body, new_scope=False)
        fs.blocks.pop()

    def walk_stat(self, fs: FuncInfo, st):
        if isinstance(st, A.Local):
            # the right-hand side resolves *before* the new names exist
            for e in st.exprs:
                self.walk_expr(fs, e)
            self.decls[id(st)] = [self.declare(fs, nm) for nm in st.names]
        elif isinstance(st, A.Assign):
            for t in st.targets:
                self.walk_expr(fs, t)
            for e in st.exprs:
                self.walk_expr(fs, e)
        elif isinstance(st, A.CallStat):
            self.walk_expr(fs, st.call)
        elif isinstance(st, A.Do):
            self.walk_block(fs, st.body)
        elif isinstance(st, A.While):
            self.walk_expr(fs, st.cond)
            self.walk_block(fs, st.body)
        elif isinstance(st, A.Repeat):
            fs.blocks.append({})
            for s in st.body.stmts:
                self.walk_stat(fs, s)
            self.walk_expr(fs, st.cond)     # sees the body's locals
            fs.blocks.pop()
        elif isinstance(st, A.If):
            for cond, body in st.clauses:
                self.walk_expr(fs, cond)
                self.walk_block(fs, body)
            if st.orelse is not None:
                self.walk_block(fs, st.orelse)
        elif isinstance(st, A.NumericFor):
            self.walk_expr(fs, st.start)
            self.walk_expr(fs, st.stop)
            if st.step is not None:
                self.walk_expr(fs, st.step)
            fs.blocks.append({})
            self.decls[id(st)] = [self.declare(fs, st.var)]
            self.walk_block(fs, st.body, new_scope=False)
            fs.blocks.pop()
        elif isinstance(st, A.GenericFor):
            for e in st.exprs:
                self.walk_expr(fs, e)
            fs.blocks.append({})
            self.decls[id(st)] = [self.declare(fs, nm) for nm in st.names]
            self.walk_block(fs, st.body, new_scope=False)
            fs.blocks.pop()
        elif isinstance(st, A.FunctionStat):
            if st.is_local:
                self.decls[id(st)] = [self.declare(fs, st.target.name)]
            else:
                self.walk_expr(fs, st.target)
            self.walk_function(fs, st.func)
        elif isinstance(st, A.Return):
            for e in st.exprs:
                self.walk_expr(fs, e)
        elif isinstance(st, (A.Break, A.Continue, A.Goto, A.Label, A.Empty,
                             A.TypeAlias, A.Attr)):
            pass
        else:  # pragma: no cover
            raise CompileError(f"unhandled statement {type(st).__name__}")

    def walk_expr(self, fs: FuncInfo, e):
        if isinstance(e, A.Name):
            loc = self.find_local(fs, e.name)
            if loc is not None:
                self.res[id(e)] = ("local", loc)
                return
            idx = self.resolve_upval(fs, e.name)
            if idx is not None:
                self.res[id(e)] = ("upval", idx)
                return
            self.res[id(e)] = ("global", e.name)
        elif isinstance(e, A.Index):
            self.walk_expr(fs, e.obj)
            self.walk_expr(fs, e.key)
        elif isinstance(e, A.Call):
            self.walk_expr(fs, e.func)
            for a in e.args:
                self.walk_expr(fs, a)
        elif isinstance(e, A.Function):
            self.walk_function(fs, e)
        elif isinstance(e, A.Table):
            for kind, key, val in e.fields:
                if kind == "keyed":
                    self.walk_expr(fs, key)
                self.walk_expr(fs, val)
        elif isinstance(e, A.BinOp):
            self.walk_expr(fs, e.lhs)
            self.walk_expr(fs, e.rhs)
        elif isinstance(e, A.UnOp):
            self.walk_expr(fs, e.expr)
        elif isinstance(e, A.Group):
            self.walk_expr(fs, e.expr)
        elif isinstance(e, A.IfExpr):
            self.walk_expr(fs, e.cond)
            self.walk_expr(fs, e.then)
            self.walk_expr(fs, e.orelse)
        elif isinstance(e, (A.Nil, A.True_, A.False_, A.Vararg, A.Number,
                            A.Str)):
            pass
        else:  # pragma: no cover
            raise CompileError(f"unhandled expression {type(e).__name__}")


# --------------------------------------------------------------------------
# code generation
# --------------------------------------------------------------------------


class V:
    """A compiled value: the register holding it, and whether we own it."""
    __slots__ = ("r", "own")

    def __init__(self, r: int, own: bool):
        self.r = r
        self.own = own


class Loop:
    __slots__ = ("breaks", "continues", "kind", "top", "cont")

    def __init__(self, kind: str):
        self.breaks: list = []
        self.continues: list = []
        self.kind = kind
        self.top = 0        # where the loop body starts
        self.cont = None    # where `continue` must jump to


_BINOP = {
    "+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "%": "MOD", "^": "POW",
    "//": "IDIV", "&": "BAND", "|": "BOR", "~": "BXOR", "<<": "SHL",
    ">>": "SHR", "..": "CONCAT",
    "==": "EQ", "~=": "NEQ", "<=": "LE", ">=": "GE", "<": "LT", ">": "GT",
}
_UNOP = {"-": "UNM", "not": "NOT", "#": "LEN", "~": "BNOT"}


class FuncState:
    def __init__(self, comp: "Compiler", finfo: FuncInfo, proto: Prototype):
        self.c = comp
        self.spec = comp.spec
        self.finfo = finfo
        self.proto = proto
        self.nactive = 0
        self.freereg = 1
        self.locals: list = []
        self.marks: list = []
        self.loops: list = []
        self.maxstack = 2
        self.labels: dict = {}
        self.gotos: list = []
        self.pending_opens: list = []   # for `and`/`or` patching

    # -- registers ---------------------------------------------------------
    def reserve(self, n: int = 1):
        self.freereg += n
        if self.freereg > MAX_REGS:
            raise CompileError(
                "expression or function too complex: needs more than "
                f"{MAX_REGS} registers")
        if self.freereg > self.maxstack:
            self.maxstack = self.freereg

    def need_stack(self, n: int):
        """Reserve high registers used directly (multi-value staging)."""
        if n > MAX_REGS + 1:
            raise CompileError("function needs too many registers")
        if n > self.maxstack:
            self.maxstack = n

    def alloc(self) -> int:
        r = self.freereg
        self.reserve(1)
        return r

    def release(self, r: int):
        if r < self.freereg:
            self.freereg = r

    def recycle(self):
        """Drop every temporary (locals below nactive stay)."""
        self.freereg = self.nactive + 1

    def result(self, v: V) -> int:
        """Where an operation's result goes: reuse the operand when we own it."""
        if v.own:
            return v.r
        return self.alloc()

    # -- emission ----------------------------------------------------------
    def emit(self, op: str, a: int = 0, b: int = 0, c: int = 0):
        """Register-format instruction."""
        return self.proto.emit(self.spec, op, a, b, c)

    def emit_w(self, op: str, a: int, w: int):
        """Wide-operand instruction (constant index)."""
        return self.proto.emit_w(self.spec, op, a, w)

    def jump(self, op: str, a: int = 0) -> int:
        return self.proto.emit_jump(self.spec, op, a)

    def here(self) -> int:
        return self.proto.pc()

    def patch(self, off: int):
        self.proto.patch_to(off, self.here())

    def k_str(self, value: bytes) -> int:
        k = self.proto.kstr(value)
        if k > K_MAX:
            raise CompileError("too many string constants in one function")
        return k

    def k_num(self, value: float) -> int:
        k = self.proto.knum(value)
        if k > K_MAX:
            raise CompileError("too many numeric constants in one function")
        return k


class Compiler:
    def __init__(self, spec: Optional[OpSpec] = None, jump_bias: int = 0):
        self.spec = spec or OpSpec()
        self.spec.jump_bias = jump_bias & 0xFFFF
        self.module = Module()
        self.resolver = Resolver()

    # ------------------------------------------------------------------
    def compile_chunk(self, block: A.Block) -> Module:
        main = self.resolver.run(block)
        proto = self.module.new_proto("main", 0, True)
        self.module.main = proto.id
        fs = FuncState(self, main, proto)
        fs.reserve(1)                     # register 0: the reserved nil slot
        self.gen_block(fs, block, new_scope=False)
        self.emit_return(fs, [], False)
        proto.maxstack = fs.maxstack
        proto.patch_jumps(self.spec.jump_bias)
        return self.module

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def emit_getidx(self, fs: FuncState, obj: int, dest: int, key: bytes):
        """``dest = obj[key]`` using a constant key when it fits.

        The constant index shares the C operand with its 8-bit range, so a
        function with more than 255 string constants falls back to
        materialising the key in a register -- correct for any input size.
        """
        k = fs.k_str(key)
        if k < 256:
            fs.emit("GETIDX", obj, dest, k)
        else:
            t = fs.alloc()
            fs.emit_w("LOADS", t, k)
            fs.emit("GETTABLE", dest, obj, t)
            fs.release(t)

    def emit_setidx(self, fs: FuncState, obj: int, value: int, key: bytes):
        k = fs.k_str(key)
        if k < 256:
            fs.emit("SETIDX", obj, value, k)
        else:
            t = fs.alloc()
            fs.emit_w("LOADS", t, k)
            fs.emit("SETTABLE", obj, t, value)
            fs.release(t)

    def decls_of(self, node) -> list:
        got = self.resolver.decls.get(id(node))
        if got is None:  # pragma: no cover - defensive
            raise CompileError(f"internal: no declaration recorded for "
                               f"{type(node).__name__}")
        return got

    def res_of(self, node: A.Name):
        got = self.resolver.res.get(id(node))
        if got is None:  # pragma: no cover - defensive
            raise CompileError(f"internal: unresolved name '{node.name}'")
        return got

    def enter(self, fs: FuncState):
        fs.marks.append((fs.nactive, fs.freereg, len(fs.locals)))

    def leave(self, fs: FuncState):
        nactive, freereg, nloc = fs.marks.pop()
        del fs.locals[nloc:]
        fs.nactive = nactive
        fs.recycle()

    def bind_hidden(self, fs: FuncState, n: int) -> int:
        """Reserve `n` anonymous local slots (loop control registers).

        These must count as locals rather than temporaries: `recycle()` resets
        `freereg` to `nactive + 1`, so a control register living above that
        line would be recycled and clobbered by the very next temporary.
        """
        first = fs.nactive + 1
        for i in range(n):
            loc = Local(-1, f"@ctrl{first + i}", fs.finfo)
            self.bind_local(fs, loc)
        return first

    def bind_local(self, fs: FuncState, loc: Local) -> int:
        """Give ``loc`` the next local slot.

        Locals always occupy a contiguous block at the bottom of the frame
        (1..nactive) so that ``recycle()`` can drop every temporary by simply
        resetting ``freereg``.  The caller stores the value afterwards, which
        is a no-op when the value already sits in this slot.
        """
        fs.freereg = fs.nactive + 1
        r = fs.freereg
        fs.reserve(1)
        loc.reg = r
        fs.locals.append(loc)
        fs.nactive += 1
        fs.freereg = fs.nactive + 1
        return r

    # ------------------------------------------------------------------
    # statements
    # ------------------------------------------------------------------
    def gen_block(self, fs: FuncState, block: A.Block, new_scope: bool = True):
        if new_scope:
            self.enter(fs)
        for st in block.stmts:
            self.gen_stat(fs, st)
            fs.recycle()
        if new_scope:
            self.leave(fs)

    def gen_stat(self, fs: FuncState, st):
        if isinstance(st, A.Local):
            self.gen_local(fs, st)
        elif isinstance(st, A.Assign):
            self.gen_assign(fs, st)
        elif isinstance(st, A.CallStat):
            self.gen_call(fs, st.call, 0)
            fs.recycle()
        elif isinstance(st, A.Do):
            self.gen_block(fs, st.body)
        elif isinstance(st, A.While):
            self.gen_while(fs, st)
        elif isinstance(st, A.Repeat):
            self.gen_repeat(fs, st)
        elif isinstance(st, A.If):
            self.gen_if(fs, st)
        elif isinstance(st, A.NumericFor):
            self.gen_numeric_for(fs, st)
        elif isinstance(st, A.GenericFor):
            self.gen_generic_for(fs, st)
        elif isinstance(st, A.FunctionStat):
            self.gen_function_stat(fs, st)
        elif isinstance(st, A.Return):
            self.gen_return(fs, st)
        elif isinstance(st, A.Break):
            if not fs.loops:
                raise CompileError("'break' outside a loop")
            fs.loops[-1].breaks.append(fs.jump("JUMP"))
        elif isinstance(st, A.Continue):
            if not fs.loops:
                raise CompileError("'continue' outside a loop")
            fs.loops[-1].continues.append(fs.jump("JUMP"))
        elif isinstance(st, (A.Empty, A.TypeAlias, A.Attr)):
            pass
        elif isinstance(st, A.Label):
            fs.labels[st.name] = fs.here()
            fs.gotos.append((st.name, None))
        elif isinstance(st, A.Goto):
            fs.gotos.append((st.label, fs.jump("JUMP")))
        else:  # pragma: no cover
            raise CompileError(f"unhandled statement {type(st).__name__}")

    # -- locals ------------------------------------------------------------
    def emit_moves(self, fs: "FuncState", dests: list, srcs: list):
        """Copy values into ``dests`` without destroying an unread source.

        Plain ascending MOVs are wrong whenever a destination register is also
        the source of a later copy -- ``a, b = b, a`` and
        ``local a, b = x, f()`` both hit this.  When that happens every source
        is staged into fresh registers above the whole set first.
        """
        n = len(srcs)
        live = list(srcs)
        clash = any(live[j] == dests[i]
                    for i in range(n) for j in range(i + 1, n))
        if clash:
            hi = max(list(dests) + list(live)) + 1
            staged = [hi + k for k in range(n)]
            fs.need_stack(hi + n)
            for k in range(n):
                fs.emit("MOV", staged[k], live[k])
            live = staged
        for i in range(n):
            if live[i] != dests[i]:
                fs.emit("MOV", dests[i], live[i])

    def gen_local(self, fs: FuncState, st: A.Local):
        locs = self.decls_of(st)
        vals = self.eval_list(fs, st.exprs, len(locs))
        dests = [self.bind_local(fs, loc) for loc in locs]
        srcs = []
        for i in range(len(locs)):
            v = vals[i] if i < len(vals) else None
            if v is None:
                r = fs.alloc()
                fs.emit("LOADNIL", r)
                srcs.append(r)
            else:
                srcs.append(v.r)
        self.emit_moves(fs, dests, srcs)
        for loc in locs:
            if loc.is_cell:
                fs.emit("NEWCELL", loc.reg, loc.reg)
        fs.recycle()

    # -- assignment --------------------------------------------------------
    def gen_assign(self, fs: FuncState, st: A.Assign):
        from .parser import _CompoundIndex
        if isinstance(st, _CompoundIndex):
            self.gen_compound(fs, st)
            return

        # Target subexpressions are evaluated left-to-right BEFORE the values.

        prep: list = []
        for t in st.targets:
            if isinstance(t, A.Name):
                prep.append(("var", self.res_of(t)))
            elif isinstance(t.key, A.Str) and t.dot:
                obj = self.expr(fs, t.obj)
                prep.append(("idx", obj, None, t.key.value))
            else:
                obj = self.expr(fs, t.obj)
                key = self.expr(fs, t.key)
                prep.append(("idx", obj, key, None))

        vals = self.eval_list(fs, st.exprs, len(st.targets))
        got = []
        for i in range(len(st.targets)):
            v = vals[i] if i < len(vals) else None
            if v is None:
                r = fs.alloc()
                fs.emit("LOADNIL", r)
                v = V(r, True)
            got.append(v)
        # a store into a local can overwrite a value another store still needs
        written = [t[1][1].reg for t in prep
                   if t[0] == "var" and t[1][0] == "local"
                   and not t[1][1].is_cell]
        if any(written[j] == got[i].r for i in range(len(got))
               for j in range(i)):
            hi = max(written + [v.r for v in got]) + 1
            staged = []
            fs.need_stack(hi + len(got))
            for k, v in enumerate(got):
                fs.emit("MOV", hi + k, v.r)
                staged.append(V(hi + k, True))
            got = staged
        for i, target in enumerate(prep):
            self.store(fs, target, got[i])
        fs.recycle()

    def store(self, fs: FuncState, target, value: V):
        kind = target[0]
        if kind == "var":
            res = target[1]
            if res[0] == "local":
                loc = res[1]
                if loc.is_cell:
                    fs.emit("SETCELL", value.r, loc.reg)
                elif loc.reg != value.r:
                    fs.emit("MOV", loc.reg, value.r)
            elif res[0] == "upval":
                fs.emit_w("SETUPVAL", value.r, res[1])
            else:
                r = fs.alloc()
                fs.emit_w("LOADS", r, fs.k_str(res[1].encode()))
                fs.emit("SETGLOBAL", value.r, r)
            return
        obj, key, skey = target[1], target[2], target[3]
        if skey is not None:
            self.emit_setidx(fs, obj.r, value.r, skey)
        else:
            fs.emit("SETTABLE", obj.r, key.r, value.r)

    def gen_compound(self, fs: FuncState, st):
        """``x += v`` / ``t[k] += v`` with the target evaluated once."""
        target = st.target
        if isinstance(target, A.Name):
            res = self.res_of(target)
            cur = self.load_var(fs, res)
            rhs = self.expr(fs, st.value)
            dest = fs.result(cur)
            fs.emit(_BINOP[st.op], dest, cur.r, rhs.r)
            v = V(dest, True)
            self.store(fs, ("var", res), v)
        else:
            obj = self.expr(fs, target.obj)
            if isinstance(target.key, A.Str) and target.dot:
                cur = fs.alloc()
                self.emit_getidx(fs, obj.r, cur, target.key.value)
                key = None
                skey = target.key.value
            else:
                key = self.expr(fs, target.key)
                cur = fs.alloc()
                fs.emit("GETTABLE", cur, obj.r, key.r)
                skey = None
            rhs = self.expr(fs, st.value)
            dest = fs.result(cur)
            fs.emit(_BINOP[st.op], dest, cur.r, rhs.r)
            if skey is not None:
                self.emit_setidx(fs, obj.r, dest, skey)
            else:
                fs.emit("SETTABLE", obj.r, key.r, dest)
        fs.recycle()

    # -- control flow ------------------------------------------------------
    def gen_while(self, fs: FuncState, st: A.While):
        top = fs.here()
        cond = self.expr(fs, st.cond)
        jf = fs.jump("JUMPIFNOT", cond.r)
        fs.recycle()
        loop = Loop("while")
        loop.top = top
        loop.cont = top
        fs.loops.append(loop)
        self.gen_block(fs, st.body)
        fs.loops.pop()
        back = fs.jump("JUMP")
        fs.proto.patch_to(back, top)
        end = fs.here()
        self.finish_loop(fs, loop, end, top)
        fs.patch(jf)

    def gen_repeat(self, fs: FuncState, st: A.Repeat):
        top = fs.here()
        self.enter(fs)
        loop = Loop("repeat")
        fs.loops.append(loop)
        for s in st.body.stmts:
            self.gen_stat(fs, s)
            fs.recycle()
        fs.loops.pop()
        loop.cont = fs.here()              # `continue` falls into the test
        cond = self.expr(fs, st.cond)      # still sees the body's locals
        jb = fs.jump("JUMPIFNOT", cond.r)
        fs.proto.patch_to(jb, top)          # false -> run the body again
        self.leave(fs)
        end = fs.here()
        self.finish_loop(fs, loop, end, top)

    def gen_if(self, fs: FuncState, st: A.If):
        ends: list = []
        last = len(st.clauses) - 1
        for i, (cond, body) in enumerate(st.clauses):
            c = self.expr(fs, cond)
            jf = fs.jump("JUMPIFNOT", c.r)
            fs.recycle()
            self.gen_block(fs, body)
            if i < last or st.orelse is not None:
                ends.append(fs.jump("JUMP"))
            fs.patch(jf)
        if st.orelse is not None:
            self.gen_block(fs, st.orelse)
        for j in ends:
            fs.patch(j)

    def gen_numeric_for(self, fs: FuncState, st: A.NumericFor):
        start = self.expr_owned(fs, st.start)
        stop = self.expr_owned(fs, st.stop)
        if st.step is not None:
            stepv = self.expr_owned(fs, st.step)
        else:
            stepv = None
        self.enter(fs)
        base = self.bind_hidden(fs, 3)      # index, limit, step
        fs.emit("MOV", base, start.r)
        fs.emit("MOV", base + 1, stop.r)
        if stepv is not None:
            fs.emit("MOV", base + 2, stepv.r)
        else:
            fs.emit_w("LOADK", base + 2, fs.k_num(1.0))

        prep = fs.jump("FORPREP", base)
        top = fs.here()
        loc = self.decls_of(st)[0]
        r = self.bind_local(fs, loc)
        if r != top:  # pragma: no cover - bind order guarantees this
            pass
        if loc.is_cell:
            fs.emit("NEWCELL", r, r)
        loop = Loop("numericfor")
        loop.top = top
        fs.loops.append(loop)
        self.gen_block(fs, st.body, new_scope=False)
        fs.loops.pop()
        loop_ins = fs.here()               # FORPREP lands on the FORLOOP
        back = fs.jump("FORLOOP", base)
        # `continue` has to land on the FORLOOP so the index still advances;
        # jumping to the body top instead would spin forever
        for j in loop.continues:
            fs.proto.patch_to(j, loop_ins)
        fs.proto.patch_to(back, top)
        fs.proto.patch_to(prep, loop_ins)
        end = fs.here()
        self.finish_loop(fs, loop, end, top)
        self.leave(fs)

    def gen_generic_for(self, fs: FuncState, st: A.GenericFor):
        vals = self.eval_list(fs, st.exprs, 3)
        self.enter(fs)
        base = self.bind_hidden(fs, 3)      # iterator, state, control
        for i in range(3):
            v = vals[i] if i < len(vals) else None
            if v is None:
                fs.emit("LOADNIL", base + i)
            else:
                fs.emit("MOV", base + i, v.r)

        locs = self.decls_of(st)
        for loc in locs:
            self.bind_local(fs, loc)
        # The iterator call lives *after* the body and TFORLOOP jumps back to
        # it (the shape Lua itself emits).  Putting the call before the body
        # runs the body once with a stale control variable and once after the
        # iterator is exhausted.
        prep = fs.jump("JUMP")
        top = fs.here()
        loop = Loop("genericfor")
        loop.top = top
        fs.loops.append(loop)
        self.gen_block(fs, st.body, new_scope=False)
        fs.loops.pop()
        for loc in locs:
            if loc.is_cell:
                fs.emit("NEWCELL", loc.reg, loc.reg)
        loop_ins = fs.here()
        for j in loop.continues:
            fs.proto.patch_to(j, loop_ins)
        fs.emit("TFORCALL", base, len(locs))
        back = fs.jump("TFORLOOP", base)
        fs.proto.patch_to(back, top)
        fs.proto.patch_to(prep, loop_ins)
        fs.proto.patch_to(back, top)
        end = fs.here()
        self.finish_loop(fs, loop, end, top)
        self.leave(fs)

    def finish_loop(self, fs: FuncState, loop: Loop, end: int, top: int):
        """Resolve `break` to the loop exit and `continue` to its step.

        ``top`` is only the fallback: numeric/generic `for` loops patch their
        own continues onto the FORLOOP/TFORCALL so the iteration variable
        still advances.
        """
        for j in loop.breaks:
            fs.proto.patch_to(j, end)
        cont = loop.cont if loop.cont is not None else top
        for j in loop.continues:
            fs.proto.patch_to(j, cont)

    # -- functions ---------------------------------------------------------
    def gen_function_stat(self, fs: FuncState, st: A.FunctionStat):
        if st.is_local:
            loc = self.decls_of(st)[0]
            r = self.bind_local(fs, loc)     # reserve first: recursion works
            if loc.is_cell:
                # Box *before* compiling the body: the nested closure captures
                # register r by value, so an empty cell has to be there first
                # or the function cannot see itself.
                fs.emit("NEWCELL", r, r)
            cl = self.compile_function(fs, st.func)
            if loc.is_cell:
                fs.emit("SETCELL", cl.r, r)
            else:
                fs.emit("MOV", r, cl.r)
            fs.recycle()
            return
        target = st.target
        if isinstance(target, A.Name):
            # `function f() ... end` where f is a global, an upvalue or a
            # local declared earlier -- the target has no `.obj` at all
            res = self.res_of(target)
            cl = self.compile_function(fs, st.func)
            self.store(fs, ("var", res), cl)
            fs.recycle()
            return
        obj = self.expr(fs, target.obj)
        cl = self.compile_function(fs, st.func)
        if isinstance(target.key, A.Str) and target.dot:
            self.emit_setidx(fs, obj.r, cl.r, target.key.value)
        else:  # pragma: no cover
            raise CompileError("unsupported function assignment target")
        fs.recycle()

    def compile_function(self, fs: FuncState, fn: A.Function) -> V:
        finfo = self.resolver.by_node[id(fn)]
        proto = self.module.new_proto(fn.name or "?", len(fn.params),
                                      fn.is_vararg)
        if len(finfo.upvals) > 255:
            raise CompileError("too many upvalues")
        proto.nups = len(finfo.upvals)
        for kind, ref in finfo.upvals:
            if kind == "local":
                if ref.reg < 0:
                    raise CompileError(
                        f"internal: upvalue '{ref.name}' has no register")
                proto.up_kind.append(0)
                proto.up_index.append(ref.reg)
            else:
                proto.up_kind.append(1)
                proto.up_index.append(ref)

        sub = FuncState(self, finfo, proto)
        sub.reserve(1)
        self.enter(sub)
        for loc in self.decls_of(fn):
            self.bind_local(sub, loc)
        self.gen_block(sub, fn.body, new_scope=False)
        self.emit_return(sub, [], False)
        self.leave(sub)
        proto.maxstack = sub.maxstack
        proto.patch_jumps(self.spec.jump_bias)

        r = fs.alloc()
        fs.emit_w("CLOSURE", r, proto.id)
        return V(r, True)

    def gen_return(self, fs: FuncState, st: A.Return):
        if not st.exprs:
            fs.emit("RETURN", 0, 0, 0)
            return
        vals, pending = self.eval_list_multi(fs, st.exprs, 0)
        nfix = len(vals)
        base = vals[0].r if vals else 1
        fs.emit("RETURN", base, 1 if pending else 0, nfix)

    def emit_return(self, fs: FuncState, vals, pending: bool):
        fs.emit("RETURN", 0, 0, 0)

    # ------------------------------------------------------------------
    # expression lists
    # ------------------------------------------------------------------
    def is_multi(self, e) -> bool:
        return isinstance(e, (A.Call, A.Vararg))

    def eval_list(self, fs: FuncState, exprs, want: int) -> list:
        vals, pending = self.eval_list_multi(fs, exprs, want)
        out = list(vals)
        while len(out) < want:
            out.append(None)
        return out[:want] if want else out

    def eval_list_multi(self, fs: FuncState, exprs, want: int):
        """Evaluate an expression list.

        Returns ``(vals, pending)``; ``pending`` means the final expression
        produced an unbounded number of results into the pending buffer.
        """
        if not exprs:
            return [], False
        *fixed, last = exprs
        vals = [self.expr(fs, e, want=1) for e in fixed]
        nfix = len(fixed)
        if self.is_multi(last):
            if want:
                need = want - nfix
                if need <= 0:
                    vals.append(self.expr(fs, last, want=1))
                    return vals, False
                r = self.gen_multi_fixed(fs, last, need)
                # the call/vararg wrote `need` contiguous results starting at
                # r; report them all, or the caller pads the tail with nil and
                # clobbers a real result
                for k in range(need):
                    vals.append(V(r + k, k == 0))
                return vals, False
            r = self.gen_multi(fs, last)
            return vals, True
        vals.append(self.expr(fs, last, want=1))
        return vals, False

    def gen_multi(self, fs: FuncState, e) -> int:
        if isinstance(e, A.Vararg):
            r = fs.alloc()
            fs.emit("VARARG", r, 0)
            return r
        return self.gen_call(fs, e, 0)

    def gen_multi_fixed(self, fs: FuncState, e, n: int) -> int:
        if isinstance(e, A.Vararg):
            r = fs.alloc()
            fs.emit("VARARG", r, n)
            return r
        return self.gen_call(fs, e, n)

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------
    def expr(self, fs: FuncState, e, want: int = 1) -> V:
        if isinstance(e, A.Number):
            r = fs.alloc()
            fs.emit_w("LOADK", r, fs.k_num(e.value))
            return V(r, True)
        if isinstance(e, A.Str):
            r = fs.alloc()
            fs.emit_w("LOADS", r, fs.k_str(e.value))
            return V(r, True)
        if isinstance(e, A.Nil):
            r = fs.alloc()
            fs.emit("LOADNIL", r)
            return V(r, True)
        if isinstance(e, A.True_):
            r = fs.alloc()
            fs.emit("LOADBOOL", r, 1)
            return V(r, True)
        if isinstance(e, A.False_):
            r = fs.alloc()
            fs.emit("LOADBOOL", r, 0)
            return V(r, True)
        if isinstance(e, A.Name):
            return self.load_var(fs, self.res_of(e))
        if isinstance(e, A.Group):
            if self.is_multi(e.expr):
                r = self.gen_multi_fixed(fs, e.expr, 1)
                return V(r, True)
            return self.expr(fs, e.expr, 1)
        if isinstance(e, A.Index):
            return self.gen_index(fs, e)
        if isinstance(e, A.Call):
            r = self.gen_call(fs, e, max(1, want))
            return V(r, True)
        if isinstance(e, A.Vararg):
            r = fs.alloc()
            fs.emit("VARARG", r, max(1, want))
            return V(r, True)
        if isinstance(e, A.Function):
            return self.compile_function(fs, e)
        if isinstance(e, A.Table):
            return self.gen_table(fs, e)
        if isinstance(e, A.BinOp):
            return self.gen_binop(fs, e)
        if isinstance(e, A.UnOp):
            return self.gen_unop(fs, e)
        if isinstance(e, A.IfExpr):
            return self.gen_ifexpr(fs, e)
        raise CompileError(f"unhandled expression {type(e).__name__}")  # pragma: no cover

    def load_var(self, fs: FuncState, res) -> V:
        if res[0] == "local":
            loc = res[1]
            if loc.is_cell:
                r = fs.alloc()
                fs.emit("GETCELL", r, loc.reg)
                return V(r, True)
            return V(loc.reg, False)
        if res[0] == "upval":
            r = fs.alloc()
            fs.emit_w("GETUPVAL", r, res[1])
            return V(r, True)
        r = fs.alloc()
        fs.emit_w("LOADS", r, fs.k_str(res[1].encode()))
        fs.emit("GETGLOBAL", r, r)
        return V(r, True)

    def gen_index(self, fs: FuncState, e: A.Index) -> V:
        obj = self.expr(fs, e.obj)
        r = fs.alloc()
        if isinstance(e.key, A.Str) and e.dot:
            self.emit_getidx(fs, obj.r, r, e.key.value)
        else:
            key = self.expr(fs, e.key)
            fs.emit("GETTABLE", r, obj.r, key.r)
            if key.own:
                fs.release(key.r)
        return V(r, True)

    def gen_table(self, fs: FuncState, e: A.Table) -> V:
        r = fs.alloc()
        # Pre-size the array part to the number of positional slots, which is
        # what a Lua table literal does.  Without it `#{1, 2, 3, nil, 5}` is 3
        # instead of 5, because incremental writes leave a different border in
        # the array part.
        nseq = sum(1 for kind, _k, _v in e.fields if kind == "array")
        fs.emit_w("NEWTABLE", r, nseq)
        seq = 0
        fields = e.fields
        for i, (kind, key, val) in enumerate(fields):
            last = i == len(fields) - 1
            if kind == "array":
                if last and self.is_multi(val):
                    self.gen_multi(fs, val)
                    fs.emit("SETLISTM", r, seq)
                    fs.release(r + 1)
                    continue
                seq += 1
                v = self.expr(fs, val)
                k = fs.alloc()
                fs.emit_w("LOADK", k, fs.k_num(float(seq)))
                fs.emit("SETTABLE", r, k, v.r)
                fs.release(r + 1)
            elif kind == "named":
                v = self.expr(fs, val)
                self.emit_setidx(fs, r, v.r, key)
                fs.release(r + 1)
            else:
                k = self.expr(fs, key)
                v = self.expr(fs, val)
                fs.emit("SETTABLE", r, k.r, v.r)
                fs.release(r + 1)
        return V(r, True)

    def gen_binop(self, fs: FuncState, e: A.BinOp) -> V:
        op = e.op
        if op in ("and", "or"):
            lhs = self.expr_owned(fs, e.lhs)
            j = fs.jump("JUMPIFNOT" if op == "and" else "JUMPIF", lhs.r)
            rhs = self.expr(fs, e.rhs)
            if rhs.r != lhs.r:
                fs.emit("MOV", lhs.r, rhs.r)
                fs.release(lhs.r + 1)
            fs.patch(j)
            return V(lhs.r, True)
        lhs = self.expr(fs, e.lhs)
        rhs = self.expr(fs, e.rhs)
        dest = fs.result(lhs)
        fs.emit(_BINOP[op], dest, lhs.r, rhs.r)
        if rhs.own:
            fs.release(rhs.r)
        if lhs.own and dest != lhs.r:
            fs.release(lhs.r)
        elif not lhs.own:
            fs.release(dest + 1)
        return V(dest, True)

    def gen_unop(self, fs: FuncState, e: A.UnOp) -> V:
        v = self.expr(fs, e.expr)
        dest = fs.result(v)
        fs.emit(_UNOP[e.op], dest, v.r)
        if v.own and dest != v.r:
            fs.release(v.r)
        return V(dest, True)

    def gen_ifexpr(self, fs: FuncState, e: A.IfExpr) -> V:
        dest = fs.alloc()
        c = self.expr(fs, e.cond)
        jf = fs.jump("JUMPIFNOT", c.r)
        fs.release(dest + 1)
        t = self.expr(fs, e.then)
        fs.emit("MOV", dest, t.r)
        jend = fs.jump("JUMP")
        fs.release(dest + 1)
        fs.patch(jf)
        f = self.expr(fs, e.orelse)
        fs.emit("MOV", dest, f.r)
        fs.release(dest + 1)
        fs.patch(jend)
        return V(dest, True)

    def expr_owned(self, fs: FuncState, e) -> V:
        v = self.expr(fs, e)
        if v.own:
            return v
        r = fs.alloc()
        fs.emit("MOV", r, v.r)
        return V(r, True)

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------
    def gen_call(self, fs: FuncState, call: A.Call, nresults: int) -> int:
        pending = 0
        if call.args and self.is_multi(call.args[-1]):
            pending = 1
            fixed = call.args[:-1]
        else:
            fixed = call.args
        nargs = len(fixed)

        if call.method is not None:
            obj = self.expr(fs, call.func)
            base = fs.alloc()      # the function
            fs.alloc()             # the implicit `self`
            mk = fs.k_str(call.method.encode())
            if mk < 256:
                fs.emit("SELF", base, obj.r, mk)
            else:  # pragma: no cover - pathological constant counts
                t = fs.alloc()
                fs.emit_w("LOADS", t, mk)
                fs.emit("GETTABLE", base, obj.r, t)
                fs.emit("MOV", base + 1, obj.r)
                fs.release(t)
            fs.release(base + 2)
            self.load_args(fs, fixed, base + 2)
            if pending:
                self.load_pending_tail(fs, call.args[-1], base + 2 + nargs)
            # `self` counts as the first argument
            fs.emit("CALL", base, (pending << 7) | (nargs + 1), nresults)
            return base

        f = self.expr(fs, call.func)
        base = f.r if f.own else fs.alloc()
        if not f.own:
            fs.emit("MOV", base, f.r)
        fs.release(base + 1)
        self.load_args(fs, fixed, base + 1)
        if pending:
            self.load_pending_tail(fs, call.args[-1], base + 1 + nargs)
        fs.emit("CALL", base, (pending << 7) | nargs, nresults)
        return base

    def load_args(self, fs: FuncState, args, base: int):
        """Materialise the fixed arguments into consecutive registers."""
        for i, a in enumerate(args):
            r = base + i
            fs.freereg = r
            fs.reserve(1)
            v = self.expr(fs, a)
            if v.r != r:
                fs.emit("MOV", r, v.r)

    def load_pending_tail(self, fs: FuncState, e, base: int):
        """Evaluate the trailing multi-result argument into the pending buffer."""
        fs.freereg = base
        fs.reserve(1)
        if isinstance(e, A.Vararg):
            fs.emit("VARARG", base, 0)
        else:
            got = self.gen_call(fs, e, 0)
            if got != base:
                fs.emit("MOV", base, got)


def compile_chunk(block: A.Block, spec: Optional[OpSpec] = None,
                  jump_bias: int = 0) -> Module:
    """Compile a parsed chunk into a Z32 module."""
    return Compiler(spec, jump_bias).compile_chunk(block)
