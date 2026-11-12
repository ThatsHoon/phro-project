"""Test helpers for the document reader tests."""
import zipfile
from pathlib import Path


def make_zip(path, members, stored=("mimetype",)):
    """Write a zip from {name: str | bytes}; names in `stored` go first, uncompressed."""
    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        names = [n for n in stored if n in members] + [n for n in members if n not in stored]
        for name in names:
            data = members[name]
            if isinstance(data, str):
                data = data.encode("utf-8")
            method = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return path


def make_cfb(path, streams, sector_size=512):
    """Write a compound file (v3 for 512-byte sectors, v4 for 4096) from {"Storage/Name": bytes}.

    Storages come from the "/" parts; siblings are chained through right links. Streams under 4096 bytes go in
    the mini stream. Returns {"fat": [sid], "dir": [sid], "chains": {name: [sid]}, "mini": {name: [mini sid]}},
    so a test can patch bytes at (sid + 1) * sector_size.
    """
    import struct
    ss = sector_size
    assert ss in (512, 4096)
    END, FREE, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    entry = "<64sHBBIII16sIQQIQ"  # name, name bytes, type, color, left, right, child, clsid, state, 2 times, start, size
    # tree: node = [name, type, children, data, id]
    root = ["Root Entry", 5, [], None, 0]
    nodes = [root]
    for full, data in streams.items():
        parent, parts = root, full.split("/")
        for part in parts[:-1]:
            nxt = next((c for c in parent[2] if c[0] == part and c[1] == 1), None)
            if nxt is None:
                nxt = [part, 1, [], None, len(nodes)]
                nodes.append(nxt)
                parent[2].append(nxt)
            parent = nxt
        leaf = [parts[-1], 2, [], bytes(data), len(nodes)]
        nodes.append(leaf)
        parent[2].append(leaf)
    path_of, right = {}, {}

    def walk(node, prefix):
        for i, c in enumerate(node[2]):
            right[c[4]] = node[2][i + 1][4] if i + 1 < len(node[2]) else FREE
            if c[1] == 2:
                path_of[c[4]] = prefix + c[0]
            else:
                walk(c, prefix + c[0] + "/")
    walk(root, "")
    # sector layout: regular streams, mini stream, mini FAT, directory, FAT
    per = ss // 4
    sectors, fat = [], []
    chains, mini_of, start, size_of = {}, {}, {}, {}

    def add(data):
        first = len(sectors)
        n = max(1, -(-len(data) // ss))
        for i in range(n):
            sectors.append(data[i * ss:(i + 1) * ss].ljust(ss, b"\0"))
            fat.append(first + i + 1 if i + 1 < n else END)
        return list(range(first, first + n))
    mini, minifat = bytearray(), []
    for node in nodes[1:]:
        if node[1] != 2:
            continue
        data, nid = node[3], node[4]
        size_of[nid] = len(data)
        if len(data) == 0:
            start[nid] = END
        elif len(data) >= 4096:
            chains[path_of[nid]] = add(data)
            start[nid] = chains[path_of[nid]][0]
        else:
            n = -(-len(data) // 64)
            first = len(mini) // 64
            ids = list(range(first, first + n))
            mini_of[path_of[nid]] = ids
            start[nid] = first
            minifat.extend(i + 1 for i in ids[:-1])
            minifat.append(END)
            mini.extend(data.ljust(n * 64, b"\0"))
    root_start, root_size = END, 0
    if mini:
        mchain = add(bytes(mini))
        root_start, root_size = mchain[0], len(mini)
    mini_first, mini_count = END, 0
    if minifat:
        minifat += [FREE] * (-len(minifat) % per)
        mf = add(struct.pack("<%dI" % len(minifat), *minifat))
        mini_first, mini_count = mf[0], len(mf)
    dirblob = b""
    for node in nodes:
        nid = node[4]
        raw = node[0].encode("utf-16-le") + b"\0\0"
        assert len(raw) <= 64
        st, sz = (root_start, root_size) if nid == 0 else (start.get(nid, 0), size_of.get(nid, 0))
        child = node[2][0][4] if node[2] else FREE
        dirblob += struct.pack(entry, raw, len(raw), node[1], 1, FREE, right.get(nid, FREE), child,
                               b"\0" * 16, 0, 0, 0, st, sz)
    free_entry = struct.pack(entry, b"", 0, 0, 0, FREE, FREE, FREE, b"\0" * 16, 0, 0, 0, 0, 0)
    dirblob += free_entry * (-(-len(dirblob) // ss) * ss // 128 - len(dirblob) // 128)
    dirs = add(dirblob)
    nfat = 1
    while nfat * per < len(fat) + nfat:
        nfat += 1
    assert nfat <= 109, "make_cfb writes no DIFAT"
    fat_ids = list(range(len(sectors), len(sectors) + nfat))
    fat += [FATSECT] * nfat
    fat += [FREE] * (nfat * per - len(fat))
    for i in range(nfat):
        sectors.append(struct.pack("<%dI" % per, *fat[i * per:(i + 1) * per]))
    hdr = bytearray(ss)
    hdr[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 3 if ss == 512 else 4, 0xFFFE, 9 if ss == 512 else 12, 6)
    struct.pack_into("<IIIIIIIII", hdr, 0x28, len(dirs) if ss == 4096 else 0, nfat, dirs[0], 0, 4096,
                     mini_first, mini_count, END, 0)
    struct.pack_into("<109I", hdr, 0x4C, *(fat_ids + [FREE] * (109 - nfat)))
    with open(path, "wb") as f:
        f.write(bytes(hdr))
        f.write(b"".join(sectors))
    return {"fat": fat_ids, "dir": dirs, "chains": chains, "mini": mini_of}


def hwp_record(tag, level, data):
    """One HWP record: u32 header (tag 10 bits, level 10 bits, size 12 bits), extended size from 0xFFF bytes."""
    import struct
    data = bytes(data)
    if len(data) >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), len(data)) + data
    return struct.pack("<I", tag | (level << 10) | (len(data) << 20)) + data


_HWP_EXTENDED = (1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23)  # control characters that own a CTRL_HEADER


def hwp_para(text, *, shape=0, style=0, chars=((0, 0),), level=0, controls=(), ranges=()):
    """One HWP paragraph: PARA_HEADER at `level`, then PARA_TEXT and PARA_CHAR_SHAPE one level deeper.

    `text` is written as UTF-16LE units and ends with unit 13. Each U+FFFC takes the next (code, ctrl_id, children)
    of `controls` and becomes the eight-unit control character: code, the id, four units of padding, code. `ctrl_id`
    is big-endian ASCII such as b"tbl " (stored byte-reversed); bytes past its first four are the CTRL_HEADER's own
    fields after the id. For an extended code the builder then appends that CTRL_HEADER at level + 1 and `children`
    as given (records the caller wrote at level + 2); an inline code such as 4 (field end) or 9 (tab) has none.
    `chars` are the PARA_CHAR_SHAPE (position, char shape id) pairs. `ranges` are PARA_RANGE_TAG (start, end, tag)
    triples in units, written after PARA_CHAR_SHAPE; a tag's top byte is its kind (0x10 insert, 0x11 delete).
    """
    import struct
    units, ctrls = bytearray(), []
    pending = iter(controls)
    for ch in text:
        if ch != "\ufffc":
            units += ch.encode("utf-16-le")
            continue
        code, cid, children = next(pending)
        raw = bytes(cid[:4]).ljust(4, b"\0")[::-1]
        units += struct.pack("<H", code) + raw + bytes(8) + struct.pack("<H", code)
        if code in _HWP_EXTENDED:
            ctrls += [hwp_record(71, level + 1, raw + bytes(cid[4:])), bytes(children)]
    units += struct.pack("<H", 13)
    header = struct.pack("<IIHBBHHHI", len(units) // 2, 0, shape, style, 0, len(chars), len(ranges), 0, 0)
    pairs = b"".join(struct.pack("<II", pos, sid) for pos, sid in chars)
    tags = hwp_record(70, level + 1, b"".join(struct.pack("<III", *r) for r in ranges)) if ranges else b""
    return (hwp_record(66, level, header) + hwp_record(67, level + 1, bytes(units))
            + hwp_record(68, level + 1, pairs) + tags + b"".join(ctrls))


def _hwp_caption(caption, level):
    """A caption list: its LIST_HEADER and one paragraph, both at `level`."""
    import struct
    return [hwp_record(72, level, struct.pack("<HIH", 1, 0, 0)), hwp_para(caption, level=level)]


def hwp_table(rows, *, level=2, caption=None):
    """A table for hwp_para's `controls`: (11, b"tbl ", records), the records written at `level` (the paragraph's
    level + 2): an optional caption list, TABLE, then a LIST_HEADER per cell with the cell's paragraphs beside it.

    `rows` lists rows of cells. A cell is text (one paragraph), a list of texts (one paragraph each), bytes (paragraph
    records already written at `level`) or a tuple (cell, colspan, rowspan). A cell's column is the sum of the
    colspans before it in its row.
    """
    import struct
    out = _hwp_caption(caption, level) if caption is not None else []
    cells, ncols = [], 0
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            body, cs, rs = cell if isinstance(cell, tuple) else (cell, 1, 1)
            if isinstance(body, (bytes, bytearray)):
                n, paras = 1, bytes(body)
            else:
                texts = [body] if isinstance(body, str) else list(body)
                n, paras = len(texts), b"".join(hwp_para(t, level=level) for t in texts)
            # paragraph count, attr, width ref, then col, row, colspan, rowspan, size, margins, border fill
            cells += [hwp_record(72, level, struct.pack("<HIH4H", n, 0, 0, c, r, cs, rs) + bytes(18)), paras]
            c += cs
        ncols = max(ncols, c)
    # attr, rows, cols, spacing, margins, a row size per row, border fill
    out.append(hwp_record(77, level, struct.pack("<I2H", 0, len(rows), ncols) + bytes(10 + 2 * len(rows) + 2)))
    return 11, b"tbl ", b"".join(out + cells)


def hwp_picture(bin_id, alt="", *, level=2, caption=None):
    """A picture for hwp_para's `controls`: (11, b"gso " + its object attributes, records at `level`).

    The CTRL_HEADER holds the shape comment `alt` at offset 44 (u16 length, UTF-16LE); an optional caption list comes
    first, then SHAPE_COMPONENT, with SHAPE_PICTURE one level deeper holding the 1-based `bin_id` at offset 71.
    """
    import struct
    cid = b"gso " + bytes(40) + struct.pack("<H", len(alt)) + alt.encode("utf-16-le")
    out = _hwp_caption(caption, level) if caption is not None else []
    out += [hwp_record(76, level, b"cip$cip$" + bytes(32)),
            hwp_record(85, level + 1, bytes(71) + struct.pack("<H", bin_id) + bytes(9))]
    return 11, cid, b"".join(out)


def make_hwp(path, sections, doc_info=b"", bins=None, flags=1, extra=None):
    """Write an HWP 5.0.3.0 compound file: FileHeader, DocInfo, BodyText/Section<N> (raw-deflated when flags & 1),
    BinData/<name> and any extra streams as given."""
    import struct
    import zlib

    def pack(raw):
        if not flags & 1:
            return bytes(raw)
        c = zlib.compressobj(wbits=-15)
        return c.compress(bytes(raw)) + c.flush()

    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, flags)
    streams = {"FileHeader": header.ljust(256, b"\0"), "DocInfo": pack(doc_info)}
    for i, body in enumerate(sections):
        streams[f"BodyText/Section{i}"] = pack(body)
    for name, data in (bins or {}).items():
        streams[f"BinData/{name}"] = bytes(data)
    streams.update(extra or {})
    path = Path(path)
    make_cfb(path, streams)
    return path


def make_dist_section(section: bytes, key: bytes, seed: int, compressed: bool = True) -> bytes:
    """A distribution document's ViewText/Section<N> stream, the inverse of hwp_records.view_text (tests only).

    DISTRIBUTE_DOC_DATA (tag 28) carries 256 bytes: `seed` as u32 LE at 0-3, then filler XORed with the MSVC rand
    stream seeded with it (key = rand() & 0xFF, then n = (rand() & 0xF) + 1 bytes), with the 16-byte AES `key` at
    4 + (seed & 0xF). After it comes `section` (raw-deflated when `compressed`), zero-padded to whole blocks and
    encrypted with AES-128-ECB under `key`. Needs the reader's scripts folder on sys.path, as the tests set it.
    """
    import struct
    import zlib
    from reader.containers.crypto import MsvcRand, aes128_ecb_encrypt
    rand = MsvcRand(seed)
    mask = bytearray()
    while len(mask) < 256:
        fill = rand() & 0xFF
        mask += bytes([fill]) * ((rand() & 0xF) + 1)
    # filler that is not zero, so a key read from the wrong offset cannot decrypt
    plain = bytearray(struct.pack("<I", seed & 0xFFFFFFFF) + bytes((i * 37 + 11) & 0xFF for i in range(4, 256)))
    at = 4 + (seed & 0xF)
    plain[at:at + 16] = key
    payload = plain[:4] + bytes(b ^ m for b, m in zip(plain[4:], mask[4:256]))
    body = bytes(section)
    if compressed:
        c = zlib.compressobj(wbits=-15)
        body = c.compress(body) + c.flush()
    body += bytes(-len(body) % 16)
    return hwp_record(28, 0, payload) + aes128_ecb_encrypt(bytes(key), body)


DOC_EXTRA = 0x400  # where make_doc puts word_extra in WordDocument; the piece text follows it
DOC_STORIES = ("text", "footnote", "header", "macro", "annotation", "endnote", "textbox", "header_textbox")


def _doc_pcds(clx):
    """(cp0, cp1, fc, bytes a CP) of each piece of a Clx: Prc records skipped, then the Pcdt's PlcPcd."""
    import struct
    pos = 0
    while clx[pos] == 1:
        pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
    size = struct.unpack_from("<I", clx, pos + 1)[0]
    plc = clx[pos + 5:pos + 5 + size]
    n = (size - 4) // 12
    cps = struct.unpack_from("<%dI" % (n + 1), plc)
    out = []
    for i in range(n):
        raw = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        packed = bool(raw & 0x40000000)
        at = raw & 0x3FFFFFFF
        out.append((cps[i], cps[i + 1], at // 2 if packed else at, 1 if packed else 2))
    return out


def _doc_fkp(spans, pcds, papx, pn):
    """FKP pages and their PlcBte for (cp0, cp1, bytes) spans: each span is mapped to FCs through the pieces (split
    where a piece ends), the runs are sorted by FC and the gaps between them filled with runs without properties, so
    each page's runs are contiguous. A CHPX is cb u8 + grpprl; a PAPX (bytes: istd u16 + grpprl) is cb u8 with size
    2cb - 1 for an odd size, else 0 and cb' u8 with size 2cb'. Pages are numbered from `pn`."""
    import struct
    from bisect import bisect_right
    starts = [p[0] for p in pcds]
    runs = []
    for c0, c1, data in spans:
        k = max(bisect_right(starts, c0) - 1, 0)
        while k < len(pcds) and pcds[k][0] < c1:
            p0, p1, f, w = pcds[k]
            a, b = max(c0, p0), min(c1, p1)
            if a < b:
                runs.append((f + (a - p0) * w, f + (b - p0) * w, bytes(data)))
            k += 1
    runs.sort(key=lambda r: r[0])
    filled = []
    for r in runs:
        if filled and filled[-1][1] < r[0]:
            filled.append((filled[-1][1], r[0], b""))
        filled.append(r)
    width = 13 if papx else 1

    def blob(data):
        if not data:
            return b""
        if not papx:
            return bytes([len(data)]) + data
        return bytes([(len(data) + 1) // 2]) + data if len(data) % 2 else bytes([0, len(data) // 2]) + data

    pages, fcs, pns, i = [], [], [], 0
    while i < len(filled):
        page, top, chosen = bytearray(512), 511, []
        while i < len(filled) and len(chosen) < 255:
            b = blob(filled[i][2])
            c = len(chosen) + 1
            off = (top - len(b)) & ~1 if b else 0
            if (c + 1) * 4 + c * width > (off if b else top):
                break
            if b:
                page[off:off + len(b)] = b
                top = off
            chosen.append((filled[i], off))
            i += 1
        assert chosen, "a run too large for one FKP page"
        c = len(chosen)
        for j, ((f0, _, _), off) in enumerate(chosen):
            struct.pack_into("<I", page, 4 * j, f0)
            page[(c + 1) * 4 + j * width] = off // 2
        struct.pack_into("<I", page, 4 * c, chosen[-1][0][1])
        page[511] = c
        fcs.append(chosen[0][0][0])
        pns.append(pn + len(pages))
        pages.append(bytes(page))
    if not pages:
        return b"", b""
    fcs.append(filled[-1][1])
    return b"".join(pages), struct.pack("<%dI" % (2 * len(pages) + 1), *fcs, *pns)


def doc_stsh(styles, base=18):
    """An STSH for make_doc's fc={1: ...}. `styles` lists the istd slots: None for an empty one, else (sti, stk,
    istd_base, name, papx, chpx), stk 1 a paragraph style (UpxPapx: its istd + papx, then UpxChpx: chpx) and 2 a
    character style (UpxChpx only); papx and chpx are grpprls. The Stshi is the 18-byte Stshif plus ftcBi;
    cbSTDBaseInFile is `base`: 10 (Word 97) or 18 (with StdfPost2000). Each UPX is padded to an even size."""
    import struct

    def upx(data):
        return struct.pack("<H", len(data)) + data + bytes(len(data) % 2)

    stshi = struct.pack("<10H", len(styles), base, 1, 0x5B, 15, 0, 0, 0, 0, 0)
    out = bytearray(struct.pack("<H", len(stshi)) + stshi)
    for istd, s in enumerate(styles):
        if s is None:
            out += struct.pack("<H", 0)
            continue
        sti, stk, based, name, papx, chpx = s
        std = struct.pack("<5H", sti & 0xFFF, stk | based << 4, 2 if stk == 1 else 1, 0, 0).ljust(base, b"\0")
        std += struct.pack("<H", len(name)) + name.encode("utf-16-le") + b"\0\0"
        if stk == 1:
            std += upx(struct.pack("<H", istd) + bytes(papx))
        std += upx(bytes(chpx))
        out += struct.pack("<H", len(std)) + std
    return bytes(out)


def _doc_lvl(level):
    """One LVL from (nfc, text[, start[, papx, chpx]]): the 28-byte LVLF (iStartAt, nfc, rgbxchNums,
    cbGrpprlChpx at 24, cbGrpprlPapx at 25), grpprlPapx, grpprlChpx, then the number text xst (u16 count, UTF-16).
    "%N" (N 1-9) in text is a placeholder for level N's number: the unit N - 1, its 1-based offset in rgbxchNums."""
    import struct
    nfc, text, *rest = level
    start = rest[0] if rest else 1
    papx, chpx = (bytes(rest[1]), bytes(rest[2])) if len(rest) > 2 else (b"", b"")
    units, nums, i = bytearray(), [], 0
    while i < len(text):
        if text[i] == "%" and text[i + 1:i + 2] in tuple("123456789"):
            units += struct.pack("<H", int(text[i + 1]) - 1)
            nums.append(len(units) // 2)
            i += 2
        else:
            units += text[i].encode("utf-16-le")
            i += 1
    lvlf = struct.pack("<iBB9sBiiBBBB", start, nfc, 0, bytes(nums[:9]), 0, 0, 0, len(chpx), len(papx), 0, 0)
    return lvlf + papx + chpx + struct.pack("<H", len(units) // 2) + bytes(units)


def doc_lists(lists, lfos=()):
    """FibRgFcLcb97 entries 73 (PlfLst) and 74 (PlfLfo) for make_doc's `fc`, each as (data, trailing): the LVLs
    follow the PlfLst past its lcb, and the LFOData follow the LFOs.

    `lists` are (lsid, levels): one level makes an fSimpleList; more are padded to nine with decimal "%N." levels.
    A level is _doc_lvl's (nfc, text[, start[, papx, chpx]]). `lfos` are (lsid, overrides), ilfo k + 1 being lfos[k];
    an override is (ilvl, start) for a start-at override (LFOLVL fStartAt), or (ilvl, start, level) for an LVL that
    replaces the level (fFormatting; fStartAt too unless start is None). Each LFOData has cp 0xFFFFFFFF.
    """
    import struct
    head, lvls = bytearray(struct.pack("<h", len(lists))), bytearray()
    for lsid, levels in lists:
        levels = list(levels)
        simple = len(levels) == 1
        if not simple:
            levels += [(0, f"%{k + 1}.") for k in range(len(levels), 9)]
        head += struct.pack("<iI9HBB", lsid, 0, *([0x0FFF] * 9), 1 if simple else 0, 0)
        lvls += b"".join(_doc_lvl(lv) for lv in levels)
    lfo, data = bytearray(struct.pack("<I", len(lfos))), bytearray()
    for lsid, overrides in lfos:
        lfo += struct.pack("<iIIBBBB", lsid, 0, 0, len(overrides), 0, 0, 0)
        data += struct.pack("<I", 0xFFFFFFFF)
        for ov in overrides:
            ilvl, start, level = (*ov, None) if len(ov) == 2 else ov
            bits = ilvl | (0x10 if start is not None else 0) | (0x20 if level is not None else 0)
            data += struct.pack("<iI", start or 0, bits) + (_doc_lvl(level) if level is not None else b"")
    return {73: (bytes(head), bytes(lvls)), 74: (bytes(lfo), bytes(data))}


def make_doc(path, pieces, *, lid=0x0409, nfib=0x00C1, flags=0x0200, ccp=None, fc=None, word_extra=b"",
             streams=None, lid_fe=None, chpx=None, papx=None):
    """Write a Word 97 .doc compound file.

    WordDocument holds the FIB (FibBase with `lid`, `nfib` and `flags`; 14 FibRgW words, lidFE at 13, `lid_fe` or
    `lid`; 22 FibRgLw longs, the story lengths at 3-10; 93 FibRgFcLcb97 pairs; cswNew 0), `word_extra` at DOC_EXTRA,
    then the text of `pieces`, (text, compressed) in CP order: compressed text is one CP a byte, in the code page of
    the document's language (lidFE when flags has fFarEast 0x4000 and lidFE is set, else `lid`) when that code page is
    single-byte, else cp1252; the rest is UTF-16LE from an even offset. The table stream (1Table when flags has
    0x0200, else 0Table) holds each `fc` entry (FibRgFcLcb97 index -> bytes, or (data, trailing): lcb covers data and
    trailing follows it, as the LVLs follow a PlfLst) in index order with its (fc, lcb) set in the FIB, then the Clx
    (one Pcdt, no Prc) at index 33 unless `fc` gives it. `ccp` sets story lengths by DOC_STORIES
    name; "text" takes the CPs the others leave. `streams` adds streams and replaces a built one of the same name.
    `chpx` and `papx` are (cp0, cp1, bytes) spans in CPs, a CHPX grpprl or a PAPX (istd u16 + grpprl): they are mapped
    to FCs through the piece table at index 33 (built or given), written as FKP pages after the text, from the next
    512-byte boundary, and their PlcBteChpx (12) and PlcBtePapx (13) join the table stream unless `fc` gives them.
    Needs the reader's scripts folder on sys.path, as the tests set it.
    """
    import struct
    from reader.text import codepage
    fe = lid if lid_fe is None else lid_fe
    eight = codepage(fe if flags & 0x4000 and fe else lid)
    if eight in ("cp932", "cp936", "cp949", "cp950"):  # one byte a CP: a double-byte code page is not used
        eight = "cp1252"
    body = bytearray(bytes(word_extra))
    cps, pcds = [0], []
    for text, compressed in pieces:
        if compressed:
            raw = text.encode(eight)
            at, n = DOC_EXTRA + len(body), len(raw)
            fcv = at * 2 | 0x40000000
        else:
            body += bytes(len(body) % 2)
            raw = text.encode("utf-16-le")
            at, n = DOC_EXTRA + len(body), len(raw) // 2
            fcv = at
        body += raw
        cps.append(cps[-1] + n)
        pcds.append(struct.pack("<HIH", 0, fcv, 0))
    lengths = dict(ccp or {})
    if "text" not in lengths:
        lengths["text"] = max(0, cps[-1] - sum(lengths.values()))
    entries = dict(fc or {})
    if 33 not in entries:
        plc = struct.pack("<%dI" % len(cps), *cps) + b"".join(pcds)
        entries[33] = b"\x02" + struct.pack("<I", len(plc)) + plc
    pages = b""
    if chpx or papx:
        first = -(-(DOC_EXTRA + len(body)) // 512)  # the page number of the first FKP
        clx = entries[33]
        pcd = _doc_pcds(bytes(clx[0] if isinstance(clx, tuple) else clx))
        for index, spans in ((12, chpx), (13, papx)):
            if spans and index not in entries:
                blob, bte = _doc_fkp(spans, pcd, index == 13, first + len(pages) // 512)
                pages += blob
                entries[index] = bte
        if pages:
            pages = bytes(first * 512 - DOC_EXTRA - len(body)) + pages
    pairs, table = [(0, 0)] * 93, bytearray()
    for i in sorted(entries):
        data, trailing = entries[i] if isinstance(entries[i], tuple) else (entries[i], b"")
        pairs[i] = (len(table), len(data))
        table += bytes(data) + bytes(trailing)
    rgw = [0] * 14
    rgw[13] = fe
    rglw = [0] * 22
    rglw[0] = DOC_EXTRA + len(body)  # cbMac
    for k, name in enumerate(DOC_STORIES):
        rglw[3 + k] = lengths.get(name, 0)
    fib = (struct.pack("<7HI2B2H2I", 0xA5EC, nfib, 0, lid, 0, flags, 0x00BF, 0, 0, 0, 0, 0, 0, 0)
           + struct.pack("<15H", 14, *rgw) + struct.pack("<H22I", 22, *rglw)
           + struct.pack("<H", 93) + b"".join(struct.pack("<II", *p) for p in pairs) + struct.pack("<H", 0))
    assert len(fib) <= DOC_EXTRA
    out = {"WordDocument": fib.ljust(DOC_EXTRA, b"\0") + bytes(body) + pages,
           "1Table" if flags & 0x0200 else "0Table": bytes(table)}
    out.update(streams or {})
    path = Path(path)
    make_cfb(path, out)
    return path
