"""Z32 packager -- turn a module into a runnable obfuscated artifact."""

from __future__ import annotations

import random
import re
from typing import Optional

from .bytecode import OpSpec
from .vmgen import VMGen

__all__ = ["obfuscate", "obfuscate_ast", "identifier_spice", "make_spec"]

# Globals the generated runtime reads directly.  A decoy local named after one
# of these would shadow it for the whole chunk and break the artifact -- the
# failure mode is a run-time error deep inside the loader, so they are filtered
# out up front.
RESERVED = frozenset("""
type error math string table bit32 bit unpack tonumber tostring select
getfenv setfenv _G _ENV pcall xpcall setmetatable getmetatable rawget rawset
rawequal assert print pairs ipairs next require io os coroutine debug
self _ arg load loadstring loadstring newproxy collectgarbage
""".split())


KEYWORDS = frozenset("""
and break do else elseif end false for function if in local nil not or repeat
return then true until while goto continue export type
""".split())


def identifier_spice(src: str, limit: int = 400) -> list:
    """Collect the user's own identifier names, most local-looking first.

    Decoy locals named after the protected script's own variables are the
    cheapest and most effective camouflage: a reader skimming the artifact
    sees names that belong to the program they are trying to read, and a
    variable-name-based heuristic cannot separate them from real state.
    """
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,30}", src)
    seen = set()
    out = []
    for n in names:
        if n in seen or n in RESERVED or n in KEYWORDS:
            continue
        seen.add(n)
        out.append(n)
    # shortest-first keeps the preamble compact
    out.sort(key=len)
    return out[:limit]


def make_spec(rng: random.Random) -> OpSpec:
    """One opcode permutation + jump bias for a whole artifact.

    The compiler emits *wire* opcode numbers, so the spec has to be fixed
    before compilation and then handed to the VM generator.  Generating the
    two independently produces a program that decodes to the wrong handlers,
    which is exactly the kind of bug that only shows up at run time.
    """
    perm = list(range(56))
    rng.shuffle(perm)
    return OpSpec(perm, jump_bias=rng.randrange(1, 65536))


def obfuscate_ast(block, *, seed: Optional[int] = None, target: str = "luau",
                  spice: Optional[list] = None) -> str:
    from .compile import compile_chunk
    rng = random.Random(seed)
    spec = make_spec(rng)
    module = compile_chunk(block, spec, spec.jump_bias)
    gen = VMGen(module, spec=spec, rng=rng, target=target, spice=spice or [])
    return gen.generate()


def obfuscate(module, *, seed: Optional[int] = None, target: str = "luau",
              spice: Optional[list] = None, spec: Optional[OpSpec] = None) -> str:
    rng = random.Random(seed)
    spec = spec or make_spec(rng)
    gen = VMGen(module, spec=spec, rng=rng, target=target, spice=spice or [])
    return gen.generate()
