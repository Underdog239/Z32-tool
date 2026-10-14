"""Capture the dispatch bodies the generator emits, keyed by wire opcode."""
import sys, random
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import make_spec
from z32.compile import compile_chunk
from z32.vmgen import VMGen
from z32.bytecode import OPNAMES

captured = {}
orig = VMGen.build_tree
def spy(self, wires, branches, depth):
    captured.update(branches)
    return orig(self, wires, branches, depth)
VMGen.build_tree = spy

src = 'local function add(a, b) return a + b end\nprint(add(3, 4))\n'
rng = random.Random(7)
spec = make_spec(rng)
m = compile_chunk(parse(src, luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau')
g.generate()

print("branches captured:", len(captured))
inv = {w: OPNAMES[s] for s, w in enumerate(spec.mapping)}
for name in ["MOV", "LOADS", "LOADK", "GETGLOBAL", "CLOSURE", "CALL", "RETURN", "ADD"]:
    sem = OPNAMES.index(name)
    w = spec.encode_op(sem)
    body = captured.get(w, "<MISSING>")
    print(f"\n{name:10} sem={sem:3} wire={w:3}")
    print("   ", body.split("\n")[0][:120])
