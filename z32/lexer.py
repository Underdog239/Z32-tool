r"""Z32 lexer -- Lua 5.1 / Lua 5.2+ / Luau tokenizer.

Handles the union of the three dialects so that a single parser can accept
"Roblox Luau" and "plain Lua" input:

  * Lua 5.1      : classic tokens, ``\ddd`` escapes, long strings, ``[[ ]]==]``
  * Lua 5.2/5.3  : ``\z`` ``\x HH`` ``\u{...}`` escapes, ``goto`` labels, ``//``
  * Luau         : ``continue``, compound assignment (``+=`` ...), ``//``
                   floor division, backtick interpolation, number
                   separators (``1_000``), ``0b`` binary literals and the
                   type syntax punctuation (``::`` ``->`` ``?`` ``|`` ``&``).

The lexer is deliberately *lossless about values*: every token carries the
concrete Python value it denotes (bytes for strings, float for numbers) so the
back-end never has to re-parse a literal.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Token", "Lexer", "LexError", "KEYWORDS", "lex"]


class LexError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"{line}:{col}: {msg}")
        self.msg = msg
        self.line = line
        self.col = col


# --------------------------------------------------------------------------
# token kinds
# --------------------------------------------------------------------------
# Keywords are emitted with kind == the keyword text itself, so a parser can
# simply test ``tok.kind == 'function'``.
KEYWORDS = frozenset(
    """
    and break do else elseif end false for function if in local nil not or
    repeat return then true until while
    """.split()
)

# Contextual keywords (Luau): valid as identifiers in most positions.
CONTEXTUAL = frozenset({"continue", "type", "export"})

SYMBOLS = [
    # longest first -- the scanner does a longest-match walk
    "...", "..=", "//=",
    "==", "~=", "<=", ">=", "..", "::", "->", "//",
    "+=", "-=", "*=", "/=", "%=", "^=", "<<", ">>",
    "+", "-", "*", "/", "%", "^", "#", "&", "~", "|", "<", ">", "=",
    "(", ")", "{", "}", "[", "]", ";", ":", ",", ".", "?", "@",
]

# A fast dispatch table: first character -> list of candidate symbols.
# Shift tokens exist in Lua 5.3+ but *not* in Luau.  In Luau `<<` and `>>`
# must be scanned as two separate angle brackets, otherwise generic type
# syntax such as `keyof<MyObject>>` and `f<<number>>()` cannot be parsed --
# a real bug that bites type-annotation-skipping.
_SHIFT_SYMBOLS = frozenset({"<<", ">>"})


def _symbol_table(luau: bool) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for sym in SYMBOLS:
        if luau and sym in _SHIFT_SYMBOLS:
            continue
        table.setdefault(sym[0], []).append(sym)
    for lst in table.values():
        lst.sort(key=len, reverse=True)
    return table


_SYMBOL_START_BY_MODE = {True: _symbol_table(True), False: _symbol_table(False)}


@dataclass(slots=True)
class Token:
    kind: str          # 'name' | 'number' | 'string' | keyword | symbol text | 'eof'
    value: object      # bytes for strings, float for numbers, str for names
    line: int
    col: int
    # Luau interpolated strings keep their raw parts for the parser.
    extra: object = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_DIGITS = "0123456789"
_HEX = "0123456789abcdefABCDEF"

_ESCAPES = {
    "a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
    "\\": 92, '"': 34, "'": 39, "\n": 10,
}


class Lexer:
    def __init__(self, src: str, *, luau: bool = True, name: str = "?"):
        self.src = src
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.col = 1
        self.luau = luau
        self.name = name
        self.tokens: list[Token] = []
        self._symbols = _SYMBOL_START_BY_MODE[bool(luau)]

    # -- low level ---------------------------------------------------------
    def error(self, msg: str) -> LexError:
        return LexError(msg, self.line, self.col)

    def _advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.i < self.n:
                if self.src[self.i] == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.i += 1

    def _peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    # -- long bracket ------------------------------------------------------
    def _long_bracket_level(self) -> int:
        """If positioned at ``[`` of a long bracket, return its level, else -1.

        Does not consume anything when it is not a long bracket.
        """
        if self._peek() != "[":
            return -1
        j = self.i + 1
        level = 0
        while j < self.n and self.src[j] == "=":
            level += 1
            j += 1
        if j < self.n and self.src[j] == "[":
            return level
        return -1

    # -- main --------------------------------------------------------------
    def tokenize(self) -> list[Token]:
        src = self.src
        n = self.n
        toks = self.tokens
        while self.i < n:
            c = src[self.i]

            # whitespace ---------------------------------------------------
            if c in " \t\r\v\f":
                self._advance()
                continue
            if c == "\n":
                self._advance()
                continue
            # BOM
            if c == "\ufeff":
                self._advance()
                continue

            # comments -----------------------------------------------------
            if c == "-" and self._peek(1) == "-":
                self._advance(2)
                if self._long_bracket_level() >= 0:
                    self._read_long_string(comment=True)
                else:
                    while self.i < n and src[self.i] != "\n":
                        self._advance()
                continue

            # long strings ------------------------------------------------
            if c == "[":
                lvl = self._long_bracket_level()
                if lvl >= 0:
                    start = (self.line, self.col)
                    val = self._read_long_string()
                    toks.append(Token("string", val, start[0], start[1]))
                    continue

            # names / keywords --------------------------------------------
            if c.isalpha() or c == "_":
                line, col = self.line, self.col
                j = self.i
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1
                word = src[self.i:j]
                self._advance(j - self.i)
                if word in KEYWORDS:
                    toks.append(Token(word, word, line, col))
                else:
                    toks.append(Token("name", word, line, col))
                continue

            # numbers ------------------------------------------------------
            if c in _DIGITS or (c == "." and self._peek(1) in _DIGITS):
                toks.append(self._read_number())
                continue

            # strings ------------------------------------------------------
            if c == '"' or c == "'":
                toks.append(self._read_string(c))
                continue
            if c == "`":
                toks.append(self._read_interp_string())
                continue

            # symbols ------------------------------------------------------
            cands = self._symbols.get(c)
            if cands:
                line, col = self.line, self.col
                for sym in cands:
                    if src.startswith(sym, self.i):
                        self._advance(len(sym))
                        toks.append(Token(sym, sym, line, col))
                        break
                else:  # pragma: no cover - defensive
                    raise self.error(f"unexpected symbol near {c!r}")
                continue

            raise self.error(f"unexpected symbol {c!r}")

        toks.append(Token("eof", None, self.line, self.col))
        return toks

    # -- token producers ---------------------------------------------------
    def _read_long_string(self, comment: bool = False) -> bytes:
        src = self.src
        start_line, start_col = self.line, self.col
        lvl = self._long_bracket_level()
        if lvl < 0:
            raise self.error("invalid long string delimiter")
        self._advance(lvl + 2)  # '[' '='*lvl '['
        # a newline immediately after the opening bracket is skipped
        if self._peek() == "\r":
            self._advance()
        if self._peek() == "\n":
            self._advance()
        out = bytearray()
        close = "]" + "=" * lvl + "]"
        while True:
            if self.i >= self.n:
                raise LexError(
                    "unfinished long string" if not comment else "unfinished long comment",
                    start_line, start_col,
                )
            if src.startswith(close, self.i):
                self._advance(len(close))
                break
            ch = src[self.i]
            out.extend(ch.encode("utf-8", "surrogateescape"))
            self._advance()
        return bytes(out)

    def _read_string(self, quote: str) -> Token:
        src = self.src
        line, col = self.line, self.col
        self._advance()  # opening quote
        out = bytearray()
        while True:
            if self.i >= self.n:
                raise LexError("unfinished string", line, col)
            c = src[self.i]
            if c == quote:
                self._advance()
                break
            if c == "\n":
                raise LexError("unfinished string", line, col)
            if c != "\\":
                out.extend(c.encode("utf-8", "surrogateescape"))
                self._advance()
                continue
            # escape sequence
            self._advance()
            e = self._peek()
            if e == "":
                raise LexError("unfinished string", line, col)
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                self._advance()
                continue
            if e == "x":
                self._advance()
                h = src[self.i:self.i + 2]
                if len(h) < 2 or any(ch not in _HEX for ch in h):
                    raise self.error("hexadecimal digit expected")
                out.append(int(h, 16))
                self._advance(2)
                continue
            if e == "z":
                self._advance()
                while self.i < self.n and src[self.i] in " \t\r\n\v\f":
                    self._advance()
                continue
            if e == "u":
                self._advance()
                if self._peek() != "{":
                    raise self.error("missing '{' in \\u{xxxx}")
                self._advance()
                j = self.i
                while self.i < self.n and src[self.i] != "}":
                    if src[self.i] not in _HEX:
                        raise self.error("hexadecimal digit expected")
                    self._advance()
                if self.i >= self.n:
                    raise self.error("missing '}' in \\u{xxxx}")
                h = src[j:self.i]
                self._advance()  # '}'
                if not h or len(h) > 16:
                    raise self.error("hexadecimal digit expected")
                out.extend(chr(int(h, 16)).encode("utf-8", "surrogatepass"))
                continue
            if e in _DIGITS:
                j = self.i
                while self.i < self.n and self.i - j < 3 and src[self.i] in _DIGITS:
                    self._advance()
                val = int(src[j:self.i])
                if val > 255:
                    raise self.error("decimal escape too large")
                out.append(val)
                continue
            if e == "\r":
                self._advance()
                if self._peek() == "\n":
                    self._advance()
                out.append(10)
                continue
            if e == "\n":
                self._advance()
                out.append(10)
                continue
            # Luau semantics: an unrecognised escape yields the bare character.
            out.extend(e.encode("utf-8", "surrogatepass"))
            self._advance()
        return Token("string", bytes(out), line, col)

    def _read_interp_string(self) -> Token:
        """Luau backtick string: `` `a{b}c` ``.

        The *raw* segments are kept in ``extra`` so the parser can build the
        concatenation, while ``value`` holds the full raw text for diagnostics.
        """
        if not self.luau:
            raise self.error("string interpolation is a Luau extension")
        line, col = self.line, self.col
        self._advance()  # backtick
        parts: list[object] = []   # bytes for text, or ('expr', tokens)
        cur = bytearray()
        while True:
            if self.i >= self.n:
                raise LexError("unfinished interpolated string", line, col)
            c = self.src[self.i]
            if c == "`":
                self._advance()
                break
            if c == "\\":
                self._advance()
                e = self._peek()
                if e in _ESCAPES:
                    cur.append(_ESCAPES[e])
                    self._advance()
                elif e == "x":
                    self._advance()
                    h = self.src[self.i:self.i + 2]
                    if len(h) < 2 or any(ch not in _HEX for ch in h):
                        raise self.error("hexadecimal digit expected")
                    cur.append(int(h, 16))
                    self._advance(2)
                elif e == "z":
                    self._advance()
                    while self.i < self.n and self.src[self.i] in " \t\r\n\v\f":
                        self._advance()
                elif e == "u":
                    self._advance()
                    if self._peek() != "{":
                        raise self.error("missing '{' in \\u{xxxx}")
                    self._advance()
                    j = self.i
                    while self.i < self.n and self.src[self.i] != "}":
                        self._advance()
                    h = self.src[j:self.i]
                    self._advance()
                    cur.extend(chr(int(h, 16)).encode("utf-8", "surrogatepass"))
                elif e in _DIGITS:
                    j = self.i
                    while self.i < self.n and self.i - j < 3 and self.src[self.i] in _DIGITS:
                        self._advance()
                    cur.append(int(self.src[j:self.i]))
                elif e == "`":
                    cur.append(96)
                    self._advance()
                elif e == "{":
                    cur.append(123)
                    self._advance()
                elif e == "}":
                    cur.append(125)
                    self._advance()
                else:
                    # Luau: unrecognised escape yields the bare character,
                    # which is what lets `\ ` (escaped space) through.
                    cur.extend(e.encode("utf-8", "surrogatepass"))
                    self._advance()
                continue
            if c == "{":
                # scan a balanced brace expression
                parts.append(bytes(cur))
                cur = bytearray()
                self._advance()
                depth = 1
                j = self.i
                while self.i < self.n:
                    ch = self.src[self.i]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    elif ch == '"' or ch == "'":
                        self._skip_quoted(ch)
                        continue
                    elif ch == "`":
                        # nested interpolation; recurse lexically
                        self._skip_backtick()
                        continue
                    self._advance()
                if self.i >= self.n:
                    raise LexError("unfinished interpolation in string", line, col)
                inner = self.src[j:self.i]
                self._advance()  # '}'
                parts.append(("expr", inner, self.line, self.col))
                continue
            cur.extend(c.encode("utf-8", "surrogatepass"))
            self._advance()
        parts.append(bytes(cur))
        return Token("interp", None, line, col, extra=parts)

    def _skip_quoted(self, quote: str) -> None:
        self._advance()
        while self.i < self.n:
            c = self.src[self.i]
            if c == "\\":
                self._advance(2)
                continue
            if c == quote:
                self._advance()
                return
            if c == "\n":
                return
            self._advance()

    def _skip_backtick(self) -> None:
        self._advance()
        while self.i < self.n:
            c = self.src[self.i]
            if c == "\\":
                self._advance(2)
                continue
            if c == "`":
                self._advance()
                return
            if c == "{":
                self._advance()
                depth = 1
                while self.i < self.n and depth:
                    ch = self.src[self.i]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    elif ch == '"' or ch == "'":
                        self._skip_quoted(ch)
                        continue
                    self._advance()
                continue
            self._advance()

    def _is_int_suffix(self) -> bool:
        """True when the cursor sits on the `i` of a Luau integer literal."""
        if not self.luau or self._peek() not in ("i", "I"):
            return False
        nxt = self._peek(1)
        return not (nxt.isalnum() or nxt == "_")

    def _read_number(self) -> Token:
        src = self.src
        line, col = self.line, self.col
        start = self.i
        luau = self.luau

        def take_while(pred) -> None:
            while self.i < self.n and pred(src[self.i]):
                self._advance()

        def digits() -> None:
            if luau:
                take_while(lambda ch: ch in _DIGITS or ch == "_")
            else:
                take_while(lambda ch: ch in _DIGITS)

        if src[self.i] == "0" and self._peek(1) in ("x", "X"):
            self._advance(2)
            had = False
            if luau:
                take_while(lambda ch: ch in _HEX or ch == "_")
                had = self.i > start + 2
            else:
                j = self.i
                take_while(lambda ch: ch in _HEX)
                had = self.i > j
            if self._peek() == "." and luau:
                self._advance()
                take_while(lambda ch: ch in _HEX or ch == "_")
            if self._peek() in ("p", "P") and luau:
                self._advance()
                if self._peek() in ("+", "-"):
                    self._advance()
                take_while(lambda ch: ch in _DIGITS or ch == "_")
            text = src[start:self.i].replace("_", "")
            if not had and "." not in text:
                raise self.error("malformed number")
            if luau and self._is_int_suffix():
                self._advance()
                return Token("integer", int(text, 16), line, col)
            try:
                val = float.fromhex(_fix_float_hex(text))
            except ValueError:
                raise self.error("malformed number") from None
            return Token("number", val, line, col)

        if src[self.i] == "0" and self._peek(1) in ("b", "B") and luau:
            self._advance(2)
            j = self.i
            take_while(lambda ch: ch in "01" or ch == "_")
            if self.i == j:
                raise self.error("malformed number")
            text = src[start + 2:self.i].replace("_", "")
            if luau and self._is_int_suffix():
                self._advance()
                return Token("integer", int(text, 2), line, col)
            return Token("number", float(int(text, 2)), line, col)

        digits()
        if self._peek() == ".":
            self._advance()
            digits()
        if self._peek() in ("e", "E"):
            # only consume when it really is an exponent
            nxt = self._peek(1)
            nxt2 = self._peek(2)
            if nxt in _DIGITS or (nxt in ("+", "-") and nxt2 in _DIGITS):
                self._advance()
                if self._peek() in ("+", "-"):
                    self._advance()
                digits()
        # Luau 64-bit integer literals (`123i`).  The value is carried
        # exactly; the parser refuses them rather than silently demoting a
        # 64-bit integer to a double (which would corrupt arithmetic).
        integral = luau and self._is_int_suffix()
        if integral:
            self._advance()
        text = src[start:self.i].replace("_", "")
        if integral:
            text = text[:-1]
        try:
            if integral and text.lower().startswith("0x"):
                intval = int(text, 16)
            elif integral and text.lower().startswith("0b"):
                intval = int(text[2:], 2)
            elif integral:
                intval = int(text)
            else:
                intval = None
                val = float(text)
        except ValueError:
            raise self.error("malformed number") from None
        if integral:
            tok = Token("integer", intval, line, col)
            return tok
        return Token("number", val, line, col)


def _fix_float_hex(text: str) -> str:
    """``0x1.8`` -> ``0x1.8p0`` (python's fromhex wants a binary exponent)."""
    low = text.lower()
    if "p" not in low:
        return text + "p0"
    return text


def lex(src: str, *, luau: bool = True, name: str = "?") -> list[Token]:
    return Lexer(src, luau=luau, name=name).tokenize()
