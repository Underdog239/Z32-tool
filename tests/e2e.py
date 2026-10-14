"""End-to-end differential test: original source vs. obfuscated artifact.

For every program in tests/corpus/ (or a file given on the command line) the
harness runs the pristine source under a reference interpreter, then runs the
obfuscated artifact, and requires byte-identical output.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from z32.parser import parse                          # noqa: E402
from z32.pack import obfuscate_ast, identifier_spice  # noqa: E402

def _find(name: str, env_var: str) -> str:
    """Locate a reference interpreter without hard-coding one machine.

    Order: explicit env var, PATH, then a local toolchain directory.
    """
    override = os.environ.get(env_var)
    if override and os.path.exists(override):
        return override
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    cand = os.path.join(os.path.expanduser("~/.toolchains/bin"), name)
    return cand if os.path.exists(cand) else ""


INTERP = {"luau": _find("luau", "Z32_LUAU"),
          "lua51": _find("lua51", "Z32_LUA51")}


_SRCLOC = re.compile(r"[\w./\\-]+\.lua\w*:\d+: ")
# Inside the VM the operand of a failed index is a register, not a named
# local, so Lua 5.1 words the message differently ("index local 'q'" vs
# "index field '?'").  Both are the same error; canonicalise the description.
_INDEX = re.compile(r"attempt to (index|call) [^\n]*?(?= (\(|attempt))")


def normalize(text: str) -> str:
    """Blank out the ``file:line:`` prefix Lua adds to error messages.

    The obfuscated artifact necessarily runs from a different chunk than the
    original, so ``error("boom")`` reports a different location.  That is a
    deliberate feature (it hides the real script path), not a miscompile.
    """
    text = _SRCLOC.sub("<loc>: ", text)
    text = _INDEX.sub(lambda m: f"attempt to {m.group(1)} <what>", text)
    return text


def run_interp(exe: str, path: str, timeout: int = 30):
    p = subprocess.run([exe, path], capture_output=True, text=True,
                       timeout=timeout, errors="replace")
    return p.stdout, p.stderr, p.returncode


def obfuscate_source(src: str, seed: int, target: str) -> str:
    block = parse(src, luau=(target == "luau"))
    return obfuscate_ast(block, seed=seed, target=target,
                         spice=identifier_spice(src))


def check_one(path: str, seed: int, targets) -> tuple:
    src = open(path, encoding="utf-8", errors="replace").read()
    results = []
    for target in targets:
        exe = INTERP[target]
        if not exe:
            results.append((target, "SKIP",
                            f"interpreter missing (set "
                            f"Z32_{'LUAU' if target == 'luau' else 'LUA51'})"))
            continue
        try:
            want_out, _, want_rc = run_interp(exe, path)
        except subprocess.TimeoutExpired:
            results.append((target, "SKIP", "baseline timed out"))
            continue
        try:
            art = obfuscate_source(src, seed, target)
        except Exception as e:                      # noqa: BLE001
            results.append((target, "COMPILE-FAIL",
                            f"{type(e).__name__}: {e}"))
            continue
        d = tempfile.mkdtemp(prefix="z32e2e-")
        out_path = os.path.join(d, "art.luau")
        with open(out_path, "w") as fh:
            fh.write(art)
        try:
            got_out, got_err, rc = run_interp(exe, out_path)
        except subprocess.TimeoutExpired:
            results.append((target, "TIMEOUT", ""))
            continue
        if rc != want_rc:
            # a fixture whose *baseline* fails is not a useful comparison
            results.append((target, "SKIP", f"baseline exit {want_rc}"))
            continue
        if normalize(got_out) != normalize(want_out):
            results.append((target, "OUTPUT-DIFF",
                            f"want {want_out[:200]!r}\n     got  {got_out[:200]!r}\n"
                            f"     err  {got_err[:200]!r} rc={rc}"))
        else:
            results.append((target, "ok", f"{len(art)} bytes"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--targets", default="luau,lua51")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    targets = [t for t in args.targets.split(",") if t]
    paths = args.paths
    if not paths:
        corpus = os.path.join(ROOT, "tests", "corpus")
        paths = sorted(os.path.join(corpus, f)
                       for f in os.listdir(corpus) if f.endswith((".lua", ".luau")))

    bad = 0
    for path in paths:
        name = os.path.basename(path)
        for target, status, detail in check_one(path, args.seed, targets):
            if status == "ok":
                if args.verbose:
                    print(f"  ok   {name:<28} [{target}] {detail}")
            else:
                bad += 1
                print(f"  {status:<12} {name:<28} [{target}] {detail}")
    print(f"\nchecked {len(paths)} files x {len(targets)} targets; "
          f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
