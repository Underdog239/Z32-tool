"""Trace the generated VM's execution to find divergence from the oracle."""
import sys, random, subprocess
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import make_spec
from z32.compile import compile_chunk
from z32.vmgen import VMGen
from z32.bytecode import OPNAMES

src = 'local function add(a, b) return a + b end\nprint(add(3, 4))\n'
rng = random.Random(7)
spec = make_spec(rng)
m = compile_chunk(parse(src, luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau')
art = g.generate()

old = "    local o = S[pc]\n"
assert old in art, "dispatch anchor not found"
trace = (
    "    local o = S[pc]\n"
    "    ENV.print(pi..' pc='..pc..' o='..o..' A='..tostring(RA[pc])..' B='..tostring(RB[pc])..' C='..tostring(RC[pc])"
    "..' | '..'1='..tostring(R[1])..' 4='..tostring(R[4])..' 5='..tostring(R[5])..' 6='..tostring(R[6])..' 7='..tostring(R[7]))\n"
)
open('/tmp/trace.luau', 'w').write(art.replace(old, trace, 1))

p = subprocess.run(['/home/user/.toolchains/bin/luau', '/tmp/trace.luau'],
                   capture_output=True, text=True)
inv = {wire: OPNAMES[sem] for sem, wire in enumerate(spec.mapping)}
print("=== VM trace ===")
for l in p.stdout.splitlines()[:40]:
    parts = l.split()
    if not l.startswith("0 pc=") and " pc=" not in l:
        print("   OUT:", l)
        continue
    pi = parts[0]; pc = parts[1].split('=')[1]; o = int(parts[2].split('=')[1])
    regs = " ".join(parts[3:])
    print(f"   proto{pi} {pc:>4} {inv.get(o, '??'):<10} {regs}")
print("=== stderr ===")
print(p.stderr[:800])
