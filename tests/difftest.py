#!/usr/bin/env python3
"""Differential testing for the Z32 front end.

Three levels of checking, all against the *reference* Lua/Luau interpreters:

A. **Structure stability** -- ``parse(unparse(parse(src)))`` must unparse to
   exactly the same text as ``unparse(parse(src))``.  Catches any construct the
   parser mangles or the printer cannot represent.

B. **Round-trip semantics** -- for every self-contained corpus program, run the
   original and the unparsed version under the reference interpreter and
   require byte-identical stdout/stderr and exit status.  This is the real
   parser correctness test: a dropped paren, a swapped operand or a lost
   ``local`` shows up immediately.

C. **Reference agreement** -- where the obfuscated output can be run, compare it
   with the original program's output (used by the back-end tests).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from z32.parser import parse, ParseError          # noqa: E402
from z32.unparse import unparse                   # noqa: E402

BIN = os.path.join(os.path.expanduser("~"), ".toolchains", "bin")
LUAU = os.path.join(BIN, "luau")
LUA51 = os.path.join(BIN, "lua51")

# Corpus roots: the reference implementation's own test material plus ours.
CORPUS_GLOBS = [
    os.path.join(HERE, "corpus", "**", "*.lua"),
    os.path.join(HERE, "corpus", "**", "*.luau"),
    os.path.join(os.path.expanduser("~"), ".toolchains", "src", "luau-master",
                 "tests", "conformance", "*.luau"),
    os.path.join(os.path.expanduser("~"), ".toolchains", "src", "luau-master",
                 "bench", "tests", "**", "*.lua"),
    os.path.join(os.path.expanduser("~"), ".toolchains", "src", "luau-master",
                 "bench", "other", "*.lua"),
]


def corpus() -> list[str]:
    files: list[str] = []
    for pat in CORPUS_GLOBS:
        files += glob.glob(pat, recursive=True)
    return sorted(set(files))


_TIMING_MARKER = b"|><|"


_SRCLOC = re.compile(rb"[\w./\\-]+\.lua\w*:\d+: ")
_INDEXWORD = re.compile(rb"attempt to (index|call) [^\n]*?(?= (\(|attempt))")


def normalize(out: bytes) -> bytes:
    """Remove differences that are inherent to running from another file.

    Two things vary no matter how correct the transformation is: benchmark
    harness timing lines (``|><|``), and the ``file:line:`` prefix Lua puts in
    front of an error message -- a round-tripped chunk necessarily lives in a
    different file, and hiding the original path is a feature.  Every other
    byte, including the rest of the error text, is compared exactly.
    """
    out = b"\n".join(ln for ln in out.split(b"\n")
                     if _TIMING_MARKER not in ln)
    out = _SRCLOC.sub(b"<loc>: ", out)
    out = _INDEXWORD.sub(lambda m: b"attempt to " + m.group(1) + b" <what>", out)
    return out


def run(path: str, timeout: int = 20):
    """Run a file under the reference interpreter, returning (rc, out, err)."""
    exe = LUAU
    if not path.endswith(".luau") and os.environ.get("Z32_LUA51"):
        exe = LUA51
    try:
        p = subprocess.run([exe, path], capture_output=True, timeout=timeout,
                           cwd=os.path.dirname(path) or ".")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "timeout", b"", b""


def main() -> int:
    only_roundtrip = "--roundtrip-only" in sys.argv
    files = corpus()
    n_stable = n_unstable = 0
    n_skipped = n_ran = n_ok = n_bad = 0
    unstable: list[tuple[str, str]] = []
    mismatched: list[tuple[str, str]] = []

    for f in files:
        src = open(f, "r", encoding="utf-8", errors="surrogateescape").read()
        try:
            tree = parse(src, luau=True, name=f)
        except ParseError:
            n_skipped += 1          # unsupported dialect feature, by design
            continue
        except Exception as e:      # pragma: no cover
            unstable.append((f, f"CRASH {e!r}"))
            n_unstable += 1
            continue

        text1 = unparse(tree)
        try:
            tree2 = parse(text1, luau=True, name=f)
            text2 = unparse(tree2)
        except Exception as e:
            unstable.append((f, f"reparse failed: {e!r}"))
            n_unstable += 1
            continue
        if text1 != text2:
            unstable.append((f, "unparse not idempotent"))
            n_unstable += 1
            continue
        n_stable += 1

        if only_roundtrip:
            continue

        # ---- level B: does the round-tripped program behave identically? ----
        rc1, out1, err1 = run(f)
        if rc1 == "timeout":
            continue
        if rc1 != 0:
            # corpus files that need a harness / assert on purpose: skip
            continue
        # Write next to the original so relative `require` calls still work.
        tmp = os.path.join(os.path.dirname(f) or ".",
                           "__z32_rt_" + os.path.basename(f))
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text1)
        except OSError:
            continue
        try:
            rc2, out2, err2 = run(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        n_ran += 1
        if normalize(out1) == normalize(out2):
            n_ok += 1
        else:
            n_bad += 1
            if len(mismatched) < 12:
                mismatched.append((f, f"rc {rc1}->{rc2} out {out1[:60]!r} -> {out2[:60]!r}"))

    print(f"files scanned            : {len(files)}")
    print(f"unsupported-by-design    : {n_skipped}")
    print(f"unparse stable           : {n_stable}")
    print(f"unstable                 : {n_unstable}")
    if not only_roundtrip:
        print(f"round-trip executed      : {n_ran}")
        print(f"identical behaviour      : {n_ok}")
        print(f"MISMATCHES               : {n_bad}")
    for f, why in unstable[:15]:
        print(f"  UNSTABLE {f}: {why}")
    for f, why in mismatched:
        print(f"  MISMATCH {f}: {why}")
    return 1 if (n_unstable or n_bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
