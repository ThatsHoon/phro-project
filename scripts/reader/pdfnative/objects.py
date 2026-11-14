"""pdf object syntax: the lexer and the basic object parsers.

Port of dochan 1.21.0 pdf/objects.py (MIT), Python path only. ISO 32000-1 7.3: booleans,
numbers, strings, names, arrays, dictionaries, streams, null and indirect references.
Differences from dochan: collections past MAX_COLLECTION_ITEMS drop their extra items and
set the lexer's `cut` flag instead of failing; nesting is capped at limits.MAX_DEPTH;
parse_object_stream returns the parsed objects.
"""
from collections import Counter
from dataclasses import dataclass
import re
from typing import Any, Callable, List, Optional, Tuple

from .. import limits

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"

_TOKEN_RE = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")

MAX_COLLECTION_ITEMS = 100_000


class PDFSyntaxError(ValueError):
    """A violation of the pdf object syntax."""


@dataclass(frozen=True)
class PDFRef:
    """An indirect reference (`12 0 R`)."""
    num: int
    gen: int


class PDFName(str):
    """A name object (`/Name`); a str subclass, so it is a dictionary key as it is."""


@dataclass
class PDFStream:
    """A stream object: its dictionary and its raw (still filtered) bytes."""
    dictionary: dict
    raw: bytes


class PDFLexer:
    """A sequential parser over a byte buffer."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos
        self._depth = 0
        # Set when a collection dropped items past MAX_COLLECTION_ITEMS or nesting passed
        # limits.MAX_DEPTH: content was cut.
        self.cut = False
        # Content streams only: a stray operator inside an array ("[(a) 0 Tc (b)] TJ") is skipped.
        self.skip_array_operators = False
        # A dictionary value written as "(text))" leaves one unbalanced ")" where the next
        # key belongs; _parse_dict skips it and counts it here.
        self.stray_closers = 0

    def _peek(self) -> int:
        if self.pos < 0 or self.pos >= len(self.data):
            return -1
        return self.data[self.pos]

    def skip_whitespace(self) -> None:
        data, n = self.data, len(self.data)
        if self.pos < 0:
            self.pos = 0
        while self.pos < n:
            ch = data[self.pos]
            if ch in WHITESPACE:
                self.pos += 1
            elif ch == 0x25:  # '%' comment to the end of the line
                while self.pos < n and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                return

    def read_token(self) -> bytes:
        start = self.pos
        if start < 0 or start >= len(self.data):
            return b""
        match = _TOKEN_RE.match(self.data, start)
        self.pos = match.end()
        return self.data[start:self.pos]

    def next_object(self) -> Any:
        return self.parse_object()

    def parse_object(self) -> Any:
        self.skip_whitespace()
        ch = self._peek()
        if ch < 0:
            raise PDFSyntaxError("unexpected end of data")
        if ch == 0x3C:  # '<'
            if self.data[self.pos:self.pos + 2] == b"<<":
                return self._parse_dict()
            return self._parse_hex_string()
        if ch == 0x28:  # '('
            return self._parse_literal_string()
        if ch == 0x2F:  # '/'
            return self._parse_name()
        if ch == 0x5B:  # '['
            return self._parse_array()
        token = self.read_token()
        if not token:
            raise PDFSyntaxError(f"unparsable character: {self.data[self.pos:self.pos + 8]!r}")
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        return self._parse_number_or_ref(token)

    def _parse_number_or_ref(self, token: bytes) -> Any:
        try:
            if b"." in token:
                return float(token)
            value = int(token)
        except ValueError:
            raise PDFSyntaxError(f"bad number token: {token!r}")
        if value >= 0:
            saved = self.pos
            self.skip_whitespace()
            token2 = self.read_token()
            if token2.isdigit():
                self.skip_whitespace()
                if self.read_token() == b"R":
                    return PDFRef(value, int(token2))
            self.pos = saved
        return value

    def _parse_name(self) -> PDFName:
        self.pos += 1  # '/'
        raw = self.read_token()
        out = bytearray()
        i = 0
        while i < len(raw):
            if raw[i:i + 1] == b"#" and i + 2 < len(raw):
                try:
                    out.append(int(raw[i + 1:i + 3], 16))
                    i += 3
                    continue
                except ValueError:
                    pass
            out.append(raw[i])
            i += 1
        return PDFName(out.decode("utf-8", errors="replace"))

    def _parse_literal_string(self) -> bytes:
        self.pos += 1  # '('
        out = bytearray()
        depth = 1
        data, n = self.data, len(self.data)
        while self.pos < n:
            ch = data[self.pos]
            if ch == 0x5C:  # '\'
                self.pos += 1
                if self.pos >= n:
                    break
                esc = data[self.pos]
                if esc == 0x6E:
                    out.append(0x0A)
                elif esc == 0x72:
                    out.append(0x0D)
                elif esc == 0x74:
                    out.append(0x09)
                elif esc == 0x62:
                    out.append(0x08)
                elif esc == 0x66:
                    out.append(0x0C)
                elif esc in (0x28, 0x29, 0x5C):
                    out.append(esc)
                elif 0x30 <= esc <= 0x37:  # octal escape, at most three digits
                    octal = chr(esc)
                    for _ in range(2):
                        nxt = data[self.pos + 1] if self.pos + 1 < n else -1
                        if 0x30 <= nxt <= 0x37:
                            self.pos += 1
                            octal += chr(nxt)
                        else:
                            break
                    out.append(int(octal, 8) & 0xFF)
                elif esc in (0x0D, 0x0A):  # a line continuation is dropped
                    if esc == 0x0D and self.pos + 1 < n and data[self.pos + 1] == 0x0A:
                        self.pos += 1
                else:
                    out.append(esc)
                self.pos += 1
            elif ch == 0x28:
                depth += 1
                out.append(ch)
                self.pos += 1
            elif ch == 0x29:
                depth -= 1
                self.pos += 1
                if depth == 0:
                    return bytes(out)
                out.append(ch)
            else:
                out.append(ch)
                self.pos += 1
        raise PDFSyntaxError("unclosed literal string")

    def _parse_hex_string(self) -> bytes:
        self.pos += 1  # '<'
        end = self.data.find(b">", self.pos)
        if end < 0:
            raise PDFSyntaxError("unclosed hex string")
        hex_chars = bytes(c for c in self.data[self.pos:end] if c not in WHITESPACE)
        self.pos = end + 1
        if len(hex_chars) % 2:
            hex_chars += b"0"
        try:
            return bytes.fromhex(hex_chars.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise PDFSyntaxError("bad hex string")

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > limits.MAX_DEPTH:
            self.cut = True
            raise PDFSyntaxError("nesting is too deep")

    def _parse_array(self) -> list:
        # Lenient pass first; if the array does not close cleanly, re-parse it strictly so a
        # missing ']' fails at the first operator instead of swallowing the rest of the
        # content stream.
        if self.skip_array_operators and self._depth == 0:
            start = self.pos
            try:
                return self._parse_array_body()
            except PDFSyntaxError:
                self.pos = start
                self.skip_array_operators = False
                try:
                    return self._parse_array_body()
                finally:
                    self.skip_array_operators = True
        return self._parse_array_body()

    def _parse_array_body(self) -> list:
        self.pos += 1  # '['
        self._enter()
        try:
            items = []
            while True:
                self.skip_whitespace()
                if self._peek() == 0x5D:
                    self.pos += 1
                    return items
                if self._peek() < 0:
                    raise PDFSyntaxError("unclosed array")
                if self.skip_array_operators and (
                        0x41 <= self._peek() <= 0x5A or 0x61 <= self._peek() <= 0x7A
                        or self._peek() in (0x22, 0x27)):
                    saved = self.pos
                    token = self.read_token()
                    if not token:
                        self.pos = saved + 1
                        continue
                    if token not in (b"true", b"false", b"null"):
                        continue
                    self.pos = saved
                item = self.parse_object()
                if len(items) < MAX_COLLECTION_ITEMS:
                    items.append(item)
                else:
                    self.cut = True
        finally:
            self._depth -= 1

    def _parse_dict(self) -> dict:
        self.pos += 2  # '<<'
        self._enter()
        try:
            result = {}
            after_string = False
            while True:
                self.skip_whitespace()
                if after_string and self._peek() == 0x29:
                    # Some writers end a link target with an unescaped ")": "/URI (http://x))".
                    # The string ends at the first ")", so exactly one ")" is left over.
                    self.pos += 1
                    self.stray_closers += 1
                    after_string = False
                    continue
                if self.data[self.pos:self.pos + 2] == b">>":
                    self.pos += 2
                    return result
                if self._peek() != 0x2F:
                    raise PDFSyntaxError("a dictionary key must be a name")
                key = self._parse_name()
                self.skip_whitespace()
                after_string = self._peek() == 0x28
                value = self.parse_object()
                if len(result) < MAX_COLLECTION_ITEMS or str(key) in result:
                    result[str(key)] = value
                else:
                    self.cut = True
        finally:
            self._depth -= 1


def _stream_bytes(data: bytes, start: int, length: Any) -> bytes:
    raw = None
    if isinstance(length, int) and 0 <= length <= len(data) - start:
        candidate_end = start + length
        tail = data[candidate_end:candidate_end + 20]
        if tail.lstrip(WHITESPACE).startswith(b"endstream"):
            raw = data[start:candidate_end]
    if raw is None:
        end = data.find(b"endstream", start)
        if end < 0:
            raise PDFSyntaxError("endstream is missing")
        raw = data[start:end]
        # Drop one EOL delimiter only: a binary payload may end in 0x0A.
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith((b"\n", b"\r")):
            raw = raw[:-1]
    return raw


def parse_indirect_object(
    data: bytes,
    offset: int,
    resolve: Optional[Callable[[PDFRef], Any]] = None,
    recovered: Optional[list] = None,
    cuts: Optional[Counter] = None,
) -> Tuple[int, int, Any]:
    """Parse `num gen obj ... endobj`.

    resolve: turns an indirect stream /Length into an int. A wrong or missing /Length is
    corrected by searching for `endstream`.
    recovered: gets the count of skipped unbalanced ")" appended when there are any.
    cuts: a Counter; `collection` grows by 1 when the object dropped items or nesting.
    """
    lexer = PDFLexer(data, offset)
    lexer.skip_whitespace()
    num_tok = lexer.read_token()
    lexer.skip_whitespace()
    gen_tok = lexer.read_token()
    lexer.skip_whitespace()
    obj_tok = lexer.read_token()
    if not num_tok.isdigit() or not gen_tok.isdigit() or obj_tok != b"obj":
        raise PDFSyntaxError(f"offset {offset}: not an indirect object header")
    try:
        obj = lexer.parse_object()
    finally:
        if cuts is not None and lexer.cut:
            cuts["collection"] += 1
    if recovered is not None and lexer.stray_closers:
        recovered.append(lexer.stray_closers)
    lexer.skip_whitespace()
    keyword_pos = lexer.pos
    keyword = lexer.read_token()
    if keyword == b"stream" and isinstance(obj, dict):
        if lexer.data[lexer.pos:lexer.pos + 2] == b"\r\n":
            lexer.pos += 2
        elif lexer.data[lexer.pos:lexer.pos + 1] in (b"\n", b"\r"):
            lexer.pos += 1
        start = lexer.pos
        length = obj.get("Length")
        if resolve is not None and isinstance(length, PDFRef):
            length = resolve(length)
        raw = _stream_bytes(data, start, length)
        return int(num_tok), int(gen_tok), PDFStream(obj, raw)
    lexer.pos = keyword_pos
    return int(num_tok), int(gen_tok), obj


def parse_object_stream(data: bytes, n: int, first: int,
                        cuts: Optional[Counter] = None) -> List[Tuple[int, Any]]:
    """The `(object number, object)` pairs of an object stream's decoded bytes.

    The header holds n pairs of (object number, offset from `first`). An object that fails
    to parse is left out. Objects of an honest stream do not overlap, so the bytes parsed
    in total are capped at twice the stream plus a small floor: offsets that all name one
    big object cannot multiply the work.
    cuts: a Counter; `collection` counts objects that dropped items, `objstm` the objects
    left unparsed by the work cap.
    """
    header = PDFLexer(data)
    pairs = []
    for _ in range(n):
        header.skip_whitespace()
        obj_tok = header.read_token()
        header.skip_whitespace()
        off_tok = header.read_token()
        if not obj_tok.isdigit() or not off_tok.isdigit():
            break
        pairs.append((int(obj_tok), int(off_tok)))
    work = 2 * len(data) + 4096
    result = []
    for num, off in pairs:
        start = first + off
        if start >= len(data):
            continue
        if work <= 0:
            if cuts is not None:
                cuts["objstm"] += 1
            continue
        lexer = PDFLexer(data, start)
        try:
            obj = lexer.parse_object()
        except PDFSyntaxError:
            work -= max(lexer.pos - start, 1)
            if cuts is not None and lexer.cut:
                cuts["collection"] += 1
            continue
        work -= max(lexer.pos - start, 1)
        if cuts is not None and lexer.cut:
            cuts["collection"] += 1
        result.append((num, obj))
    return result
