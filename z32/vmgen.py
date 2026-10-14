"""Z32 VM generator -- compiled modules to a standalone Lua artifact.

The generated program contains, in order:

1. a preamble of decoy locals derived from the user's own identifiers (the
   highest-value litter: it makes the artifact read like a continuation of the
   protected script rather than a VM dump),
2. the encrypted prototype blobs,
3. decoders that are only reachable through the dispatch loop,
4. the virtual machine, emitted as a randomised dispatch tree over
   per-artifact opcode numbers, and
5. a bootstrap that starts the main prototype.

Design choices worth calling out
--------------------------------

**Two wire formats, no floating point in the decoder.**  Register instructions
are ``[op][A][B][C]``; wide instructions are ``[op][A][lo][hi]``.  Every operand
is plain byte arithmetic on the instruction stream, so there is no exponent
edge case to get wrong -- a family of bugs that has bitten real obfuscators,
whose constant decoders broke on denormal, infinity or non-canonical NaN
values that were perfectly legal in the original script.

**Exact numeric constants.**  Doubles travel as their shortest
round-tripping decimal text and are converted with ``tonumber``.  That is
lossless for every double, including denormals, infinities and NaN, and needs
no bit-level float reconstruction at all.

**Per-prototype lazy decryption.**  Each function's code and constants live in
their own encrypted blob and are decrypted only the first time that function is
*called*.  A dump of the running program therefore never contains the whole
script at once, and a hostile environment that captures decryptions only ever
sees the functions that actually ran.

**One dispatch tree per artifact.**  Opcodes are numbers, the tree is a binary
search on opcode value with randomly chosen thresholds, jumps are additively
biased, prototype numbering is shuffled, and every identifier is re-randomised.
A deobfuscator written against one build does not transfer to the next.
"""

from __future__ import annotations

import base64
import math
import random
import struct
from typing import Optional

from .bytecode import INS_SIZE, OP, OPNAMES, OpSpec, decode_ins

__all__ = ["generate", "VMGen", "xor_bytes", "serialize_proto"]

LUA_KEYWORDS = frozenset("""
and break do else elseif end false for function if in local nil not or
repeat return then true until while goto continue type export self
""".split())


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def _u8(v: int) -> bytes:
    return bytes((v & 255,))


def _u16(v: int) -> bytes:
    return bytes((v & 255, (v >> 8) & 255))


def _put_counted(out: bytearray, data: bytes):
    if len(data) > 65535:
        raise ValueError("component too large")
    out += _u16(len(data))
    out += data


def num_text(v: float) -> bytes:
    """Shortest decimal text that round-trips a double exactly."""
    if v != v:
        return b"\x00nan"
    if v == math.inf:
        return b"\x00inf"
    if v == -math.inf:
        return b"\x00-inf"
    for prec in (15, 16, 17):
        s = "%.*g" % (prec, v)
        if float(s) == v:
            return s.encode()
    return ("%.17g" % v).encode()


def serialize_proto(p) -> bytes:
    """Binary form of one prototype, ready to be encrypted."""
    out = bytearray()
    out += _u8(p.numparams)
    out += _u8(1 if p.is_vararg else 0)
    out += _u8(p.maxstack)
    out += _u8(len(p.up_index))
    for kind, idx in zip(p.up_kind, p.up_index):
        out += _u8(((128 if kind else 0) | (idx & 127)))
    nins = len(p.code) // INS_SIZE
    out += _u16(nins)
    out += bytes(p.code)
    out += _u16(len(p.nums))
    for v in p.nums:
        _put_counted(out, num_text(v))
    out += _u16(len(p.strs))
    for s in p.strs:
        _put_counted(out, s)
    return bytes(out)


def xor_bytes(data: bytes, key: bytes) -> bytes:
    n = len(key)
    return bytes(b ^ key[i % n] for i, b in enumerate(data))


# --------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------


BITFALLBACK = """  {BAND} = function(a, b)
    local r, p = 0, 1
    while a > 0 and b > 0 do
      if a % 2 >= 1 and b % 2 >= 1 then r = r + p end
      a = {FLOOR}(a / 2) b = {FLOOR}(b / 2) p = p * 2
    end
    return r
  end
  {BOR} = function(a, b)
    local r, p = 0, 1
    while a > 0 or b > 0 do
      if a % 2 >= 1 or b % 2 >= 1 then r = r + p end
      a = {FLOOR}(a / 2) b = {FLOOR}(b / 2) p = p * 2
    end
    return r
  end
  {BXOR} = function(a, b)
    local r, p = 0, 1
    while a > 0 or b > 0 do
      if (a % 2) ~= (b % 2) then r = r + p end
      a = {FLOOR}(a / 2) b = {FLOOR}(b / 2) p = p * 2
    end
    return r
    end
  {SHL} = function(a, n) return {FLOOR}(a * 2 ^ n) end
  {SHR} = function(a, n) return {FLOOR}(a / 2 ^ n) end
  {BNOT} = function(a) return 4294967295 - {BAND}(a, 4294967295) end
"""


class NameGen:
    """Deterministic-random identifiers that cannot collide with user code."""

    ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.used: set = set()

    def __call__(self) -> str:
        while True:
            n = self.rng.choice((2, 3, 3, 4))
            s = "".join(self.rng.choice(self.ALPHABET) for _ in range(n))
            if s in self.used or s in LUA_KEYWORDS:
                continue
            self.used.add(s)
            return s


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

# Lua snippets for every opcode.  `pc` is the instruction index; `R` the
# register file; `S/RA/RB/RC` the decoded instruction arrays; `KN/KS` the
# constant pools; `UP` the upvalue cells; `mva/mvn` the pending multi-value
# buffer.  Each handler ends by advancing or setting `pc`.
HANDLERS = {
    "NOP": "pc = pc + 1",
    "MOV": "R[RA[pc]] = R[RB[pc]] pc = pc + 1",
    "ADD": "R[RA[pc]] = R[RB[pc]] + R[RC[pc]] pc = pc + 1",
    "SUB": "R[RA[pc]] = R[RB[pc]] - R[RC[pc]] pc = pc + 1",
    "MUL": "R[RA[pc]] = R[RB[pc]] * R[RC[pc]] pc = pc + 1",
    "DIV": "R[RA[pc]] = R[RB[pc]] / R[RC[pc]] pc = pc + 1",
    "MOD": "R[RA[pc]] = R[RB[pc]] % R[RC[pc]] pc = pc + 1",
    "POW": "R[RA[pc]] = R[RB[pc]] ^ R[RC[pc]] pc = pc + 1",
    "IDIV": "R[RA[pc]] = IDIV(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "BAND": "R[RA[pc]] = BAND(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "BOR": "R[RA[pc]] = BOR(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "BXOR": "R[RA[pc]] = BXOR(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "SHL": "R[RA[pc]] = SHL(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "SHR": "R[RA[pc]] = SHR(R[RB[pc]], R[RC[pc]]) pc = pc + 1",
    "CONCAT": "R[RA[pc]] = R[RB[pc]] .. R[RC[pc]] pc = pc + 1",
    "UNM": "R[RA[pc]] = -R[RB[pc]] pc = pc + 1",
    "NOT": "R[RA[pc]] = not R[RB[pc]] pc = pc + 1",
    "LEN": "R[RA[pc]] = #R[RB[pc]] pc = pc + 1",
    "BNOT": "R[RA[pc]] = BNOT(R[RB[pc]]) pc = pc + 1",
    "EQ": "R[RA[pc]] = (R[RB[pc]] == R[RC[pc]]) pc = pc + 1",
    "NEQ": "R[RA[pc]] = (R[RB[pc]] ~= R[RC[pc]]) pc = pc + 1",
    "LE": "R[RA[pc]] = (R[RB[pc]] <= R[RC[pc]]) pc = pc + 1",
    "GE": "R[RA[pc]] = (R[RB[pc]] >= R[RC[pc]]) pc = pc + 1",
    "LT": "R[RA[pc]] = (R[RB[pc]] < R[RC[pc]]) pc = pc + 1",
    "GT": "R[RA[pc]] = (R[RB[pc]] > R[RC[pc]]) pc = pc + 1",
    "ISTRUE": ("local q = R[RB[pc]] "
               "R[RA[pc]] = (q ~= nil and q ~= false) pc = pc + 1"),
    "LOADK": "R[RA[pc]] = KN[RB[pc]] pc = pc + 1",
    "LOADS": "R[RA[pc]] = KS[RB[pc]] pc = pc + 1",
    "LOADBOOL": "R[RA[pc]] = (RB[pc] ~= 0) pc = pc + 1",
    "LOADNIL": "R[RA[pc]] = nil pc = pc + 1",
    "NEWTABLE": "R[RA[pc]] = NEWT(RB[pc]) pc = pc + 1",
    "NEWCELL": "local t = {} t[1] = R[RB[pc]] R[RA[pc]] = t pc = pc + 1",
    "GETCELL": "R[RA[pc]] = R[RB[pc]][1] pc = pc + 1",
    "SETCELL": "R[RB[pc]][1] = R[RA[pc]] pc = pc + 1",
    "GETGLOBAL": "R[RA[pc]] = ENV[R[RB[pc]]] pc = pc + 1",
    # B is a *register holding the key*, exactly as in GETGLOBAL; using the
    # register number itself writes to the wrong slot and the assignment
    # silently disappears.
    "SETGLOBAL": "ENV[R[RB[pc]]] = R[RA[pc]] pc = pc + 1",
    "GETUPVAL": "R[RA[pc]] = UP[RB[pc]][1] pc = pc + 1",
    "SETUPVAL": "UP[RB[pc]][1] = R[RA[pc]] pc = pc + 1",
    "GETTABLE": "R[RA[pc]] = R[RB[pc]][R[RC[pc]]] pc = pc + 1",
    "SETTABLE": "R[RA[pc]][R[RB[pc]]] = R[RC[pc]] pc = pc + 1",
    "GETIDX": "R[RB[pc]] = R[RA[pc]][KS[RC[pc]]] pc = pc + 1",
    "SETIDX": "R[RA[pc]][KS[RC[pc]]] = R[RB[pc]] pc = pc + 1",
    "SELF": ("local t = R[RB[pc]] local x = RA[pc] "
             "R[x] = t[KS[RC[pc]]] R[x + 1] = t pc = pc + 1"),
    "CLOSURE": ("local x = RA[pc] local q = GETPROTO(RB[pc]) "
                "local u = {} local uk = q[7] local ui = q[8] "
                "for i = 1, #ui do "
                "if uk[i] == 0 then u[i] = R[ui[i]] else u[i] = UP[ui[i]] end "
                "end "
                "R[x] = MK(RB[pc], u) pc = pc + 1"),
    "JUMP": "pc = pc + 1 + RB[pc]",
    "JUMPIF": ("if R[RA[pc]] then pc = pc + 1 + RB[pc] "
               "else pc = pc + 1 end"),
    "JUMPIFNOT": ("if R[RA[pc]] then pc = pc + 1 "
                  "else pc = pc + 1 + RB[pc] end"),
    "CLOSE": "pc = pc + 1",
    "SETLISTM": ("local t = R[RA[pc]] local o = RB[pc] "
                 "for i = 1, mvn do t[o + i] = mva[i] end pc = pc + 1"),
}

# CALL / VARARG / RETURN need multi-line bodies.
CALL_BODY = """local x = RA[pc] local f = R[x] local inf = RB[pc] local nr = RC[pc]
local na = inf % 128
local ar = {}
for i = 1, na do ar[i] = R[x + i] end
if inf > 127 then
  for i = 1, mvn do ar[na + i] = mva[i] end
  na = na + mvn
end
local res = { f(UNPACK(ar, 1, na)) }
if nr == 0 then
  for i = 1, #res do R[x + i - 1] = res[i] end
  mva, mvn = res, #res
else
  for i = 1, nr do R[x + i - 1] = res[i] end
end
pc = pc + 1"""

VARARG_BODY = """local x = RA[pc] local nr = RB[pc]
if nr == 0 then
  local r = {}
  for i = 1, van do r[i] = va[vo + i] end
  for i = 1, van do R[x + i - 1] = r[i] end
  mva, mvn = r, van
else
  for i = 1, nr do R[x + i - 1] = va[vo + i] end
end
pc = pc + 1"""

RETURN_BODY = """local x = RA[pc] local nx = RC[pc]
if RB[pc] ~= 0 then
  local r = {}
  for i = 1, nx do r[i] = R[x + i - 1] end
  for i = 1, mvn do r[nx + i] = mva[i] end
  return UNPACK(r, 1, nx + mvn)
end
if nx == 0 then return end
return UNPACK(R, x, x + nx - 1)"""

FORPREP_BODY = """local b = RA[pc]
local i0, l0, s0 = R[b], R[b + 1], R[b + 2]
if TYPE(i0) ~= "number" then ERROR("'for' initial value must be a number") end
if TYPE(l0) ~= "number" then ERROR("'for' limit must be a number") end
if TYPE(s0) ~= "number" then ERROR("'for' step must be a number") end
if s0 == 0 then ERROR("'for' step is zero") end
R[b] = i0 - s0
pc = pc + 1 + RB[pc]"""

FORLOOP_BODY = """local b = RA[pc]
local st = R[b + 2]
local ix = R[b] + st
if (st > 0 and ix <= R[b + 1]) or (st < 0 and ix >= R[b + 1]) then
  R[b] = ix
  R[b + 3] = ix
  pc = pc + 1 + RB[pc]
else
  pc = pc + 1
end"""

TFORCALL_BODY = """local b = RA[pc] local nv = RB[pc]
local res = { R[b](R[b + 1], R[b + 2]) }
for i = 1, nv do R[b + 2 + i] = res[i] end
pc = pc + 1"""

TFORLOOP_BODY = """local b = RA[pc]
if R[b + 3] ~= nil then
  R[b + 2] = R[b + 3]
  pc = pc + 1 + RB[pc]
else
  pc = pc + 1
end"""


class VMGen:
    def __init__(self, module, spec: Optional[OpSpec] = None,
                 rng: Optional[random.Random] = None, target: str = "luau",
                 spice: Optional[list] = None, **kw):
        self.module = module
        self.rng = rng or random.Random()
        self.spec = spec or OpSpec()
        self.target = target
        self.spice = spice or []
        self.names_gen = NameGen(self.rng)
        self.v: dict = {}

    # ------------------------------------------------------------------
    def name(self) -> str:
        return self.names_gen()

    def reserve_names(self, names):
        """Claim user-supplied identifiers so generated names avoid them."""
        for n in names:
            self.names_gen.used.add(n)

    def setup_names(self):
        # Idempotent on purpose: calling it twice would hand the preamble and
        # the interpreter two *different* name tables, silently rebinding every
        # hoisted capability (TYPE would end up holding `error`, and so on).
        if getattr(self, "v", None):
            return
        self.v = {k: self.name() for k in (
            "S RA RB RC KN KS UP R pc mva mvn va vo van UNPACK MK GETPROTO "
            "JUMPY NEWT "
            "TYPE ERROR BAND BOR BXOR SHL SHR BNOT IDIV FLOOR BYTE SUB CHAR "
            "UNPACK2 CONCAT ENV PROTO BIAS WIDE RUN LUT AB TABLE blobs DEC NEWT "
            "B64LUT TMP main".split())}

    # ------------------------------------------------------------------
    def generate(self) -> str:
        self.setup_names()
        self.reserve_names(self.spice)
        rng = self.rng

        # -- shuffle prototype numbering so indices leak no structure ------
        nprotos = len(self.module.protos)
        order = list(range(nprotos))
        rng.shuffle(order)
        remap = {old: new for new, old in enumerate(order)}
        for p in self.module.protos:
            code = bytearray(p.code)
            for i in range(0, len(code), INS_SIZE):
                op = code[i]
                # `op` is a *wire* number: decode the permutation first, or
                # this never matches and CLOSURE keeps pointing at the proto
                # index from before the shuffle
                if OPNAMES[self.spec.decode_op(op)] == "CLOSURE":
                    child = code[i + 2] | (code[i + 3] << 8)
                    if child in remap:
                        nw = remap[child]
                        code[i + 2] = nw & 255
                        code[i + 3] = (nw >> 8) & 255
            p.code = code
        main_index = remap[self.module.main]

        # -- serialise + encrypt each prototype ----------------------------
        rows = []
        for pid in order:
            blob = serialize_proto(self.module.protos[pid])
            key = bytes(rng.randrange(1, 256) for _ in range(16))
            enc = xor_bytes(blob, key)
            # The key is emitted as escaped raw bytes: XORing against the
            # *characters* of an encoded key (an easy mistake) would decrypt
            # to garbage while still looking self-consistent.
            rows.append((base64.b64encode(enc).decode("ascii"),
                         "".join("\\%03d" % b for b in key),
                         len(blob)))

        # the bias was chosen before compilation and is already baked into
        # the patched jumps, so the loader must undo exactly this value
        jump_bias = self.spec.jump_bias

        out = []
        out.append(self.emit_preamble())
        out.append(self.emit_blobs(rows))
        out.append(self.emit_runtime(jump_bias))
        out.append(self.emit_run())
        out.append(self.emit_bootstrap(main_index))
        return "\n".join(p for p in out if p)

    # ------------------------------------------------------------------
    def emit_preamble(self) -> str:
        lines = []
        if self.spice:
            rng = self.rng
            pool = [s for s in self.spice if s and len(s) < 40][:400]
            for _ in range(min(len(pool), rng.randint(2, 6))):
                nm = rng.choice(pool)
                if nm in LUA_KEYWORDS:
                    continue
                lines.append(f"local {nm} = {rng.randint(0, 100000)}")
        for _ in range(self.rng.randint(1, 3)):
            lines.append(f"local {self.name()} = {self.rng.randint(0, 999999)}")
        return "\n".join(lines)

    def emit_blobs(self, rows) -> str:
        v = self.v
        out = [f"local {v['blobs']} = {{"]
        for data, key, plen in rows:
            out.append(f'{{"{data}","{key}",{plen}}},')
        out.append("}")
        return "\n".join(out)

    # ------------------------------------------------------------------
    def emit_runtime(self, jump_bias: int) -> str:
        v = self.v
        rng = self.rng
        # the base64 alphabet, split up so the literal never appears whole
        alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        p1, p2 = alpha[:32], alpha[32:]
        n1, n2 = self.name(), self.name()

        parts = []
        # Plain Lua 5.1 has neither `bit32` nor `bit`, and no `//` operator, so
        # every arithmetic primitive is written against a fallback.  Luau
        # always ships bit32, so it gets the direct bindings only.
        if self.target == "luau":
            bitfallback = ("    " + v['ERROR'] +
                           '("environment lacks bit32")')
        else:
            bitfallback = BITFALLBACK.format(**v)
        parts.append(f"""local {v['ENV']}
local {v['TYPE']} = type
local {v['ERROR']} = error
local {v['FLOOR']} = math.floor
local {v['SUB']} = string.sub
local {v['BYTE']} = string.byte
local {v['CHAR']} = string.char
local {v['TABLE']} = table
local {v['UNPACK']} = {v['TABLE']}.unpack or unpack
local {v['NEWT']} = {v['TABLE']}.create or function() return {{}} end
local {n1}, {n2} = "{p1}", "{p2}"
local {v['BAND']}, {v['BOR']}, {v['BXOR']}, {v['SHL']}, {v['SHR']}, {v['BNOT']}
do
  local bt = bit32 or bit
  if bt then
    {v['BAND']}, {v['BOR']}, {v['BXOR']} = bt.band, bt.bor, bt.bxor
    {v['SHL']}, {v['SHR']}, {v['BNOT']} = bt.lshift, bt.rshift, bt.bnot
  else
{bitfallback}
  end
end""")


        if self.target == "luau":
            parts.append(f"""local {v['IDIV']} = function(a, b) return a // b end""")
        else:
            parts.append(f"""local {v['IDIV']} = function(a, b) return {v['FLOOR']}(a / b) end""")

        parts.append(f"""local {v['B64LUT']} = {{}}
do
  local s = {n1} .. {n2}
  for i = 1, 64 do {v['B64LUT']}[{v['BYTE']}(s, i)] = i - 1 end
end""")

        # decoder: base64 + xor in one pass, chunked so no single unpack
        # call can overflow the C stack
        parts.append(f"""local function {v['DEC']}({v['TMP']}, k, mn)
  local L = {v['B64LUT']}
  local n = #{v['TMP']}
  local b = {{}}
  local j = 1
  local i = 1
  while i < n do
    local c1, c2, c3, c4 = {v['BYTE']}({v['TMP']}, i, i + 3)
    local u, w, x, y = L[c1] or 0, L[c2] or 0, L[c3] or 0, L[c4] or 0
    b[j] = {v['BXOR']}({v['BAND']}(u * 4 + {v['SHR']}(w, 4), 255), {v['BYTE']}(k, (j - 1) % #k + 1))
    b[j + 1] = {v['BXOR']}({v['BAND']}({v['BAND']}(w, 15) * 16 + {v['SHR']}(x, 2), 255), {v['BYTE']}(k, j % #k + 1))
    b[j + 2] = {v['BXOR']}({v['BAND']}({v['BAND']}(x, 3) * 64 + y, 255), {v['BYTE']}(k, (j + 1) % #k + 1))
    j = j + 3
    i = i + 4
  end
  local out = {{}}
  for s = 1, j - 1, 2048 do
    out[#out + 1] = {v['CHAR']}({v['UNPACK']}(b, s, (s + 2047 < j) and s + 2047 or j - 1))
  end
  local r = {v['TABLE']}.concat(out)
  if mn and #r > mn then r = {v['SUB']}(r, 1, mn) end
  return r
end""")

        # binary prototype loader
        parts.append(self.emit_protoloader(jump_bias))
        return "\n".join(parts)

    def emit_protoloader(self, jump_bias: int) -> str:
        v = self.v
        # wide-instruction lookup is baked in as a comparison against the
        # per-artifact opcode numbers, so nothing reveals the format table
        # W-format opcodes carry a 16-bit operand; only the *jump* ones are
        # biased, because only the compiler biases jump targets.  Applying the
        # bias to constant indices as well silently turns every constant into
        # a lookup of a non-existent pool entry.
        jump_names = ("JUMP", "JUMPIF", "JUMPIFNOT", "FORPREP", "FORLOOP",
                      "TFORLOOP")
        wide_ops = [self.spec.encode_op(i) for i, nm in enumerate(OPNAMES)
                    if nm in ("LOADK", "LOADS", "LOADNIL", "NEWTABLE",
                              "GETUPVAL", "SETUPVAL", "CLOSURE", "JUMP",
                              "JUMPIF", "JUMPIFNOT", "FORPREP", "FORLOOP",
                              "TFORLOOP", "CLOSE", "NOP")]
        jump_ops = [self.spec.encode_op(i) for i, nm in enumerate(OPNAMES)
                    if nm in jump_names]
        return f"""local {v['PROTO']} = {{}}
local {v['BIAS']} = {jump_bias}
local {v['WIDE']} = {{}}
do
  local w = {{ {", ".join(str(x) for x in wide_ops)} }}
  for i = 1, #w do {v['WIDE']}[w[i]] = true end
end
local {v['JUMPY']} = {{}}
do
  local w = {{ {", ".join(str(x) for x in jump_ops)} }}
  for i = 1, #w do {v['JUMPY']}[w[i]] = true end
end
local function {v['GETPROTO']}(pi)
  local idx = pi + 1
  local p = {v['PROTO']}[idx]
  if p then return p end
  local row = {v['blobs']}[idx]
  local blob = {v['DEC']}(row[1], row[2], row[3])
  local B = {v['BYTE']}
  local pos = 1
  local nparams = B(blob, pos); pos = pos + 1
  local isva = B(blob, pos); pos = pos + 1
  local mstack = B(blob, pos); pos = pos + 1
  local nups = B(blob, pos); pos = pos + 1
  local uk, ui = {{}}, {{}}
  for i = 1, nups do
    local q = B(blob, pos); pos = pos + 1
    if q >= 128 then uk[i] = 1; ui[i] = q - 128 else uk[i] = 0; ui[i] = q end
  end
  local nins = B(blob, pos) + B(blob, pos + 1) * 256; pos = pos + 2
  local S, RA, RB, RC = {{}}, {{}}, {{}}, {{}}
  local LW, LJ = {v['WIDE']}, {v['JUMPY']}
  for i = 1, nins do
    local o = B(blob, pos)
    local a = B(blob, pos + 1)
    local b2 = B(blob, pos + 2)
    local c2 = B(blob, pos + 3)
    pos = pos + 4
    S[i] = o
    RA[i] = a
    if LW[o] then
      local w = b2 + c2 * 256
      if LJ[o] then
        w = (w + 65536 - {v['BIAS']}) % 65536
        if w >= 32768 then w = w - 65536 end
      end
      RB[i] = w
      RC[i] = 0
    else
      RB[i] = b2
      RC[i] = c2
    end
  end
  local nk = B(blob, pos) + B(blob, pos + 1) * 256; pos = pos + 2
  local KN = {{}}
  for i = 0, nk - 1 do
    local len = B(blob, pos) + B(blob, pos + 1) * 256; pos = pos + 2
    local t = {v['SUB']}(blob, pos, pos + len - 1); pos = pos + len
    local q = {v['BYTE']}(t, 1)
    if q == 0 then
      local tag = {v['SUB']}(t, 2)
      if tag == "nan" then KN[i] = 0 / 0
      elseif tag == "inf" then KN[i] = math.huge
      else KN[i] = -math.huge end
    else
      KN[i] = tonumber(t)
    end
  end
  local ns = B(blob, pos) + B(blob, pos + 1) * 256; pos = pos + 2
  local KS = {{}}
  for i = 0, ns - 1 do
    local len = B(blob, pos) + B(blob, pos + 1) * 256; pos = pos + 2
    KS[i] = {v['SUB']}(blob, pos, pos + len - 1)
    pos = pos + len
  end
  p = {{S, RA, RB, RC, KN, KS, uk, ui, nparams, isva, mstack}}
  {v['PROTO']}[idx] = p
  return p
end"""

    # ------------------------------------------------------------------
    def emit_run(self) -> str:
        v = self.v
        rng = self.rng
        branches = {}
        for name in OPNAMES:
            sem = OP[name]
            wire = self.spec.encode_op(sem)
            if name == "CALL":
                body = CALL_BODY
            elif name == "VARARG":
                body = VARARG_BODY
            elif name == "RETURN":
                body = RETURN_BODY
            elif name == "FORPREP":
                body = FORPREP_BODY
            elif name == "FORLOOP":
                body = FORLOOP_BODY
            elif name == "TFORCALL":
                body = TFORCALL_BODY
            elif name == "TFORLOOP":
                body = TFORLOOP_BODY
            else:
                body = HANDLERS[name]
            branches[wire] = body

        tree = self.build_tree(sorted(branches), branches, 0)
        noise = self.emit_noise()

        return f"""local {v['RUN']}
local function {v['MK']}(pi, u)
  local f = function(...)
    return {v['RUN']}(pi, u, ...)
  end
  return f
end
{v['RUN']} = function(pi, UP, ...)
  local van0 = select('#', ...)
  local va = {{n = van0, ...}}
  local p = {v['GETPROTO']}(pi)
  local S, RA, RB, RC, KN, KS = p[1], p[2], p[3], p[4], p[5], p[6]
  local R = {{}}
  local vo = p[9]
  local van = van0 - vo
  if van < 0 then van = 0 end
  for i = 1, vo do R[i] = va[i] end
  local mva, mvn = nil, 0
  local pc = 1
  local UNPACK = {v['UNPACK']}
  local TYPE = {v['TYPE']}
  local ERROR = {v['ERROR']}
  local IDIV, BAND, BOR, BXOR, SHL, SHR, BNOT = {v['IDIV']}, {v['BAND']}, {v['BOR']}, {v['BXOR']}, {v['SHL']}, {v['SHR']}, {v['BNOT']}
  local ENV = {v['ENV']}
  local GETPROTO = {v['GETPROTO']}
  local MK, NEWT = {v['MK']}, {v['NEWT']}
  while true do
    local o = S[pc]
{noise}{tree}
  end
end"""

    def emit_noise(self) -> str:
        rng = self.rng
        v = self.v
        lines = []
        for _ in range(rng.randint(1, 3)):
            r = rng.randrange(256)
            lines.append(
                f"    if o >= {r} and pc < 0 then "
                f"R[{rng.randrange(1, 200)}] = {v['BAND']}({rng.randrange(999)}, {rng.randrange(64)}) end")
        return ("\n".join(lines) + "\n") if lines else ""

    def build_tree(self, wires: list, branches: dict, depth: int) -> str:
        """A binary search on opcode value with random split points.

        Values are the per-artifact opcode numbers, so the shape of the tree
        is re-randomised on every build even before the thresholds are chosen.
        """
        pad = "      " + "  " * depth
        if not wires:
            return pad + "pc = pc + 1"
        if len(wires) == 1:
            body = branches[wires[0]]
            return pad + self.indent_body(body, depth)
        # choose a split that keeps both sides non-empty
        k = len(wires) // 2
        if len(wires) > 3:
            k = self.rng.randint(1, len(wires) - 1)
        left, right = wires[:k], wires[k:]
        threshold = left[-1]
        inner = "  " * (depth + 1)
        return (f"{pad}if o <= {threshold} then\n"
                f"{self.build_tree(left, branches, depth + 1)}\n"
                f"{pad}else\n"
                f"{self.build_tree(right, branches, depth + 1)}\n"
                f"{pad}end")

    def indent_body(self, body: str, depth: int) -> str:
        pad = "      " + "  " * (depth + 1)
        lines = body.split("\n")
        if len(lines) == 1:
            return lines[0]
        return lines[0] + "\n" + "\n".join(pad + ln for ln in lines[1:])

    # ------------------------------------------------------------------
    def emit_bootstrap(self, main_index: int) -> str:
        v = self.v
        return f"""do
  local ok, e
  if getfenv then
    ok, e = pcall(function() return getfenv(1) end)
    if ok and {v['TYPE']}(e) == "table" then {v['ENV']} = e end
  end
  if not {v['ENV']} then {v['ENV']} = _G end
end
return {v['RUN']}({main_index}, {{}}, ...)"""


def generate(module, rng: Optional[random.Random] = None, target: str = "luau",
             spice=None, **kw) -> str:
    return VMGen(module, rng=rng, target=target, spice=spice, **kw).generate()
