"""A reference Z32 bytecode interpreter, in Python.

This exists purely as a debugging oracle: it executes a compiled
:class:`~z32.bytecode.Module` directly, with no Lua involved, so a failing
end-to-end test can be attributed to either the compiler or the generated VM.
It is deliberately written for clarity rather than speed.
"""

from __future__ import annotations

import math
import sys

from z32.bytecode import INS_SIZE, OPNAMES, decode_w

TRACE = False


class Z32Error(Exception):
    pass


class Frame:
    def __init__(self, proto, upvals, args, host):
        self.p = proto
        self.up = upvals
        self.R = {}
        self.host = host
        self.pc = 0
        self.mva = None
        self.mvn = 0
        self.va = list(args)
        np = proto.numparams
        for j in range(np):
            self.R[j + 1] = self.va[j] if j < len(self.va) else None
        if proto.is_vararg:
            self.va = self.va[np:]
        else:
            self.va = []


class Interp:
    def __init__(self, module, env=None, trace=False):
        self.m = module
        self.env = env if env is not None else {}
        self.trace = trace

    # -- helpers -----------------------------------------------------------
    def truthy(self, v):
        return v is not None and v is not False

    def k(self, fr, w):
        return fr.p.nums[w] if w < len(fr.p.nums) else None

    def ks(self, fr, w):
        if w >= len(fr.p.strs):
            return None
        raw = fr.p.strs[w]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw

    # -- execution ---------------------------------------------------------
    def call(self, f, args):
        if isinstance(f, Closure):
            return self.run(f.proto, f.up, args)
        if callable(f):
            r = f(*args)
            return [] if r is None else (r if isinstance(r, list) else [r])
        raise Z32Error(f"attempt to call a {type(f).__name__} value")

    def run(self, proto, up, args, depth=0):
        if depth > 200:
            raise Z32Error("stack overflow")
        fr = Frame(proto, up, args, self)
        R = fr.R
        code = proto.code
        n = len(code) // INS_SIZE
        while fr.pc < n:
            i = fr.pc
            off = i * INS_SIZE
            o, a, b, c = code[off], code[off + 1], code[off + 2], code[off + 3]
            name = OPNAMES[o]
            fr.pc = i + 1
            if self.trace:
                print(f"  {name:<9} A={a} B={b} C={c}", file=sys.stderr)
            if name == "NOP":
                pass
            elif name == "MOV":
                R[a] = R.get(b)
            elif name in ("ADD", "SUB", "MUL", "DIV", "MOD", "POW"):
                x, y = R.get(b), R.get(c)
                R[a] = {"ADD": lambda: x + y, "SUB": lambda: x - y,
                        "MUL": lambda: x * y, "DIV": lambda: x / y,
                        "MOD": lambda: x % y, "POW": lambda: x ** y}[name]()
            elif name == "IDIV":
                R[a] = math.floor(R.get(b) / R.get(c))
            elif name == "CONCAT":
                R[a] = f"{R.get(b)}{R.get(c)}"
            elif name == "UNM":
                R[a] = -R.get(b)
            elif name == "NOT":
                R[a] = not self.truthy(R.get(b))
            elif name == "LEN":
                R[a] = len(R.get(b))
            elif name == "EQ":
                R[a] = R.get(b) == R.get(c)
            elif name == "NEQ":
                R[a] = R.get(b) != R.get(c)
            elif name == "LE":
                R[a] = R.get(b) <= R.get(c)
            elif name == "GE":
                R[a] = R.get(b) >= R.get(c)
            elif name == "LT":
                R[a] = R.get(b) < R.get(c)
            elif name == "GT":
                R[a] = R.get(b) > R.get(c)
            elif name == "ISTRUE":
                R[a] = self.truthy(R.get(b))
            elif name == "LOADK":
                R[a] = self.k(fr, b)
            elif name == "LOADS":
                R[a] = self.ks(fr, b)
            elif name == "LOADBOOL":
                R[a] = b != 0
            elif name == "LOADNIL":
                R[a] = None
            elif name == "NEWTABLE":
                R[a] = {}
            elif name == "NEWCELL":
                R[a] = [R.get(b)]
            elif name == "GETCELL":
                R[a] = R[b][0]
            elif name == "SETCELL":
                R[b][0] = R.get(a)
            elif name == "GETGLOBAL":
                R[a] = self.env.get(R.get(b))
            elif name == "SETGLOBAL":
                self.env[R.get(b)] = R.get(a)
            elif name == "GETUPVAL":
                R[a] = up[b][0]
            elif name == "SETUPVAL":
                up[b][0] = R.get(a)
            elif name == "GETTABLE":
                R[a] = R[b][R.get(c)]
            elif name == "SETTABLE":
                R[a][R.get(b)] = R.get(c)
            elif name == "GETIDX":
                R[b] = R[a][self.ks(fr, c)]
            elif name == "SETIDX":
                R[a][self.ks(fr, c)] = R.get(b)
            elif name == "SELF":
                t = R[b]
                R[a] = t[self.ks(fr, c)]
                R[a + 1] = t
            elif name == "CALL":
                x = a
                f = R.get(x)
                inf = b
                nr = c
                na = inf % 128
                args = [R.get(x + i) for i in range(1, na + 1)]
                if inf > 127:
                    if fr.mva:
                        args += fr.mva[:fr.mvn]
                got = list(self.call(f, args))
                if nr == 0:
                    for j, v in enumerate(got):
                        R[x + j] = v
                    fr.mva, fr.mvn = got, len(got)
                else:
                    for j in range(nr):
                        R[x + j] = got[j] if j < len(got) else None
            elif name == "VARARG":
                nr = b
                xs = fr.va
                if nr == 0:
                    for j, v in enumerate(xs):
                        R[a + j] = v
                    fr.mva, fr.mvn = xs, len(xs)
                else:
                    for j in range(nr):
                        R[a + j] = xs[j] if j < len(xs) else None
            elif name == "RETURN":
                x, nx = a, c
                out = [R.get(x + j) for j in range(nx)]
                if b != 0 and fr.mva:
                    out += fr.mva[:fr.mvn]
                return out
            elif name == "JUMP":
                fr.pc = i + 1 + decode_w(b, c)
            elif name == "JUMPIF":
                fr.pc = i + 1 + decode_w(b, c) if self.truthy(R.get(a)) else i + 1
            elif name == "JUMPIFNOT":
                fr.pc = i + 1 if self.truthy(R.get(a)) else i + 1 + decode_w(b, c)
            elif name == "CLOSURE":
                child = decode_w(b, c)
                cp = self.m.protos[child]
                ups = []
                for kind, idx in zip(cp.up_kind, cp.up_index):
                    ups.append(R[idx] if kind == 0 else up[idx])
                R[a] = Closure(cp, ups, self)
            elif name == "FORPREP":
                base = a
                i0, l0, s0 = R.get(base), R.get(base + 1), R.get(base + 2)
                if s0 == 0:
                    raise Z32Error("'for' step is zero")
                R[base] = i0 - s0
                fr.pc = i + 1 + decode_w(b, c)
            elif name == "FORLOOP":
                base = a
                st = R[base + 2]
                ix = R[base] + st
                if (st > 0 and ix <= R[base + 1]) or (st < 0 and ix >= R[base + 1]):
                    R[base] = ix
                    R[base + 3] = ix
                    fr.pc = i + 1 + decode_w(b, c)
            elif name == "TFORCALL":
                base, nv = a, b
                got = list(self.call(R[base], [R.get(base + 1), R.get(base + 2)]))
                for j in range(nv):
                    R[base + 2 + j + 1] = got[j] if j < len(got) else None
            elif name == "TFORLOOP":
                base = a
                if R.get(base + 3) is not None:
                    R[base + 2] = R[base + 3]
                    fr.pc = i + 1 + decode_w(b, c)
            elif name == "SETLISTM":
                t, o = R[a], b
                for j in range(fr.mvn):
                    t[o + j + 1] = fr.mva[j]
            elif name == "CLOSE":
                pass
            else:
                raise Z32Error(f"unimplemented opcode {name}")
        return []


class Closure:
    def __init__(self, proto, up, interp):
        self.proto = proto
        self.up = up
        self.interp = interp

    def __call__(self, *args):
        return self.interp.run(self.proto, self.up, list(args))


def run_module(module, env=None, trace=False):
    it = Interp(module, env, trace)
    main = module.protos[module.main]
    return it.run(main, [], [])
