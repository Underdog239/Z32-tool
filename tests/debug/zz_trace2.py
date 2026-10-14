"""Trace the full smoke program; stop after N steps."""
import sys, random, subprocess
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import obfuscate_ast, identifier_spice
from z32.vmgen import VMGen
from z32.bytecode import OPNAMES
from z32.pack import make_spec
from z32.compile import compile_chunk

src = '''
local x = 1 + 2
local t = {1, 2, name = "z"}
print(x, t.name, #t)
local function add(a, b) return a + b end
print(add(3, 4))
for i = 1, 3 do print(i) end
local s = "a" .. "b"
print(s, x > 1 and "yes" or "no")
'''
rng = random.Random(42)
spec = make_spec(rng)
m = compile_chunk(parse(src, luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau', spice=identifier_spice(src))
art = g.generate()

old = "    local o = S[pc]\n"
trace = ("    local o = S[pc]\n"
         "    do STEP = STEP + 1 if STEP > 120 then ENV.print('ABORT') return end end\n"
         "    ENV.print(pi..' pc='..pc..' o='..o..' A='..tostring(RA[pc])..' B='..tostring(RB[pc]))\n")
art = art.replace("local ENV = ", "STEP = 0 local ENV = ", 1) if False else art
art = art.replace("  local R = {}\n", "  local R = {}\n  STEP = (STEP or 0) + 0\n", 1)
open('/tmp/trace2.luau', 'w').write(art.replace(old, trace, 1))

p = subprocess.run(['/home/user/.toolchains/bin/luau', '/tmp/trace2.luau'],
                   capture_output=True, text=True)
inv = {w: OPNAMES[s] for s, w in enumerate(spec.mapping)}
for l in p.stdout.splitlines()[-70:]:
    parts = l.split()
    if len(parts) >= 3 and 'pc=' in parts[1]:
        pi, pc, o = parts[0], parts[1].split('=')[1], int(parts[2].split('=')[1])
        print(f"  p{pi:<3} pc={pc:<4} {inv.get(o,'??'):<10} {' '.join(parts[3:])}")
    else:
        print("  OUT:", l)
print("stderr:", p.stderr[:300])
