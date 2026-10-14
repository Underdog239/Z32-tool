"""Run the differential harness over every standalone-runnable file found in
a directory tree, to get a coverage picture on outside code."""
import os, subprocess, sys, tempfile, re
sys.path.insert(0, '/home/user/Z32-tool')
from tests.e2e import run_interp, normalize, obfuscate_source, INTERP

root = sys.argv[1] if len(sys.argv) > 1 else '/home/user/.toolchains/src/luau-master'
targets = (sys.argv[2] if len(sys.argv) > 2 else 'luau').split(',')
files = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in ('build', '.git')]
    for fn in filenames:
        if fn.endswith(('.lua', '.luau')):
            files.append(os.path.join(dirpath, fn))
print(f"found {len(files)} candidate files")
ok = skip = bad = 0
badlist = []
for path in sorted(files):
    try:
        src = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        continue
    for target in targets:
        exe = INTERP[target]
        try:
            want, _, wrc = run_interp(exe, path, timeout=10)
        except subprocess.TimeoutExpired:
            skip += 1; continue
        if wrc != 0:
            skip += 1; continue
        try:
            art = obfuscate_source(src, 1234, target)
        except Exception as e:
            skip += 1; continue
        # write next to the original so relative `require` still resolves
        out = os.path.join(os.path.dirname(path), '.z32sw.luau')
        open(out, 'w').write(art)
        try:
            got, err, rc = run_interp(exe, out, timeout=20)
        except subprocess.TimeoutExpired:
            bad += 1; badlist.append((path, 'TIMEOUT')); continue
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass
        if rc == 0 and normalize(got) == normalize(want):
            ok += 1
        else:
            bad += 1
            badlist.append((path, (err or ('rc=' + str(rc))).splitlines()[0][:110]))
print(f"\nRESULT ran-ok={ok} failed={bad} skipped={skip} (of {len(files)})")
for p, why in badlist[:25]:
    print("  FAIL", os.path.relpath(p, root), '\n       ', why)
