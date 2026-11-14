"""pdf file structure: startxref, xref tables and streams, object streams, trailer, page tree.

Port of dochan 1.21.0 pdf/structure.py (MIT). A broken xref, or an offset that is not an
object header, is rebuilt by scanning for `N G obj`. An encrypted file opens its security
handler (crypto.py) once the structure is read; objects fetched after that are decrypted.
Strings inside an object stream come out of the stream's own decryption only.
"""
import bisect
import re
from collections import Counter, OrderedDict, deque
from typing import Any, Dict, List, Optional, Tuple

from ..limits import ReadError
from .crypto import StandardSecurityHandler, open_handler
from .filters import DecodeBudget, decode_stream
from .objects import (PDFLexer, PDFRef, PDFStream, PDFSyntaxError,
                      parse_indirect_object, parse_object_stream)

MAX_XREF_SECTIONS = 32
MAX_OBJECTS = 500_000
MAX_PAGES = 10_000
MAX_RESOLVE_DEPTH = 32
MAX_TREE_DEPTH = 64
HEADER_WINDOW = 1024  # the %PDF- header may sit this far into the file
_SCAN_ROOT_LIMIT = 10_000
# Image samples must not consume the body budget or pile up in its cache.
MAX_IMAGE_DECODED_CACHE = 50 * 1024 * 1024
MAX_IMAGE_CACHE_ENTRIES = 128

_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")

_OBJ_RE = re.compile(rb"(?<!\d)(\d{1,10})\s+(\d{1,5})\s+obj\b")
_HEADER_AT_RE = re.compile(rb"[\x00\t\n\x0c\r ]*\d{1,10}\s+\d{1,5}\s+obj\b")
_ENCRYPT_RE = re.compile(rb"/Encrypt\s+\d+\s+\d+\s+R")
_CATALOG_RE = re.compile(rb"/Type\s*/Catalog(?![A-Za-z0-9])")


def _identity_crypt(dictionary: dict) -> bool:
    """True when the stream's own /Crypt filter names the Identity filter: it is not encrypted."""
    filters = dictionary.get("Filter")
    parms = dictionary.get("DecodeParms", dictionary.get("DP"))
    if not isinstance(filters, list):
        filters, parms = [filters], [parms]
    elif not isinstance(parms, list):
        parms = [parms] * len(filters)
    for name, parm in zip(filters, parms):
        if str(name) == "Crypt":
            return not isinstance(parm, dict) or str(parm.get("Name", "Identity")) == "Identity"
    return False


class PDFFile:
    """A parsed pdf: object access and the page tree walk."""

    def __init__(self, data: bytes, password: str = ""):
        self.password = password
        header = data.find(b"%PDF-", 0, HEADER_WINDOW + 5)
        self._whole = data
        self._junk = header > 0
        if self._junk:
            # Offsets count from the header first (what PDFium and pdf.js do); ISO 32000
            # counts from byte 0, which _parse_structure tries when the first reading fails.
            data = data[header:]
        self.data = data
        # Content that a cap dropped, by cap: collection, tree_depth, pages, objects,
        # xref_sections, objstm, resolve_depth.
        self.cuts: Counter = Counter()
        self.warnings: List[str] = []
        self.trailer: Dict[str, Any] = {}
        self.xref: Dict[int, Optional[int]] = {}  # object number -> offset (None = deleted)
        self.recovered = False  # the xref was rebuilt by scanning for objects
        self.budget = DecodeBudget()
        self._nest = 0
        self._pages: Optional[list] = None
        self._cache: Dict[int, Any] = {}
        self._rescanned = False
        self._decoded_cache: Dict[int, Tuple[PDFStream, bytes]] = {}
        self._image_decoded_cache: OrderedDict = OrderedDict()
        self._image_cached_bytes = 0
        # PDF 1.5+ compressed objects: object number -> (ObjStm number, index in the stream)
        self._compressed: Dict[int, Tuple[int, int]] = {}
        self._objstm_cache: Dict[int, Tuple[list, Dict[int, Any]]] = {}
        self._catalog: Optional[dict] = None
        # The security handler lives outside _reset: a junk-preamble retry keeps it.
        self._handler: Optional[StandardSecurityHandler] = None
        self._encrypt_num: Optional[int] = None
        if header < 0:
            self.warnings.append("WARN: no %PDF- header in the first 1024 bytes")
        self._parse_structure()
        self._open_encryption()
        if not self.catalog:
            raise ReadError("damaged", "no catalog")

    # -- structure --

    def _parse_structure(self) -> None:
        warnings, cuts = list(self.warnings), self.cuts.copy()  # before the first attempt
        start = self._find_startxref()
        ok = start is not None and self._parse_xref_chain(start)
        if (not ok or "Root" not in self.trailer) and self._junk:
            stripped = self.data
            self._reset()
            self.data = self._whole
            start = self._find_startxref()
            ok = start is not None and self._parse_xref_chain(start)
            if not ok or "Root" not in self.trailer:
                self._reset()
                self.data = stripped
                ok = False
            else:
                # The byte-0 reading parsed: the failed first attempt leaves nothing behind.
                self.warnings[:] = warnings
                self.cuts.clear()
                self.cuts.update(cuts)
        if not ok or "Root" not in self.trailer:
            self._rescanned = True  # a file recovered by scanning is not scanned again
            self._scan_objects()

    def _reset(self) -> None:
        self.trailer = {}
        self.xref = {}
        self._compressed = {}
        self._cache = {}
        self._objstm_cache = {}

    def _cut(self, key: str, message: str) -> None:
        self.cuts[key] += 1
        self._warn_once(message)

    def _warn_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _find_startxref(self) -> Optional[int]:
        tail = self.data[-2048:]
        idx = tail.rfind(b"startxref")
        if idx < 0:
            self.warnings.append("WARN: startxref not found; scanning for objects")
            return None
        base = len(self.data) - len(tail)
        lexer = PDFLexer(self.data, base + idx + len(b"startxref"))
        lexer.skip_whitespace()
        token = lexer.read_token()
        if not token.isdigit():
            self.warnings.append("WARN: startxref offset is damaged; scanning for objects")
            return None
        offset = int(token)
        if offset >= len(self.data):
            self.warnings.append("WARN: startxref offset is outside the file; scanning for objects")
            return None
        return offset

    def _parse_xref_chain(self, offset: int) -> bool:
        seen = set()
        current: Optional[int] = offset
        while current is not None and current not in seen and len(seen) < MAX_XREF_SECTIONS:
            seen.add(current)
            lexer = PDFLexer(self.data, current)
            lexer.skip_whitespace()
            if lexer.read_token() == b"xref":
                trailer = self._parse_xref_table(lexer)
                if trailer is None:
                    return False
            else:
                trailer = self._parse_xref_stream_at(current)
                if trailer is None:
                    self.warnings.append("WARN: xref stream did not parse; scanning for objects")
                    return False
            for key, value in trailer.items():
                self.trailer.setdefault(key, value)  # the newest trailer wins
            prev = trailer.get("Prev")
            current = prev if isinstance(prev, int) and prev >= 0 else None
        if current is not None and current not in seen:
            self._cut("xref_sections",
                      f"WARN: more than {MAX_XREF_SECTIONS} xref sections; older revisions are ignored")
        return True

    def _parse_xref_table(self, lexer: PDFLexer) -> Optional[dict]:
        while True:
            lexer.skip_whitespace()
            token = lexer.read_token()
            if token == b"trailer":
                lexer.skip_whitespace()
                try:
                    obj = lexer.parse_object()
                except PDFSyntaxError:
                    self.warnings.append("WARN: trailer is damaged; scanning for objects")
                    return None
                return obj if isinstance(obj, dict) else None
            if not token.isdigit():
                self.warnings.append("WARN: xref table is damaged; scanning for objects")
                return None
            start_num = int(token)
            lexer.skip_whitespace()
            count_tok = lexer.read_token()
            if not count_tok.isdigit() or int(count_tok) > MAX_OBJECTS:
                self.warnings.append("WARN: xref table is damaged; scanning for objects")
                return None
            for i in range(int(count_tok)):
                lexer.skip_whitespace()
                off_tok = lexer.read_token()
                lexer.skip_whitespace()
                gen_tok = lexer.read_token()
                lexer.skip_whitespace()
                kind = lexer.read_token()
                if not off_tok.isdigit() or not gen_tok.isdigit() or kind not in (b"n", b"f"):
                    self.warnings.append("WARN: xref entry is damaged; scanning for objects")
                    return None
                obj_num = start_num + i
                if obj_num not in self.xref:
                    # Free entries are tombstones too: without them an object deleted by an
                    # incremental update would come back from an older revision.
                    self.xref[obj_num] = int(off_tok) if kind == b"n" else None

    def _parse_xref_stream_at(self, offset: int) -> Optional[dict]:
        """Parse a `/Type /XRef` stream object; its dictionary is the trailer."""
        try:
            _num, _gen, obj = parse_indirect_object(self.data, offset, resolve=None)
        except PDFSyntaxError:
            return None
        if not isinstance(obj, PDFStream) or str(obj.dictionary.get("Type", "")) != "XRef":
            return None
        stream_dict = obj.dictionary
        data = self.decode_stream_bytes(obj)
        if not data:
            return None
        w = stream_dict.get("W")
        if not (isinstance(w, list) and 1 <= len(w) <= 3
                and all(isinstance(x, int) and 0 <= x <= 8 for x in w)):
            return None
        widths = list(w) + [0] * (3 - len(w))
        entry_width = sum(widths)
        if entry_width <= 0:
            return None
        size = stream_dict.get("Size")
        size = size if isinstance(size, int) and size >= 0 else 0
        index = stream_dict.get("Index")
        if not (isinstance(index, list) and index and len(index) % 2 == 0
                and all(isinstance(x, int) and x >= 0 for x in index)):
            index = [0, size]
        pos = 0
        for pair_idx in range(0, len(index), 2):
            start, count = index[pair_idx], index[pair_idx + 1]
            if count > MAX_OBJECTS:
                return None
            for i in range(count):
                if pos + entry_width > len(data) or len(self.xref) + len(self._compressed) > MAX_OBJECTS:
                    break
                fields = []
                for width in widths:
                    fields.append(int.from_bytes(data[pos:pos + width], "big") if width else None)
                    pos += width
                entry_type = fields[0] if widths[0] else 1
                obj_num = start + i
                if obj_num in self.xref or obj_num in self._compressed:
                    continue  # the newest section wins
                if entry_type == 1 and fields[1] is not None:
                    self.xref[obj_num] = fields[1]
                elif entry_type == 2 and fields[1] is not None:
                    self._compressed[obj_num] = (fields[1], fields[2] or 0)
                elif entry_type == 0:
                    self.xref[obj_num] = None  # free: a tombstone
        return stream_dict

    def _scan_objects(self) -> None:
        self.recovered = True
        self.xref = {}
        self._cache = {}
        self._compressed = {}
        self._objstm_cache = {}
        count = 0
        for match in _OBJ_RE.finditer(self.data):
            # A later definition is the newer one (incremental update), so it overwrites.
            self.xref[int(match.group(1))] = match.start()
            count += 1
            if count > MAX_OBJECTS:
                self._cut("objects", "WARN: object count passed the cap; parsing part of the file")
                break
        self._recover_trailers()
        objstm_warning = ("WARN: scan recovery does not find objects inside object streams "
                          "(/ObjStm); some objects may be missing")
        if b"/ObjStm" in self.data and objstm_warning not in self.warnings:
            self.warnings.append(objstm_warning)
        if "Root" not in self.trailer:
            self._find_root_by_scan()

    def _rescan_keeping_compressed(self) -> None:
        """Rebuild offsets by scanning, keeping object-stream entries the scan cannot see."""
        compressed = dict(self._compressed)
        self._scan_objects()
        for num, location in compressed.items():
            # The newest xref said "compressed"; a plain scanned match is older.
            self.xref.pop(num, None)
            self._compressed[num] = location

    def _recover_trailers(self) -> None:
        """Recover trailer dictionaries on the scan fallback, newest first.

        Dropping the trailer while rebuilding the xref would hide /Encrypt and read an
        encrypted file as plain. An xref-stream file has no `trailer` keyword, so /Encrypt
        is also detected by its reference pattern.
        """
        pos = len(self.data)
        count = 0
        while count < MAX_XREF_SECTIONS:
            pos = self.data.rfind(b"trailer", 0, pos)
            if pos < 0:
                break
            count += 1
            lexer = PDFLexer(self.data, pos + len(b"trailer"))
            try:
                lexer.skip_whitespace()
                obj = lexer.parse_object()
            except PDFSyntaxError:
                continue
            if isinstance(obj, dict):
                for key, value in obj.items():
                    self.trailer.setdefault(key, value)
        if "Encrypt" not in self.trailer and _ENCRYPT_RE.search(self.data):
            self.trailer["Encrypt"] = True

    def _find_root_by_scan(self) -> None:
        # Only objects whose bytes say /Type /Catalog are examined, the latest in the file
        # first so an incremental update's catalog wins. The matches sit in a bounded deque,
        # and only the dictionary after the header is parsed: a catalog is never a stream.
        located = sorted((offset, num) for num, offset in self.xref.items()
                         if isinstance(offset, int))
        starts = [offset for offset, _ in located]
        tried = set()
        recent = deque(_CATALOG_RE.finditer(self.data), maxlen=_SCAN_ROOT_LIMIT)
        for match in reversed(recent):
            index = bisect.bisect_right(starts, match.start()) - 1
            if index < 0:
                continue
            offset, num = located[index]
            if num in tried:
                continue
            tried.add(num)
            lexer = PDFLexer(self.data, offset)
            try:
                lexer.skip_whitespace()
                head = (lexer.read_token(), lexer.skip_whitespace(), lexer.read_token(),
                        lexer.skip_whitespace(), lexer.read_token())
                if not (head[0].isdigit() and head[2].isdigit() and head[4] == b"obj"):
                    continue
                obj = lexer.parse_object()
            except PDFSyntaxError:
                continue
            if isinstance(obj, dict) and str(obj.get("Type", "")) == "Catalog":
                self.trailer["Root"] = PDFRef(num, 0)
                return

    # -- encryption --

    def _open_encryption(self) -> None:
        """Open the security handler named by the trailer's /Encrypt, with the trailer's /ID."""
        entry = self.trailer.get("Encrypt")
        if entry is None:
            return
        if isinstance(entry, PDFRef):
            self._encrypt_num = entry.num
        encrypt = self.resolve(entry)
        if not isinstance(encrypt, dict):
            raise ReadError("encrypted", "encryption dictionary not found")
        ids = self.resolve(self.trailer.get("ID"))
        id0 = ids[0] if isinstance(ids, list) and ids and isinstance(ids[0], bytes) else b""
        # The dictionary's values may be indirect (/CF, the filter names); one level is read.
        flat = {key: self.resolve(value) for key, value in encrypt.items()}
        if isinstance(flat.get("CF"), dict):
            flat["CF"] = {name: self.resolve(item) for name, item in flat["CF"].items()}
        self._handler = open_handler(flat, id0, self.password)
        # What was read before the handler existed is still ciphertext.
        self._cache = {}
        self._decoded_cache = {}
        self._objstm_cache = {}

    def _decrypt_object(self, num: int, gen: int, obj: Any) -> Any:
        """Decrypt the strings and the stream bytes of one indirect object."""
        handler = self._handler
        if handler is None or num == self._encrypt_num:
            return obj
        if isinstance(obj, PDFStream):
            kind = str(obj.dictionary.get("Type", ""))
            if kind == "XRef":
                return obj
            obj.dictionary = self._decrypt_value(num, gen, obj.dictionary)
            if (kind == "Metadata" and not handler.encrypt_metadata) or _identity_crypt(obj.dictionary):
                return obj
            try:
                obj.raw = handler.decrypt(num, gen, obj.raw)
            except ReadError as e:
                if e.kind != "too_large":
                    raise
                obj.raw = b""
                self._cut("decrypt_budget", "WARN: the decryption budget is spent; streams are dropped")
            return obj
        return self._decrypt_value(num, gen, obj)

    def _decrypt_value(self, num: int, gen: int, obj: Any) -> Any:
        if isinstance(obj, dict):
            for key, value in obj.items():
                obj[key] = self._decrypt_value(num, gen, value)
            return obj
        if isinstance(obj, list):
            return [self._decrypt_value(num, gen, v) for v in obj]
        if isinstance(obj, bytes):
            try:
                return self._handler.decrypt(num, gen, obj, string=True)
            except ReadError as e:
                if e.kind != "too_large":
                    raise
                self._cut("decrypt_budget", "WARN: the decryption budget is spent; strings are dropped")
                return b""
        return obj

    # -- objects --

    def get_object(self, ref: PDFRef) -> Any:
        if ref.num in self._cache:
            return self._cache[ref.num]
        # A stream /Length that names another stream nests get_object once per object.
        if self._nest >= MAX_RESOLVE_DEPTH:
            self._cut("resolve_depth", "WARN: object references nest too deep; some are dropped")
            return None
        self._nest += 1
        try:
            return self._get_object(ref)
        finally:
            self._nest -= 1

    def _get_object(self, ref: PDFRef) -> Any:
        offset = self.xref.get(ref.num)
        if offset is None:
            if ref.num in self._compressed:
                return self._load_compressed(ref.num)
            return None
        if offset >= len(self.data):
            return None
        self._cache[ref.num] = None  # guard against reference cycles
        recovered = []
        try:
            num, gen, obj = parse_indirect_object(
                self.data, offset, resolve=self.resolve, recovered=recovered,
                cuts=self.cuts)
        except PDFSyntaxError as e:
            if not self._rescanned and not _HEADER_AT_RE.match(self.data, offset):
                # A stale offset that lands on no object header gets the same scan recovery
                # as one that names another object.
                self._rescanned = True
                self.warnings.append(f"WARN: object {ref.num} did not parse: {e}; rebuilding by scan")
                self._rescan_keeping_compressed()
                return self.get_object(ref)
            self.warnings.append(f"WARN: object {ref.num} did not parse: {e}")
            return None
        if num != ref.num:
            if not self._rescanned:
                self._rescanned = True
                self.warnings.append("WARN: xref offset does not match its object; rebuilding by scan")
                self._rescan_keeping_compressed()
                return self.get_object(ref)
            self.warnings.append(f"WARN: object {ref.num} offset names object {num}")
            return None
        if self.cuts["collection"]:
            self._warn_once("WARN: a collection passed its item cap or nesting cap; content was dropped")
        if recovered:
            self.warnings.append(f"WARN: object {ref.num}: skipped an unbalanced ')' after a string")
        if self._handler is not None:
            obj = self._decrypt_object(ref.num, gen, obj)
        self._cache[ref.num] = obj
        return obj

    def resolve(self, obj: Any, depth: int = 0) -> Any:
        while isinstance(obj, PDFRef):
            if depth > MAX_RESOLVE_DEPTH:
                return None
            obj = self.get_object(obj)
            depth += 1
        return obj

    def _load_compressed(self, num: int) -> Any:
        """Load an object stored in an object stream."""
        self._cache[num] = None  # guard against reference cycles
        objstm_num, idx = self._compressed[num]
        pairs, by_num = self._objstm_contents(objstm_num)
        if num in by_num:
            obj = by_num[num]
        elif 0 <= idx < len(pairs):
            obj = pairs[idx][1]
        else:
            self.warnings.append(f"WARN: object {num} not found in ObjStm {objstm_num}")
            return None
        self._cache[num] = obj
        return obj

    def _objstm_contents(self, objstm_num: int) -> Tuple[list, Dict[int, Any]]:
        cached = self._objstm_cache.get(objstm_num)
        if cached is not None:
            return cached
        empty: Tuple[list, Dict[int, Any]] = ([], {})
        self._objstm_cache[objstm_num] = empty  # a cycle guard and a failure cache
        stm = self.get_object(PDFRef(objstm_num, 0))
        if not isinstance(stm, PDFStream) or str(stm.dictionary.get("Type", "")) != "ObjStm":
            return empty
        data = self.decode_stream_bytes(stm)
        n = stm.dictionary.get("N")
        first = stm.dictionary.get("First")
        if not data or not isinstance(n, int) or not isinstance(first, int) \
                or n < 0 or n > MAX_OBJECTS or first < 0:
            return empty
        pairs = parse_object_stream(data, n, first, self.cuts)
        if self.cuts["collection"] or self.cuts["objstm"]:
            self._warn_once("WARN: an object stream passed a cap; content was dropped")
        result = (pairs, {num: obj for num, obj in reversed(pairs)})
        self._objstm_cache[objstm_num] = result
        return result

    # -- stream bytes --

    def decode_stream_bytes(self, stream: PDFStream) -> bytes:
        """Decode a stream under the document budget, once per stream object.

        The same stream named many times decodes once, which stops amplification by
        repeated references.
        """
        key = id(stream)
        cached = self._decoded_cache.get(key)
        if cached is not None and cached[0] is stream:
            return cached[1]
        out = decode_stream(stream.dictionary, stream.raw, self.warnings, self.budget)
        # The stream stays in the cache beside its bytes, so a later stream cannot reuse its id.
        self._decoded_cache[key] = (stream, out)
        return out

    def decode_image_bytes(self, stream: PDFStream) -> bytes:
        """Decode image samples under the document budget, with a small bounded cache."""
        key = id(stream)
        cached = self._image_decoded_cache.get(key)
        if cached is not None:
            if cached[0] is stream:
                self._image_decoded_cache.move_to_end(key)
                return cached[1]
            self._image_cached_bytes -= len(self._image_decoded_cache.pop(key)[1])
        out = decode_stream(stream.dictionary, stream.raw, self.warnings, self.budget)
        # Repeated small images stay cheap without keeping every page's pixels.
        if out and len(out) <= MAX_IMAGE_DECODED_CACHE:
            while self._image_decoded_cache and (
                    self._image_cached_bytes + len(out) > MAX_IMAGE_DECODED_CACHE or
                    len(self._image_decoded_cache) >= MAX_IMAGE_CACHE_ENTRIES):
                _, (_, evicted) = self._image_decoded_cache.popitem(last=False)
                self._image_cached_bytes -= len(evicted)
            self._image_decoded_cache[key] = (stream, out)
            self._image_cached_bytes += len(out)
        return out

    def decode_form_bytes(self, stream: PDFStream) -> Optional[bytes]:
        """Decode a Form XObject under the document budget; None when it is spent or the form was cut.

        A content stream cut in the middle breaks its operators, so a form that hits a cap
        is skipped whole. The caller asks once per stream, so nothing is cached here.
        """
        if self.budget.total <= 0:
            return None
        cuts = self.budget.cuts
        out = decode_stream(stream.dictionary, stream.raw, self.warnings, self.budget)
        if self.budget.cuts != cuts:
            return None
        return out

    # -- page tree --

    @property
    def catalog(self) -> dict:
        """The document catalog, or {} when there is none."""
        if self._catalog is None:
            root = self.resolve(self.trailer.get("Root"))
            if not (isinstance(root, dict)
                    and (str(root.get("Type", "")) == "Catalog" or "Pages" in root)):
                # A broken update may name a non-catalog object as Root. Look for a real
                # /Type /Catalog once before giving up.
                declared = self.trailer.get("Root")
                self._find_root_by_scan()
                if getattr(self.trailer.get("Root"), "num", None) != getattr(declared, "num", None):
                    self.warnings.append("WARN: trailer Root is not a catalog; using a scanned catalog")
                    root = self.resolve(self.trailer.get("Root"))
                else:
                    root = None  # no catalog anywhere
            self._catalog = root if isinstance(root, dict) else {}
        return self._catalog

    def pages(self) -> List[Tuple[dict, dict]]:
        """The pages in document order: (page dict, inherited Resources/MediaBox/CropBox/Rotate)."""
        if self._pages is None:
            self._pages = self._collect_pages()
        return self._pages

    def _collect_pages(self) -> List[Tuple[dict, dict]]:
        root = self.catalog
        if not root:
            self.warnings.append("WARN: pdf catalog (Root) not found")
            return []
        pages_root = self.resolve(root.get("Pages"))
        if not isinstance(pages_root, dict):
            self.warnings.append("WARN: page tree not found")
            return []
        result: List[Tuple[dict, dict]] = []
        self._walk_pages(pages_root, {}, result, set(), 0)
        if not result:
            self.warnings.append("WARN: no pages found")
        return result

    def _walk_pages(self, node: dict, inherited: dict, result, visited, depth) -> None:
        if depth > MAX_TREE_DEPTH:
            self._cut("tree_depth", f"WARN: page tree is deeper than {MAX_TREE_DEPTH}; pages below are dropped")
            return
        node_id = id(node)
        if node_id in visited:
            return
        visited.add(node_id)
        effective = dict(inherited)
        for key in _INHERITED:
            value = self.resolve(node.get(key))
            if value is None or (key == "Resources" and not isinstance(value, dict)):
                continue
            effective[key] = value
        node_type = str(node.get("Type", ""))
        kids = node.get("Kids")
        if kids is not None and node_type != "Page":
            kids = self.resolve(kids)
            if not isinstance(kids, list):
                return
            for kid_ref in kids:
                kid = self.resolve(kid_ref)
                if isinstance(kid, dict):
                    self._walk_pages(kid, effective, result, visited, depth + 1)
            return
        if len(result) >= MAX_PAGES:
            self._cut("pages", f"WARN: page count passed the cap ({MAX_PAGES}); the rest are dropped")
            return
        result.append((node, effective))
