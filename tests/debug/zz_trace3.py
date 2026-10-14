"""Trace a program through the generated VM (debug aid)."""
import sys, random, subprocess, os
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import make_spec
from z32.compile import compile_chunk
from z32.vmgen import VMGen
from z32.bytecode import OPNAMES

path = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
lo = int(sys.argv[3]) if len(sys.argv) > 3 else 0
seed = int(os.environ.get('SEED', '7'))
rng = random.Random(seed)
spec = make_spec(rng)
m = compile_chunk(parse(open(path).read(), luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau')
art = g.generate()

old = "    local o = S[pc]\n"
assert old in art
trace = ("    local o = S[pc]\n"
         "    do CNT = (CNT or 0) + 1\n"
         "       if CNT > " + str(lo) + " and CNT < " + str(lo + limit) + " then\n"
         "         local rr = ''\n"
         "         for k = 1, 9 do if R[k] ~= nil then rr = rr .. k .. '=' .. tostring(R[k]) .. ' ' end end\n"
         "         ENV.print('# ' .. pi .. ' pc=' .. pc .. ' ' .. rr)\n"
         "       end end\n")
# the trace needs a vararg-safe place: insert a CNT local next to R
art = art.replace("  local pc = 1\n", "  local pc = 1\n  local CNT = 0\n", 1)
open('/tmp/trace3.luau', 'w').write(art.replace(old, trace, 1))
p = subprocess.run(['/home/user/.toolchains/bin/luau', '/tmp/trace3.luau'],
                   capture_output=True, text=True)
inv = {w: OPNAMES[s] for s, w in enumerate(spec.mapping)}
for l in p.stdout.splitlines():
    if l.startswith('# '):
        parts = l[2:].split(' ', 1)
        pi = parts[0]
        rest = parts[1] if len(parts) > 1 else ''
        pc = rest.split()[0].split('=')[1]
        # resolve the opcode from the artifact's instruction table is not
        # available here; print pc and registers only
        print(f"  p{pi:<3} pc={pc:<5} {rest.split(' ', 1)[1] if ' ' in rest else ''}")
    else:
        print("  OUT:", l)
print("stderr:", p.stderr[:400])
