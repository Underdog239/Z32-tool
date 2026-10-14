"""Print the artifact's own identifiers with their meaning, then show the
loader lines with names substituted."""
import sys, random, re
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import make_spec
from z32.compile import compile_chunk
from z32.vmgen import VMGen

src = 'local function add(a, b) return a + b end\nprint(add(3, 4))\n'
rng = random.Random(7)
spec = make_spec(rng)
m = compile_chunk(parse(src, luau=True), spec, spec.jump_bias)
g = VMGen(m, spec=spec, rng=rng, target='luau')
art = g.generate()
v = g.v
legend = {}
for k, nm in v.items():
    decl = [l.strip() for l in art.splitlines() if re.match(rf"local {re.escape(nm)}\b", l)]
    legend[nm] = f"{k}: {decl[0][:60] if decl else '?'}"
print("=== legend (identifier -> meaning) ===")
for k in sorted(v, key=lambda x: v[x]):
    print(f"  {v[k]:<8} {legend.get(v[k], k)}")

# annotate the prototype-loader body with resolved names
start = art.index(f"local function {v['GETPROTO']}")
end = art.index(f"local {v['RUN']}\nlocal function {v['MK']}")
body = art[start:end]
names = sorted((nm for nm in v.values() if nm), key=len, reverse=True)
out = []
for ln in body.splitlines():
    ann = []
    for nm in names:
        if re.search(rf"\b{re.escape(nm)}\b", ln):
            ann.append(nm)
    tag = ("   # " + ",".join(ann)) if ann else ""
    out.append(ln + tag)
print("\n=== prototype loader (annotated) ===")
print("\n".join(out))
