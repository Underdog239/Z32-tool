# Z32

A from-scratch Lua/Luau obfuscator: a real lexer, parser, compiler and virtual
machine, written in Python, that turns Roblox-style scripts into a single
self-contained artifact.

Z32 does **not** wrap an existing obfuscator and does **not** compile to Lua
bytecode. It compiles the source to its own instruction set and ships a
generated interpreter for that instruction set, so at no point does the
protected program exist as a real Lua closure.

```
source ──▶ lexer ──▶ parser ──▶ compile ──▶ VM generator ──▶ artifact.luau
                                  │              │
                            Z32 bytecode    interpreter + encrypted
                            (R/W formats)   protos and constants
```

## Why not "just use Lua bytecode"

The attack that deobfuscated several public Roblox obfuscators is a
general-purpose `string.dump` hook: the dumper hijacks `string.dump`, waits for
the obfuscator's VM to load a chunk, and dumps every `LClosure` that gets
created. Any design where the protected program's functions are real Lua
closures is fully exposed to that, regardless of how it is encrypted on disk.

Z32 has no such step. Prototypes are decoded into a Lua table by a loader, and
the interpreter is a plain function over that table — there is no `load` or
`loadstring` anywhere in an artifact, so there is nothing for a `string.dump`
hook to catch. See `docs/ISA.md` for the format and the threat model.

## Layout

| Path | Contents |
| --- | --- |
| `z32/lexer.py` | Tokenizer. Luau mode, `\z`/`\u{}`/hex escapes, long strings, interpolated strings, integer suffixes. |
| `z32/parser.py` | Dialect-superset recursive-descent parser producing `z32/ast.py` nodes. |
| `z32/unparse.py` | AST back to source; used to prove the parser is lossless. |
| `z32/bytecode.py` | Z32 instruction set: opcode table, two wire formats, per-build opcode permutation, prototype layout. |
| `z32/compile.py` | AST to Z32 bytecode: name resolution, upvalue capture, register allocation, control flow. |
| `z32/vmgen.py` | The artifact generator: encrypts the bytecode, then emits the runtime that decrypts and runs it. |
| `z32/pack.py` | Ties it together (`obfuscate_ast`) and picks the per-build randomness. |
| `z32/disasm.py` | Development disassembler for the Z32 instruction set. |
| `tests/` | Differential test suites (see below). |

## Testing

Everything is checked by differential testing: run the original program under a
reference interpreter, run the artifact, require identical output.

```sh
python3 tests/difftest.py            # parser round-trip: unparse stable/unstable counts
python3 tests/e2e.py                 # whole corpus, both targets
python3 tests/e2e.py --targets luau  # one target
python3 tests/e2e.py path/to/script.lua
```

The harness finds `luau` and `lua51` on `PATH`, or via `Z32_LUAU` / `Z32_LUA51`.
`tests/corpus/` holds the fixtures; `tests/z32run.py` is a Python interpreter for
Z32 bytecode used as an oracle when a failure could be either a compiler bug or
a VM bug. `tests/debug/` holds the tracing and bisecting tools used to find
those failures.

## Status

Working end to end on both targets: a source program obfuscated by Z32 produces
the same output as the original under both `luau` and Lua 5.1.

Known gaps are tracked in the project notes and are mostly breadth rather than
correctness: `goto`/labels, integer division and bitwise operators on the Lua
5.1 target, and `continue` inside `repeat`.
