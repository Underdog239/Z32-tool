"""Decode an artifact's blob exactly as its own loader does, and diff it
against the module the compiler produced."""
import sys, base64, re, random
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import make_spec
from z32.compile import compile_chunk
from z32.vmgen import VMGen, serialize_proto, xor_bytes
from z32.bytecode import OPNAMES

src = 'local function add(a, b) return a + b end\nprint(add(3, 4))\n'
rng = random.Random(7)
spec = make_spec(rng)
m = compile_chunk(parse(src, luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau')
art = g.generate()

# pull the blob rows straight out of the artifact text
rows = re.findall(r'\{"([A-Za-z0-9+/=]*)","((?:\\\d{3})*)",(\d+)\}', art)
print("blob rows found:", len(rows), "protos:", len(m.protos))
data, keytxt, plen = rows[0]
key = bytes(int(keytxt[i+1:i+4]) for i in range(0, len(keytxt), 4))
print("key len", len(key), "plen", plen)
raw = xor_bytes(base64.b64decode(data), key)[:int(plen)]

pos = 0
def u8():
    global pos
    v = raw[pos]; pos += 1; return v
def u16():
    global pos
    v = raw[pos] | (raw[pos+1] << 8); pos += 2; return v
def s16():
    v = u16()
    return v - 65536 if v >= 32768 else v

np_, isva, ms, nu = u8(), u8(), u8(), u8()
print(f"nparams={np_} isva={isva} maxstack={ms} nups={nu}")
for _ in range(nu): u8()
nins = u16()
wire = [(u8(), u8(), u8(), u8()) for _ in range(nins)]
nk = u16(); nums = [raw:=(raw)]  # placeholder
nums = []
for _ in range(nk):
    L = u16(); nums.append(raw[pos:pos+L].decode()); pos += L
ns = u16(); strs = []
for _ in range(ns):
    L = u16(); strs.append(raw[pos:pos+L]); pos += L
print("nins", nins, "nums", nums, "strs", strs, "consumed", pos, "of", len(raw))

p = m.protos[0]
inv = {}
for sem, wireno in enumerate(spec.mapping):
    inv[wireno] = OPNAMES[sem]
print("\n%-4s %-10s %-14s | %s" % ("idx", "wire-op", "artifact", "compiler code (raw)"))
for i in range(nins):
    o, a, b, c = wire[i]
    off = i*4
    cvec = tuple(p.code[off:off+4])
    flag = "" if (o, a, b, c) == cvec else "   <-- MISMATCH"
    print("%-4d %-10s %-14s | %s%s" % (i, inv.get(o, f"?{o}"), (o, a, b, c), cvec, flag))
