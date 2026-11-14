"""pdf file structure: startxref, xref tables and streams, object streams, trailer, page tree.

Port of dochan 1.21.0 pdf/structure.py (MIT), without its encryption setup (the standard
security handler comes with crypto.py). A broken xref, or an offset that is not an object
header, is rebuilt by scanning for `N G obj`.
"""
import bisect
import re
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional, Tuple

from ..limits import ReadError
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
# Form XObjects decode under their own budget, so a big drawing form does not use up the
# body budget and lose the pages after it.
MAX_FORM_TOTAL_DECODED = 200 * 1024 * 1024
# Image samples must not consume the body budget or pile up in its cache.
MAX_IMAGE_DECODED_CACHE = 50 * 1024 * 1024
MAX_IMAGE_CACHE_ENTRIES = 128

_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")

_OBJ_RE = re.compile(rb"(?<!\d)(\d{1,10})\s+(\d{1,5})\s+obj\b")
_HEADER_AT_RE = re.compile(rb"[\x00\t\n\x0c\r ]*\d{1,10}\s+\d{1,5}\s+obj\b")
_ENCRYPT_RE = re.compile(rb"/Encrypt\s+\d+\s+\d+\s+R")
_CATALOG_RE = re.compile(rb"/Type\s*/Catalog(?![A-Za-z0-9])")


class PDFFile:
    """A parsed pdf: object access and the page tree walk."""

    def __init__(self, data: bytes, password: str = ""):
        self.password = password
        header = data.find(b"%PDF-", 0, HEADER_WINDOW + 5)
        if header > 0:
            data = data[header:]  # offsets in the file count from the header
        self.data = data
        self.warnings: List[str] = []
        self.trailer: Dict[str, Any] = {}
        self.xref: Dict[int, Optional[int]] = {}  # object number -> offset (None = deleted)
        self.recovered = False  # the xref was rebuilt by scanning for objects
        self.budget = DecodeBudget()
        self._form_budget = DecodeBudget(MAX_FORM_TOTAL_DECODED)
        self._image_budget = DecodeBudget()
        self._cache: Dict[int, Any] = {}
        self._rescanned = False
        self._decoded_cache: Dict[int, Tuple[PDFStream, bytes]] = {}
        self._image_decoded_cache: OrderedDict = OrderedDict()
        self._image_cached_bytes = 0
        # PDF 1.5+ compressed objects: object number -> (ObjStm number, index in the stream)
        self._compressed: Dict[int, Tuple[int, int]] = {}
        self._objstm_cache: Dict[int, Tuple[list, Dict[int, Any]]] = {}
        self._catalog: Optional[dict] = None
        if header < 0:
            self.warnings.append("WARN: no %PDF- header in the first 1024 bytes")
        self._parse_structure()
        if self.trailer.get("Encrypt") is not None:
            raise ReadError("encrypted", "not supported yet")
        if "Root" not in self.trailer:
            raise ReadError("damaged", "no catalog")

    # -- structure --

    def _parse_structure(self) -> None:
        start = self._find_startxref()
        ok = start is not None and self._parse_xref_chain(start)
        if not ok or "Root" not in self.trailer:
            self._rescanned = True  # a file recovered by scanning is not scanned again
            self._scan_objects()

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
            self.warnings.append(
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
                self.warnings.append("WARN: object count passed the cap; parsing part of the file")
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

    # -- objects --

    def get_object(self, ref: PDFRef) -> Any:
        if ref.num in self._cache:
            return self._cache[ref.num]
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
            num, _gen, obj = parse_indirect_object(
                self.data, offset, resolve=self.resolve, recovered=recovered)
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
        if recovered:
            self.warnings.append(f"WARN: object {ref.num}: skipped an unbalanced ')' after a string")
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
        pairs = parse_object_stream(data, n, first)
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
        """Decode image samples under their own budget, with a small bounded cache."""
        key = id(stream)
        cached = self._image_decoded_cache.get(key)
        if cached is not None:
            if cached[0] is stream:
                self._image_decoded_cache.move_to_end(key)
                return cached[1]
            self._image_cached_bytes -= len(self._image_decoded_cache.pop(key)[1])
        out = decode_stream(stream.dictionary, stream.raw, self.warnings, self._image_budget)
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
        """Decode a Form XObject under its own budget; None when it is spent or the form was cut.

        A content stream cut in the middle breaks its operators, so a form that hits a cap
        is skipped whole. The caller asks once per stream, so nothing is cached here.
        """
        if self._form_budget.total <= 0:
            return None
        cuts = self._form_budget.cuts
        out = decode_stream(stream.dictionary, stream.raw, self.warnings, self._form_budget)
        if self._form_budget.cuts != cuts:
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
            self._catalog = root if isinstance(root, dict) else {}
        return self._catalog

    def pages(self) -> List[Tuple[dict, dict]]:
        """The pages in document order: (page dict, inherited Resources/MediaBox/CropBox/Rotate)."""
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
        elif len(result) >= MAX_PAGES:
            self.warnings.append(f"WARN: page count passed the cap ({MAX_PAGES}); parsing part of the file")
        return result

    def _walk_pages(self, node: dict, inherited: dict, result, visited, depth) -> None:
        if depth > MAX_TREE_DEPTH or len(result) >= MAX_PAGES:
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
        result.append((node, effective))
