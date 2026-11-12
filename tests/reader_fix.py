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
