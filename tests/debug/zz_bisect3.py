"""Try single constructs that showed up in a failing external file."""
import sys, subprocess
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import obfuscate_ast
EXE = '/home/user/.toolchains/bin/luau'
SN = {
 "bit32field": "local v = 12345 print(bit32.extract(v, 6, 6), bit32.band(255, 15))\n",
 "nested_global": "local function f(v) return bit32.extract(v, 0, 4) end\nprint(f(255))\n",
 "selfcall": "local M = {} function M.a(x) return x * 2 end\nprint(M.a(21))\n",
 "rowsel": "local P = {}\nfunction P.new(v) return {v = v} end\nfunction P:get() return self.v end\nprint(P.new(5):get())\n",
 "bitinloop": "local s = 0 for i = 1, 8 do s = s + bit32.extract(255, i - 1, 1) end print(s)\n",
 "bitinfunc": "local function g(v) local a = bit32.extract(v, 0, 4) local b = bit32.extract(v, 4, 4) return a + b end print(g(255))\n",
 "bitinmethod": "local M = {}\nfunction M:sum(v) return bit32.extract(v, 0, 4) + self.n end\nprint(M:sum(255))\n",
 "deeprec": "local function d(n) if n == 0 then return 0 end return 1 + d(n - 1) end print(d(200))\n",
 "mutualrec": "local function a(n) if n == 0 then return 0 end return b(n - 1) + 1 end\nfunction b(n) if n == 0 then return 0 end return a(n - 1) + 1 end\nprint(a(10))\n",
 "globalfn": "function g(x) return x * 3 end\nprint(g(4))\n",
 "methodok": "local M = {n = 5}\nfunction M:sum(v) return bit32.extract(v, 0, 4) + self.n end\nprint(M:sum(255))\n",
 "upvalfn": "local function outer()\n  function inner(x) return x + 1 end\n  return inner(41)\nend\nprint(outer())\n",
}
for name, src in SN.items():
    try:
        art = obfuscate_ast(parse(src, luau=True), seed=7)
    except Exception as e:
        print(f"  {name:<12} COMPILE-FAIL {type(e).__name__}: {str(e)[:70]}"); continue
    open('/tmp/b3.luau','w').write(art)
    open('/tmp/b3.lua','w').write(src)
    w = subprocess.run([EXE,'/tmp/b3.lua'],capture_output=True,text=True)
    g = subprocess.run([EXE,'/tmp/b3.luau'],capture_output=True,text=True)
    if w.returncode != 0:
        tag = "BOTH-FAIL" if g.returncode != 0 else "DIFF"
    elif g.returncode != 0 or g.stdout != w.stdout:
        tag = "DIFF"
    else:
        tag = "ok  "
    print(f"  {name:<12} {tag} want={w.stdout[:34]!r} got={g.stdout[:34]!r} {g.stderr.splitlines()[0][-58:] if g.stderr else ''}")
