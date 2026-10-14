import sys, subprocess
sys.path.insert(0,'/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import obfuscate_ast
EXE='/home/user/.toolchains/bin/luau'
SNIPPETS = {
 "tailrec": "local function tail(n, acc) if n == 0 then return acc end return tail(n - 1, acc + n) end\nprint(tail(100, 0))\n",
 "vararg2": "local function f(...) return select('#', ...), ... end\nprint(f(1, nil, 3))\n",
 "counter": "local function counter()\n  local n = 0\n  return function() n = n + 1 return n end\nend\nlocal c1, c2 = counter(), counter()\nprint(c1(), c1(), c2())\n",
 "method": "local t = {v = 42}\nfunction t:get() return self.v end\nprint(t:get())\n",
 "apply": "local function add(a,b) return a+b end\nlocal function apply(f, ...) return f(...) end\nprint(apply(add, 20, 22))\n",
 "pairloop": "local s = 0 for k, v in pairs({a = 1, b = 2}) do s = s + v end print(s)\n",
 "ipairsf": "local function go() local s = 0 for _, v in ipairs({1,2,3}) do s = s + v end return s end print(go())\n",
 "mathmax": "print(math.max(1, 2, 3), math.min(1, 2, 3))\n",
 "topower": "print(2 ^ 0.5, 1 / 0, 0x10, 0xff)\n",
 "lentostr": "print(#tostring(1e300))\n",
 "numtoint": "print(math.tointeger and math.tointeger(3.0) or 'n/a')\n",
 "tonumb": "print(tonumber('1e3'), tonumber('0x1f'))\n",
 "rep": "print(string.rep('ab', 3))\n",
 "gsub": "local a, b = ('a,b,c'):gsub(',', ';') print(a, b)\n",
 "select": "print(select(2, 'a', 'b', 'c'))\n",
 "unpack5": "print(table.unpack({1,2,3}, 1, 2))\n",
 "hashlen": "local t = {1,2,3,nil,5} print(#t)\n",
}
for name, src in SNIPPETS.items():
    try:
        art = obfuscate_ast(parse(src, luau=True), seed=7)
    except Exception as e:
        print(f"  {name:<10} COMPILE-FAIL {type(e).__name__}: {e}")
        continue
    open('/tmp/b.luau','w').write(art)
    w = subprocess.run([EXE,'-e',src], capture_output=True, text=True)
    if w.returncode:  # -e may be unsupported
        open('/tmp/b1.lua','w').write(src)
        w = subprocess.run([EXE,'/tmp/b1.lua'], capture_output=True, text=True)
    g = subprocess.run([EXE,'/tmp/b.luau'], capture_output=True, text=True)
    tag = "ok  " if g.stdout == w.stdout else "DIFF"
    print(f"  {name:<10} {tag} want={w.stdout[:44]!r} got={g.stdout[:44]!r} {g.stderr.splitlines()[0][-60:] if g.stderr else ''}")
