"""Human-readable dumper for compiled Z32 modules.

Primarily a development and debugging aid: ``z32 obfuscate --dump`` prints the
compiled module instead of an obfuscated artifact.  Note that this dump is a
*representation of the compiler's output*, not something the obfuscated
artifact can be made to print -- the VM deliberately has no such facility.
"""

from __future__ import annotations

from .bytecode import INS_SIZE, OP_FMT, OPNAMES, OP_SPEC, decode_w

__all__ = ["disassemble", "dump_proto", "format_ins"]


def format_ins(m, op: int, a: int, b: int, c: int) -> str:
    name = OPNAMES[op] if op < len(OPNAMES) else f"?{op}"
    fmt = OP_FMT.get(name, "R")
    if fmt == "W":
        w = decode_w(b, c)
        spec = OP_SPEC.get(name, ())
        if spec and spec[0] == "S":
            target = None
            return f"{name:<9} A={a} W={w}"
        if name in ("LOADS", "LOADK", "GETUPVAL", "SETUPVAL", "CLOSURE"):
            return f"{name:<9} A={a} K={w}"
        return f"{name:<9} A={a} W={w}"
    return f"{name:<9} A={a} B={b} C={c}"


def dump_proto(m, pid: int, lines: list, base_pc: int = 0) -> int:
    p = m.protos[pid]
    lines.append(f"proto #{pid} <{p.name}> params={p.numparams} "
                 f"vararg={p.is_vararg} upvals={p.nups} "
                 f"stack={p.maxstack} ins={len(p.code) // INS_SIZE}")
    for i, (kind, idx) in enumerate(zip(p.up_kind, p.up_index)):
        lines.append(f"    upval[{i}] <- {'register' if kind == 0 else 'upval'}"
                     f" {idx}")
    if p.nums:
        lines.append(f"    nums = {[round(n, 6) for n in p.nums]}")
    if p.strs:
        lines.append(f"    strs = {[s[:40] for s in p.strs]}")
    n = len(p.code) // INS_SIZE
    for i in range(n):
        off = i * INS_SIZE
        op, a, b, c = (p.code[off], p.code[off + 1], p.code[off + 2],
                       p.code[off + 3])
        text = format_ins(m, op, a, b, c)
        extra = ""
        if OPNAMES[op] == "CLOSURE":
            k = b | (c << 8)
            if k < len(m.protos):
                extra = f"   ; -> #{k} <{m.protos[k].name}>"
        lines.append(f"    {i:5d}  {text}{extra}")
    return n


def disassemble(m, header: str = "") -> str:
    lines: list = []
    if header:
        lines.append(header)
    for pid in range(len(m.protos)):
        dump_proto(m, pid, lines)
    return "\n".join(lines)
