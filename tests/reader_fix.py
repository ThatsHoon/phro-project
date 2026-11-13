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


def doc_plc(cps, data=b""):
    """A PLC for make_doc's fc: the CPs as u32, then the data elements, already joined."""
    import struct
    return struct.pack("<%dI" % len(cps), *cps) + bytes(data)


def doc_stories(main, *, footnotes=(), endnotes=(), comments=(), authors=(), headers=(), textboxes=(),
                header_textboxes=()):
    """make_doc's pieces, ccp and fc for a main story and the stories after it, laid out as Word writes them: every
    story but the main one ends with one more "\\r" that no entry owns (its guard), and a story's text PLC holds the
    start of each entry, then the end of the last one and the guard's end (the story's length), relative to the story.

    - footnotes, endnotes: (ref_cp, text). PlcffndRef/PlcfendRef (2/46) hold ref_cp, a CP in the whole text, with a
      2-byte FRD (nAuto 1) each and, last, a CP past every reference; PlcffndTxt/PlcfendTxt (3/47) the texts' CPs.
    - comments: (ref_cp, ibst, text[, initials]): PlcfandRef (4) with a 30-byte ATRDPre10 each (xstUsrInitl: u16
      count and 9 UTF-16 units; ibst u16 at 20; bitsNotUsed, grfNotUsed, ITagBkmk -1), PlcfandTxt (5) as above.
      authors: GrpXstAtnOwners (36), an Xst (u16 count, UTF-16) a name; none when empty.
    - headers: the PlcfHdd (11) stories in order, the 6 separators first, then 6 a section (even header, odd header,
      even footer, odd footer, first header, first footer); "" for one that is not there.
    - textboxes, header_textboxes: PlcftxbxTxt (56) and PlcfHdrtxbxTxt (58), a 22-byte FTXBXS each (cTxbx 1, fReusable
      0, the shape id) and a dummy one, all zero, for the guard.

    The text is one Unicode piece; a CP is a UTF-16 unit. Returns {"pieces", "ccp", "fc"} for make_doc(path, **it).
    """
    import struct

    def units(s):
        return len(s.encode("utf-16-le")) // 2

    def story(texts):
        """The texts and their guard as one story, and its text PLC."""
        cps, at = [], 0
        for t in texts:
            cps.append(at)
            at += units(t)
        return "".join(texts) + "\r", doc_plc(cps + [at, at + 1])

    def refs(cps, data):
        return doc_plc(list(cps) + [max([*cps, units(main)]) + 1], b"".join(data))

    out, fc = {"text": main}, {}
    for name, ref, txt, notes in (("footnote", 2, 3, footnotes), ("endnote", 46, 47, endnotes)):
        if notes:
            out[name], fc[txt] = story([t for _, t in notes])
            fc[ref] = refs([r for r, _ in notes], [struct.pack("<h", 1)] * len(notes))
    if comments:
        out["annotation"], fc[5] = story([c[2] for c in comments])
        atrd = []
        for c in comments:
            initials = (c[3] if len(c) > 3 else "").encode("utf-16-le")[:18]
            atrd.append(struct.pack("<H", len(initials) // 2) + initials.ljust(18, b"\0")
                        + struct.pack("<HHHi", c[1], 0, 0, -1))
        fc[4] = refs([c[0] for c in comments], atrd)
    if authors:
        fc[36] = b"".join(struct.pack("<H", units(n)) + n.encode("utf-16-le") for n in authors)
    if headers:
        out["header"], fc[11] = story(list(headers))
    for name, index, boxes in (("textbox", 56, textboxes), ("header_textbox", 58, header_textboxes)):
        if boxes:
            out[name], plc = story(list(boxes))
            fc[index] = plc + b"".join(struct.pack("<iihiii", 1, 0, 0, -1, k + 1, 0) for k in range(len(boxes)))
            fc[index] += bytes(22)
    text = "".join(out.get(name, "") for name in DOC_STORIES)
    return {"pieces": [(text, False)], "ccp": {name: units(out.get(name, "")) for name in DOC_STORIES}, "fc": fc}


def oa_record(typ, body=b"", *, ver=0, inst=0):
    """One OfficeArt record (MS-ODRAW 2.2.1): u16 ver (low 4 bits) | inst << 4, u16 type, u32 length, then body."""
    import struct
    return struct.pack("<HHI", ver | inst << 4, typ, len(body)) + bytes(body)


def oa_container(typ, *children, inst=0):
    """An OfficeArt container (ver 0xF) holding the records `children`, already built."""
    return oa_record(typ, b"".join(children), ver=0xF, inst=inst)


def blip_bitmap(typ, inst, data):
    """A bitmap BLIP (JPEG 0xF01D, PNG 0xF01E, DIB 0xF01F, TIFF 0xF029): one 16-byte UID, two when inst is odd, the
    tag byte 0xFF, then the image bytes."""
    return oa_record(typ, bytes(range(16)) * (1 + (inst & 1)) + b"\xff" + bytes(data), inst=inst)


def blip_png(data):
    """A PNG BLIP (0xF01E, instance 0x6E0) of the PNG bytes `data`."""
    return blip_bitmap(0xF01E, 0x6E0, data)


def blip_metafile(typ, inst, data, deflate=True, cb_save=None):
    """A metafile BLIP (EMF 0xF01A/0x3D4, WMF 0xF01B/0x216, PICT 0xF01C/0x542; inst + 1 has two UIDs): the UIDs, the
    34-byte OfficeArtMetafileHeader (cbSize, rcBounds, ptSize, cbSave, compression 0 deflate or 0xFE none, filter
    0xFE), then `data`, zlib-deflated when `deflate`. cb_save overrides cbSave."""
    import struct
    import zlib
    packed = zlib.compress(bytes(data)) if deflate else bytes(data)
    save = len(packed) if cb_save is None else cb_save
    head = struct.pack("<I16s8sIBB", len(data), bytes(16), bytes(8), save, 0 if deflate else 0xFE, 0xFE)
    return oa_record(typ, bytes(range(16)) * (1 + (inst & 1)) + head + packed, inst=inst)


def oa_fbse(blip=b"", delay=0xFFFFFFFF, *, name=b"", cref=1):
    """An FBSE (0xF007): btWin32, btMacOS, rgbUid, tag, size, cRef, foDelay, unused1, cbName, unused2, unused3, the
    name, then the BLIP record `blip` embedded; b"" for one delay-loaded from WordDocument at `delay`."""
    import struct
    body = struct.pack("<BB16sHIIIBBBB", 6, 6, bytes(16), 0xFF, len(blip), cref, delay, 0, len(name), 0, 0)
    return oa_record(0xF007, body + bytes(name) + bytes(blip), ver=2, inst=6)


def oa_shape(spid, pib=None, anchor: bytes | None = None):
    """An SpContainer (0xF004): its FSP (0xF00A: spid, grfPersistent) and, when `pib` is given, an FOPT (0xF00B)
    whose one property is pib (0x4104); when `anchor` is given, a ClientAnchor (0xF010) with that body."""
    import struct
    out = oa_record(0xF00A, struct.pack("<II", spid, 0xA00), ver=2, inst=75)
    if pib is not None:
        out += oa_record(0xF00B, struct.pack("<HI", 0x4104, pib), ver=3, inst=1)
    if anchor is not None:
        out += oa_record(0xF010, anchor)
    return oa_container(0xF004, out)


def oa_group(spid, *members):
    """An SpgrContainer (0xF003): its group shape first (an SpContainer whose FSP has fGroup 0x1), then the members,
    records already built (oa_shape, oa_group), in order."""
    import struct
    shape = oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", spid, 0x201), ver=2))
    return oa_container(0xF003, shape, *members)


def picf_inline(blip, mm=0x64, *, name=b"", pib=None, fbses=None):
    """A PICFAndOfficeArtData for the Data stream: the 68-byte PICF (lcb, cbHeader 0x44, mfpf.mm at offset 6); for mm
    0x66 cchPicName and the name; then the inline SpContainer (spid 1025, `pib` when given) and one FBSE embedding
    the BLIP record `blip` (none when `blip` is b""), or the FBSE records `fbses` as given. For mm 8 `blip` is the
    raw WMF that follows the PICF."""
    import struct
    if mm == 8:
        rest = bytes(blip)
    else:
        rest = (bytes([len(name)]) + bytes(name) if mm == 0x66 else b"") + oa_shape(1025, pib)
        rest += (oa_fbse(blip) if blip else b"") if fbses is None else bytes(fbses)
    return struct.pack("<IHH", 68 + len(rest), 0x44, mm).ljust(68, b"\0") + rest


def dgg_with_bstore(blips, shapes=None, header_shapes=None):
    """A DggInfo (FibRgFcLcb97 50, OfficeArtContent) for make_doc's fc: the DggContainer (an FDGG, then a
    BStoreContainer with an FBSE a blip: bytes embed that BLIP record, an int is the foDelay of a BLIP in
    WordDocument), then the main document's drawing (dgglbl 0, a DgContainer whose SpgrContainer holds the shapes)
    and, when header_shapes is given, the header document's (dgglbl 1). A shape is (spid, pib), pib None for one
    without a pib (a text box, a line), or bytes: records already built (oa_shape, oa_group); `shapes` defaults to
    spid 1025 + k showing pib k + 1."""
    import struct
    store = [oa_fbse(b) if isinstance(b, (bytes, bytearray)) else oa_fbse(delay=b) for b in blips]
    dgg = oa_container(0xF000, oa_record(0xF006, struct.pack("<4I", 1026, 2, 1, 1)),
                       oa_container(0xF001, *store, inst=len(store)))

    def drawing(label, shapes):
        group = oa_container(0xF003, oa_container(0xF004, oa_record(0xF00A, struct.pack("<II", 1024, 5), ver=2)),
                             *(s if isinstance(s, (bytes, bytearray)) else oa_shape(*s) for s in shapes))
        return bytes([label]) + oa_container(0xF002, oa_record(0xF008, struct.pack("<II", len(shapes), 1024)), group)

    if shapes is None:
        shapes = [(1025 + k, k + 1) for k in range(len(blips))]
    out = dgg + drawing(0, shapes)
    if header_shapes is not None:
        out += drawing(1, header_shapes)
    return out


def doc_spa(anchors):
    """A PlcfSpa (PlcSpaMom 40, PlcSpaHdr 41) for make_doc's fc: (cp, spid) anchors, then a last CP past them, and a
    26-byte FSPA each, spid first."""
    import struct
    cps = [cp for cp, _ in anchors]
    return doc_plc(cps + [max(cps, default=0) + 1], b"".join(struct.pack("<I", s).ljust(26, b"\0") for _, s in anchors))


def mtef_v3(body):
    """MTEF v3 data, as Equation Editor 3.0 writes it: the 5-byte header (version 3, Windows, Equation Editor, 3.0),
    then the records `body`, already built, and the END of the top-level list."""
    return b"\x03\x01\x01\x03\x00" + bytes(body) + b"\x00"


def mtef_v5(body):
    """MTEF v5 data, as MathType writes it: the header (version 5, Windows, MathType, 6.0), the application key
    "DSMT6" and the equation options byte, then the records `body` and the END of the top-level list."""
    return b"\x05\x01\x00\x06\x00DSMT6\x00\x00" + bytes(body) + b"\x00"


def eq_native(mtef, cb=28):
    """An Equation Native stream: the 28-byte EQNOLEFILEHDR (cbHdr u16, version u32 0x00020000, cf u16, cbObject
    u32, four reserved u32), its cbHdr `cb` and the header padded or cut to it, then the MTEF bytes."""
    import struct
    head = struct.pack("<HIHI", cb, 0x00020000, 0, len(mtef)).ljust(28, b"\0")
    return head[:cb].ljust(cb, b"\0") + bytes(mtef)


def comp_obj(prog, user=b"Object"):
    """A \\x01CompObj stream (MS-OLEDS CompObjStream): the 28-byte header, the AnsiUserType, no clipboard format,
    then the ProgID `prog`, each string a u32 length with its terminating zero."""
    import struct

    def ansi(s):
        return struct.pack("<I", len(s) + 1) + bytes(s) + b"\0"
    return struct.pack("<II", 0xFFFE0001, 0xA03).ljust(28, b"\xff") + ansi(user) + bytes(4) + ansi(prog)


def object_pool(objects):
    """make_doc streams for the ObjectPool storage: {id: {stream name: bytes}} -> {"ObjectPool/_<id>/<name>": bytes},
    one storage an object."""
    return {f"ObjectPool/_{oid}/{name}": bytes(data) for oid, streams in objects.items()
            for name, data in streams.items()}


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


def rc4_header(password, *, cryptoapi=True, key_bits=128, salt=bytes(range(16))):
    """The bytes officecrypt.parse_rc4 reads at offset 0: an RC4 or RC4 CryptoAPI header for `password`."""
    import hashlib
    import struct
    from reader.containers.crypto import rc4
    pw = password.encode("utf-16le")
    verifier = bytes(range(100, 116))
    if cryptoapi:
        base = hashlib.sha1(salt + pw).digest()
        key = hashlib.sha1(base + bytes(4)).digest()[:key_bits // 8]
        key = key + bytes(11) if key_bits == 40 else key
        enc = rc4(key, verifier + hashlib.sha1(verifier).digest())
        header = struct.pack("<8I", 0x24, 0, 0x6801, 0x8004, 0 if key_bits == 40 else key_bits, 1, 0, 0) + b"\0\0"
        return (struct.pack("<HHII", 3, 2, 0x24, len(header)) + header + struct.pack("<I", 16) + salt
                + enc[:16] + struct.pack("<I", 20) + enc[16:])
    h0 = hashlib.md5(pw).digest()[:5]
    base = hashlib.md5((h0 + salt) * 16).digest()[:5]
    enc = rc4(hashlib.md5(base + bytes(4)).digest(), verifier + hashlib.md5(verifier).digest())
    return struct.pack("<HH", 1, 1) + salt + enc


def biff(rid, body=b""):
    """One BIFF record: id, size, body."""
    import struct
    return struct.pack("<HH", rid, len(body)) + body


def _xl_string(text, short=False):
    import struct
    wide = any(ord(ch) > 255 for ch in text)
    raw = text.encode("utf-16-le" if wide else "latin-1")
    return struct.pack("<B" if short else "<H", len(text)) + bytes([1 if wide else 0]) + raw


def _biff_sst(strings, split):
    """The SST record and its CONTINUEs; with `split` each body holds at most that many bytes and a string cut
    by a boundary restarts with a flags byte, as BIFF8 writes it."""
    import struct
    head = struct.pack("<II", len(strings), len(strings))
    if not split:
        return biff(0x00FC, head + b"".join(_xl_string(s) for s in strings))
    assert split >= 12
    bodies, cur = [], bytearray(head)
    for s in strings:
        wide = any(ord(ch) > 255 for ch in s)
        unit = 2 if wide else 1
        raw = s.encode("utf-16-le" if wide else "latin-1")
        if len(cur) + 3 + (unit if raw else 0) > split:
            bodies.append(bytes(cur))
            cur = bytearray()
        cur += struct.pack("<HB", len(s), 1 if wide else 0)
        while raw:
            take = min(len(raw) // unit, (split - len(cur)) // unit) * unit
            cur += raw[:take]
            raw = raw[take:]
            if raw:
                bodies.append(bytes(cur))
                cur = bytearray([1 if wide else 0])
    bodies.append(bytes(cur))
    return biff(0x00FC, bodies[0]) + b"".join(biff(0x003C, b) for b in bodies[1:])


def _biff_cell(r, c, v, sst, fmt_xf):
    import struct
    pre = struct.pack("<HHH", r, c, 15)
    if isinstance(v, tuple) and v[0] == "fmt":
        return biff(0x0203, struct.pack("<HHH", r, c, fmt_xf[v[2]]) + struct.pack("<d", v[1]))
    if isinstance(v, tuple) and v[0] == "err":
        return biff(0x0205, pre + bytes([v[1], 1]))
    if isinstance(v, tuple) and v[0] == "f":
        cached, extra = v[1], b""
        if isinstance(cached, tuple):
            res = bytes([2, 0, cached[1], 0, 0, 0, 0xFF, 0xFF])
        elif isinstance(cached, bool):
            res = bytes([1, 0, int(cached), 0, 0, 0, 0xFF, 0xFF])
        elif isinstance(cached, str):
            res, extra = bytes([0, 0, 0, 0, 0, 0, 0xFF, 0xFF]), biff(0x0207, _xl_string(cached))
        else:
            res = struct.pack("<d", float(cached))
        return biff(0x0006, pre + res + struct.pack("<HIH", 0, 0, 0)) + extra
    if isinstance(v, bool):
        return biff(0x0205, pre + bytes([int(v), 0]))
    if isinstance(v, str):
        return biff(0x00FD, pre + struct.pack("<I", sst[v]))
    if isinstance(v, int) and -(1 << 29) <= v < (1 << 29):
        return biff(0x027E, pre + struct.pack("<I", ((v << 2) & 0xFFFFFFFF) | 2))
    return biff(0x0203, pre + struct.pack("<d", float(v)))


def _biff_sheet(sh, sst, fmt_xf):
    """One worksheet (or chart, macro, VBA) substream. Merges are (first row, first col, last row, last col). A
    `drawing` (bytes) is the MSODRAWING 0x00EC body, cut after `drawing_split` bytes into a CONTINUE when that is set."""
    import struct
    if "raw" in sh:
        return sh["raw"]
    kind = sh.get("kind", 0)
    dt = {0: 0x0010, 1: 0x0040, 2: 0x0020, 6: 0x0006}.get(kind, 0x0010)
    out = [biff(0x0809, struct.pack("<HHHHII", 0x0600, dt, 0x0DBB, 0x07CC, 0, 6))]
    cells = dict(sorted(sh.get("cells", {}).items()))
    hidden_rows, hidden_cols = set(sh.get("hidden_rows", ())), sorted(set(sh.get("hidden_cols", ())))
    rows = sorted({r for r, _ in cells} | hidden_rows)
    if kind == 0:
        top = max((r for r, _ in cells), default=-1) + 1
        wide = max((c for _, c in cells), default=-1) + 1
        out.append(biff(0x0200, struct.pack("<IIHHH", 0, top, 0, wide, 0)))
        out += [biff(0x007D, struct.pack("<HHHHHH", c, c, 2560, 15, 1, 0)) for c in hidden_cols]
        for r in rows:
            out.append(biff(0x0208, struct.pack("<HHHHHHHH", r, 0, 0, 255, 0, 0, 0x20 if r in hidden_rows else 0, 15)))
        out += [_biff_cell(r, c, v, sst, fmt_xf) for (r, c), v in cells.items()]
        merges = list(sh.get("merges", ()))
        for i in range(0, len(merges), 1000):
            part = merges[i:i + 1000]
            out.append(biff(0x00E5, struct.pack("<H", len(part))
                            + b"".join(struct.pack("<HHHH", m[0], m[2], m[1], m[3]) for m in part)))
    for extra in sh.get("records", ()):
        out.append(extra if isinstance(extra, bytes) else biff(*extra))
    if sh.get("drawing") is not None:
        out += _biff_drawing(0x00EC, sh["drawing"], sh.get("drawing_split"))
    out.append(biff(0x000A))
    return b"".join(out)


def _biff_drawing(rid, body, split=None):
    """An MSODRAWINGGROUP or MSODRAWING record of `body`, its tail from byte `split` on in a CONTINUE."""
    if split is None:
        return [biff(rid, body)]
    return [biff(rid, body[:split]), biff(0x003C, body[split:])]


def biff_workbook(sheets, *, formats=None, date1904=False, active=0, filepass=None, sst_split=None,
                  globals_extra=b"", drawing=None, drawing_split=None):
    """A BIFF8 Workbook stream: globals, then one substream per sheet dict.

    A sheet dict: name, state (0 visible, 1 hidden, 2 very hidden), kind (0 sheet, 1 macro, 2 chart, 6 VBA);
    cells {(row, col): value} with str (LABELSST), float (NUMBER), int (RK), bool (BOOLERR), ("err", code),
    ("f", cached) (FORMULA, plus STRING for text) and ("fmt", number, code) (NUMBER in a cell XF of that format);
    merges [(r1, c1, r2, c2)], hidden_rows, hidden_cols, records (raw records before EOF: framed bytes or
    (id, body)) and raw (the whole substream). Cell XFs start at index 16, one per format code the cells use, in
    order of first use; every other cell uses XF 15. `drawing` (bytes, a DggContainer) is the globals' MSODRAWINGGROUP,
    cut after `drawing_split` bytes into a CONTINUE; a sheet's `drawing` and `drawing_split` do the same for its
    MSODRAWING.
    """
    import struct
    formats = dict(formats or {})
    strings = list(dict.fromkeys(v for sh in sheets for v in sh.get("cells", {}).values() if isinstance(v, str)))
    sst = {s: i for i, s in enumerate(strings)}
    codes = list(dict.fromkeys(v[2] for sh in sheets for v in sh.get("cells", {}).values()
                               if isinstance(v, tuple) and v[0] == "fmt"))
    by_code = {code: fid for fid, code in formats.items()}
    for code in codes:
        if code not in by_code:
            by_code[code] = max([163, *formats]) + 1
            formats[by_code[code]] = code
    fmt_xf = {code: 16 + i for i, code in enumerate(codes)}
    head = [biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6))]
    if filepass is not None:
        head.append(biff(0x002F, filepass))
    head.append(biff(0x0042, struct.pack("<H", 1200)))
    head.append(biff(0x0022, struct.pack("<H", int(date1904))))
    head.append(biff(0x003D, struct.pack("<9H", 0, 0, 0x4000, 0x2000, 0x38, active, 0, 1, 600)))
    head += [biff(0x041E, struct.pack("<H", fid) + _xl_string(code)) for fid, code in formats.items()]
    xf = struct.pack("<HHH", 0, 0, 0xFFF5) + bytes(14)
    head += [biff(0x00E0, xf)] * 16
    head += [biff(0x00E0, struct.pack("<HHH", 0, by_code[code], 1) + bytes(14)) for code in codes]
    head.append(_biff_sst(strings, sst_split))
    names = [biff(0x0085, struct.pack("<IBB", 0, sh.get("state", 0), sh.get("kind", 0))
                  + _xl_string(sh["name"], short=True)) for sh in sheets]
    if drawing is not None:
        globals_extra += b"".join(_biff_drawing(0x00EB, drawing, drawing_split))
    tail = biff(0x000A)
    base = sum(map(len, head)) + sum(map(len, names)) + len(globals_extra) + len(tail)
    subs = [_biff_sheet(sh, sst, fmt_xf) for sh in sheets]
    pos, bounds = base, []
    for n, sub in zip(names, subs):
        bounds.append(n[:4] + struct.pack("<I", pos) + n[8:])
        pos += len(sub)
    return b"".join(head) + b"".join(bounds) + globals_extra + tail + b"".join(subs)


def biff_chart(series, *, title=None, kinds=((0x1017, bytes(6)),), cache=True, eof=True, extra=()):
    """A chart substream (BOF dt 0x20 ... EOF), as a chart sheet's `raw` or inside a worksheet's `records`.

    `series` is a list of dicts: name, vals and cats (lists), ifmt (BRAI number format id), aux (carries SerParent 0x104A) and ref ((role,
    tokens) pairs that replace the cache for that role). With `cache` the values and categories follow as SIIndex
    caches (NUMBER for a number, LABEL for a string, col = the series), else each series has only its BRAIs.
    `kinds` are (id, body) records; `extra` raw records go before the EOF."""
    import struct
    short = lambda t: _xl_string(t, short=True)

    def brai(role, source, tokens=b"", ifmt=0):
        return biff(0x1051, struct.pack("<BBHHH", role, source, 1 if ifmt else 0, ifmt, len(tokens)) + tokens)

    out = [biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0020, 0x0DBB, 0x07CC, 0, 6))]
    out += [biff(0x1002, bytes(16)), biff(0x1033)]
    for s in series:
        refs = dict(s.get("ref", ()))
        out += [biff(0x1003, struct.pack("<6H", 1, 1, len(s.get("vals", ())), len(s.get("cats", ())), 0, 0)),
                biff(0x1033)]
        out.append(brai(0, 1))
        if s.get("name") is not None:
            out.append(biff(0x100D, struct.pack("<H", 0) + short(s["name"])))
        out.append(brai(1, 2 if 1 in refs else 1, refs.get(1, b""), s.get("ifmt", 0)))
        out.append(brai(2, 2 if 2 in refs else 1, refs.get(2, b"")))
        if s.get("aux"):  # a trendline or error-bar series carries SerParent
            out.append(biff(0x104A, bytes(2)))
        out.append(biff(0x1034))
    for rid, body in kinds:
        out.append(biff(rid, body))
    if title is not None:
        out += [biff(0x1025, bytes(32)), biff(0x1033), biff(0x1027, struct.pack("<HHH", 1, 0, 0)),
                biff(0x100D, struct.pack("<H", 0) + short(title)), biff(0x1034)]
    if cache:
        out.append(biff(0x1006, bytes(16)))
        for role, key in ((1, "vals"), (2, "cats")):
            out.append(biff(0x1065, struct.pack("<H", role) + bytes(2)))
            for col, s in enumerate(series):
                for row, v in enumerate(s.get(key, ())):
                    if isinstance(v, str):
                        out.append(biff(0x0204, struct.pack("<HHH", row, col, 0) + _xl_string(v)))
                    else:
                        out.append(biff(0x0203, struct.pack("<HHH", row, col, 0) + struct.pack("<d", float(v))))
    out += [e if isinstance(e, bytes) else biff(*e) for e in extra]
    out.append(biff(0x1034))
    if eof:
        out.append(biff(0x000A))
    return b"".join(out)


def area3d(sheet_xti, r1, r2, c1, c2):
    """A BRAI Area3d token: ptg 0x3B, the XTI index, then the rows and columns."""
    import struct
    return struct.pack("<BHHHHH", 0x3B, sheet_xti, r1, r2, c1, c2)


def externsheet(*sheets):
    """SUPBOOK (this workbook, `len(sheets)` sheets) and an EXTERNSHEET with one XTI per sheet index given."""
    import struct
    xti = b"".join(struct.pack("<Hhh", 0, i, i) for i in sheets)
    return biff(0x01AE, struct.pack("<HH", max(sheets, default=0) + 1, 0x0401))         + biff(0x0017, struct.pack("<H", len(sheets)) + xti)


def make_xls(path, sheets, **kw):
    """Write a compound file whose Workbook stream is biff_workbook(sheets, **kw); returns that stream."""
    data = biff_workbook(sheets, **kw)
    make_cfb(path, {"Workbook": data})
    return data


def excel_object(sheets, prog=b"Excel.Sheet.8", **kw):
    """The compound file of an embedded Excel object (an OLE storage's streams): a Workbook stream that is
    biff_workbook(sheets, **kw) and a \\x01CompObj naming `prog`, as bytes."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "obj.cfb"
        make_cfb(path, {"Workbook": biff_workbook(sheets, **kw), "\x01CompObj": comp_obj(prog)})
        return path.read_bytes()


def msgraph_object(cats, series, kind="bar", title="", *, by_rows=False, codepage=1252, codepage_at="in"):
    """The compound file of an embedded MS Graph 97 object: a Workbook stream (BOF vers 0x0680 dt 0x8000) holding the
    chart records and the datasheet (CODEPAGE `codepage_at` "in" the substream after its BOF, "before" the BOF or
    "late", after the first cell), and a \\x01CompObj naming MSGraph.Chart.8, as bytes.

    `series` is a list of (name, values). The datasheet's row 0 (column 0 with by_rows) holds the series names, its
    first column (row) the categories and the cells use Graph's own ids, NUMBER 3 and LABEL 4: row, col, 3 attribute
    bytes, then the f64 or a u16-sized narrow string. Selections 0x1053 / 0x1054 stop after the rows / columns, 0x1055
    says whether the series are in rows; each Series' BRAIs end in the datasheet index of their row or column."""
    import struct
    import tempfile
    n = len(series)
    head = [biff(0x0809, struct.pack("<HHHHII", 0x0680, 0x8000, 0, 0, 0, 0)),
            biff(0x1055, bytes([1 if by_rows else 0]) + bytes(5)),
            biff(0x1053, struct.pack("<HH", 0, (n if by_rows else len(cats)) + 1)),
            biff(0x1054, struct.pack("<HH", 0, (len(cats) if by_rows else n) + 1)), biff(0x1033)]

    def brai(role, idx):
        return biff(0x1051, struct.pack("<BBHHH", role, 1, 0, 0, idx))
    for k, (_, vals) in enumerate(series):
        head += [biff(0x1003, struct.pack("<6H", 1, 1, len(cats), len(vals), 0, 0)), biff(0x1033),
                 brai(0, k + 1), brai(1, k + 1), brai(2, 0), biff(0x1034)]
    ids = {"bar": (0x1017, bytes(6)), "line": (0x1018, bytes(2)), "pie": (0x1019, bytes(6)),
           "area": (0x101A, bytes(2))}
    head.append(biff(*ids[kind]))
    if title:
        head += [biff(0x1025, bytes(32)), biff(0x1033), biff(0x1027, struct.pack("<HHH", 1, 0, 0)),
                 biff(0x100D, struct.pack("<H", 0) + _xl_string(title, short=True)), biff(0x1034)]
    head.append(biff(0x1034))

    def cell(point, axis, value):
        row, col = (axis, point) if by_rows else (point, axis)
        if isinstance(value, str):
            raw = value.encode(f"cp{codepage}")
            return biff(4, struct.pack("<HH", row, col) + bytes(3) + struct.pack("<H", len(raw)) + raw)
        return biff(3, struct.pack("<HH", row, col) + bytes(3) + struct.pack("<d", float(value)))
    cells = [cell(0, k + 1, name) for k, (name, _) in enumerate(series)]
    cells += [cell(i + 1, 0, c) for i, c in enumerate(cats)]
    cells += [cell(i + 1, k + 1, v) for k, (_, vals) in enumerate(series) for i, v in enumerate(vals)]
    cp = biff(0x0042, struct.pack("<H", codepage))
    if codepage_at == "before":  # ahead of the dt 0x8000 BOF
        head.insert(0, cp)
    elif codepage_at == "late":  # after the first label cell
        cells.insert(1, cp)
    else:
        head.insert(1, cp)
    data = b"".join(head + cells + [biff(0x000A)])
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "obj.cfb"
        make_cfb(path, {"Workbook": data, "CompObj": comp_obj(b"MSGraph.Chart.8", b"Microsoft Graph Chart")})
        return path.read_bytes()


def encrypt_xls(sheets, password, kind, **kw):
    """A Workbook stream encrypted with `password`: kind "xor", "rc4", "cryptoapi" (128-bit) or "cryptoapi40".

    Built from biff_workbook(sheets, filepass=..., **kw). Everything up to and including FILEPASS stays clear, as do
    every record header, the BOF/INTERFACEHDR-style records and the first 4 bytes of each BOUNDSHEET ([MS-XLS] 2.2.10).
    XOR: per record body, rotate right by 3 and XOR with the array from index (record end offset) % 16.
    RC4: the stream is encrypted in 1024-byte blocks, block key = key(offset // 1024).
    """
    import struct
    from reader.containers import officecrypt
    from reader.containers.crypto import rc4
    from reader.formats import xls_crypt
    clear = {0x0809, 0x0009, 0x0209, 0x0409, 0x002F, 0x0194, 0x0195, 0x00E1, 0x0196, 0x0138}
    if kind == "xor":
        key = xls_crypt.xor_key(password)
        filepass = struct.pack("<HHH", 0, key, xls_crypt.xor_verifier(password))
    else:
        assert kind in ("rc4", "cryptoapi", "cryptoapi40"), kind
        filepass = struct.pack("<H", 1) + rc4_header(password, cryptoapi=kind != "rc4",
                                                    key_bits=40 if kind == "cryptoapi40" else 128)
    data = biff_workbook(sheets, filepass=filepass, **kw)
    recs, pos = [], 0
    while pos + 4 <= len(data):
        rid, size = struct.unpack_from("<HH", data, pos)
        recs.append((rid, pos, pos + 4 + size))
        pos += 4 + size
    start = next(e for rid, _, e in recs if rid == 0x002F)
    out = bytearray(data)
    if kind == "xor":
        arr = xls_crypt.xor_array(password, key)
        rot_right = bytes(((v >> 3) | (v << 5)) & 0xFF for v in range(256))
        for rid, p, e in recs:
            if p < start or rid in clear or e == p + 4:
                continue
            n = e - p - 4
            ks = (arr * (n // 16 + 2))[e % 16:e % 16 + n]
            x = int.from_bytes(data[p + 4:e].translate(rot_right), "little") ^ int.from_bytes(ks, "little")
            out[p + 4:e] = x.to_bytes(n, "little")
    else:
        keys = officecrypt.block_keys(officecrypt.parse_rc4(filepass, 2), password)
        assert keys is not None
        for b in range(start // 1024, (len(data) - 1) // 1024 + 1):
            lo, hi = max(b * 1024, start), min((b + 1) * 1024, len(data))
            out[lo:hi] = rc4(keys(b), data[lo:hi], skip=lo - b * 1024)
        for rid, p, e in recs:
            if p >= start:
                out[p:p + 4] = data[p:p + 4]
                if rid in clear:
                    out[p + 4:e] = data[p + 4:e]
    for rid, p, e in recs:
        if p >= start and rid == 0x0085:
            out[p + 4:p + 8] = data[p + 4:p + 8]
    return bytes(out)


def biff12(rid: int, body: bytes = b"") -> bytes:
    """One BIFF12 record: a 1 or 2 byte id and a size of 1 to 4 bytes, 7 bits each."""
    def seven(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)
    return (seven(rid) if rid >= 0x80 else bytes([rid])) + seven(len(body)) + bytes(body)


def xlsb_wide(text):
    """An XLWideString (u32 cch, UTF-16LE), or the null string for None."""
    import struct
    if text is None:
        return struct.pack("<I", 0xFFFFFFFF)
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def xlsb_bundle(name, rid, state=0, tab=1, extra=b""):
    """A BrtBundleSh record; `extra` goes before strRelID."""
    import struct
    return biff12(156, struct.pack("<II", state, tab) + extra + xlsb_wide(rid) + xlsb_wide(name))


def make_xlsb(path, sheets, *, formats=None, date1904=False, workbook_records=None, extra_parts=None):
    """Write an .xlsb package: content types, rels, workbook.bin, styles.bin, sharedStrings.bin, one sheet part per dict.

    A sheet dict has Task 5's keys (name, state, cells, ...) and `records`, raw BIFF12 records (bytes) for the sheet
    part. Shared strings come from the str cells; each ("fmt", number, code) cell format gets a cell XF after the
    General one. `workbook_records` replaces the generated BrtBundleSh records. `extra_parts` adds parts or
    replaces generated ones ({name: bytes}); a None value leaves a part out.
    """
    import struct
    formats = dict(formats or {})
    strings = list(dict.fromkeys(v for sh in sheets for v in sh.get("cells", {}).values() if isinstance(v, str)))
    codes = list(dict.fromkeys(v[2] for sh in sheets for v in sh.get("cells", {}).values()
                               if isinstance(v, tuple) and v[0] == "fmt"))
    by_code = {code: fid for fid, code in formats.items()}
    for code in codes:
        if code not in by_code:
            by_code[code] = max([163, *formats]) + 1
            formats[by_code[code]] = code
    wb = [biff12(153, struct.pack("<I", int(date1904)) + bytes(4)), biff12(143)]
    if workbook_records is None:
        wb += [xlsb_bundle(sh["name"], f"rId{i + 1}", sh.get("state", 0), i + 1) for i, sh in enumerate(sheets)]
    else:
        wb += list(workbook_records)
    wb.append(biff12(144))
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    wb_rels = "".join(f'<Relationship Id="rId{i + 1}" Type="{rel}worksheet" Target="worksheets/sheet{i + 1}.bin"/>'
                      for i in range(len(sheets)))
    wb_rels += (f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel}styles" Target="styles.bin"/>'
                f'<Relationship Id="rId{len(sheets) + 2}" Type="{rel}sharedStrings" Target="sharedStrings.bin"/>')

    def xf(parent, fid):
        return biff12(47, struct.pack("<HH", parent, fid) + bytes(8))
    styles = [biff12(278)]
    if formats:
        styles += [biff12(615, struct.pack("<I", len(formats)))]
        styles += [biff12(44, struct.pack("<H", fid) + xlsb_wide(code)) for fid, code in formats.items()]
        styles += [biff12(616)]
    styles += [biff12(626, struct.pack("<I", 1)), xf(0xFFFF, 0), biff12(627)]
    styles += [biff12(617, struct.pack("<I", 1 + len(codes))), xf(0, 0)]
    styles += [xf(0, by_code[code]) for code in codes] + [biff12(618), biff12(279)]
    sst = [biff12(159, struct.pack("<II", len(strings), len(strings)))]
    sst += [biff12(19, b"\0" + xlsb_wide(s)) for s in strings] + [biff12(160)]
    members = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="bin" ContentType="application/vnd.ms-excel.sheet.binary.macroEnabled.main"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}officeDocument" Target="xl/workbook.bin"/></Relationships>'),
        "xl/workbook.bin": b"".join(wb),
        "xl/_rels/workbook.bin.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{wb_rels}'
            '</Relationships>'),
        "xl/styles.bin": b"".join(styles),
        "xl/sharedStrings.bin": b"".join(sst),
    }
    for i, sh in enumerate(sheets):
        members[f"xl/worksheets/sheet{i + 1}.bin"] = biff12(129) + b"".join(sh.get("records", ())) + biff12(130)
    for name, data in (extra_parts or {}).items():
        if data is None:
            members.pop(name, None)
        else:
            members[name] = data
    make_zip(path, members, stored=())
