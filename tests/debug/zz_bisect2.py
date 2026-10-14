"""Line-by-line bisect of one corpus file (debug aid)."""
import sys, subprocess
sys.path.insert(0, '/home/user/Z32-tool')
from z32.parser import parse
from z32.pack import obfuscate_ast
EXE = '/home/user/.toolchains/bin/luau'
path = sys.argv[1]
lines = open(path).read().split('\n')
# group top-level statements using the parsed AST is overkill; use indentation
groups, cur = [], []
for ln in lines:
    if ln and not ln[0].isspace() and not ln.startswith(('end', 'else')):
        if cur:
            groups.append(cur)
        cur = [ln]
    else:
        cur.append(ln)
if cur:
    groups.append(cur)
src = ''
for i, g in enumerate(groups):
    src += '\n'.join(g) + '\n'
    try:
        art = obfuscate_ast(parse(src, luau=True), seed=7)
    except Exception as e:
        print(f"  [{i}] COMPILE-FAIL {type(e).__name__}: {e}")
        print("      " + ' | '.join(g)[:90])
        break
    open('/tmp/b.luau', 'w').write(art)
    g2 = subprocess.run([EXE, '/tmp/b.luau'], capture_output=True, text=True)
    w = subprocess.run([EXE, '-e', 'print(1)'], capture_output=True, text=True)
    if g2.returncode != 0:
        print(f"  [{i}] FAILS: {g2.stderr.splitlines()[0][-90:]}")
        print("      " + ' | '.join(g)[:90])
        break
else:
    print("  all prefixes ran")
