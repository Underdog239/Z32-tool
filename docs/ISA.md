# Z32 virtual machine -- instruction set

Z32 compiles Lua/Luau into a **register-based bytecode for a private virtual
machine**, then executes it inside a Lua/Luau interpreter.  Nothing in the
protected program is ever handed to the host's own `loadstring`, so there is no
`debug.getinfo`/`debug.getupvalue` handle on the real code to steal, and no
Lua source anywhere in the artifact.

Register 0 is **always set to `nil`** before every executed instruction, so any
opcode that reads register 0 is implicitly reading a `nil` constant.  This is a
deliberate hazard: it also means a later pass can bias random instruction
operands through register 0 without changing semantics.

## Encoding

Every instruction is a 16-bit unsigned integer:

```
  load = (A << 8) | 15                            -- load byte A into the accumulator
  ins  = (((op * 256 + A) << 8) | B) << 8 | C      -- execute op with A, B, C
```

`A` is 5 bits, `B` and `C` are 3 bits each (little-endian bit order;
`B` holds `C`'s overflow).  Instructions are packed 15 to a 64-bit chunk,
least significant first, biased by `SALT`.  Strings and numeric constants are
stored in separate encrypted pools; numbers are kept as 8 raw IEEE doubles.
Micro-opcodes (`08` and `01`) drive a small accumulator machine that expands
the immediates into full operands.

## Opcodes

| op | mnemonic | R[A] <- |
|----|----------|---------|
| 00 | NOP      | (nothing) |
| 01 | LDI      | immediate integer from the micro-op stream |
| 02 | MOV      | R[B] |
| 03 | ADD      | R[B] + R[C] |
| 04 | SUB      | R[B] - R[C] |
| 05 | MUL      | R[B] * R[C] |
| 06 | DIV      | R[B] / R[C] |
| 07 | MOD      | R[B] % R[C] |
| 08 | POW      | R[B] ^ R[C] |
| 09 | IDIV     | R[B] // R[C] |
| 0A | BAND     | R[B] & R[C] |
| 0B | BOR      | R[B] \| R[C] |
| 0C | BXOR     | R[B] ~ R[C] |
| 0D | SHL      | R[B] << R[C] |
| 0E | SHR      | R[B] >> R[C] |
| 0F | CONCAT   | R[B] .. R[C] |
| 10 | BNOT     | ~R[B] |
| 11 | NEG      | -R[B] |
| 12 | NOT      | not R[B] |
| 13 | LEN      | #R[B] |
| 14 | EQ       | R[B] == R[C] |
| 15 | NEQ      | R[B] ~= R[C] |
| 16 | LEQ      | R[B] <= R[C] |
| 17 | GEQ      | R[B] >= R[C] |
| 18 | LT       | R[B] < R[C] |
| 19 | GT       | R[B] > R[C] |
| 1A | TEST     | `R[B]` (truthiness, preserves the operand value) |
| 1B | NOTEST   | `not R[B]` |
| 1C | LEN2     | `#R[B]` (alternate encoding) |
| 1D | LOADK    | constant #B |
| 1E | LOADS    | string #B |
| 1F | LOADBOOL | boolean #B |

### Tables, upvalues, calls, jumps

| op | mnemonic | meaning |
|----|----------|---------|
| 20 | NEWTABLE | R[A] <- `{}` |
| 21 | GETGLOBAL | R[A] <- `_ENV[R[B]]` |
| 22 | SETGLOBAL | `_ENV[R[B]] = R[A]` |
| 23 | GETUPVAL | R[A] <- `U[B]` |
| 24 | SETUPVAL | `U[B] = R[A]` |
| 25 | GETTABLE | R[A] <- `R[B][R[A]]` |
| 26 | SETTABLE | `R[B][R[A]] = R[C]` |
| 27 | GETINDEX | R[A] <- `R[B][C]` (small integer key) |
| 28 | SETINDEX | `R[B][C] = R[A]` |
| 29 | SELF     | R[A] <- method lookup using name index `B` |
| 2A | CALL     | call `R[A]` with `B` args, `C` results |
| 2B | VARARG   | collect `...` into `R[A]` |
| 2C | RETURN   | return `B` values starting at `R[A]` |
| 2D | JUMP     | `pc += sBx` (16-bit signed) |
| 2E | JUMPIF   | `if R[A] then pc += sBx` |
| 2F | JUMPIFNOT| `if not R[A] then pc += sBx` |
| 30 | CLOSURE  | R[A] <- closure over prototype `B`, upvalue spec at pc+1 |
| 31 | FORPREP  | numeric-for initialisation, jump by sBx |
| 32 | FORLOOP  | numeric-for step, jump by sBx while in range |
| 33 | TFORCALL | generic-for: call iterator, `B` results |
| 34 | TFORLOOP | generic-for: advance or exit, jump by sBx |

### Why this shape is hard to auto-decompile

* Register allocation is *not* Lua's.  Locals, temporaries and constants are
  assigned from a shuffled global order, so a value that lived in `R3` in the
  original ends up scattered; there is no 1:1 mapping back to variables.
* Constants live in an encrypted pool addressed through a per-instruction
  decoder, so they cannot be recovered without executing the decoder.
* Every function is encrypted separately; functions are only decrypted when
  they are first called, so a static scan of the file reveals one function at
  a time and never the whole program.
* Jumps are biased by an opcode-keyed table, so control flow cannot be read
  off the instruction stream without emulating the key schedule.

## Constants

* String pool: each entry is XOR-masked with a key derived from the module key
  and the entry index.
* Number pool: raw IEEE-754 little-endian doubles, biased **additively** with
  0.5 so the bytes are never a valid double pattern.  There is no exponent
  arithmetic anywhere in the decoder -- this deliberately avoids the
  "denormal float" arithmetic bug class that has broken several obfuscators on
  unusual-but-legal inputs.
