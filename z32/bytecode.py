"""Z32 bytecode container: instruction encoding, prototypes, serialisation.

Design notes (why it looks like this)
-------------------------------------
**4 bytes per instruction, two wire formats.**

    format R (three register/count operands):
        [ op ][ A ][ B ][ C ]      -- 8 bits each, full 0..255 range

    format W (one wide 16-bit operand):
        [ op ][ A ][ lo ][ hi ]    -- constant index, jump offset, ...

An opcode belongs to exactly one format, so the interpreter reads the same
four bytes and interprets them according to the opcode it just dispatched on.
The two shapes are what let a full register ISA fit in four bytes: a
three-register opcode genuinely needs 24 bits of operands, which no amount of
bit-packing can compress, while a jump needs 16 contiguous bits.  Having two
shapes is also mildly hostile to static analysis -- a scanner cannot decide
what a byte means without knowing the opcode map, which is per-artifact.

**No floating point is involved anywhere in the decoder.**  Every operand
falls out of plain byte arithmetic on the instruction stream, so there is no
"exponent edge case" to get wrong -- a family of bugs that has bitten real
obfuscators, whose constant decoders broke on denormal, infinity or
non-canonical NaN inputs that happened to be legal in the original script.

**Opcodes are permuted per artifact**, and jumps are additively biased by a
per-artifact constant so a jump target cannot be spotted by its raw value.
Two builds of the same script therefore share no opcode numbers, no constant
ordering and no jump bias.
"""

from __future__ import annotations

import struct
from typing import Optional

__all__ = ["OP", "OPNAMES", "OP_FMT", "OP_SPEC", "OpSpec", "Prototype",
           "Module", "encode_ins", "decode_ins", "decode_w", "pack_k",
           "INS_SIZE", "K_MAX"]

INS_SIZE = 4
SX_MAX = (1 << 15) - 1
SX_MIN = -(1 << 15)
K_MAX = 0xFFFF

# --------------------------------------------------------------------------
# opcodes
# --------------------------------------------------------------------------
OPNAMES = [
    "NOP",          # 00
    "MOV",          # 01   R[A] = R[B]
    "ADD",          # 02   R[A] = R[B] op R[C]
    "SUB",          # 03
    "MUL",          # 04
    "DIV",          # 05
    "MOD",          # 06
    "POW",          # 07
    "IDIV",         # 08
    "BAND",         # 09
    "BOR",          # 0A
    "BXOR",         # 0B
    "SHL",          # 0C
    "SHR",          # 0D
    "CONCAT",       # 0E
    "UNM",          # 0F   R[A] = -R[B]
    "NOT",          # 10   R[A] = not R[B]
    "LEN",          # 11   R[A] = #R[B]
    "BNOT",         # 12   R[A] = ~R[B]
    "EQ",           # 13   R[A] = R[B] == R[C]
    "NEQ",          # 14
    "LE",           # 15
    "GE",           # 16
    "LT",           # 17
    "GT",           # 18
    "ISTRUE",       # 19   R[A] = truthiness of R[B]
    "LOADK",        # 1A   R[A] = numeric constant K
    "LOADS",        # 1B   R[A] = string constant K
    "LOADBOOL",     # 1C   R[A] = boolean B
    "LOADNIL",      # 1D   R[A] = nil
    "NEWTABLE",     # 1E   R[A] = {}
    "NEWCELL",      # 1F   R[A] = {R[B]}       (box a captured local)
    "GETCELL",      # 20   R[A] = R[B][1]
    "SETCELL",      # 21   R[B][1] = R[A]
    "GETGLOBAL",    # 22   R[A] = ENV[R[B]]
    "SETGLOBAL",    # 23   ENV[R[B]] = R[A]
    "GETUPVAL",     # 24   R[A] = U[K][1]
    "SETUPVAL",     # 25   U[K][1] = R[A]
    "GETTABLE",     # 26   R[A] = R[B][R[C]]
    "SETTABLE",     # 27   R[A][R[B]] = R[C]
    "GETIDX",       # 28   R[A] = R[B][K]
    "SETIDX",       # 29   R[A][K] = R[B]
    "SELF",         # 2A   R[A] = R[B][K]; R[A+1] = R[B]
    "CALL",         # 2B   call R[A]: B args (bit 7 set = pending tail)
    "VARARG",       # 2C   R[A..] = ... (B results, 0 = all)
    "RETURN",       # 2D   return B fixed values at R[A] (+ pending)
    "JUMP",         # 2E   pc += W
    "JUMPIF",       # 2F   if R[A] then pc += W
    "JUMPIFNOT",    # 30   if not R[A] then pc += W
    "CLOSURE",      # 31   R[A] = closure(prototype K)
    "FORPREP",      # 32   numeric for, jump W
    "FORLOOP",      # 33
    "TFORCALL",     # 34   generic for
    "TFORLOOP",     # 35
    "SETLISTM",     # 36   append pending results to a table constructor
    "CLOSE",        # 37   reserved (the cell model needs no close)
]
assert len(OPNAMES) == 56, len(OPNAMES)
OP = {name: i for i, name in enumerate(OPNAMES)}

# Which wire format each opcode uses.
OP_FMT = {
    "NOP": "W", "MOV": "R", "ADD": "R", "SUB": "R", "MUL": "R", "DIV": "R",
    "MOD": "R", "POW": "R", "IDIV": "R", "BAND": "R", "BOR": "R", "BXOR": "R",
    "SHL": "R", "SHR": "R", "CONCAT": "R", "UNM": "R", "NOT": "R", "LEN": "R",
    "BNOT": "R", "EQ": "R", "NEQ": "R", "LE": "R", "GE": "R", "LT": "R",
    "GT": "R", "ISTRUE": "R", "LOADK": "W", "LOADS": "W", "LOADBOOL": "R",
    "LOADNIL": "W", "NEWTABLE": "W", "NEWCELL": "R", "GETCELL": "R",
    "SETCELL": "R", "GETGLOBAL": "R", "SETGLOBAL": "R", "GETUPVAL": "W",
    "SETUPVAL": "W", "GETTABLE": "R", "SETTABLE": "R", "GETIDX": "R",
    "SETIDX": "R", "SELF": "R", "CALL": "R", "VARARG": "R", "RETURN": "R",
    "JUMP": "W", "JUMPIF": "W", "JUMPIFNOT": "W", "CLOSURE": "W",
    "FORPREP": "W", "FORLOOP": "W", "TFORCALL": "R", "TFORLOOP": "W",
    "SETLISTM": "R", "CLOSE": "W",
}
assert set(OP_FMT) == set(OPNAMES)

# Operand roles.  "A"/"B"/"C" are byte operands of the R format, "K" is the
# 16-bit constant index of the W format, "S" a signed 16-bit jump offset.
#
#   GETIDX  A B C :  R[B] = R[A][K(C)]
#   SETIDX  A B C :  R[A][K(C)] = R[B]
#   SELF    A B C :  R[A] = R[B][K(C)] ; R[A+1] = R[B]
OP_SPEC = {
    "MOV": ("A", "B"), "NEWCELL": ("A", "B"), "GETCELL": ("A", "B"),
    "SETCELL": ("A", "B"), "GETGLOBAL": ("A", "B"), "SETGLOBAL": ("A", "B"),
    "LOADBOOL": ("A", "B"), "UNM": ("A", "B"), "NOT": ("A", "B"),
    "LEN": ("A", "B"), "BNOT": ("A", "B"), "ISTRUE": ("A", "B"),
    "ADD": ("A", "B", "C"), "SUB": ("A", "B", "C"), "MUL": ("A", "B", "C"),
    "DIV": ("A", "B", "C"), "MOD": ("A", "B", "C"), "POW": ("A", "B", "C"),
    "IDIV": ("A", "B", "C"), "BAND": ("A", "B", "C"), "BOR": ("A", "B", "C"),
    "BXOR": ("A", "B", "C"), "SHL": ("A", "B", "C"), "SHR": ("A", "B", "C"),
    "CONCAT": ("A", "B", "C"), "EQ": ("A", "B", "C"), "NEQ": ("A", "B", "C"),
    "LE": ("A", "B", "C"), "GE": ("A", "B", "C"), "LT": ("A", "B", "C"),
    "GT": ("A", "B", "C"), "GETTABLE": ("A", "B", "C"),
    "SETTABLE": ("A", "B", "C"), "CALL": ("A", "B", "C"),
    "VARARG": ("A", "B"), "RETURN": ("A", "B"), "TFORCALL": ("A", "B", "C"),
    "SETLISTM": ("A", "B"), "LOADK": ("A", "K"), "LOADS": ("A", "K"),
    "GETIDX": ("A", "B", "C"), "SETIDX": ("A", "B", "C"),
    "SELF": ("A", "B", "C"),
    "GETUPVAL": ("A", "K"), "SETUPVAL": ("A", "K"), "CLOSURE": ("A", "K"),
    "JUMP": ("S",), "JUMPIF": ("A", "S"), "JUMPIFNOT": ("A", "S"),
    "FORPREP": ("A", "S"), "FORLOOP": ("A", "S"), "TFORLOOP": ("A", "S"),
    "NEWTABLE": ("A",), "LOADNIL": ("A",), "CLOSE": ("A",), "NOP": (),
}


class OpSpec:
    """Per-artifact opcode mapping and jump bias."""

    def __init__(self, mapping: Optional[list] = None, jump_bias: int = 0):
        self.mapping = list(mapping) if mapping else list(range(len(OPNAMES)))
        if sorted(self.mapping) != list(range(len(OPNAMES))):
            raise ValueError("opcode mapping must be a permutation")
        self.jump_bias = jump_bias & 0xFFFF
        # inverse lookup, so callers that walk *wire* numbers (the artifact
        # generator) can recover the semantic opcode
        self.inverse = {w: s for s, w in enumerate(self.mapping)}

    def encode_op(self, sem: int) -> int:
        return self.mapping[sem]

    def decode_op(self, wire: int) -> int:
        """Wire opcode number -> semantic opcode number."""
        return self.inverse[wire]

    def format_of(self, sem: int) -> str:
        return OP_FMT[OPNAMES[sem]]


# --------------------------------------------------------------------------
# wire encoding
# --------------------------------------------------------------------------


def encode_ins(fmt: str, op_wire: int, a: int = 0, b: int = 0,
               c: int = 0) -> bytes:
    """Pack one instruction into its four wire bytes."""
    assert 0 <= op_wire < 256, op_wire
    assert 0 <= a < 256, a
    if fmt == "R":
        assert 0 <= b < 256 and 0 <= c < 256, (b, c)
        return bytes((op_wire, a, b, c))
    assert -32768 <= b <= 65535, b
    w = b & 0xFFFF
    return bytes((op_wire, a, w & 255, (w >> 8) & 255))


def decode_ins(buf, off: int):
    """Return the raw four bytes of an instruction."""
    return buf[off], buf[off + 1], buf[off + 2], buf[off + 3]


def decode_w(lo: int, hi: int) -> int:
    """Interpret a W-format operand as a signed 16-bit value."""
    w = lo | (hi << 8)
    return w - 0x10000 if w & 0x8000 else w


def encode_w(value: int) -> int:
    return value & 0xFFFF


def pack_k(value: int) -> int:
    if not (0 <= value <= K_MAX):
        raise ValueError(f"constant index out of range: {value}")
    return value


# --------------------------------------------------------------------------
# prototypes
# --------------------------------------------------------------------------


class Prototype:
    """One compiled function: code, constants and its upvalue recipe."""

    __slots__ = ("code", "nums", "strs", "num_index", "str_index", "nups",
                 "numparams", "is_vararg", "maxstack", "name", "id",
                 "up_kind", "up_index", "children", "jumps", "lines")

    def __init__(self, name: str = "?", numparams: int = 0,
                 is_vararg: bool = False):
        self.code = bytearray()
        self.nums: list = []
        self.strs: list = []
        self.num_index: dict = {}
        self.str_index: dict = {}
        self.nups = 0
        self.numparams = numparams
        self.is_vararg = is_vararg
        self.maxstack = 2
        self.name = name
        self.id = -1
        self.up_kind: list = []       # 0 = parent's register, 1 = parent's upvalue
        self.up_index: list = []
        self.children: list = []
        self.jumps: dict = {}         # byte offset -> target instruction index
        self.lines: list = []

    # -- emission ----------------------------------------------------------
    def emit(self, spec: OpSpec, opname: str, a: int = 0, b: int = 0,
             c: int = 0, line: int = 0) -> int:
        off = len(self.code)
        self.code += encode_ins(OP_FMT[opname], spec.encode_op(OP[opname]),
                                a, b, c)
        self.lines.append(line)
        return off

    def emit_w(self, spec: OpSpec, opname: str, a: int, w: int,
               line: int = 0) -> int:
        off = len(self.code)
        self.code += encode_ins(OP_FMT[opname], spec.encode_op(OP[opname]),
                                a, w)
        self.lines.append(line)
        return off

    def emit_jump(self, spec: OpSpec, opname: str, a: int = 0) -> int:
        """Emit a jump whose target is patched once it is known."""
        off = len(self.code)
        self.code += encode_ins("W", spec.encode_op(OP[opname]), a, 0)
        self.lines.append(0)
        self.jumps[off] = -1
        return off

    def pc(self) -> int:
        return len(self.code) // INS_SIZE

    def patch_to(self, off: int, target_ins: int) -> None:
        if off not in self.jumps:
            raise ValueError("not a pending jump")
        self.jumps[off] = target_ins

    def patch_jumps(self, bias: int = 0) -> None:
        """Resolve every pending jump now that all targets are known.

        The stored offset is additively biased with a per-artifact constant so
        a jump cannot be recognised by its raw value; the VM subtracts the
        bias when it decodes the operand.
        """
        for off, target in self.jumps.items():
            if target < 0:
                raise ValueError("unpatched jump")
            delta = target - (off // INS_SIZE + 1)
            if not (SX_MIN <= delta <= SX_MAX):
                raise ValueError("jump distance out of range")
            w = (delta + bias) & 0xFFFF
            self.code[off + 2] = w & 255
            self.code[off + 3] = (w >> 8) & 255
        self.jumps.clear()

    # -- constants ---------------------------------------------------------
    def knum(self, value: float) -> int:
        key = struct.pack("<d", value)
        idx = self.num_index.get(key)
        if idx is None:
            idx = len(self.nums)
            self.nums.append(value)
            self.num_index[key] = idx
        return idx

    def kstr(self, value: bytes) -> int:
        idx = self.str_index.get(value)
        if idx is None:
            idx = len(self.strs)
            self.strs.append(value)
            self.str_index[value] = idx
        return idx

    def find_stale(self):
        """Constants the compiler added but never referenced (spice)."""
        used_num = set()
        used_str = set()
        for i in range(0, len(self.code), INS_SIZE):
            op, a, b, c = decode_ins(self.code, i)
            name = OPNAMES[op]
            if name == "LOADK":
                used_num.add(b | (c << 8))
            elif name == "LOADS":
                used_str.add(b | (c << 8))
        return used_num, used_str


class Module:
    """A whole compiled chunk: a tree of prototypes."""

    def __init__(self):
        self.protos: list = []
        self.main = -1

    def new_proto(self, name: str = "?", numparams: int = 0,
                  is_vararg: bool = False) -> Prototype:
        p = Prototype(name, numparams, is_vararg)
        p.id = len(self.protos)
        self.protos.append(p)
        return p

    def __len__(self) -> int:
        return len(self.protos)
